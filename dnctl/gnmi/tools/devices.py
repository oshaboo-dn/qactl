"""Device-registry tools — read the shared device map.

The map lives at ``<repo-root>/devices/devices_mgmt0.json`` and is
owned/written by ``netconf-mcp``'s ``netconf_add_device`` tool (which
SSH-resolves mgmt0, verifies SNs, and writes atomically). This MCP only
reads — duplicating the writer logic would create two writers for the
same file.
"""

from __future__ import annotations

from typing import Any, Dict

from dnctl.core import devices as _devices

from dnctl.gnmi.core.envelope import make_envelope


def gnmi_list_devices() -> Dict[str, Any]:
    """List devices known to the shared device map.

    Read-only. No device traffic. Each entry surfaces ``mgmt0``,
    ``expected_role`` (``"SA"``/``"CL"``), and ``expected_sns``.
    """
    env = make_envelope(kind="list_devices")
    try:
        data = _devices.load_device_map()
        env["result"] = {
            "map_file": _devices.default_device_map_path(),
            "generated_at": data.get("generated_at"),
            "devices": data.get("devices", {}),
        }
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {e}")
        return env


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(gnmi_list_devices)
