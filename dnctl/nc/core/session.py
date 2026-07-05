"""Session: connection, device map, session IDs, and operation-file resolution."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from lxml import etree
from ncclient import manager

from dnctl.core import credentials as _creds
from dnctl.core import devices as _devices

from .netconf_rpc import get_serial_numbers
from .xml_payload import extract_payload_for_edit


from dnctl.core import paths as _paths

ROOT_DIR = _paths.state_dir("nc")
OPERATIONS_DIR = ROOT_DIR / "operations"
IL_TZ = ZoneInfo("Asia/Jerusalem")

DEFAULT_USER = _creds.DEFAULT_USER
DEFAULT_PASSWORD = _creds.DEFAULT_PASSWORD
DEFAULT_PORT = 830
DEFAULT_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Session ID / time
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(IL_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def _session_id() -> str:
    return f"{_utc_now()}-{os.getpid()}"


# ---------------------------------------------------------------------------
# Operation file resolution + payload loading
# ---------------------------------------------------------------------------


def _resolve_operation_file(path: str, category: Optional[str] = None) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        if path.startswith("operations/"):
            candidate = ROOT_DIR / candidate
        elif category:
            candidate = OPERATIONS_DIR / category / candidate
        else:
            candidate = ROOT_DIR / candidate
    resolved = candidate.resolve()
    ops_root = OPERATIONS_DIR.resolve()
    if ops_root not in resolved.parents:
        raise ValueError(f"Operation file must be under {ops_root}")
    if not resolved.exists():
        old_candidate = OPERATIONS_DIR / "old" / (category or "") / Path(path).name
        if old_candidate.exists():
            return old_candidate.resolve()
        raise FileNotFoundError(f"Operation file not found: {resolved}")
    return resolved


def _load_payload(file_path: Path) -> str:
    with Path(file_path).open("r", encoding="utf-8") as f:
        return extract_payload_for_edit(f.read().strip())


def _extract_rpc_command(rpc_xml: str) -> etree._Element:
    """Extract the inner command element from an <rpc> wrapper."""
    root = etree.fromstring(rpc_xml.encode("utf-8"))
    if root.tag == "rpc" or root.tag.endswith("}rpc"):
        children = list(root)
        if not children:
            raise ValueError("Empty <rpc> element — no command found")
        return children[0]
    return root


# ---------------------------------------------------------------------------
# Device map (device -> mgmt0 mapping) and SN recovery
# ---------------------------------------------------------------------------


def default_device_map_file() -> str:
    """Return default canonical device->mgmt0 mapping file path.

    Thin wrapper around :func:`dnctl.core.devices.default_device_map_path`
    kept for back-compat with any caller still importing this name.
    """
    return _devices.default_device_map_path()


def resolve_device_host_from_map(device: str, map_file: str) -> Optional[str]:
    """Resolve NETCONF host for a device from the canonical device map."""
    return _devices.resolve_mgmt0(device, map_file)


def _load_device_map(map_file: str) -> dict:
    """Load the full device map JSON via dnctl.core."""
    return _devices.load_device_map(map_file)


_VALID_ROLES = ("SA", "CL")


def _get_expected_role(device: str, map_file: str) -> Optional[str]:
    """Read expected_role ("SA"|"CL") for a device from the map file.

    Returns None when the field is missing or invalid — callers treat this as
    "device not eligible to connect" since role is required.
    """
    data = _load_device_map(map_file)
    entry = data.get("devices", {}).get(device)
    if isinstance(entry, dict):
        role = entry.get("expected_role")
        if isinstance(role, str):
            normalized = role.strip().upper()
            if normalized in _VALID_ROLES:
                return normalized
    return None


def _get_expected_sns(device: str, map_file: str) -> List[str]:
    """Read expected_sns (list) for a device from the map file.

    Falls back to the legacy expected_sn (string) field for backward compatibility.
    """
    data = _load_device_map(map_file)
    entry = data.get("devices", {}).get(device)
    if isinstance(entry, dict):
        sns = entry.get("expected_sns")
        if isinstance(sns, list):
            return [s.strip() for s in sns if isinstance(s, str) and s.strip()]
        sn = entry.get("expected_sn")
        if isinstance(sn, str) and sn.strip():
            return [sn.strip()]
    return []


def _update_device_map_entry(map_file: str, device: str, **fields: object) -> None:
    """Update fields for a device in the map file (delegated to dnctl.core)."""
    _devices.update_device(device, map_file, **fields)


class StaleMgmt0Error(RuntimeError):
    """Raised when the cached mgmt0 / SN no longer fits the live chassis.

    The ``nc`` group does not SSH the device itself — the ``cli`` group
    owns every CLI interaction. When the cached mgmt0 stops responding
    (TCP / NETCONF timeout) or the chassis at that IP reports a SN that
    no longer matches ``expected_sns``, callers must run
    ``dnctl cli device refresh <alias>`` so the registry can be
    re-probed via the CLI transport pool, then retry the original
    NETCONF call.

    The message is shaped to be useful as the error envelope's
    ``errors[0]`` and a hint emitted in ``next_actions``.
    """


def _stale_mgmt0_message(device: str, host: str, detail: str) -> str:
    return (
        f"NETCONF connect to device='{device}' at cached mgmt0={host!r} "
        f"failed: {detail}. The nc group does not re-probe devices; "
        f"run `dnctl cli device refresh {device}` to re-discover mgmt0 / "
        f"system_id / expected_role from the chassis, then retry."
    )


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


@dataclass
class ConnectResult:
    """NETCONF session with resolved connection metadata, usable as a context manager."""
    mgr: manager.Manager
    host: str
    port: int
    user: str
    device: Optional[str] = field(default=None, repr=False)
    sn_verified: bool = field(default=False, repr=False)
    serial_numbers: List[str] = field(default_factory=list, repr=False)
    role: Optional[str] = field(default=None, repr=False)
    mgmt0_verified: bool = field(default=False, repr=False)
    mgmt0_warnings: List[str] = field(default_factory=list, repr=False)

    def __enter__(self):
        self.mgr.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self.mgr.__exit__(*exc_info)


class _SnMismatchError(Exception):
    """Raised internally when connected device SN doesn't match expected."""


