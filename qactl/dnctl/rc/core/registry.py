"""Endpoint + device registry loaders.

Two JSON files feed this MCP:

* ``restconf_endpoints.json`` (next to this package) — RESTCONF speakers
  (ODL controllers today; native DNOS RESTCONF when/if it ships). Each
  entry stores ``base_url``, controller credentials, ``uri_style`` and
  the per-device mounts that have been created on that controller. This
  is RESTCONF-specific so it stays per-MCP.

* The shared DNOS device map at ``<repo-root>/devices/devices_mgmt0.json``,
  owned by ``dnctl.core.devices``. The same alias the agent passes to
  ``netconf-mcp`` / ``gnmi-mcp`` works here (``cl``, ``sa``, ``kira``,
  ``ariel-cl``, ``slava-1``, ``slava-2``).

Resolving an alias for a RESTCONF call walks both: find which endpoint
has a mount whose ``device`` matches the alias, then layer the DNOS
metadata from ``dnctl.core.devices``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from qactl.dnctl.core import devices as _devices


from qactl.dnctl.core import paths as _paths

_PKG_ROOT = Path(__file__).resolve().parent.parent
_ENDPOINTS_PATH = _paths.state_dir("rc") / "restconf_endpoints.json"


def load_endpoints() -> Dict[str, Any]:
    if not _ENDPOINTS_PATH.exists():
        # Seed from the bundled default on first use so the known ODL
        # controllers / mounts are available out of the box.
        seed = _paths.DATA_DIR / "restconf_endpoints.json"
        if seed.exists():
            try:
                _ENDPOINTS_PATH.parent.mkdir(parents=True, exist_ok=True)
                _ENDPOINTS_PATH.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                return json.loads(seed.read_text(encoding="utf-8"))
        else:
            return {"endpoints": {}}
    with _ENDPOINTS_PATH.open() as f:
        return json.load(f)


def save_endpoints(doc: Dict[str, Any]) -> None:
    """Persist the endpoints registry. Keeps the file small + sorted."""
    _ENDPOINTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _ENDPOINTS_PATH.open("w") as f:
        json.dump(doc, f, indent=2, sort_keys=False)
        f.write("\n")


def load_devices() -> Dict[str, Any]:
    """Pass-through to ``dnctl.core.devices.load_device_map``."""
    return _devices.load_device_map()


def get_endpoint(endpoint_alias: str) -> Optional[Dict[str, Any]]:
    return load_endpoints().get("endpoints", {}).get(endpoint_alias)


def get_device(device_alias: str) -> Optional[Dict[str, Any]]:
    return _devices.get_device_entry(device_alias)


def canonical_device(device_alias: Optional[str]) -> Optional[str]:
    """Best-effort canonical name for a device alias.

    Mirrors the canonicalisation the CLI front-end applies to ``-d`` so a
    mount registered under the canonical name (or a secondary alias)
    resolves regardless of which name the caller passes. Unknown names
    pass through untouched.
    """
    if not device_alias:
        return device_alias
    return _devices.resolve_canonical(device_alias) or device_alias


def find_mount(device_alias: str) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """Locate which RESTCONF endpoint has a mount for ``device_alias``.

    Both the requested alias and each mount's stored ``device`` are
    canonicalised before comparison, so an alias and the canonical name
    match the same mount. Returns ``(endpoint_alias, mount_name,
    mount_cfg)`` or all ``None``.
    """
    target = canonical_device(device_alias)
    endpoints = load_endpoints().get("endpoints", {})
    for ep_alias, ep_cfg in endpoints.items():
        for mount_name, mcfg in (ep_cfg.get("mounts") or {}).items():
            if canonical_device(mcfg.get("device")) == target:
                return ep_alias, mount_name, mcfg
    return None, None, None
