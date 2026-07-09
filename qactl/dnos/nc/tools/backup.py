"""Local NETCONF backup tools.

Four tools that move per-section XML configurations through the **local**
tarball store in :mod:`qactl.nc.core.backup_store` (``<state_dir>/backups/nc``):

- ``netconf_backup`` — pull ``<get-config>``, split into per-section XMLs,
  pack into a single ``.tar.gz`` and write it to the local backup store.
- ``netconf_restore`` — destructive: read a backup, extract into a
  ``tempfile.TemporaryDirectory``, edit-config each section onto the
  candidate datastore and commit. ``confirm=True`` required to execute.
- ``netconf_diff`` — compare running config against a backup tarball or
  an inline XML payload (per-section unified diffs for tarballs).
- ``netconf_list_backups`` — list the local backup tree; no NETCONF
  session opened.

The filename grammar (``<device>__<UTC-YYYYMMDD-HHMMSS>[__<desc>].tar.gz``)
and bucket validation live in ``qactl.nc.core.backup_store``; this module is
just the tool-facing surface.
"""

from __future__ import annotations

import difflib
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from qactl.dnos.nc.core import backup_store
from qactl.dnos.nc.core.change_ops import _try_commit
from qactl.dnos.nc.core.device_log import _begin, _log_action, _log_event
from qactl.dnos.nc.core.netconf_rpc import edit_config, get_config, require_candidate
from qactl.dnos.nc.core.results import _base_result, _error_result
from qactl.dnos.nc.core.session import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    _connect_device,
    _session_id,
)
from qactl.dnos.nc.core.xml_payload import (
    extract_dn_top_payloads,
    load_restore_sections,
    split_config_to_sections,
)


