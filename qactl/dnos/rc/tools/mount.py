"""Mount-management tools — wraps :mod:`qactl.rc.core.odl_mount`.

Workflow this MCP enforces:

1. ``restconf_mount_add(device, endpoint, mount_name=None)`` — pick a name
   (default = device alias upper-cased), look up DNOS mgmt0 + creds in
   ``devices_mgmt0.json``, ``PUT`` the mount config to the controller,
   poll until ``connection-status: connected``, persist the entry into
   ``restconf_endpoints.json``.
2. ``restconf_mount_status(device | mount_name, endpoint)`` — return live
   ``connection-status`` + capability counts.
3. ``restconf_mount_remove(mount_name, endpoint)`` — DELETE the
   controller config + drop from local registry.

Native (non-ODL) RESTCONF endpoints don't need mounts; calling these
tools against ``kind != "odl"`` is rejected up front.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from qactl.dnos.rc.core.envelope import error_envelope, make_envelope
from qactl.dnos.rc.core.odl_mount import (
    delete_mount,
    get_node_status,
    put_mount,
    wait_until_connected,
)
from qactl.dnos.rc.core.registry import (
    canonical_device,
    get_device,
    get_endpoint,
    load_endpoints,
    save_endpoints,
)


_DEFAULT_DEVICE_USER = "dnroot"
_DEFAULT_DEVICE_PASSWORD = "dnroot"
_DEFAULT_NETCONF_PORT = 830


def _require_odl_endpoint(endpoint: str) -> Optional[Dict[str, Any]]:
    ep = get_endpoint(endpoint)
    if not ep:
        return None
    if ep.get("kind") != "odl":
        return None
    return ep


def restconf_mount_add(
    device: str,
    endpoint: str = "odl-lab1",
    mount_name: Optional[str] = None,
    netconf_port: int = _DEFAULT_NETCONF_PORT,
    device_user: str = _DEFAULT_DEVICE_USER,
    device_password: str = _DEFAULT_DEVICE_PASSWORD,
    wait_timeout_s: float = 90.0,
    persist: bool = True,
    verify_mgmt0: bool = True,
) -> Dict[str, Any]:
    """Mount a DNOS device on an ODL controller and wait for it to connect."""
    ep = _require_odl_endpoint(endpoint)
    if not ep:
        return error_envelope(
            f"endpoint '{endpoint}' is not an ODL controller (or unknown)",
            kind="mount_add", endpoint=endpoint, device=device,
        )
    dev = get_device(device)
    if not dev:
        return error_envelope(
            f"unknown device alias '{device}'",
            kind="mount_add", endpoint=endpoint, device=device,
        )

    host = dev.get("mgmt0")

    # Issue #71: verify the cached mgmt0 against the chassis's live mgmt0
    # (via the cli group) before baking the address into the ODL mount —
    # a stale cached IP would make ODL manage the wrong box. When the
    # chassis can't confirm the address the mount is refused (error
    # envelope), not created unverified; --no-verify-mgmt0 is the opt-out.
    mgmt0_warnings = []
    if verify_mgmt0:
        from qactl.dnos.cli.core.mgmt0_verify import (
            Mgmt0UnverifiedError,
            require_verified,
            verify_device_mgmt0,
        )
        try:
            verification = require_verified(verify_device_mgmt0(device))
        except Mgmt0UnverifiedError as exc:
            return error_envelope(
                str(exc), kind="mount_add", endpoint=endpoint, device=device,
            )
        mgmt0_warnings = list(verification.warnings)
        if verification.address:
            host = verification.address
    else:
        mgmt0_warnings.append(
            "mgmt0 pre-verification skipped by --no-verify-mgmt0; "
            "using the cached address as-is."
        )

    if not host:
        return error_envelope(
            f"device '{device}' has no mgmt0 configured",
            kind="mount_add", endpoint=endpoint, device=device,
        )

    name = mount_name or f"{device.upper()}-RC"
    base = ep["base_url"]
    auth = ep.get("auth") or {}

    env = make_envelope(
        kind="mount_add", device=device, endpoint=endpoint, base_url=base,
        request={
            "device": device, "endpoint": endpoint, "mount_name": name,
            "host": host, "port": netconf_port,
        },
    )
    env["warnings"].extend(mgmt0_warnings)

    sc, body = put_mount(
        base_url=base,
        auth_user=auth.get("user", "admin"),
        auth_password=auth.get("password", "admin"),
        node_id=name,
        host=host, port=netconf_port,
        device_user=device_user, device_password=device_password,
        verify=False,
    )
    if sc not in (200, 201, 204):
        env["status"] = "error"
        env["errors"].append(f"PUT failed: HTTP {sc}: {body[:400]}")
        return env

    status = wait_until_connected(
        base_url=base,
        auth_user=auth.get("user", "admin"),
        auth_password=auth.get("password", "admin"),
        node_id=name,
        overall_timeout=wait_timeout_s,
        verify=False,
    )
    env["result"] = {
        "mount_name": name,
        "put_http_status": sc,
        "connection-status": status.get("connection-status"),
        "elapsed_s": status.get("elapsed_s"),
        "available_caps": status.get("available_caps"),
        "unavailable_caps": status.get("unavailable_caps"),
        "host": status.get("host"),
        "port": status.get("port"),
    }
    if status.get("connection-status") != "connected":
        env["status"] = "error"
        env["errors"].append(
            "ODL did not reach 'connected' within the timeout — "
            "check device reachability from the controller and "
            "device NETCONF credentials."
        )
        env["next_actions"].extend([
            "verify TCP/830 reachability from the ODL host to the device mgmt0",
            "confirm device NETCONF user/password match those passed here",
        ])
        return env

    if persist:
        doc = load_endpoints()
        ep_doc = doc["endpoints"].setdefault(endpoint, ep)
        ep_doc.setdefault("mounts", {})[name] = {
            "device": canonical_device(device),
            "host": host,
            "port": netconf_port,
            "device_user": device_user,
            "device_password": device_password,
            "tcp_only": False,
            "keepalive_delay": 0,
        }
        save_endpoints(doc)
    return env


def _mount_for_device(ep: Dict[str, Any], device_alias: str) -> Optional[str]:
    """Registered mount name for a device alias on ``ep``, or ``None``."""
    target = canonical_device(device_alias)
    for mn, mcfg in (ep.get("mounts") or {}).items():
        if canonical_device(mcfg.get("device")) == target:
            return mn
    return None


def restconf_mount_status(
    mount_name: Optional[str] = None,
    device: Optional[str] = None,
    endpoint: str = "odl-lab1",
) -> Dict[str, Any]:
    """Live status of one mount.

    Pass either ``mount_name`` or ``device``. A ``mount_name`` that is not
    a registered mount on the endpoint is retried as a device alias, so
    ``mount status cl`` finds the ``CL-RC`` mount the same way the data
    verbs resolve ``-d cl`` (issue #73).
    """
    ep = _require_odl_endpoint(endpoint)
    if not ep:
        return error_envelope(
            f"endpoint '{endpoint}' is not an ODL controller (or unknown)",
            kind="mount_status", endpoint=endpoint, device=device,
        )

    name = mount_name
    if name and name not in (ep.get("mounts") or {}):
        name = _mount_for_device(ep, name) or name
    if not name and device:
        name = _mount_for_device(ep, device)
    if not name:
        return error_envelope(
            "specify either mount_name or a device that has a registered mount",
            kind="mount_status", endpoint=endpoint, device=device,
        )

    base = ep["base_url"]
    auth = ep.get("auth") or {}
    env = make_envelope(
        kind="mount_status", device=device, endpoint=endpoint, base_url=base,
        request={"mount_name": name, "device": device},
    )
    s = get_node_status(
        base_url=base,
        auth_user=auth.get("user", "admin"),
        auth_password=auth.get("password", "admin"),
        node_id=name,
        verify=False,
    )
    env["result"] = s
    if s.get("http_status") != 200:
        env["status"] = "error"
        env["errors"].append(
            f"HTTP {s.get('http_status')} fetching mount status — "
            f"is mount '{name}' present on '{endpoint}'?"
        )
    return env


def restconf_mount_remove(
    mount_name: str,
    endpoint: str = "odl-lab1",
    persist: bool = True,
) -> Dict[str, Any]:
    """Remove a mount from the controller and (optionally) the local registry."""
    ep = _require_odl_endpoint(endpoint)
    if not ep:
        return error_envelope(
            f"endpoint '{endpoint}' is not an ODL controller (or unknown)",
            kind="mount_remove", endpoint=endpoint,
        )
    base = ep["base_url"]
    auth = ep.get("auth") or {}
    env = make_envelope(
        kind="mount_remove", endpoint=endpoint, base_url=base,
        request={"mount_name": mount_name},
    )
    sc, body = delete_mount(
        base_url=base,
        auth_user=auth.get("user", "admin"),
        auth_password=auth.get("password", "admin"),
        node_id=mount_name,
        verify=False,
    )
    env["result"] = {"http_status": sc, "body": body[:200]}
    if sc not in (200, 204, 404):
        env["status"] = "error"
        env["errors"].append(f"DELETE failed: HTTP {sc}")
    if persist:
        doc = load_endpoints()
        ep_doc = doc["endpoints"].get(endpoint)
        if ep_doc and "mounts" in ep_doc and mount_name in ep_doc["mounts"]:
            ep_doc["mounts"].pop(mount_name, None)
            save_endpoints(doc)
    return env


def register(mcp) -> None:
    mcp.tool()(restconf_mount_add)
    mcp.tool()(restconf_mount_status)
    mcp.tool()(restconf_mount_remove)