def _raw_connect(
    host: str,
    port: int,
    user: str,
    password: str,
    hostkey_verify: bool,
    timeout: int,
) -> manager.Manager:
    """Low-level NETCONF connect with the single lab account. Returns the manager."""
    key = _creds.SSH_KEY
    return manager.connect(
        host=host, port=port,
        username=user, password=password,
        key_filename=key,
        allow_agent=bool(key), look_for_keys=bool(key),
        hostkey_verify=hostkey_verify,
        device_params={"name": "default"},
        timeout=timeout,
    )


def connect(
    host: Optional[str] = None,
    device: Optional[str] = None,
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    hostkey_verify: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    device_map_file: Optional[str] = None,
    verify_mgmt0: bool = True,
) -> ConnectResult:
    """Open a NETCONF session with automatic host resolution and SN verification.

    When device= is used:
      1. Read expected_role from the canonical map (required) — picks the
         right SN subtree (NCC for CL, NCP for SA).
      2. Verify the cached mgmt0 against the chassis's live mgmt0 via the
         cli group (``show interfaces management`` over the expected_sns
         SSH hosts — issue #71). On mismatch the map is refreshed and the
         live address is used; when the chassis can't be probed we proceed
         with the cached address carrying an UNVERIFIED warning.
      3. Connect to the (verified) mgmt0 IP.
      4. Verify serial number matches expected_sns from the map.
      5. On TCP / NETCONF failure or SN mismatch: raise
         :class:`StaleMgmt0Error` pointing the caller at
         ``dnctl cli device refresh <alias>`` — the nc group never SSHes
         the chassis itself; the cli group owns the wire (the mgmt0
         pre-check above is delegated to it, not done here).

    When host= is used directly: no SN verification (caller knows the target).
    """
    map_file = device_map_file or default_device_map_file()
    actual_user = user if user is not None else DEFAULT_USER
    actual_pass = password if password is not None else DEFAULT_PASSWORD

    if host:
        mgr = _raw_connect(
            host, port, actual_user, actual_pass, hostkey_verify, timeout,
        )
        return ConnectResult(
            mgr=mgr, host=host, port=port, user=actual_user,
        )

    if not device:
        raise ValueError("Provide host or device")

    expected_role = _get_expected_role(device, map_file)
    if expected_role is None:
        raise RuntimeError(
            f"Device '{device}' has no 'expected_role' in {map_file}. "
            f"Set it to one of {_VALID_ROLES} via `dnctl cli device add` "
            f"before opening a session."
        )

    resolved_host = resolve_device_host_from_map(device, map_file)

    # Issue #71: ask the chassis itself for its CURRENT mgmt0 (via the cli
    # group's transport pool) before trusting the cached address — a stale
    # cached IP can point at a different box that still answers NETCONF.
    mgmt0_verified = False
    mgmt0_warnings: List[str] = []
    if verify_mgmt0:
        try:
            from dnctl.cli.core.mgmt0_verify import verify_device_mgmt0
            verification = verify_device_mgmt0(device, map_file=map_file)
            mgmt0_verified = verification.verified
            mgmt0_warnings = list(verification.warnings)
            if verification.address:
                resolved_host = verification.address
        except Exception as exc:  # noqa: BLE001 - verification is best-effort
            mgmt0_warnings.append(
                f"mgmt0 CLI pre-verification errored "
                f"({type(exc).__name__}: {exc}); proceeding with cached "
                f"mgmt0={resolved_host!r} UNVERIFIED."
            )
    else:
        mgmt0_warnings.append(
            "mgmt0 pre-verification skipped by --no-verify-mgmt0; "
            "using the cached address as-is."
        )

    expected_sns = _get_expected_sns(device, map_file)

    def _try_connect(target_host: str) -> ConnectResult:
        mgr = _raw_connect(
            target_host, port, actual_user, actual_pass, hostkey_verify, timeout,
        )

        actual_sns = get_serial_numbers(mgr, role=expected_role)

        if expected_sns and actual_sns and not (set(actual_sns) & set(expected_sns)):
            try:
                mgr.close_session()
            except Exception:
                pass
            raise _SnMismatchError(
                f"Device '{device}' at {target_host} (expected role {expected_role}): "
                f"expected SNs {expected_sns}, got {actual_sns}"
            )

        sn_verified = bool(expected_sns and actual_sns and (set(actual_sns) & set(expected_sns)))
        if actual_sns and not expected_sns:
            _update_device_map_entry(map_file, device, expected_sns=actual_sns)

        return ConnectResult(
            mgr=mgr, host=target_host, port=port, user=actual_user,
            device=device,
            sn_verified=sn_verified, serial_numbers=actual_sns,
            role=expected_role,
            mgmt0_verified=mgmt0_verified, mgmt0_warnings=mgmt0_warnings,
        )

    if not resolved_host:
        raise StaleMgmt0Error(
            f"device='{device}' has no cached mgmt0 in the canonical map. "
            f"Run `dnctl cli device add {device} --sn <ssh-host>` to "
            f"register it (the cli group will SSH-probe the chassis and "
            f"populate mgmt0 / system_id / expected_role / expected_sns), "
            f"then retry."
        )
    try:
        return _try_connect(resolved_host)
    except _SnMismatchError as exc:
        raise StaleMgmt0Error(_stale_mgmt0_message(
            device, resolved_host,
            f"chassis at this IP reports a different SN ({exc})",
        )) from exc
    except Exception as exc:  # noqa: BLE001 - any TCP / NETCONF failure
        raise StaleMgmt0Error(_stale_mgmt0_message(
            device, resolved_host, f"{type(exc).__name__}: {exc}",
        )) from exc


def _connect_device(
    host: Optional[str],
    device: Optional[str],
    port: int,
    user: Optional[str],
    password: Optional[str],
    no_verify: bool,
    timeout: int,
    verify_mgmt0: bool = True,
) -> ConnectResult:
    """Thin wrapper around connect() — translates MCP's no_verify convention."""
    return connect(
        host=host,
        device=device,
        port=port,
        user=user,
        password=password,
        hostkey_verify=not no_verify,
        timeout=timeout,
        verify_mgmt0=verify_mgmt0,
    )
