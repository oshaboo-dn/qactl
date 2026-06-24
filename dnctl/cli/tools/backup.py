"""Backup / restore tools.

Four tools that move DNOS configurations to/from **this host** (the
machine running ``dnctl``) over its own sshd. The device is the SFTP
client; the artefacts land on the local filesystem owned by
:mod:`dnctl.cli.core.backup_store`. The shared external ``dnftp`` host is
reserved for the large device-pushed tech-support tarballs.

- ``backup_device`` — DNOS ``save`` + ``request file upload config`` of
  the saved file to this host. The store then stats the local file to
  verify size and existence.
- ``list_backups`` — pure local listing of the backup tree (no device
  contact).
- ``read_backup`` — read a saved config off the local disk.
- ``restore_device`` — ``request file download`` from this host followed
  by ``configure`` / ``load`` / ``commit`` on the device. Destructive,
  guarded by ``confirm=True``. Filename's device prefix MUST match the
  ``device`` argument so a backup from one box can never land on
  another.

All serialise per-device through ``dnctl.cli.core.locks.device_lock``
(shared with ``edit_config`` / ``create_techsupport``) — DNOS' candidate
configuration is shared across SSH sessions, so concurrent edits would
stomp each other.

The ``request file upload|download`` command strings come from
:func:`dnctl.core.dnftp.build_upload_command` /
:func:`dnctl.core.dnftp.build_download_command` so the grammar lives in one
place; the *self* target (host / user / password / VRF the device dials
back into) is resolved at runtime by :mod:`dnctl.core.local_sftp`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Tuple

from dnctl.cli.core import backup_store
from dnctl.cli.core.commit_sequence import parse_commit_output
from dnctl.cli.core.configure_commit import (
    build_configure_commit_steps,
    drive_configure_commit,
)
from dnctl.cli.core.dnftp import (
    build_download_command,
    build_upload_command,
)
from dnctl.core.local_sftp import (
    LOCAL_SFTP_HOST,
    LOCAL_SFTP_USER,
    LOCAL_SFTP_VRF,
    LocalSftpNotConfigured,
    require_password,
)
from dnctl.cli.core.edit_helpers import validate_edit_statements
from dnctl.cli.core.envelope import error_response, make_response
from dnctl.cli.core.errors import BACKUP_NEXT_ACTION, RESTORE_NEXT_ACTION, detect_error
from dnctl.cli.core.locks import device_lock
from dnctl.cli.core.logging import log_invocation, log_request
from dnctl.cli.core.redact import scrub_password, scrub_steps
from dnctl.cli.core.registry import transport_registry
from dnctl.cli.core.session import (
    DEFAULT_PASSWORD,
    DEFAULT_USER,
    ConnectError,
    run_sequence_pw,
)
from dnctl.cli.vendors import CAP_BACKUP, requires


# Minimum plausible size for a saved DNOS config. Real configs are multi-KB;
# an empty file means the transfer silently truncated or never happened.
_BACKUP_MIN_BYTES = 64
# Upper bound for read_backup: a saved config is text and realistically a few
# hundred KB at most. Refuse to slurp a pathologically large file into the
# JSON envelope (OOM / huge response) — point the caller at the file path.
_READ_BACKUP_MAX_BYTES = 16 * 1024 * 1024
# Upload / download + commit can take a while on large configs; override with
# the ``timeout`` kwarg on each tool.
_BACKUP_DEFAULT_TIMEOUT = 120
# Restore is a full SFTP download of the candidate config followed by
# ``load`` + ``commit``; on large configs both download and commit can each
# take several minutes. 120 s per step was not enough in practice.
_RESTORE_DEFAULT_TIMEOUT = 20 * 60


@requires(CAP_BACKUP)
def backup_device(
    device: Optional[str] = None,
    description: Optional[str] = None,
    bucket: Optional[str] = None,
    vrf: str = LOCAL_SFTP_VRF,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = _BACKUP_DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Save the device's running config and upload it to this host.

    Local layout (per-device, optional one-level sub-bucket under it),
    under ``<state_dir>/backups/cli`` (honours ``$DNCTL_STATE_DIR``)::

        <BACKUP_DIR>/<device>/                                        # device root
        ├── <device>__<UTC-YYYYMMDD-HHMMSS>[__<desc>].md              # bucket=None
        └── <bucket>/                                                 # bucket="<name>"
            └── <device>__<UTC-YYYYMMDD-HHMMSS>[__<desc>].md

    The ``<device>`` folder is **always** the top level — derived from
    the ``device=`` arg, not from the caller's ``bucket``. Sub-buckets
    are an *optional* second level, chosen at backup time, and exist to
    group captures by purpose:

    - ``bucket=None`` (default): file lands directly in the device
      root. Good for one-off "I want a snapshot of cl right now"
      calls where there's no recurring grouping.
    - ``bucket="nightly"``: scheduled-job convention used by the
      ``cli-mcp-backup-nightly.timer`` wrapper
      (``scripts/scheduled/backup-fleet.py``). Keeps the every-night
      stream cleanly separated from ad-hoc captures.
    - ``bucket="bug-1234-repro"`` / ``"save-for-later"`` / etc.:
      ad-hoc grouping for an investigation. The same bucket name
      under multiple devices (``cl/bug-1234/``, ``sa/bug-1234/``)
      groups a multi-device capture for one bug.

    The config lands on this host's local disk — verification, listing,
    and restore-side download all read that local tree, so backups don't
    depend on dnftp (reserved for tech-support). Both the device folder
    and the optional sub-bucket are auto-created on demand and reused if
    they already exist — no error if they were made by a previous call.
    ``restore_device`` and ``list_backups`` accept the same
    ``device`` / ``bucket`` args; pass them whatever the matching
    ``backup_device`` envelope reported in its ``device`` /
    ``bucket`` fields.

    Flow on one ephemeral SSH channel to the device:

        1. ``configure``
        2. ``save <filename>``                — writes /config/<filename>.
        3. ``exit``                           — back to operational mode.
        4. ``request file upload config <filename>
            <local-user>@<this-host>:<path> protocol sftp vrf <vrf>``
                                              — password fed on prompt.

    The local-user / host / password the device authenticates with come
    from :mod:`dnctl.core.local_sftp`. After the upload, the store stats
    the local file and confirms ``size >= 64 bytes``. On success, the
    envelope carries ``backup_path`` (absolute local path) /
    ``size_bytes`` / ``filename`` / ``bucket``. No on-device cleanup is
    performed (intentional — the user keeps the copy under ``/config/``
    on the device).

    Args:
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
            Mandatory in practice; ``host`` is the legacy fall-through
            (uses the raw host string as the device folder name, which
            must be supplied verbatim to the matching restore).
        description: Optional short description; sanitised to
            ``[A-Za-z0-9._-]{1,40}`` (illegal characters collapsed to ``_``;
            empty after sanitisation → dropped).
        bucket: Optional sub-bucket name under the device folder
            (``[A-Za-z0-9._-]{1,60}``, no ``/``, no ``__``). ``None``
            (default) lands the file directly in the device folder.
            The sub-bucket directory is auto-created on demand and
            reused if it already exists.
        vrf: DNOS VRF the device uses to reach this host. Default ``mgmt0``.
        host: Raw hostname/IP (alternative to device). If you pass ``host``
            the resulting filename will use the host string as the device
            segment, which the matching ``restore_device`` call must supply
            verbatim.
        user: SSH username on the device (default dnroot).
        password: SSH password on the device (default dnroot).
        timeout: Per-command timeout seconds.
    """
    device_key = device or host or ""
    err = backup_store.validate_device(device_key)
    if err:
        return error_response(
            err, device=device, host=host, next_action=BACKUP_NEXT_ACTION,
        )

    bucket_err = backup_store.validate_bucket(bucket)
    if bucket_err:
        return error_response(
            bucket_err, device=device, host=host,
            next_action=BACKUP_NEXT_ACTION,
        )

    try:
        local_password = require_password()
    except LocalSftpNotConfigured as exc:
        return error_response(
            str(exc), device=device, host=host,
            next_action=BACKUP_NEXT_ACTION,
        )

    try:
        filename = backup_store.make_filename(device_key, description)
    except ValueError as exc:
        return error_response(
            str(exc), device=device, host=host,
            next_action=BACKUP_NEXT_ACTION,
        )

    try:
        backup_store.ensure_dir(device=device_key, bucket=bucket)
    except (OSError, ValueError) as exc:
        return error_response(
            f"backups directory unavailable: {exc}",
            device=device, host=host, next_action=BACKUP_NEXT_ACTION,
        )

    upload_cmd = build_upload_command(
        kind="config",
        local_name=filename,
        remote_path=backup_store.remote_path(
            filename, device=device_key, bucket=bucket,
        ),
        vrf=vrf,
        user=LOCAL_SFTP_USER,
        host=LOCAL_SFTP_HOST,
    )
    commands: List[Tuple[str, Optional[str]]] = [
        ("configure", None),
        (f"save {filename}", None),
        ("exit", None),
        (upload_cmd, local_password),
    ]
    request = {
        "device": device, "host": host, "user": user,
        "description": description, "vrf": vrf, "filename": filename,
        "bucket": bucket,
    }
    response = make_response(
        device=device, host=host, command=upload_cmd, filename=filename,
        bucket=bucket,
    )

    lock = device_lock(device_key)
    with lock:
        try:
            result = run_sequence_pw(
                transport_registry,
                device=device, host=host, user=user, password=password,
                commands=commands, timeout=timeout,
            )
        except ConnectError as exc:
            response.update(
                status="connect_error",
                errors=[str(exc)],
                next_actions=[
                    "Verify the device is reachable and credentials are correct.",
                ],
            )
            log_request("backup_device", request, response)
            return response
        except Exception as exc:
            response.update(status="error", errors=[str(exc)])
            log_request("backup_device", request, response)
            return response

        response["host"] = result.host
        response["device"] = result.device or device
        scrubbed = scrub_password(result.output, local_password)
        response["stdout"] = scrubbed
        log_invocation(
            result.device or device, result.host,
            upload_cmd, scrubbed,
            result.head_prompt_line, result.tail_prompt,
            steps=scrub_steps(result.steps, local_password),
        )

        if not result.hit_prompt:
            response["status"] = "timeout"
            response["errors"].append(
                f"Timed out waiting for CLI prompt after {timeout}s."
            )
            response["next_actions"].append(BACKUP_NEXT_ACTION)
            log_request("backup_device", request, response)
            return response

        is_err, err_lines = detect_error(scrubbed)
        if is_err:
            response["status"] = "error"
            response["errors"].extend(err_lines[-5:])
            response["next_actions"].append(BACKUP_NEXT_ACTION)
            log_request("backup_device", request, response)
            return response

        stat = backup_store.stat_backup(
            filename, device=device_key, bucket=bucket,
        )
        if stat is None:
            response["status"] = "error"
            response["errors"].append(
                f"Upload completed without error but {filename!r} is not "
                "present on this host — check the local sshd landing directory."
            )
            response["next_actions"].append(BACKUP_NEXT_ACTION)
            log_request("backup_device", request, response)
            return response

        if stat.size_bytes < _BACKUP_MIN_BYTES:
            response["status"] = "error"
            response["errors"].append(
                f"Uploaded file {filename!r} is suspiciously small "
                f"({stat.size_bytes} bytes). Treating as a failed transfer."
            )
            response["next_actions"].append(BACKUP_NEXT_ACTION)
            log_request("backup_device", request, response)
            return response

        response["backup_path"] = stat.path
        response["size_bytes"] = stat.size_bytes
        response["timestamp_utc"] = stat.timestamp_utc
        response["bucket"] = stat.bucket
        log_request("backup_device", request, response)
        return response


