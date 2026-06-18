"""Endpoint + device registry tools.

* ``restconf_list_endpoints()`` — show all configured RESTCONF speakers
  (ODL controllers today; native DNOS endpoints in the future). Read-only.
* ``restconf_list_devices()`` — show DNOS devices known to the family of
  MCPs (``netconf-mcp`` / ``gnmi-mcp`` / ``restconf-mcp`` share
  ``devices_mgmt0.json``), with their current RESTCONF mount status.
* ``restconf_resolve(device)`` — return which endpoint/mount serves a
  given DNOS alias, ready to feed into ``restconf_get``.
"""

from __future__ import annotations

from typing import Any, Dict

from dnctl.rc.core.envelope import error_envelope, make_envelope
from dnctl.rc.core.registry import (
    find_mount,
    get_device,
    load_devices,
    load_endpoints,
)


def restconf_list_endpoints() -> Dict[str, Any]:
    """List configured RESTCONF speakers and their mounts."""
    env = make_envelope(kind="list_endpoints")
    eps = load_endpoints().get("endpoints", {})
    out = {}
    for alias, cfg in eps.items():
        out[alias] = {
            "kind": cfg.get("kind"),
            "base_url": cfg.get("base_url"),
            "uri_style": cfg.get("uri_style"),
            "mounts": list((cfg.get("mounts") or {}).keys()),
        }
    env["result"] = {"count": len(out), "endpoints": out}
    return env


def restconf_list_devices() -> Dict[str, Any]:
    """List DNOS devices and the RESTCONF mount that exposes each one."""
    env = make_envelope(kind="list_devices")
    devs = load_devices().get("devices", {})
    out = {}
    for alias, dcfg in devs.items():
        ep_alias, mount_name, mcfg = find_mount(alias)
        out[alias] = {
            "mgmt0": dcfg.get("mgmt0"),
            "expected_role": dcfg.get("expected_role"),
            "expected_sns": dcfg.get("expected_sns", []),
            "restconf_endpoint": ep_alias,
            "restconf_mount_name": mount_name,
            "mounted": bool(mcfg),
        }
    env["result"] = {"count": len(out), "devices": out}
    return env


def restconf_resolve(device: str) -> Dict[str, Any]:
    """Return endpoint+mount config that should be used for a DNOS alias."""
    dev = get_device(device)
    if not dev:
        return error_envelope(
            f"unknown device alias '{device}' (see devices_mgmt0.json)",
            kind="resolve", device=device,
        )
    ep_alias, mount_name, mcfg = find_mount(device)
    env = make_envelope(kind="resolve", device=device, endpoint=ep_alias,
                        request={"device": device})
    if not mcfg:
        env["status"] = "error"
        env["errors"].append(
            f"no RESTCONF mount registered for '{device}'. "
            f"Create one with 'qactl rc mount add' first."
        )
        env["next_actions"].append(
            f"qactl rc mount add {device} --endpoint odl-lab1 --yes"
        )
        return env
    env["result"] = {
        "endpoint": ep_alias,
        "mount_name": mount_name,
        "mount": mcfg,
    }
    return env


def register(mcp) -> None:
    mcp.tool()(restconf_list_endpoints)
    mcp.tool()(restconf_list_devices)
    mcp.tool()(restconf_resolve)