def netconf_backup(
    device: str,
    description: Optional[str] = None,
    bucket: Optional[str] = None,
    host: Optional[str] = None,
    source: str = "running",
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    no_verify: bool = True,
    verify_mgmt0: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Backup full device config as a single .tar.gz on dnftp.

    Pulls ``<get-config source=running>``, splits into per-section payloads
    under ``<drivenets-top>`` (one file per direct child), packs them into
    a single ``.tar.gz`` and uploads it to
    ``/ftpdisk/dn/oshaboo/netconf/backups/[<bucket>/]<device>__<UTC-YYYYMMDD-HHMMSS>[__<description>].tar.gz``
    on ``dnftp``. No local copy is kept on the host.

    Buckets are an optional one-level grouping under the backup root,
    chosen by the agent at backup time (e.g. ``bug-1234-repro``,
    ``save-for-later``). When ``bucket`` is omitted / ``None`` the tool
    falls back to today's UTC date as a bucket name (``YYYY-MM-DD``,
    e.g. ``2026-04-28``) so backups are auto-grouped by capture day
    instead of piling up in the root. Pass an explicit ``bucket=`` to
    override (for example to keep several captures together under a
    bug-id label across days). Bucket directories are auto-created on
    demand and reused if they already exist — repeated calls into the
    same date or named bucket simply add to it. ``netconf_restore`` /
    ``netconf_diff`` / ``netconf_list_backups`` accept the same
    ``bucket`` argument; the resolved bucket is reported back in the
    response envelope so a follow-up restore can pass it verbatim.

    Args:
        device: Device alias (cl, sa, kira, ...). Pinned into the filename
            so a restore can only target the same device.
        description: Optional short description; sanitised to
            ``[A-Za-z0-9._-]{1,40}`` (illegal chars collapsed to ``_``;
            empty after sanitisation -> dropped).
        bucket: Optional sub-directory name (``[A-Za-z0-9._-]{1,60}``,
            no ``/``, no ``__``). ``None`` / omitted = today's UTC date
            (``YYYY-MM-DD``); pass an explicit string to override. The
            bucket directory is auto-created on demand and reused if it
            already exists.
        host: Raw NETCONF host (alternative to device).
        source: NETCONF datastore to back up (``running`` default).
    """
    sid = _session_id()

    err = backup_store.validate_device(device)
    if err:
        return _error_result("backup", sid, ValueError(err))
    bucket_err = backup_store.validate_bucket(bucket)
    if bucket_err:
        return _error_result("backup", sid, ValueError(bucket_err))
    # bucket=None means "auto-organise by capture day". Resolve before
    # upload_bytes (which calls _mkdir_p internally) so the response
    # envelope reports the concrete YYYY-MM-DD bucket the tarball
    # actually landed in — netconf_restore / netconf_diff need that
    # exact string later.
    if bucket is None:
        bucket = backup_store.default_bucket()

    try:
        filename = backup_store.make_filename(device, description)
    except ValueError as e:
        return _error_result("backup", sid, e)
    arc_root = filename[: -len(".tar.gz")]

    try:
        with _connect_device(host, device, port, user, password, no_verify, timeout, verify_mgmt0) as cr:
            log_path = _begin(cr, sid, "backup", device=device)
            result_xml = get_config(cr.mgr, source=source, dn_only=True)
            sections = split_config_to_sections(result_xml)
            if not sections:
                raise ValueError(
                    "no <drivenets-top> sections in get-config reply; "
                    "refusing to upload an empty backup."
                )
            tarball = backup_store.pack_sections(sections, arc_root=arc_root)
            _log_action(
                log_path, "action", action="pack",
                sections=len(sections), bytes=len(tarball),
            )
            uploaded = backup_store.upload_bytes(
                tarball, filename, bucket=bucket,
            )
            _log_action(
                log_path, "action", action="upload",
                path=uploaded.path, size_bytes=uploaded.size_bytes,
                bucket=str(bucket) if bucket else "",
            )
            _log_event(log_path, sid, "end", status="ok")
            section_names = [name for name, _ in sections]
            return _base_result(
                "backup", cr, sid,
                {
                    "status": "ok",
                    "source": source,
                    "filename": uploaded.filename,
                    "bucket": uploaded.bucket,
                    "backup_path": uploaded.path,
                    "size_bytes": uploaded.size_bytes,
                    "timestamp_utc": uploaded.timestamp_utc,
                    "description": uploaded.description,
                    "sections": section_names,
                    "sections_count": len(section_names),
                },
            )
    except Exception as e:
        return _error_result("backup", sid, e)


_RESTORE_MODES = ("replace", "merge", "none")


def netconf_restore(
    device: str,
    filename: str,
    bucket: Optional[str] = None,
    confirm: bool = False,
    mode: str = "merge",
    host: Optional[str] = None,
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    no_verify: bool = True,
    verify_mgmt0: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Restore a backup .tar.gz from dnftp into the device's candidate datastore.

    DESTRUCTIVE on commit. Flow:

    1. ``stat`` the tarball on dnftp; refuse if missing or if its filename
       device prefix does not match the ``device`` argument.
    2. ``confirm=False`` (default) returns a dry-run envelope describing
       what would happen. Pass ``confirm=True`` to execute steps 3-7.
    3. Download the tarball into a :class:`tempfile.TemporaryDirectory`.
    4. Extract section files (``<section>.xml``) with strict path checks.
    5. ``edit-config target=candidate`` for each section in dependency
       order (config-groups -> system -> ... -> apply-groups), using
       ``<default-operation>`` = ``mode``.
    6. ``commit`` once at the end. On commit failure the candidate is
       left uncommitted; the device keeps its pre-restore config.
    7. Cleanup is automatic (temp dir removed on context exit).

    Args:
        device: Device alias the restore targets. Must match the
            ``<device>__...`` prefix of ``filename``.
        filename: Backup filename as listed by ``netconf_list_backups``.
        bucket: Optional sub-bucket the file lives in (must match what
            was passed to ``netconf_backup``). ``None`` = root.
        confirm: Must be ``True`` to actually edit and commit. Default
            is dry-run.
        mode: NETCONF ``<default-operation>`` applied to every per-section
            edit-config. ``"merge"`` (default) is additive — never
            deletes existing nodes; this is the conservative, backwards-
            compatible behaviour and matches what the tool did before
            ``mode`` was introduced. ``"replace"`` overrides each section
            present in the backup (closest equivalent to the CLI's
            ``commit override``); NOTE per RFC 6241 §7.2 this only
            replaces nodes that appear in the payload — top-level
            sections absent from the backup (e.g. no ``protocols.xml``
            packed) are NOT wiped on the device. ``"none"`` requires
            explicit per-element ``nc:operation`` annotations in the
            payload.
    """
    sid = _session_id()

    if mode not in _RESTORE_MODES:
        return _error_result(
            "restore", sid,
            ValueError(
                f"invalid mode {mode!r}; expected one of {_RESTORE_MODES}"
            ),
        )

    bucket_err = backup_store.validate_bucket(bucket)
    if bucket_err:
        return _error_result("restore", sid, ValueError(bucket_err))

    stat = backup_store.stat_backup(filename, bucket=bucket)
    if stat is None:
        bucket_hint = f" in bucket {bucket!r}" if bucket else ""
        return _error_result(
            "restore", sid,
            FileNotFoundError(
                f"backup {filename!r} not found{bucket_hint} on dnftp, "
                "or not a canonical backup name."
            ),
        )
    if stat.device != device:
        return _error_result(
            "restore", sid,
            ValueError(
                f"filename device prefix {stat.device!r} does not match "
                f"device argument {device!r}; refusing to restore."
            ),
        )

    if not confirm:
        return {
            "action": "restore",
            "session_id": sid,
            "status": "dry_run",
            "filename": stat.filename,
            "bucket": stat.bucket,
            "backup_path": stat.path,
            "size_bytes": stat.size_bytes,
            "timestamp_utc": stat.timestamp_utc,
            "description": stat.description,
            "device": device,
            "mode": mode,
            "warning": (
                f"confirm=False: nothing was changed on the device. "
                f"Re-invoke with confirm=True to execute "
                f"(default-operation={mode!r})."
            ),
        }

    try:
        with tempfile.TemporaryDirectory(prefix="netconf-restore-") as td:
            tar_path = os.path.join(td, filename)
            backup_store.download_to_path(filename, tar_path, bucket=bucket)
            sections_dir = os.path.join(td, "sections")
            backup_store.extract_to_dir(
                tar_path, sections_dir,
                expected_arc_root=stat.basename,
            )
            sections = load_restore_sections(sections_dir)
            section_results: List[Dict[str, Any]] = []

            with _connect_device(host, device, port, user, password, no_verify, timeout, verify_mgmt0) as cr:
                log_path = _begin(cr, sid, "restore", device=device)
                _log_action(
                    log_path, "action", action="download",
                    path=stat.path, size_bytes=stat.size_bytes,
                )

                m = cr.mgr
                require_candidate(m)

                for name, payload in sections:
                    edit_config(
                        m,
                        config_xml=payload,
                        target="candidate",
                        default_operation=mode,
                    )
                    section_results.append({
                        "section": name, "bytes": len(payload), "status": "ok",
                    })
                    _log_action(
                        log_path, "action", action="restore-edit",
                        section=name, mode=mode, result="ok",
                    )

                commit_status, commit_xml = _try_commit(m, log_path)

                _log_event(log_path, sid, "end", status="ok")
                return _base_result(
                    "restore", cr, sid,
                    {
                        "status": "ok",
                        "filename": stat.filename,
                        "bucket": stat.bucket,
                        "backup_path": stat.path,
                        "size_bytes": stat.size_bytes,
                        "timestamp_utc": stat.timestamp_utc,
                        "mode": mode,
                        "sections": section_results,
                        "sections_count": len(section_results),
                        "commit_status": commit_status,
                        "commit_xml": commit_xml,
                    },
                )
    except Exception as e:
        return _error_result("restore", sid, e)