def list_backups(
    device: Optional[str] = None,
    bucket: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """List backups stored on this host, newest first.

    Pure local filesystem read — no SSH to any lab device. Each entry
    carries the parsed ``device`` / ``timestamp_utc`` / ``description`` /
    ``bucket`` plus the file's ``size_bytes`` and absolute local
    ``path``. Files that don't match the canonical naming shape (or that
    live where the canonical layout doesn't expect) are surfaced under
    ``orphans`` so you know to investigate.

    The local tree is rooted at the device alias
    (``<state_dir>/backups/cli/<device>/[<bucket>/]<file>``). The result
    also carries:

    - ``buckets``: when ``device`` is set, the sub-bucket names under
      that device (e.g. ``["bug-1234", "nightly"]``); when ``device``
      is ``None``, the top-level device folders (e.g.
      ``["cl", "kira", "sa"]``).
    - ``orphans``: any entry that drifts from the canonical layout
      (rendered as ``"<name>/"`` for stray top-level dirs,
      ``"<name>"`` for stray top-level files, ``"<device>/<name>"``
      for files directly under a device dir whose name doesn't parse,
      ``"<device>/<bucket>/<name>"`` for non-canonical files inside a
      sub-bucket, etc.).

    Args:
        device: When set, only list backups for that device alias —
            walks ``cli/backups/<device>/`` plus every sub-bucket
            under it. When ``None``, walks every device folder under
            ``cli/backups/`` (heavy — pass a ``device`` if you can).
        bucket: When set, only list backups inside that sub-bucket
            under the device folder(s). When ``None``, walks the
            device root AND every sub-bucket under it; each entry's
            ``bucket`` field tells you which (``None`` = directly in
            the device folder).
        limit: Maximum number of entries to return (default 100).
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return error_response(
            "limit must be a positive integer.", device=device,
        )
    if device is not None:
        device_err = backup_store.validate_device(device)
        if device_err:
            return error_response(device_err, device=device)
    if bucket is not None:
        bucket_err = backup_store.validate_bucket(bucket)
        if bucket_err:
            return error_response(bucket_err, device=device)

    backups = backup_store.list_backups(
        device=device, limit=limit, bucket=bucket,
    )
    orphans = backup_store.list_orphans()
    buckets = backup_store.list_buckets(device=device)
    entries = [
        {
            "filename": b.filename,
            "device": b.device,
            "timestamp_utc": b.timestamp_utc,
            "description": b.description,
            "bucket": b.bucket,
            "size_bytes": b.size_bytes,
            "path": b.path,
        }
        for b in backups
    ]
    warnings: List[str] = []
    if orphans:
        warnings.append(
            f"{len(orphans)} entry(ies) under {backup_store.BACKUP_DIR} "
            f"drift from the canonical per-device layout (see 'orphans' "
            f"field). Rename, move, or remove them to keep the store clean."
        )
    response = make_response(
        device=device, host="", command="list_backups",
        warnings=warnings,
        backups=entries, orphans=orphans, buckets=buckets,
        backup_dir=backup_store.BACKUP_DIR,
        count=len(entries),
    )
    log_request(
        "list_backups",
        {"device": device, "bucket": bucket, "limit": limit},
        response,
    )
    return response


@requires(CAP_BACKUP)
def restore_device(
    device: str,
    filename: str,
    bucket: Optional[str] = None,
    mode: Literal["override", "merge"] = "override",
    vrf: str = LOCAL_SFTP_VRF,
    confirm: bool = False,
    post_load_commands: Optional[List[str]] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = _RESTORE_DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Restore a previously-captured backup to the device (DESTRUCTIVE).

    Safety:

    - ``filename`` must be a canonical backup name, and the device prefix
      encoded in it MUST match the ``device`` argument. Mismatch is a hard
      refusal — this is the mechanism that prevents applying one device's
      config to another.
    - ``confirm=False`` (default) returns a dry-run envelope describing
      exactly what would be sent. Pass ``confirm=True`` to actually
      execute.

    Flow on one ephemeral channel when ``confirm=True``:

        1. ``set cli-no-confirm``
        2. ``request file download <local-user>@<this-host>:<path> config
            <filename> protocol sftp vrf <vrf>``    — password fed on prompt.
        3. ``configure``
        4. ``load override <filename>`` | ``load merge <filename>``
        5. each entry of ``post_load_commands`` (in order, one per step) —
            see below; useful for fixing up nodes the load couldn't
            decrypt before the commit walks them.
        6. ``commit``                               — result parsed below.

    On commit failure the session closes with the candidate uncommitted,
    so the device is left on the pre-restore config. The envelope carries
    the parsed ``commit.user`` / ``commit.timestamp`` on success.

    Master-key-encrypted secrets
    ----------------------------

    DNOS persists ``tailf:encrypted-string`` leaves (e.g.
    ``system login ncm user dnroot password``) as ``enc-<base64>``
    ciphertext using the per-box master key. After a factory-reset the
    new master key cannot decrypt the previous box's ``enc-...`` blob,
    so a vanilla restore fails on commit with ``'password' is missing
    at 'system login ncm user dnroot'``.

    Workaround — pass ``post_load_commands=[...]`` to fix the
    candidate before commit. Two common shapes:

    - **Drop the broken subtree**, e.g.
      ``["no system login ncm user dnroot"]`` — the user gets removed,
      mandatory-leaf check passes, you re-add the user post-restore.
    - **Re-set the leaf inline**, e.g.
      ``["system login ncm user dnroot password <new-plaintext>"]``
      — DNOS hashes / re-encrypts on commit, so the candidate carries
      a freshly-encrypted-with-the-new-master-key value.

    Use :func:`read_backup` first to see exactly which paths carry an
    ``enc-...`` value, then aim ``post_load_commands`` at those.

    Args:
        device: Device alias expected to match the filename prefix.
            Also resolves the local folder
            (``<state_dir>/backups/cli/<device>/[<bucket>/]<file>``).
        filename: Backup filename as listed by ``list_backups``.
        bucket: Optional sub-bucket under the device folder (must
            match what was passed to ``backup_device``). ``None``
            (default) = directly in the device folder.
        mode: ``override`` (replace candidate; default) or ``merge``.
        vrf: DNOS VRF used to reach the backup host. Default ``mgmt0``.
        confirm: Must be ``True`` to actually execute; default is dry-run.
        post_load_commands: Optional list of configure-mode statements
            run between ``load`` and ``commit`` (one per step, in order).
            Same shape ``edit_config(statements=[...])`` accepts: each a
            single line, no control chars, ≤1000 chars, ≤200 entries.
            DNOS syntax (no Junos ``set`` prefix; use ``no <path>`` to
            delete). ``None`` (default) = no extra steps.
        host: Raw hostname/IP (alternative to device).
        user: SSH username on the device (default dnroot).
        password: SSH password on the device (default dnroot).
        timeout: Per-step timeout seconds (applies to each of the six CLI
            steps; the SFTP download and ``commit`` of a full config are the
            long poles, hence the 20-minute default).
    """
    if mode not in ("override", "merge"):
        return error_response(
            f"mode must be 'override' or 'merge' (got {mode!r}).",
            device=device, host=host, next_action=RESTORE_NEXT_ACTION,
        )

    # Reuse edit_config's statement validator so post_load_commands gets
    # the same control-char / length / count gates as edit_config —
    # ``None`` and ``[]`` both mean "no extras".
    extra_statements: List[str] = list(post_load_commands or [])
    if extra_statements:
        err = validate_edit_statements(extra_statements)
        if err:
            return error_response(
                f"post_load_commands: {err}",
                device=device, host=host, next_action=RESTORE_NEXT_ACTION,
            )

    device_err = backup_store.validate_device(device)
    if device_err:
        return error_response(
            device_err, device=device, host=host,
            next_action=RESTORE_NEXT_ACTION,
        )
    bucket_err = backup_store.validate_bucket(bucket)
    if bucket_err:
        return error_response(
            bucket_err, device=device, host=host,
            next_action=RESTORE_NEXT_ACTION,
        )

    stat = backup_store.stat_backup(filename, device=device, bucket=bucket)
    if stat is None:
        bucket_hint = (
            f" in bucket {bucket!r}" if bucket is not None else ""
        )
        return error_response(
            f"backup {filename!r} not found under device {device!r}"
            f"{bucket_hint} or not a canonical backup name. Use "
            f"list_backups(device={device!r}) to see what's available.",
            device=device, host=host, next_action=RESTORE_NEXT_ACTION,
        )
    if stat.device != device:
        return error_response(
            f"filename device prefix {stat.device!r} does not match "
            f"device argument {device!r}; refusing to restore.",
            device=device, host=host, next_action=RESTORE_NEXT_ACTION,
        )

    # The device downloads from this host; gate on the local SFTP password
    # only when actually executing — a dry-run can still preview the command
    # without it configured.
    local_password = None
    if confirm:
        try:
            local_password = require_password()
        except LocalSftpNotConfigured as exc:
            return error_response(
                str(exc), device=device, host=host,
                next_action=RESTORE_NEXT_ACTION,
            )

    download_cmd = build_download_command(
        kind="config",
        local_name=filename,
        remote_path=backup_store.remote_path(
            filename, device=device, bucket=bucket,
        ),
        vrf=vrf,
        user=LOCAL_SFTP_USER,
        host=LOCAL_SFTP_HOST,
    )
    load_cmd = f"load {mode} {filename}"
    # NB: no trailing ``exit`` — ``run_sequence_pw`` only returns the LAST
    # command's output, and ``exit`` produces no stdout, which would hide
    # the commit result (and cause ``parse_commit_output`` to flag "(empty
    # output)" even when the restore succeeded). The channel is closed in
    # the session helper's ``finally``, so a client-side ``exit`` is
    # redundant anyway.
    steps, full_command = build_configure_commit_steps(
        pre_commands=[
            ("set cli-no-confirm", None),
            (download_cmd, local_password),
        ],
        body_statements=[load_cmd, *extra_statements],
    )

    request = {
        "device": device, "host": host, "user": user,
        "filename": filename, "bucket": bucket, "mode": mode, "vrf": vrf,
        "confirm": confirm, "post_load_commands": extra_statements,
    }

    if not confirm:
        response = make_response(
            device=device, host=host or "", command=full_command,
            warnings=[
                "Dry-run: confirm=False. Re-invoke with confirm=true to execute.",
            ],
            filename=filename, bucket=bucket, mode=mode,
            post_load_commands=extra_statements,
            backup_path=stat.path, size_bytes=stat.size_bytes,
        )
        log_request("restore_device", request, response)
        return response

    response = make_response(
        device=device, host=host, command=full_command,
        filename=filename, bucket=bucket, mode=mode,
        post_load_commands=extra_statements,
    )

    lock = device_lock(device)
    with lock:
        result = drive_configure_commit(
            transport_registry, tool_name="restore_device",
            device=device, host=host, user=user, password=password,
            timeout=timeout, steps=steps, command=full_command,
            request=request, response=response,
            scrub_secret=local_password,
            # Capture every step's output, not just the final commit:
            # the SFTP download and ``load`` happen BEFORE commit, and
            # their failures don't appear in the commit line. Without
            # this a failed download + a clean (no-op) commit looked like
            # a successful restore.
            capture_all=True,
            connect_next_action=(
                "Verify the device is reachable and credentials are correct."
            ),
        )
        if result is None:
            return response

        if not result.hit_prompt:
            response["status"] = "timeout"
            response["errors"].append(
                f"Timed out waiting for CLI prompt after {timeout}s."
            )
            response["next_actions"].append(RESTORE_NEXT_ACTION)
            log_request("restore_device", request, response)
            return response

        # A restore is download → load → commit on one channel. A failure
        # in the download or load step (unreachable host, missing file,
        # bad config) must fail the whole op even if a later no-op commit
        # "succeeds". Scan those upstream steps explicitly — their output
        # is invisible to parse_commit_output (which only reads the commit).
        upstream_errors: List[str] = []
        for step in result.steps:
            cmd = (step.command or "").strip()
            if cmd == "set cli-no-confirm" or cmd.startswith("commit"):
                continue
            if not step.hit_prompt:
                upstream_errors.append(
                    f"step {cmd!r} timed out before the CLI prompt returned."
                )
                continue
            step_err, step_lines = detect_error(step.output)
            if step_err:
                label = "download" if "download" in cmd else (
                    "load" if cmd.startswith("load ") else cmd
                )
                upstream_errors.extend(
                    f"{label}: {ln}" for ln in step_lines[-2:]
                )
        if upstream_errors:
            response["status"] = "error"
            response["errors"].extend(upstream_errors)
            response["errors"].append(
                "restore aborted: a pre-commit step (SFTP download / load) "
                "failed; the running config was not cleanly restored."
            )
            response["next_actions"].append(RESTORE_NEXT_ACTION)
            log_request("restore_device", request, response)
            return response

        is_err, err_lines = detect_error(result.output)
        # The commit-parser is the decisive signal; detect_error is a broader
        # net that can catch upstream failures (download, load, ...) before
        # we even get to commit.
        commit = parse_commit_output(result.output)
        response["commit"] = {
            "status": commit.status,
            "user": commit.user,
            "timestamp": commit.timestamp,
        }
        if commit.status == "ok":
            if is_err:
                response["warnings"].append(
                    "commit succeeded but error-looking lines were detected "
                    "in stdout; review manually."
                )
                response["warnings"].extend(err_lines[-3:])
            log_request("restore_device", request, response)
            return response

        response["status"] = "error"
        if commit.status == "no_change":
            response["errors"].append(
                "Commit reported no changes to apply — the loaded file may be "
                "identical to the running config, or the load step failed "
                "silently."
            )
        else:
            response["errors"].extend(commit.error_lines or [])
        if is_err:
            response["errors"].extend(err_lines[-3:])
        response["next_actions"].append(RESTORE_NEXT_ACTION)
        log_request("restore_device", request, response)
        return response


def read_backup(
    filename: str,
    device: str,
    bucket: Optional[str] = None,
) -> Dict[str, Any]:
    """Read a saved backup off this host and return its raw text content.

    Pure local filesystem read — no device contact. The caller receives
    the file's bytes as a UTF-8 string in ``content`` so it can inspect
    what's in a saved file before invoking :func:`restore_device`.

    Concrete use case: after ``load_override_factory_default``, the
    device's master key changes and any ``enc-<base64>`` ciphertexts
    in the backup (e.g. ``system login ncm user dnroot password
    enc-...``) can no longer be decrypted — a vanilla restore then
    fails on commit with ``'password' is missing at 'system login ncm
    user dnroot'``. Use this tool to find every path that carries an
    ``enc-...`` value, then point :func:`restore_device`'s
    ``post_load_commands`` at those paths (e.g.
    ``["no system login ncm user dnroot"]`` or a fresh
    ``"system login ncm user dnroot password <new>"``) so the
    candidate is repaired between ``load`` and ``commit``.

    Args:
        filename: Backup filename as listed by ``list_backups``.
        device: Device alias whose folder holds the file
            (``<state_dir>/backups/cli/<device>/[<bucket>/]<file>``).
            Must match the in-filename device prefix; mismatch is rejected.
        bucket: Optional sub-bucket under the device folder (must
            match what ``backup_device`` was called with). ``None``
            (default) = directly in the device folder.
    """
    device_err = backup_store.validate_device(device)
    if device_err:
        return error_response(device_err, device=device)
    bucket_err = backup_store.validate_bucket(bucket)
    if bucket_err:
        return error_response(bucket_err, device=device)

    stat = backup_store.stat_backup(filename, device=device, bucket=bucket)
    if stat is None:
        bucket_hint = f" in bucket {bucket!r}" if bucket is not None else ""
        return error_response(
            f"backup {filename!r} not found under device {device!r}"
            f"{bucket_hint} or not a canonical backup name. Use "
            f"list_backups(device={device!r}) to see what's available.",
            device=device,
        )
    if stat.device != device:
        return error_response(
            f"filename device prefix {stat.device!r} does not match "
            f"device argument {device!r}; refusing to read.",
            device=device,
        )
    if stat.size_bytes > _READ_BACKUP_MAX_BYTES:
        return error_response(
            f"backup {filename!r} is {stat.size_bytes} bytes, over the "
            f"{_READ_BACKUP_MAX_BYTES}-byte read_backup cap; inspect it "
            f"directly at {stat.path} instead of loading it into the "
            f"response.",
            device=device,
        )

    try:
        payload = backup_store.download_bytes(
            filename, device=device, bucket=bucket,
        )
    except Exception as exc:
        return error_response(
            f"failed to download {filename!r}: {exc}", device=device,
        )

    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        return error_response(
            f"backup {filename!r} is not valid UTF-8 ({exc}).",
            device=device,
        )

    response = make_response(
        device=stat.device, host="", command="read_backup",
        filename=stat.filename, bucket=stat.bucket,
        backup_path=stat.path, size_bytes=stat.size_bytes,
        timestamp_utc=stat.timestamp_utc, description=stat.description,
        content=content,
    )
    log_request(
        "read_backup",
        {"filename": filename, "device": device, "bucket": bucket},
        # Don't echo the file content into the JSONL request log.
        {**response, "content": f"<{len(content)} chars elided>"},
    )
    return response


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(backup_device)
    mcp.tool()(list_backups)
    mcp.tool()(restore_device)
    mcp.tool()(read_backup)
