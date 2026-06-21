"""Device-registry MCP tools (``list_devices`` / ``manage_device``).

cli-mcp is the single authority for adding devices to the lab
registry: ``manage_device(add)`` SSHes the chassis once, derives the
alias from the device's configured ``System Name`` (no override —
the registry alias and the chassis name are kept in lockstep on
purpose), captures ``System-Id`` / ``System Type`` (→ ``expected_role``)
plus the mgmt0 IP, writes a fully-populated entry to the canonical
``<repo>/devices/devices_mgmt0.json`` map, and takes the initial
backup. Every other MCP (netconf-mcp, restconf-mcp, …) consumes that
map read-only via ``dnctl.core.devices`` — none of them have an
``add_device`` of their own.

The ``save_/remove_device_host`` helpers in ``dnctl.cli.core.session``
project the alias->SN view (``DEVICE_HOSTS``) used by the SSH
transport pool, and this module wraps them in the standard envelope
shape.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from dnctl.core import devices as _dn_devices

from dnctl.cli.core import backup_store
from dnctl.cli.core.envelope import make_response
from dnctl.cli.core.logging import log_request
from dnctl.cli.core.registry import transport_registry
from dnctl.cli.core.session import (
    DEFAULT_PASSWORD,
    DEFAULT_USER,
    DEVICE_HOSTS,
    ConnectError,
    DeviceProbe,
    probe_device,
    reload_device_hosts,
    remove_device_host,
)


_MANAGE_DEVICE_FIX = (
    "Fix the arguments and retry; use list_devices to see current aliases."
)

# Description tag baked into the very first backup taken at registration
# time. Stays inside the [A-Za-z0-9._-]{1,40} budget that
# backup_store.make_filename enforces, so it round-trips through the
# canonical filename grammar without sanitisation.
_INITIAL_ADD_DESCRIPTION = "initial-add"


def _post_add_init(device: str) -> tuple[Optional[str], List[str]]:
    """Best-effort init after a fresh ``manage_device(add)``.

    Two side effects, both opportunistic:

    1. ``backup_store.ensure_dir(device=device)`` — pre-creates the
       per-device folder ``cli/backups/<device>/`` on dnftp, so the
       first scheduled backup doesn't have to wait for it. Catches
       dnftp connectivity issues at registration time instead of 24h
       later when the nightly timer fires.
    2. ``backup_device(device=device, description="initial-add")`` —
       takes the very first config snapshot. Useful as a baseline
       (everything we change later can be diffed against it) and
       proves the SSH path to the device works.

    Both steps are wrapped: any failure is appended to the returned
    warnings list. Registration itself is never aborted by a failure
    here — a device that's offline at add-time should still register
    successfully so a later ``backup_device`` / ``configure`` call
    (when it's reachable) just works.

    Returns:
        ``(initial_backup_path, warnings)``. ``initial_backup_path``
        is the on-dnftp absolute path of the snapshot when it was
        taken successfully, ``None`` otherwise (folder pre-create
        failed, device unreachable, backup tool returned non-ok, …).
    """
    warnings: List[str] = []
    try:
        backup_store.ensure_dir(device=device)
    except Exception as exc:  # noqa: BLE001
        warnings.append(
            f"post-add ensure_dir({device!r}) failed: "
            f"{type(exc).__name__}: {exc}. The per-device folder will "
            f"be created lazily on the first successful backup."
        )
        return None, warnings

    # Local import to avoid an import cycle if ``dnctl.cli.tools/backup`` ever
    # ends up importing from ``dnctl.cli.tools/devices`` (today it doesn't, but
    # the registration order is module-by-module from cli_mcp_server.py
    # so we don't want to hard-couple the import order here).
    try:
        from dnctl.cli.tools.backup import backup_device
    except Exception as exc:  # noqa: BLE001
        warnings.append(
            f"post-add backup_device import failed: "
            f"{type(exc).__name__}: {exc}. Run backup_device manually."
        )
        return None, warnings

    try:
        env = backup_device(
            device=device, description=_INITIAL_ADD_DESCRIPTION,
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(
            f"post-add backup_device({device!r}) raised: "
            f"{type(exc).__name__}: {exc}. The device is registered; "
            f"call backup_device manually once it's reachable."
        )
        return None, warnings

    if not isinstance(env, dict) or env.get("status") != "ok":
        errs = env.get("errors") if isinstance(env, dict) else None
        first_err = errs[0] if errs else "non-ok envelope"
        warnings.append(
            f"post-add backup_device({device!r}) returned non-ok: "
            f"{first_err}. The device is registered; call backup_device "
            f"manually once the issue is resolved."
        )
        return None, warnings

    path = env.get("backup_path")
    return (path if isinstance(path, str) else None), warnings


def list_devices() -> Dict[str, Any]:
    """List known device aliases and their SN hostname candidates.

    Reads the in-memory ``DEVICE_HOSTS`` cache (backed by the canonical
    ``<repo>/devices/devices_mgmt0.json`` map shared with netconf-mcp).
    Each entry is ``{"device": <alias>, "hosts": [<sn>, ...]}``. For
    dual-NCC chassis the hosts list has both NCC SNs.

    Use this before any show/show_config call when the caller is unsure
    which ``device=`` alias is available.
    """
    devices = [
        {
            "device": alias,
            "hosts": list(hosts),
            "aliases": _dn_devices.get_aliases(alias),
        }
        for alias, hosts in sorted(DEVICE_HOSTS.items())
    ]
    response = make_response(devices=devices)
    log_request("list_devices", {}, response)
    return response


def _add_device(
    sn: str,
    user: str,
    password: str,
    timeout: int,
    request: Dict[str, Any],
    fail: Any,
) -> Dict[str, Any]:
    """Implementation of ``manage_device(operation="add")``.

    Probes the chassis once via SSH (``show system`` +
    ``show interfaces management``), uses ``System Name`` as the
    alias (no override — the registry alias and the chassis name
    stay in lockstep so the registry never disagrees with the
    device about what to call it), checks for System-Id collisions,
    persists the full entry (``expected_sns`` + ``expected_role`` +
    ``mgmt0`` + ``system_id``) to the canonical map, and runs the
    post-add backup. Split out from ``manage_device`` to keep the
    add path linear — every other operation is a one-liner.

    Multi-NCC auto-discovery: the same ``show system`` capture already
    lists every NCC slot and its Serial Number, so on a dual-NCC (CL)
    chassis we enroll BOTH NCC serials (the SSHed one + its standby peer)
    into ``expected_sns`` in this single pass — a lone
    ``add(sn=<one NCC SN>)`` fully registers the cluster. The manual
    second-``add`` path still works as a fallback: re-adding the peer SN
    finds it already present (same System-Id) and is a no-op append.
    """
    if not sn:
        return fail("sn is required for operation='add'.")

    try:
        probe: DeviceProbe = probe_device(
            transport_registry, host=sn,
            user=user, password=password, timeout=float(timeout),
        )
    except ConnectError as exc:
        return fail(
            f"SSH probe of sn={sn!r} failed: {exc}. The device must be "
            f"reachable for cli-mcp to register it (we capture System "
            f"Name, role, mgmt0 IP, and the initial backup in one shot).",
            hosts=[], added=False, derived_name=None,
        )
    except Exception as exc:  # noqa: BLE001 - probe_device wraps the SSH path
        return fail(
            f"SSH probe of sn={sn!r} failed: "
            f"{type(exc).__name__}: {exc}. Verify SSH credentials and "
            f"that `show system` runs cleanly, then retry.",
            hosts=[], added=False, derived_name=None,
        )

    alias = probe.system_name

    existing_entry = _dn_devices.get_device_entry(alias) or {}
    existing_hosts: List[str] = []
    if isinstance(existing_entry, dict):
        raw = existing_entry.get("expected_sns")
        if isinstance(raw, list):
            existing_hosts = [s for s in raw if isinstance(s, str) and s]

    if existing_entry:
        existing_sid = (
            existing_entry.get("system_id") if isinstance(existing_entry, dict)
            else None
        )
        if (
            probe.system_id
            and existing_sid
            and probe.system_id != existing_sid
        ):
            return fail(
                f"alias '{alias}' is already registered for chassis "
                f"system_id={existing_sid!r}, but sn={sn!r} reports "
                f"system_id={probe.system_id!r}. cli-mcp will not "
                f"register two distinct chassis under the same "
                f"System Name. Rename one of them on the chassis "
                f"(`set system name <new-name>` + commit), then retry "
                f"manage_device(operation='add', sn={sn!r}).",
                hosts=list(existing_hosts), added=False, derived_name=alias,
            )

    # Enroll the probed SN plus every peer NCC serial the chassis just
    # reported in the SAME `show system` pass. On a dual-NCC (CL) box this
    # means a single add fully registers BOTH NCCs (the SSHed one and its
    # standby peer) under the one System-Id, instead of needing a manual
    # second `add` for the standby. Order: existing entries first, then the
    # probed SN, then the discovered peers — dedup preserves the first
    # occurrence. The probed SN stays in the list even when the chassis
    # doesn't surface a matching serial row (e.g. added by mgmt IP /
    # hostname rather than chassis SN), so the manual second-add path and
    # hostname-based registrations keep working unchanged.
    sn_added = sn not in existing_hosts
    hosts_after = list(existing_hosts)
    if sn_added:
        hosts_after.append(sn)
    auto_enrolled: List[str] = []
    for peer in probe.ncc_serials:
        if peer and peer not in hosts_after:
            hosts_after.append(peer)
            auto_enrolled.append(peer)
    added = sn_added or bool(auto_enrolled)

    fields: Dict[str, Any] = {"expected_sns": hosts_after}
    if probe.expected_role and (
        not isinstance(existing_entry, dict)
        or not existing_entry.get("expected_role")
    ):
        fields["expected_role"] = probe.expected_role
    if probe.mgmt0:
        fields["mgmt0"] = probe.mgmt0
    if probe.system_id:
        fields["system_id"] = probe.system_id

    try:
        _dn_devices.update_device(alias, **fields)
    except Exception as exc:  # noqa: BLE001
        return fail(
            f"failed to write canonical map for alias '{alias}': "
            f"{type(exc).__name__}: {exc}",
            hosts=list(existing_hosts), added=False,
        )
    reload_device_hosts()

    warnings: List[str] = []
    if not sn_added:
        warnings.append(
            f"'{sn}' was already registered under device '{alias}'."
        )
    if auto_enrolled:
        warnings.append(
            f"auto-enrolled peer NCC serial(s) {auto_enrolled} discovered "
            f"from `show system` into device '{alias}' (same System-Id "
            f"{probe.system_id!r}); a single add now registers the whole "
            f"chassis."
        )
    if not probe.expected_role:
        warnings.append(
            "expected_role could not be parsed from `show system` "
            "(System Type missing or unrecognised); netconf-mcp will "
            "refuse to connect until expected_role is set on the entry."
        )
    if not probe.mgmt0:
        warnings.append(
            "mgmt0 IP could not be parsed from `show interfaces "
            "management`; netconf-mcp will refuse to connect until "
            "manage_device(refresh, name=...) populates it."
        )

    initial_backup_path: Optional[str] = None
    is_new_alias = not existing_entry
    if is_new_alias:
        initial_backup_path, post_warnings = _post_add_init(alias)
        warnings.extend(post_warnings)

    final_entry = _dn_devices.get_device_entry(alias) or {}
    response = make_response(
        device=alias, host=sn,
        warnings=warnings,
        operation="add", hosts=hosts_after, added=added,
        auto_enrolled_ncc_sns=auto_enrolled,
        initial_backup_path=initial_backup_path,
        derived_name=alias,
        entry=final_entry,
    )
    log_request("manage_device", request, response)
    return response


def _refresh_device(
    name: str,
    user: str,
    password: str,
    timeout: int,
    request: Dict[str, Any],
    fail: Any,
) -> Dict[str, Any]:
    """Implementation of ``manage_device(operation="refresh")``.

    Re-probes an existing alias by SSHing each ``expected_sns`` host in
    turn and overwriting ``mgmt0`` / ``expected_role`` / ``system_id``
    with the values reported by the chassis. ``expected_sns`` itself is
    NOT touched — that's an explicit ``add`` / ``remove`` decision. The
    first SN that answers and yields a parseable probe wins; the rest
    are skipped.

    This is the path netconf-mcp / restconf-mcp / gnmi-mcp tell their
    callers to invoke when their cached mgmt0 stops responding (or the
    chassis on that IP reports a different SN). Those MCPs never SSH
    themselves — cli-mcp owns the wire.
    """
    entry = _dn_devices.get_device_entry(name) or {}
    if not entry:
        return fail(
            f"device '{name}' is not registered. Use "
            f"operation='add' with sn=<ssh-host> to register it first.",
            hosts=[], refreshed=False,
        )
    # Operate on the canonical key, not a passed-in secondary nickname —
    # otherwise the map write below forks a ghost canonical entry and the
    # drift check compares the chassis name against the wrong key.
    name = _dn_devices.resolve_canonical(name) or name
    raw_sns = entry.get("expected_sns") if isinstance(entry, dict) else None
    sns: List[str] = (
        [s for s in raw_sns if isinstance(s, str) and s]
        if isinstance(raw_sns, list) else []
    )
    if not sns:
        return fail(
            f"device '{name}' has no expected_sns recorded. Re-register "
            f"via operation='add' so cli-mcp can rediscover its SSH host.",
            hosts=[], refreshed=False,
        )

    probe: Optional[DeviceProbe] = None
    sn_used: Optional[str] = None
    failures: List[str] = []
    for candidate in sns:
        try:
            probe = probe_device(
                transport_registry, host=candidate,
                user=user, password=password, timeout=float(timeout),
            )
            sn_used = candidate
            break
        except Exception as exc:  # noqa: BLE001 - try next SN
            failures.append(f"{candidate}: {type(exc).__name__}: {exc}")
            probe = None

    if probe is None or sn_used is None:
        joined = "; ".join(failures) or "no SNs reachable"
        return fail(
            f"refresh of '{name}' failed: could not SSH-probe any of "
            f"{sns}. {joined}",
            hosts=list(sns), refreshed=False,
        )

    fields: Dict[str, Any] = {}
    if probe.mgmt0:
        fields["mgmt0"] = probe.mgmt0
    if probe.expected_role:
        fields["expected_role"] = probe.expected_role
    if probe.system_id:
        fields["system_id"] = probe.system_id

    if not fields:
        return fail(
            f"refresh of '{name}' connected to {sn_used!r} but the "
            f"device exposed no parseable mgmt0 / role / system-id.",
            hosts=list(sns), refreshed=False,
        )

    try:
        _dn_devices.update_device(name, **fields)
    except Exception as exc:  # noqa: BLE001
        return fail(
            f"refresh of '{name}' probed {sn_used!r} successfully but "
            f"writing the canonical map failed: "
            f"{type(exc).__name__}: {exc}",
            hosts=list(sns), refreshed=False,
        )
    reload_device_hosts()

    warnings: List[str] = []
    if not probe.mgmt0:
        warnings.append(
            "mgmt0 was not refreshed — `show interfaces management` "
            "did not yield a parseable IPv4."
        )
    if not probe.expected_role:
        warnings.append(
            "expected_role was not refreshed — `show system` did not "
            "expose a recognised System Type prefix."
        )
    if failures:
        warnings.append(
            "earlier SN candidates were unreachable: " + "; ".join(failures)
        )
    # The chassis may have been renamed since it was registered. refresh
    # deliberately does NOT change the registry key (other MCPs hold
    # references to it), but surface the drift so the operator can adopt
    # the new name with a single rename.
    if probe.system_name and probe.system_name != name:
        warnings.append(
            f"chassis System Name is now {probe.system_name!r} but the "
            f"registry key is still {name!r}; run "
            f"manage_device(operation='rename', name={name!r}, "
            f"new_name={probe.system_name!r}) to adopt it "
            f"(qactl cli device rename {name} {probe.system_name})."
        )

    final_entry = _dn_devices.get_device_entry(name) or {}
    response = make_response(
        device=name, host=sn_used,
        warnings=warnings,
        operation="refresh", hosts=list(sns), refreshed=True,
        derived_name=probe.system_name,
        entry=final_entry,
    )
    log_request("manage_device", request, response)
    return response


def manage_device(
    operation: Literal["add", "remove", "refresh", "rename", "alias", "unalias"],
    name: Optional[str] = None,
    sn: Optional[str] = None,
    alias: Optional[str] = None,
    new_name: Optional[str] = None,
    keep_old_alias: bool = True,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = 20,
) -> Dict[str, Any]:
    """Add, remove, or refresh a device alias in the registry.

    cli-mcp is the **single authority** for adding devices to the
    monorepo's device registry AND for any SSH-based re-probe of an
    already-registered device — netconf-mcp / restconf-mcp / gnmi-mcp
    consume the canonical ``<repo>/devices/devices_mgmt0.json`` map
    read-only and never SSH the chassis themselves. When their cached
    mgmt0 stops responding (or the chassis on that IP reports a
    different SN), they return an error envelope telling the caller to
    invoke ``manage_device(operation="refresh", name="<alias>")`` here,
    then retry. Every operation persists to the canonical map and
    refreshes the in-memory ``DEVICE_HOSTS`` cache, so the change is
    live for subsequent tool calls without restarting any server.

    The registry alias for any device is **always** the chassis's
    configured ``System Name`` — there is no override on ``add``.
    When a chassis's ``System Name`` changes, use
    ``operation="rename"`` to move the registry key in place
    (preserving creds / ``expected_sns`` / history, no re-probe);
    the old name is kept as a secondary alias by default. ``add`` of
    the new SN would instead create a duplicate entry, so prefer
    ``rename``.

    Operations:

    - ``add``    – register ``sn`` (an SSH-reachable hostname / SN / IP)
                   in the device registry. cli-mcp opens an SSH session
                   to ``sn`` once and captures everything every other
                   MCP needs from a single pass:

                   1. ``System Name``  → alias (no override possible).
                   2. ``System-Id``    → recorded so a future ``add`` of
                                         the same chassis's other NCC
                                         can confirm it really is the
                                         same chassis (vs a name
                                         collision between two unrelated
                                         devices).
                   2b. NCC Serial Numbers from the ``show system``
                                         hardware table → on a dual-NCC
                                         (CL) chassis BOTH NCC serials are
                                         enrolled into ``expected_sns`` in
                                         this one pass, so a single
                                         ``add(sn=<one NCC SN>)`` fully
                                         registers the cluster (the SSHed
                                         NCC + its standby peer). Any
                                         peers added this way are listed
                                         in ``auto_enrolled_ncc_sns``.
                   3. ``System Type``  → ``expected_role`` (``SA``/``CL``)
                                         used by netconf-mcp's role
                                         probe.
                   4. ``show interfaces management`` → ``mgmt0`` IP
                                         used by netconf-mcp /
                                         restconf-mcp / gnmi-mcp as the
                                         transport target.

                   When the derived alias already exists with a
                   DIFFERENT ``System-Id`` on the existing entry, the
                   call is rejected as a true name collision and the
                   error explains how to disambiguate (rename one of
                   the chassis on the device, then retry); same-
                   ``System-Id`` collisions are treated as "second
                   NCC of the same chassis" and the new SN is
                   appended to the existing ``expected_sns`` list.
                   (With multi-NCC auto-discovery the standby NCC is
                   usually enrolled already on the first add, so a
                   manual second add of the peer SN just no-ops.)

                   ``user`` / ``password`` / ``timeout`` are used for
                   the SSH probe and the post-add backup; defaults
                   match the lab dnroot credentials. ``name=`` is
                   NOT accepted on ``add`` — pass it only on
                   ``remove`` / ``refresh``.

                   On a NEW alias two best-effort side effects fire:

                   a. Pre-create ``cli/backups/<alias>/`` on dnftp so
                      the first scheduled backup doesn't have to.
                   b. Take an immediate ``backup_device(device=<alias>,
                      description="initial-add")`` snapshot — useful
                      as a baseline for diffs and proves the SSH
                      path works end-to-end.

                   Both are best-effort: any failure (dnftp down,
                   wrong creds, …) appends to the response
                   ``warnings`` and the registry write still
                   succeeds. The successful snapshot's on-dnftp path
                   is returned in ``initial_backup_path``.
    - ``remove``  – if ``sn`` is given, drop just that host from
                    ``name`` (the alias is removed too when its host
                    list becomes empty); if ``sn`` is omitted, drop
                    the whole alias.
    - ``refresh`` – re-probe an existing alias by SSHing each of its
                    ``expected_sns`` in turn (first one that answers
                    wins) and overwriting ``mgmt0`` /
                    ``expected_role`` / ``system_id`` with whatever
                    the chassis reports. ``expected_sns`` itself is
                    NOT touched (use ``add`` / ``remove`` for that).
                    This is the path netconf-mcp / restconf-mcp /
                    gnmi-mcp tell their callers to invoke when their
                    cached mgmt0 stops responding — those MCPs never
                    SSH the chassis themselves. Pass only ``name=``;
                    ``sn=`` is ignored.
    - ``rename`` – rename a canonical device key in place: move the
                    whole entry (``mgmt0`` / ``expected_role`` /
                    ``expected_sns`` / ``system_id`` / secondary
                    aliases) from ``name`` (the current/stale key) to
                    ``new_name`` (the chassis's new ``System Name``).
                    No SSH / re-probe — creds and backup history are
                    preserved. By default the old name is retained as a
                    secondary alias (``keep_old_alias=True``) so
                    ``-d <old>`` keeps resolving; pass
                    ``keep_old_alias=False`` to drop it. Rejected when
                    ``new_name`` is already a canonical key or a
                    secondary alias of a different device.
    - ``alias``   – attach a secondary ``alias`` (nickname) to an
                    existing canonical device ``name``. The canonical
                    name (the chassis ``System Name``) stays the
                    primary key; the secondary alias is just an extra
                    name that resolves to the same device, so
                    ``-d <alias>`` reaches the same box as
                    ``-d <name>``. No SSH — purely a local map write.
    - ``unalias`` – detach a secondary ``alias`` from whichever device
                    owns it. Only the nickname is removed; the
                    canonical device is left untouched. Pass the
                    nickname as ``alias=`` (``name=`` is ignored).

    Args:
        operation: One of ``"add"``, ``"remove"``, ``"refresh"``,
            ``"alias"``, ``"unalias"``.
        name: Alias to operate on. NOT accepted for ``add`` (the
            alias is the chassis's System Name, not configurable);
            REQUIRED for ``remove`` / ``refresh``; the current
            (stale) key for ``rename``; the canonical device for
            ``alias``; IGNORED for ``unalias``.
        sn: SSH-reachable hostname for ``add``; host to drop
            (optional) for ``remove``; IGNORED for ``refresh``.
        alias: Secondary nickname to add (``alias``) or remove
            (``unalias``). Ignored for the other operations.
        new_name: The new canonical name for ``rename`` (the chassis's
            new ``System Name``). REQUIRED for ``rename``; ignored
            otherwise.
        keep_old_alias: For ``rename`` only — keep the old ``name`` as
            a secondary alias so it still resolves (default ``True``).
        user: SSH username used for the registration / refresh probe
            and the post-add backup. Default: ``dnroot``.
        password: SSH password. Default: ``dnroot``.
        timeout: Per-command timeout (seconds) for the SSH probe.
    """
    request = {
        "operation": operation, "name": name, "sn": sn,
        "alias": alias, "new_name": new_name,
        "keep_old_alias": keep_old_alias, "user": user,
    }

    def _fail(msg: str, **extra: Any) -> Dict[str, Any]:
        response = make_response(
            status="error",
            device=name or "",
            host=sn or "",
            errors=[msg],
            next_actions=[_MANAGE_DEVICE_FIX],
            operation=operation,
            **extra,
        )
        log_request("manage_device", request, response)
        return response

    if operation == "add":
        if name is not None and isinstance(name, str) and name.strip():
            return _fail(
                "name= is not accepted for operation='add'. The "
                "registry alias is the chassis's configured System "
                "Name; to use a different alias, rename the chassis "
                "on the device (`set system name <new>` + commit) "
                "and then call manage_device(operation='add', "
                f"sn={sn!r})."
            )
        return _add_device(
            sn=sn or "", user=user, password=password,
            timeout=timeout, request=request, fail=_fail,
        )

    if operation == "alias":
        if not name or not isinstance(name, str):
            return _fail(
                "name= (the canonical device) is required for "
                "operation='alias'."
            )
        if not alias or not isinstance(alias, str):
            return _fail(
                "alias= (the secondary nickname) is required for "
                "operation='alias'."
            )
        canonical = _dn_devices.resolve_canonical(name) or name
        try:
            added = _dn_devices.add_alias(alias, canonical)
        except ValueError as exc:
            return _fail(str(exc))
        reload_device_hosts()
        warnings: List[str] = []
        if not added:
            warnings.append(
                f"'{alias}' is already a secondary alias of '{canonical}'."
            )
        response = make_response(
            device=canonical, warnings=warnings,
            operation=operation, alias=alias, added=added,
            aliases=_dn_devices.get_aliases(canonical),
        )
        log_request("manage_device", request, response)
        return response

    if operation == "unalias":
        if not alias or not isinstance(alias, str):
            return _fail(
                "alias= (the secondary nickname to drop) is required "
                "for operation='unalias'."
            )
        try:
            owner = _dn_devices.remove_alias(alias)
        except ValueError as exc:
            return _fail(str(exc))
        reload_device_hosts()
        warnings = []
        if owner is None:
            warnings.append(
                f"'{alias}' is not a secondary alias of any device."
            )
        response = make_response(
            device=owner or "", warnings=warnings,
            operation=operation, alias=alias, removed=owner is not None,
            aliases=_dn_devices.get_aliases(owner) if owner else [],
        )
        log_request("manage_device", request, response)
        return response

    if not name or not isinstance(name, str):
        return _fail("name must be a non-empty string.")

    if operation == "refresh":
        return _refresh_device(
            name=name, user=user, password=password,
            timeout=timeout, request=request, fail=_fail,
        )

    if operation == "rename":
        if not new_name or not isinstance(new_name, str) or not new_name.strip():
            return _fail(
                "new_name= (the new canonical System Name) is required "
                "for operation='rename'."
            )
        if not _dn_devices.get_device_entry(name):
            return _fail(
                f"device '{name}' is not registered; nothing to rename.",
                renamed=False,
            )
        # Operate on the canonical key, not a passed-in secondary alias.
        canonical = _dn_devices.resolve_canonical(name) or name
        try:
            aliases = _dn_devices.rename_device(
                canonical, new_name.strip(),
                keep_old_as_alias=keep_old_alias,
            )
        except ValueError as exc:
            return _fail(str(exc), renamed=False)
        reload_device_hosts()
        final_entry = _dn_devices.get_device_entry(new_name.strip()) or {}
        response = make_response(
            device=new_name.strip(),
            warnings=[],
            operation=operation, renamed=True,
            old_name=canonical, new_name=new_name.strip(),
            kept_old_alias=keep_old_alias,
            aliases=aliases,
            entry=final_entry,
        )
        log_request("manage_device", request, response)
        return response

    if operation == "remove":
        try:
            changed, remaining = remove_device_host(name, sn)
        except Exception as exc:
            return _fail(str(exc), hosts=[], removed=False)
        if not changed:
            detail = (
                f"'{sn}' is not registered under device '{name}'."
                if sn else f"device '{name}' is not registered."
            )
            warnings = [detail]
        else:
            warnings = []
        response = make_response(
            device=name, host=sn or "",
            warnings=warnings,
            operation=operation, hosts=remaining, removed=changed,
        )

    else:
        return _fail(
            f"unknown operation {operation!r} (must be one of "
            f"add/remove/refresh/rename/alias/unalias). To rename a "
            f"device whose chassis System Name changed, use "
            f"operation='rename' with name=<old> new_name=<new>; to "
            f"give a device an extra nickname, use operation='alias'."
        )

    log_request("manage_device", request, response)
    return response


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(list_devices)
    mcp.tool()(manage_device)
