"""Diagnostic tools — endpoint reachability + module discovery.

* ``restconf_ping(endpoint)`` — TCP/HTTP reachability against the
  RESTCONF speaker (no device traffic). Returns latency.
* ``restconf_yang_library(endpoint, mount=...)`` — pulls
  ``ietf-yang-library:modules-state`` from the mount, optionally
  filtering module names by substring. Useful to confirm DriveNets YANG
  modules are loaded before issuing data GETs.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from qactl.dnos.rc.core.envelope import error_envelope, make_envelope
from qactl.dnos.rc.core.registry import get_endpoint
from qactl.dnos.rc.core.session import request as http_request
from qactl.dnos.rc.core.uri import build_odl_node_status_url


def restconf_ping(endpoint: str = "odl-lab1") -> Dict[str, Any]:
    """Cheap reachability check — HTTP GET on the controller root."""
    ep = get_endpoint(endpoint)
    if not ep:
        return error_envelope(
            f"unknown endpoint '{endpoint}' (see restconf_endpoints.json)",
            kind="ping", endpoint=endpoint,
        )
    base = ep["base_url"]
    auth = ep.get("auth") or {}
    env = make_envelope(kind="ping", endpoint=endpoint, base_url=base,
                        request={"endpoint": endpoint})

    t0 = time.monotonic()
    sc, _h, body, _el = http_request(
        method="GET",
        url=base.rstrip("/") + "/operational/network-topology:network-topology",
        user=auth.get("user"), password=auth.get("password"),
        verify=False, timeout=10.0,
        extra_headers={"Accept": "application/json"},
    )
    latency_ms = round((time.monotonic() - t0) * 1000)
    env["result"] = {
        "http_status": sc,
        "latency_ms": latency_ms,
        "bytes": len(body),
    }
    if sc != 200:
        env["status"] = "error"
        env["errors"].append(f"HTTP {sc} from controller root")
    return env


def restconf_yang_library(
    endpoint: str = "odl-lab1",
    mount: Optional[str] = None,
    name_contains: Optional[str] = None,
) -> Dict[str, Any]:
    """Return ``ietf-yang-library:modules-state`` from the mounted device.

    With ``mount`` set, queries via the ODL mount point (the actual device
    schema set). Without ``mount``, queries the controller's own library.
    ``name_contains`` filters the returned module list (case-insensitive).
    """
    ep = get_endpoint(endpoint)
    if not ep:
        return error_envelope(
            f"unknown endpoint '{endpoint}'", kind="yang_library", endpoint=endpoint,
        )
    base = ep["base_url"].rstrip("/")
    auth = ep.get("auth") or {}

    if mount:
        url = (
            f"{base}/operational/network-topology:network-topology/topology/"
            f"topology-netconf/node/{mount}/yang-ext:mount/"
            f"ietf-yang-library:modules-state"
        )
    else:
        url = f"{base}/operational/ietf-yang-library:modules-state"

    env = make_envelope(kind="yang_library", endpoint=endpoint, base_url=base,
                        request={"mount": mount, "name_contains": name_contains})
    sc, _h, body, _el = http_request(
        method="GET", url=url,
        user=auth.get("user"), password=auth.get("password"),
        verify=False, timeout=30.0,
        extra_headers={"Accept": "application/json"},
    )
    if sc != 200:
        env["status"] = "error"
        env["errors"].append(f"HTTP {sc}: {body[:300].decode('utf-8','replace')}")
        return env

    try:
        doc = json.loads(body.decode("utf-8"))
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"JSON parse error: {e}")
        return env

    mods: List[Dict[str, Any]] = (
        (doc.get("modules-state") or {}).get("module", [])
        or doc.get("ietf-yang-library:modules-state", {}).get("module", [])
        or []
    )
    if name_contains:
        needle = name_contains.lower()
        mods = [m for m in mods if needle in m.get("name", "").lower()]

    env["result"] = {
        "module_count": len(mods),
        "modules": [
            {"name": m.get("name"), "namespace": m.get("namespace"),
             "revision": m.get("revision")}
            for m in mods
        ],
    }
    return env


def register(mcp) -> None:
    mcp.tool()(restconf_ping)
    mcp.tool()(restconf_yang_library)
