"""Device-registry MCP tools (``list_devices`` / ``manage_device``).

cli-mcp is the single authority for adding devices to the lab
registry: ``manage_device(add)`` SSHes the chassis once, derives the
alias from the device's configured ``System Name`` (no override —
the registry alias and the chassis name are kept in lockstep on
purpose), captures ``System-Id`` / ``System Type`` (→ ``expected_role``)
plus the mgmt0 IP, auto-discovers the device's physical location
(``rack`` / ``mgmt_switch`` / ``fabric_leaf``) from
``show lldp neighbors``, writes a fully-populated entry to the canonical
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

import re
from typing import Any, Dict, List, Literal, Optional

from qactl.dnctl.core import devices as _dn_devices
from qactl.dnctl.core.credentials import resolve_device_credentials

from qactl.dnctl.cli.core import backup_store
from qactl.dnctl.cli.core.envelope import make_response
from qactl.dnctl.cli.core.logging import log_request
from qactl.dnctl.cli.core.registry import transport_registry
from qactl.dnctl.cli.core.session import (
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

# The vendors the registry understands. ``dnos`` is the default and the
# only one cli-mcp can SSH-probe (`show system` etc.); ``cisco`` /
# ``juniper`` / ``arista`` are registered manually — their CLIs speak a
# different dialect, so we skip the DNOS probe and initial backup and just
# record the operator-supplied vendor + SSH host on the entry.
DEFAULT_VENDOR = "dnos"
SUPPORTED_VENDORS = ("dnos", "cisco", "juniper", "arista")

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _looks_like_ipv4(host: str) -> bool:
    """True when ``host`` is a bare dotted-quad IPv4 literal."""
    return bool(_IPV4_RE.match(host.strip())) if isinstance(host, str) else False

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
        from qactl.dnctl.cli.tools.backup import backup_device
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

    Each entry also surfaces the device's physical location captured at
    add-time: ``rack`` (e.g. ``"B13"``), ``mgmt_switch`` (the mgmt0 LLDP
    neighbor), and ``leaf`` (the sorted unique DNAAS fabric-leaf names it
    homes on). These are ``None`` / ``[]`` for devices registered before
    location capture or added with ``--no-discover`` and no ``--rack``.
    """
    devices = []
    for alias, hosts in sorted(DEVICE_HOSTS.items()):
        entry = _dn_devices.get_device_entry(alias) or {}
        fabric_leaf = entry.get("fabric_leaf") or []
        leaves = sorted(
            {
                e.get("leaf")
                for e in fabric_leaf
                if isinstance(e, dict) and e.get("leaf")
            }
        )
        devices.append(
            {
                "device": alias,
                "hosts": list(hosts),
                "aliases": _dn_devices.get_aliases(alias),
                "vendor": entry.get("vendor") or DEFAULT_VENDOR,
                "rack": entry.get("rack"),
                "mgmt_switch": entry.get("mgmt_switch"),
                "leaf": leaves,
            }
        )
    response = make_response(devices=devices)
    log_request("list_devices", {}, response)
    return response


def _location_fields(
    probe: DeviceProbe,
    rack_override: Optional[str],
    discover: bool,
    warnings: List[str],
) -> Dict[str, Any]:
    """Resolve the rack / mgmt-switch / fabric-leaf fields to persist.

    Combines the manual ``rack_override`` (when given) with whatever LLDP
    auto-discovery surfaced on the probe. The override always wins for the
    ``rack`` field; the discovered ``mgmt_switch`` / ``fabric_leaf`` are
    recorded regardless so the registry entry carries the full physical
    context. Any discovery ambiguity (or a failure to discover at all) is
    appended to ``warnings``. Returns only the fields that have a value,
    so we never clobber an existing entry with ``None``.
    """
    rack_override = (
        rack_override.strip()
        if isinstance(rack_override, str) and rack_override.strip()
        else None
    )
    location = probe.location
    fields: Dict[str, Any] = {}
    if location is not None:
        if location.mgmt_switch:
            fields["mgmt_switch"] = location.mgmt_switch
        if location.fabric_leaf:
            fields["fabric_leaf"] = location.fabric_leaf
        for note in location.warnings:
            warnings.append(f"location discovery: {note}")

    discovered_rack = location.rack if location is not None else None
    rack = rack_override or discovered_rack
    if rack:
        fields["rack"] = rack

    if rack_override and discovered_rack and rack_override != discovered_rack:
        warnings.append(
            f"--rack override {rack_override!r} differs from the "
            f"LLDP-discovered rack {discovered_rack!r}; using the override."
        )
    if discover and not rack:
        warnings.append(
            "could not auto-discover the rack from `show lldp neighbors` "
            "(no mgmt-switch / fabric-leaf neighbor with a recognisable "
            "rack token); pass --rack <name> to set it explicitly."
        )
    return fields