def netconf_list_backups(
    device: Optional[str] = None,
    bucket: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """List backups stored on dnftp, newest first.

    Pure SFTP read against dnftp — no NETCONF session opened. Each entry
    carries the parsed ``device`` / ``timestamp_utc`` / ``description`` /
    ``bucket`` plus the file's ``size_bytes`` and absolute POSIX
    ``path``. Files that don't match the canonical naming shape are
    surfaced under ``orphans``.

    Args:
        device: When set, only list backups for that device alias.
        bucket: When set, only list backups inside that sub-bucket.
            ``None`` (default) walks the root AND every sub-bucket;
            each entry's ``bucket`` field tells you which one it came
            from (``None`` = root). Pass the special string ``"-"`` to
            list root-only.
        limit: Maximum number of entries to return (default 100).
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return {
            "action": "list_backups",
            "status": "error",
            "error": "limit must be a positive integer.",
            "device": device,
            "bucket": bucket,
        }

    if bucket == "-":
        list_bucket: Optional[str] = None
        list_root_only = True
    else:
        list_bucket = bucket
        list_root_only = False
        if bucket is not None:
            err = backup_store.validate_bucket(bucket)
            if err:
                return {
                    "action": "list_backups",
                    "status": "error",
                    "error": err,
                    "device": device,
                    "bucket": bucket,
                }

    if list_root_only:
        all_backups = backup_store.list_backups(
            device=device, limit=None, bucket=None,
        )
        backups = [b for b in all_backups if b.bucket is None]
        if limit and limit > 0:
            backups = backups[:limit]
    else:
        backups = backup_store.list_backups(
            device=device, limit=limit, bucket=list_bucket,
        )

    orphans = backup_store.list_orphans()
    buckets = backup_store.list_buckets()
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
            f"{len(orphans)} file(s) under {backup_store.BACKUP_DIR} "
            "have non-canonical names (see 'orphans' field)."
        )
    return {
        "action": "list_backups",
        "status": "ok",
        "count": len(entries),
        "backup_host": backup_store.BACKUP_HOST,
        "backup_dir": backup_store.BACKUP_DIR,
        "backups": entries,
        "buckets": buckets,
        "orphans": orphans,
        "warnings": warnings,
    }


def netconf_diff(
    device: Optional[str] = None,
    filename: Optional[str] = None,
    bucket: Optional[str] = None,
    inline_xml: Optional[str] = None,
    subtree: Optional[str] = None,
    host: Optional[str] = None,
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    no_verify: bool = True,
    verify_mgmt0: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Compare running config against a backup on dnftp or against inline XML.

    Provide exactly one reference: ``filename=`` (a backup tarball on
    dnftp; downloaded into a temp dir, extracted, diffed per-section) or
    ``inline_xml=`` (a raw payload string; produces a single unified
    diff). ``subtree`` is an optional XML filter to scope the running
    config read.

    When ``filename=`` is used and ``device=`` is provided, the
    filename's device prefix MUST match ``device`` (same safety pin as
    ``netconf_restore``).
    """
    sid = _session_id()
    if not filename and not inline_xml:
        return _error_result(
            "diff", sid,
            ValueError("Provide filename= (dnftp backup) or inline_xml="),
        )
    if filename and inline_xml:
        return _error_result(
            "diff", sid,
            ValueError("Provide only one of filename= / inline_xml="),
        )

    bucket_err = backup_store.validate_bucket(bucket)
    if bucket_err:
        return _error_result("diff", sid, ValueError(bucket_err))

    stat = None
    if filename:
        stat = backup_store.stat_backup(filename, bucket=bucket)
        if stat is None:
            bucket_hint = f" in bucket {bucket!r}" if bucket else ""
            return _error_result(
                "diff", sid,
                FileNotFoundError(
                    f"backup {filename!r} not found{bucket_hint} on dnftp, "
                    "or not a canonical backup name."
                ),
            )
        if device and stat.device != device:
            return _error_result(
                "diff", sid,
                ValueError(
                    f"filename device prefix {stat.device!r} does not match "
                    f"device argument {device!r}; refusing to diff."
                ),
            )

    try:
        with tempfile.TemporaryDirectory(prefix="netconf-diff-") as td:
            sections_dir: Optional[str] = None
            if stat is not None:
                tar_path = os.path.join(td, stat.filename)
                backup_store.download_to_path(
                    stat.filename, tar_path, bucket=bucket,
                )
                sections_dir = os.path.join(td, "sections")
                backup_store.extract_to_dir(
                    tar_path, sections_dir,
                    expected_arc_root=stat.basename,
                )

            with _connect_device(host, device, port, user, password, no_verify, timeout, verify_mgmt0) as cr:
                log_path = _begin(cr, sid, "diff", device=device)
                if stat is not None:
                    _log_action(
                        log_path, "action", action="download",
                        path=stat.path, size_bytes=stat.size_bytes,
                    )

                running_xml = get_config(cr.mgr, subtree=subtree, dn_only=True)
                diffs: List[Dict[str, Any]] = []

                if sections_dir is not None:
                    running_sections = {
                        name: payload
                        for name, payload in extract_dn_top_payloads(running_xml)
                    }
                    for xml_file in sorted(Path(sections_dir).iterdir()):
                        if xml_file.suffix != ".xml":
                            continue
                        section_name = xml_file.stem
                        ref_text = xml_file.read_text(encoding="utf-8")
                        ref_sections = extract_dn_top_payloads(ref_text)
                        ref_payload = (
                            ref_sections[0][1] if ref_sections else ref_text
                        )
                        running_payload = running_sections.get(section_name, "")
                        diff_lines = list(difflib.unified_diff(
                            ref_payload.splitlines(keepends=True),
                            running_payload.splitlines(keepends=True),
                            fromfile=f"backup/{section_name}",
                            tofile=f"running/{section_name}",
                        ))
                        diffs.append({
                            "section": section_name,
                            "has_changes": len(diff_lines) > 0,
                            "diff_text": "".join(diff_lines),
                        })
                    for section_name in running_sections:
                        if not (Path(sections_dir) / f"{section_name}.xml").exists():
                            diffs.append({
                                "section": section_name,
                                "has_changes": True,
                                "diff_text": (
                                    f"--- Section '{section_name}' not in "
                                    "backup, present in running\n"
                                ),
                            })
                else:
                    ref_text = inline_xml  # type: ignore[assignment]
                    diff_lines = list(difflib.unified_diff(
                        ref_text.splitlines(keepends=True),
                        running_xml.splitlines(keepends=True),
                        fromfile="reference",
                        tofile="running",
                    ))
                    diffs.append({
                        "section": "full",
                        "has_changes": len(diff_lines) > 0,
                        "diff_text": "".join(diff_lines),
                    })

                changed = sum(1 for d in diffs if d["has_changes"])
                _log_event(
                    log_path, sid, "end",
                    status="ok", changed_sections=changed,
                )
                payload = {
                    "status": "ok",
                    "changed_sections": changed,
                    "total_sections": len(diffs),
                    "diffs": diffs,
                }
                if stat is not None:
                    payload.update({
                        "filename": stat.filename,
                        "bucket": stat.bucket,
                        "backup_path": stat.path,
                    })
                return _base_result("diff", cr, sid, payload)
    except Exception as e:
        return _error_result("diff", sid, e)


def netconf_read_backup(
    filename: str,
    bucket: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch a backup tarball off dnftp and return its per-section XML.

    Pure SFTP read — no NETCONF session opened. The agent receives
    every per-section XML file in ``sections`` as ``{"name": str,
    "xml": str}`` pairs (the same on-disk shape :func:`netconf_restore`
    consumes), so it can inspect what's in a saved backup without
    touching the device.

    Concrete use case: after a factory-reset the device's master key
    changes and any ``<password>enc-...</password>`` ciphertexts in
    the backup can no longer be decrypted — restore commits with
    ``Invalid password. Password could not be decrypted with the
    current master key``. Use this tool to surface which leaves carry
    an ``enc-...`` value (typically under ``system.xml``); after
    :func:`netconf_restore` re-set those leaves via :func:`netconf_edit`
    on the post-restore box.
    """
    sid = _session_id()

    bucket_err = backup_store.validate_bucket(bucket)
    if bucket_err:
        return _error_result("read_backup", sid, ValueError(bucket_err))

    stat = backup_store.stat_backup(filename, bucket=bucket)
    if stat is None:
        bucket_hint = f" in bucket {bucket!r}" if bucket else ""
        return _error_result(
            "read_backup", sid,
            FileNotFoundError(
                f"backup {filename!r} not found{bucket_hint} on dnftp, "
                "or not a canonical backup name."
            ),
        )

    try:
        with tempfile.TemporaryDirectory(prefix="netconf-readbackup-") as td:
            tar_path = os.path.join(td, filename)
            backup_store.download_to_path(filename, tar_path, bucket=bucket)
            sections_dir = os.path.join(td, "sections")
            backup_store.extract_to_dir(
                tar_path, sections_dir, expected_arc_root=stat.basename,
            )
            sections: List[Dict[str, Any]] = []
            for xml_file in sorted(os.listdir(sections_dir)):
                if not xml_file.endswith(".xml"):
                    continue
                with open(os.path.join(sections_dir, xml_file), "r",
                          encoding="utf-8") as fh:
                    xml_text = fh.read()
                sections.append({
                    "name": xml_file[: -len(".xml")],
                    "xml": xml_text,
                })

        return {
            "action": "read_backup",
            "session_id": sid,
            "status": "ok",
            "filename": stat.filename,
            "bucket": stat.bucket,
            "backup_path": stat.path,
            "size_bytes": stat.size_bytes,
            "timestamp_utc": stat.timestamp_utc,
            "description": stat.description,
            "device": stat.device,
            "sections": sections,
            "sections_count": len(sections),
        }
    except Exception as e:
        return _error_result("read_backup", sid, e)


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(netconf_backup)
    mcp.tool()(netconf_restore)
    mcp.tool()(netconf_list_backups)
    mcp.tool()(netconf_diff)
    mcp.tool()(netconf_read_backup)
