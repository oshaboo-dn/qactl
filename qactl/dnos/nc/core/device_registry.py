"""Device registry helpers — read-only view over the canonical device map.

Single source of truth: ``<repo>/devices/devices_mgmt0.json``. cli-mcp
owns the write side (``manage_device(add)`` SSHes the chassis once and
writes alias / ``expected_role`` / ``expected_sns`` / ``system_id`` /
``mgmt0`` / source metadata in one shot). This module just exposes a
read helper for ``netconf_list_devices``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .session import (
    _load_device_map,
    default_device_map_file,
)


def list_devices(map_file: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Return the canonical device map as ``{alias: entry}``.

    Each entry carries ``expected_role`` / ``expected_sns`` /
    ``system_id`` / ``mgmt0`` and any source-of-record metadata. The
    ``sn_hostnames`` key is populated as an alias of ``expected_sns``
    so older callers that read that field don't have to change.
    """
    mf = map_file or default_device_map_file()
    data = _load_device_map(mf)
    devices: Dict[str, Dict[str, Any]] = {
        k: dict(v) if isinstance(v, dict) else {}
        for k, v in data.get("devices", {}).items()
    }
    for entry in devices.values():
        sns = entry.get("expected_sns")
        entry["sn_hostnames"] = list(sns) if isinstance(sns, list) else []
    return devices
