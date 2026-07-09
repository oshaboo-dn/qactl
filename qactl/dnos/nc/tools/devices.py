"""Device-registry tool — ``netconf_list_devices`` (read-only).

Adding / removing / renaming devices is owned by the ``cli`` group —
specifically ``qactl cli device add/remove/...``, which SSHes the
chassis once to capture System Name / role / mgmt0 and writes the
canonical ``<repo>/devices/devices_mgmt0.json`` map. The ``nc`` group
consumes that map read-only via ``qactl.core.devices``; this module
exposes a listing command so you can see what's available without
switching to ``cli``.
"""

from __future__ import annotations

from typing import Any, Dict

from qactl.dnos.nc.core.device_registry import list_devices as _registry_list_devices
from qactl.dnos.nc.core.results import _error_result
from qactl.dnos.nc.core.session import _session_id


def netconf_list_devices() -> Dict[str, Any]:
    """List registered devices.

    Returns the canonical device map (``<repo>/devices/devices_mgmt0.json``):
    mgmt0 IP, ``expected_role`` / ``expected_sns``, ``system_id``, and any
    source-of-record metadata. SN hostnames live under ``expected_sns`` and
    are mirrored into the legacy ``sn_hostnames`` key for older clients.

    To **register** a new device run ``qactl cli device add`` — the
    ``nc`` group has no ``add`` command by design, so the lab registry
    has a single owner.
    """
    sid = _session_id()
    try:
        devices = _registry_list_devices()
        return {
            "action": "list_devices",
            "status": "ok",
            "count": len(devices),
            "devices": devices,
        }
    except Exception as e:
        return _error_result("list-devices", sid, e)


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(netconf_list_devices)