def _add_device(
    sn: str,
    user: str,
    password: str,
    timeout: int,
    request: Dict[str, Any],
    fail: Any,
    key_name: Optional[str] = None,
    rack: Optional[str] = None,
    discover: bool = True,
) -> Dict[str, Any]:
    """Implementation of ``manage_device(operation="add")``.

    Probes the chassis once via SSH (``show system`` +
    ``show interfaces management``), checks for System-Id collisions,
    persists the full entry (``expected_sns`` + ``expected_role`` +
    ``mgmt0`` + ``system_id`` + ``system_name``) to the canonical map,
    and runs the post-add backup. Split out from ``manage_device`` to
    keep the add path linear — every other operation is a one-liner.

    The registry key is the **name the operator chose**, NOT the chassis
    ``System Name``: the key is ``key_name`` when given, otherwise the
    probed SSH host ``sn``. This deliberately decouples the registry from
    the device's configured name so that renaming the box on the chassis
    never orphans or duplicates its registry entry. The chassis
    ``System Name`` is still captured (as the ``system_name`` field, and
    surfaced in a warning when it differs from the key) but it is purely
    informational. ``derived_name_source`` records which rule applied:
    ``"explicit"`` (key differs from the SSH host) or ``"ssh-host"`` (key
    defaulted to the probed ``sn``).

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

    # Pre-stored creds (`setup --device <NAME>`) must work for the
    # registration probe even though the device isn't in the registry
    # yet (#79): key the lookup on the chosen name and the SSH host.
    user, password = resolve_device_credentials(
        key_name, user, password, host=sn,
    )

    try:
        probe: DeviceProbe = probe_device(
            transport_registry, host=sn,
            user=user, password=password, timeout=float(timeout),
            allow_missing_name=True,
            discover_location=discover,
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

    chosen = key_name.strip() if isinstance(key_name, str) and key_name.strip() else ""
    alias = chosen or sn
    derived_name_source = "explicit" if alias != sn else "ssh-host"

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

    warnings: List[str] = []
    fields: Dict[str, Any] = {"expected_sns": hosts_after, "vendor": DEFAULT_VENDOR}
    if probe.expected_role and (
        not isinstance(existing_entry, dict)
        or not existing_entry.get("expected_role")
    ):
        fields["expected_role"] = probe.expected_role
    if probe.mgmt0:
        fields["mgmt0"] = probe.mgmt0
    if probe.system_id:
        fields["system_id"] = probe.system_id
    if probe.system_name:
        fields["system_name"] = probe.system_name
    fields.update(_location_fields(probe, rack, discover, warnings))

    try:
        _dn_devices.update_device(alias, **fields)
    except Exception as exc:  # noqa: BLE001
        return fail(
            f"failed to write canonical map for alias '{alias}': "
            f"{type(exc).__name__}: {exc}",
            hosts=list(existing_hosts), added=False,
        )
    reload_device_hosts()

    if probe.system_name and probe.system_name != alias:
        warnings.append(
            f"registered under the name '{alias}'; the chassis reports "
            f"System Name '{probe.system_name}' (kept as metadata only — "
            f"the registry name is operator-chosen and independent of the "
            f"chassis name)."
        )
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
        vendor=DEFAULT_VENDOR,
        auto_enrolled_ncc_sns=auto_enrolled,
        initial_backup_path=initial_backup_path,
        derived_name=alias,
        derived_name_source=derived_name_source,
        rack=fields.get("rack"),
        entry=final_entry,
    )
    log_request("manage_device", request, response)
    return response


def _add_nondnos_device(
    sn: str,
    vendor: str,
    request: Dict[str, Any],
    fail: Any,
    key_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Register a non-DNOS device (``cisco`` / ``juniper`` / ``arista``) by hand.

    These vendors don't speak the DNOS ``show system`` dialect, so there's
    nothing to SSH-probe for a role / mgmt0 and no DNOS backup to take. We
    simply record the operator-supplied ``vendor`` plus the SSH host on
    the entry. The registry key is ``key_name`` when given, otherwise the
    ``sn`` itself (a hostname like ``jun204-rt01`` makes a fine key; a
    bare IP works too but a friendlier name is nicer). ``mgmt0`` is set to
    ``sn`` when ``sn`` is an IPv4 literal so the transport target is
    populated; otherwise it's left for a later edit.
    """
    if not sn:
        return fail("sn is required for operation='add'.")

    chosen = key_name.strip() if isinstance(key_name, str) and key_name.strip() else ""
    alias = chosen or sn
    derived_name_source = "explicit" if alias != sn else "ssh-host"

    existing_entry = _dn_devices.get_device_entry(alias) or {}
    existing_hosts: List[str] = []
    if isinstance(existing_entry, dict):
        raw = existing_entry.get("expected_sns")
        if isinstance(raw, list):
            existing_hosts = [s for s in raw if isinstance(s, str) and s]

    sn_added = sn not in existing_hosts
    hosts_after = list(existing_hosts)
    if sn_added:
        hosts_after.append(sn)

    fields: Dict[str, Any] = {"vendor": vendor, "expected_sns": hosts_after}
    if _looks_like_ipv4(sn) and not existing_entry.get("mgmt0"):
        fields["mgmt0"] = sn.strip()

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
    if not chosen:
        warnings.append(
            f"registered {vendor} device under the SSH host '{sn}' as the "
            f"name; pass an explicit NAME to register it under a friendlier "
            f"name instead."
        )
    if not sn_added:
        warnings.append(f"'{sn}' was already registered under device '{alias}'.")
    warnings.append(
        f"vendor={vendor!r}: cli-mcp's DNOS tools (show/configure/backup) "
        f"do not target this device; it was recorded for inventory only."
    )

    final_entry = _dn_devices.get_device_entry(alias) or {}
    response = make_response(
        device=alias, host=sn,
        warnings=warnings,
        operation="add", hosts=hosts_after, added=sn_added,
        vendor=vendor,
        derived_name=alias,
        derived_name_source=derived_name_source,
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
    # The probes below go out host-only (by SN), so resolve the device's
    # per-device creds against its registry name up front (#79).
    user, password = resolve_device_credentials(name, user, password)
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


def _name_check_device(
    name: str,
    user: str,
    password: str,
    timeout: int,
    request: Dict[str, Any],
    fail: Any,
    do_sync: bool = False,
    keep_old_alias: bool = True,
) -> Dict[str, Any]:
    """Implementation of ``manage_device(operation="name-check")``.

    SSH-probes an existing device for its current chassis ``System Name``
    and compares it to the registry key:

    - read-only by default: reports ``in_sync`` plus the two names and,
      on drift, a hint to rerun with ``do_sync=True``;
    - with ``do_sync=True`` it adopts the chassis name by renaming the
      registry key in place (old name kept as a secondary alias unless
      ``keep_old_alias=False``), so the operator can opt into lockstep on
      demand without it being forced at ``add`` time.

    The registry name stays operator-chosen by default — this is the
    explicit "check / request sync" knob, not an automatic rename.
    """
    entry = _dn_devices.get_device_entry(name) or {}
    if not entry:
        return fail(
            f"device '{name}' is not registered. Add it first with "
            f"operation='add'.",
            in_sync=False, synced=False,
        )
    canonical = _dn_devices.resolve_canonical(name) or name
    # Same host-only probe as refresh: resolve per-device creds by name.
    user, password = resolve_device_credentials(canonical, user, password)
    raw_sns = entry.get("expected_sns") if isinstance(entry, dict) else None
    sns: List[str] = (
        [s for s in raw_sns if isinstance(s, str) and s]
        if isinstance(raw_sns, list) else []
    )
    if not sns:
        return fail(
            f"device '{canonical}' has no expected_sns to probe; can't "
            f"read its chassis System Name.",
            in_sync=False, synced=False, hosts=[],
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
            f"name-check of '{canonical}' failed: could not SSH-probe any "
            f"of {sns}. {joined}",
            in_sync=False, synced=False, hosts=list(sns),
        )

    chassis_name = probe.system_name
    warnings: List[str] = []
    if failures:
        warnings.append(
            "earlier SN candidates were unreachable: " + "; ".join(failures)
        )

    in_sync = bool(chassis_name) and chassis_name == canonical
    synced = False
    new_canonical = canonical
    status = "ok"

    if not chassis_name:
        status = "warning"
        warnings.append(
            "chassis exposed no 'System Name:' — cannot compare; the "
            "registry name is left unchanged."
        )
    elif in_sync:
        warnings.append(
            f"registry name '{canonical}' already matches the chassis "
            f"System Name."
        )
    elif do_sync:
        try:
            _dn_devices.rename_device(
                canonical, chassis_name, keep_old_as_alias=keep_old_alias,
            )
        except ValueError as exc:
            return fail(
                f"sync of '{canonical}' -> '{chassis_name}' failed: {exc}",
                in_sync=False, synced=False,
                registry_name=canonical, chassis_system_name=chassis_name,
            )
        new_canonical = chassis_name
        synced = True
        reload_device_hosts()
        warnings.append(
            f"renamed registry key '{canonical}' -> '{chassis_name}' to "
            f"match the chassis (old name kept as alias: {keep_old_alias})."
        )
    else:
        status = "warning"
        warnings.append(
            f"registry name '{canonical}' differs from chassis System Name "
            f"'{chassis_name}'; rerun with --sync to adopt it "
            f"(qactl cli device name-check {canonical} --sync -y)."
        )

    # Keep the stored system_name metadata fresh on the (possibly renamed)
    # entry whenever we learned a real name from the chassis.
    if chassis_name:
        try:
            _dn_devices.update_device(new_canonical, system_name=chassis_name)
        except Exception:  # noqa: BLE001 - metadata refresh is best-effort
            pass

    final_entry = _dn_devices.get_device_entry(new_canonical) or {}
    response = make_response(
        status=status,
        device=new_canonical, host=sn_used,
        warnings=warnings,
        operation="name-check",
        in_sync=in_sync, synced=synced,
        registry_name=canonical, chassis_system_name=chassis_name,
        aliases=_dn_devices.get_aliases(new_canonical),
        entry=final_entry,
    )
    log_request("manage_device", request, response)
    return response


def manage_device(
    operation: Literal[
        "add", "remove", "refresh", "rename", "alias", "unalias", "name-check"
    ],
    name: Optional[str] = None,
    sn: Optional[str] = None,
    alias: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    new_name: Optional[str] = None,
    keep_old_alias: bool = True,
    sync: bool = False,
    rack: Optional[str] = None,
    discover: bool = True,
    vendor: str = DEFAULT_VENDOR,
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

    The registry key for any device is the **name the operator chooses**
    (``name=``), independent of the chassis's configured ``System Name``.
    This is deliberate: renaming the box on the chassis must never orphan
    or duplicate its registry entry. When ``name=`` is omitted the key
    defaults to the probed SSH host (``sn``). The chassis ``System Name``
    is still captured (the ``system_name`` field) for reference only.
    ``derived_name_source`` on the response records which rule applied
    (``"explicit"`` when the key differs from the SSH host, ``"ssh-host"``
    when it defaulted to ``sn``). Secondary nicknames can be attached at
    add-time via ``aliases=[...]`` or later with ``operation="alias"``;
    ``operation="rename"`` still moves a canonical key in place.

    Operations:

    - ``add``    – register ``sn`` (an SSH-reachable hostname / SN / IP)
                   in the device registry. cli-mcp opens an SSH session
                   to ``sn`` once and captures everything every other
                   MCP needs from a single pass:

                   1. ``name=``        → the registry key (operator's
                                         choice). Defaults to the SSH host
                                         ``sn`` when omitted. The chassis
                                         ``System Name`` is recorded as
                                         metadata only, never the key.
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
                   5. ``show lldp neighbors`` → physical location:
                                         ``rack`` (decoded from the mgmt
                                         switch / fabric leaf names),
                                         ``mgmt_switch``, and ``fabric_leaf``
                                         (leaf + port per data link). Best
                                         effort and skipped when
                                         ``discover=False``; ``rack=<name>``
                                         overrides the auto-discovered rack.

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
                   ``remove`` / ``refresh``; to set the alias
                   explicitly use ``alias=``.

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
    - ``name-check`` – SSH-probe ``name`` for its current chassis
                    ``System Name`` and compare it to the registry key.
                    Read-only by default (reports ``in_sync`` plus both
                    names); pass ``sync=True`` to adopt the chassis name
                    by renaming the key in place (old name kept as a
                    secondary alias unless ``keep_old_alias=False``).
                    This is the on-demand "is my registry name still
                    right?" / "make it right" knob — naming stays
                    operator-chosen otherwise.

    Args:
        operation: One of ``"add"``, ``"remove"``, ``"refresh"``,
            ``"alias"``, ``"unalias"``.
        name: For ``add``, the registry key the operator chooses (the
            device name). Defaults to ``sn`` when omitted. REQUIRED for
            ``remove`` / ``refresh``; the current (stale) key for
            ``rename``; the canonical device for ``alias``; IGNORED for
            ``unalias``.
        sn: SSH-reachable host to probe for ``add`` (defaults to
            ``name`` when omitted); host to drop (optional) for
            ``remove``; IGNORED for ``refresh``.
        alias: For ``add``, a legacy synonym for an explicit ``name``.
            For ``alias`` / ``unalias``, the secondary nickname to add or
            remove. Ignored for the other operations.
        aliases: For ``add`` only — secondary nickname(s) to attach to
            the new entry in the same call (e.g. ``["cl"]``).
        new_name: The new canonical name for ``rename`` (the chassis's
            new ``System Name``). REQUIRED for ``rename``; ignored
            otherwise.
        keep_old_alias: For ``rename`` and ``name-check`` (with
            ``sync=True``) — keep the old ``name`` as a secondary alias
            so it still resolves (default ``True``).
        sync: For ``name-check`` only — when ``True``, adopt the chassis
            System Name by renaming the registry key (default ``False``,
            i.e. report-only).
        rack: For ``add`` only — manual rack override (e.g. ``"B13"``).
            When set it wins over LLDP auto-discovery for the stored
            ``rack`` field; the discovered ``mgmt_switch`` / ``fabric_leaf``
            are still recorded. Ignored for the other operations.
        discover: For ``add`` only — auto-discover the device's physical
            location (rack / mgmt switch / fabric leaves) by reading
            ``show lldp neighbors`` during the registration probe
            (default ``True``). Set ``False`` to skip the LLDP step.
        vendor: For ``add`` only — the device vendor, one of ``"dnos"``
            (default), ``"cisco"``, ``"juniper"``, ``"arista"``. ``dnos``
            runs the full SSH probe (System Name → alias, role, mgmt0, LLDP
            location) and initial backup. ``cisco`` / ``juniper`` /
            ``arista`` skip all of that (their CLIs aren't DNOS): no probe,
            no backup — the alias is ``--alias`` or the ``sn``, and only
            the vendor + SSH host are recorded. Ignored for the other
            operations.
        user: SSH username used for the registration / refresh probe
            and the post-add backup. Default: ``dnroot``.
        password: SSH password. Default: ``dnroot``.
        timeout: Per-command timeout (seconds) for the SSH probe.
    """
    request = {
        "operation": operation, "name": name, "sn": sn,
        "alias": alias, "new_name": new_name,
        "keep_old_alias": keep_old_alias, "sync": sync, "rack": rack,
        "discover": discover, "vendor": vendor, "user": user,
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
        vendor_norm = (vendor or DEFAULT_VENDOR).strip().lower()
        if vendor_norm not in SUPPORTED_VENDORS:
            return _fail(
                f"vendor={vendor!r} is not supported; choose one of "
                f"{', '.join(SUPPORTED_VENDORS)} (default {DEFAULT_VENDOR})."
            )
        # The registry key is the operator-chosen ``name`` (independent of
        # the chassis System Name); the SSH probe target is ``sn`` when
        # given, else it doubles as the name. ``alias`` (singular) is still
        # accepted as a legacy synonym for the explicit name.
        key_name = (name or alias or "").strip() or None
        ssh_host = (sn or name or "").strip()
        if vendor_norm != DEFAULT_VENDOR:
            resp = _add_nondnos_device(
                sn=ssh_host, vendor=vendor_norm, request=request,
                fail=_fail, key_name=key_name,
            )
        else:
            resp = _add_device(
                sn=ssh_host, user=user, password=password,
                timeout=timeout, request=request, fail=_fail,
                key_name=key_name, rack=rack, discover=discover,
            )
        if isinstance(resp, dict) and resp.get("status") == "ok" and aliases:
            attached: List[str] = []
            for nick in aliases:
                nick = (nick or "").strip()
                if not nick:
                    continue
                try:
                    if _dn_devices.add_alias(nick, resp.get("device") or ""):
                        attached.append(nick)
                except ValueError as exc:
                    resp.setdefault("warnings", []).append(
                        f"secondary alias '{nick}' not attached: {exc}"
                    )
            if attached:
                reload_device_hosts()
                resp["aliases"] = _dn_devices.get_aliases(resp.get("device") or "")
                resp.setdefault("warnings", []).append(
                    f"attached secondary alias(es): {attached}"
                )
        return resp

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

    if operation == "name-check":
        return _name_check_device(
            name=name, user=user, password=password,
            timeout=timeout, request=request, fail=_fail,
            do_sync=sync, keep_old_alias=keep_old_alias,
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
            f"add/remove/refresh/rename/alias/unalias/name-check). To "
            f"check (and optionally adopt) the chassis System Name, use "
            f"operation='name-check' with sync=True; to rename a device "
            f"key by hand, use operation='rename'."
        )

    log_request("manage_device", request, response)
    return response


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(list_devices)
    mcp.tool()(manage_device)
