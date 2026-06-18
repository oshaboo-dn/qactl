"""Device-registry MCP tools — ``netconf_list_devices`` (read-only).

Adding / removing / renaming devices is owned by **cli-mcp** —
specifically its ``manage_device`` tool, which SSHes the chassis once
to capture System Name / role / mgmt0 and writes the canonical
``<repo>/devices/devices_mgmt0.json`` map. netconf-mcp consumes that
map read-only via ``dnctl.core.devices``; this module exposes a
listing tool so agents can see what's available without bouncing
over to cli-mcp.
"""

from __future__ import annotations

from typing import Any, Dict

from dnctl.nc.core.device_registry import list_devices as _registry_list_devices
from dnctl.nc.core.results import _error_result
from dnctl.nc.core.session import _session_id


def netconf_list_devices() -> Dict[str, Any]:
    """List registered devices.

    Returns the canonical device map (``<repo>/devices/devices_mgmt0.json``):
    mgmt0 IP, ``expected_role`` / ``expected_sns``, ``system_id``, and any
    source-of-record metadata. SN hostnames live under ``expected_sns`` and
    are mirrored into the legacy ``sn_hostnames`` key for older clients.

    To **register** a new device call cli-mcp's ``manage_device`` —
    netconf-mcp does not have its own ``add_device`` tool by design,
    so the lab registry has a single owner.
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
