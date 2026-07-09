"""ODL NETCONF-topology mount management.

DNOS does not run RESTCONF natively on current builds, so the canonical
RESTCONF entry point in this lab is the ODL controller acting as a
NETCONF-to-RESTCONF gateway. To make a device queryable via RESTCONF we:

1. ``PUT`` a NETCONF-topology node config under
   ``/restconf/config/network-topology:network-topology/topology/topology-netconf/node/<NAME>``,
   carrying device host / port / NETCONF user / password.
2. Poll the corresponding ``/operational/...`` URL until
   ``netconf-node-topology:connection-status`` becomes ``connected``
   (typically ~20 s; ODL pulls every YANG schema the device advertises).
3. Issue RESTCONF data-tree GETs under
   ``.../node/<NAME>/yang-ext:mount/<yang-path>``.

Cleanup is a ``DELETE`` on the same config URL.

This module wraps those three operations with the same envelope shape as
the other MCPs and surfaces helpful diagnostics (wrong creds → status
stays at ``connecting``; unreachable host → same; missing schemas → the
``available-capabilities`` set is short).
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional, Tuple

from .session import request as http_request
from .uri import build_odl_mount_url, build_odl_node_status_url


_MOUNT_TEMPLATE = """\
<node xmlns="urn:TBD:params:xml:ns:yang:network-topology">
  <node-id>{node_id}</node-id>
  <host xmlns="urn:opendaylight:netconf-node-topology">{host}</host>
  <port xmlns="urn:opendaylight:netconf-node-topology">{port}</port>
  <username xmlns="urn:opendaylight:netconf-node-topology">{user}</username>
  <password xmlns="urn:opendaylight:netconf-node-topology">{password}</password>
  <tcp-only xmlns="urn:opendaylight:netconf-node-topology">{tcp_only}</tcp-only>
  <keepalive-delay xmlns="urn:opendaylight:netconf-node-topology">{keepalive_delay}</keepalive-delay>
</node>
"""


def render_mount_xml(
    *,
    node_id: str,
    host: str,
    port: int = 830,
    user: str = "dnroot",
    password: str = "dnroot",
    tcp_only: bool = False,
    keepalive_delay: int = 0,
) -> str:
    return _MOUNT_TEMPLATE.format(
        node_id=node_id,
        host=host,
        port=port,
        user=user,
        password=password,
        tcp_only=str(tcp_only).lower(),
        keepalive_delay=int(keepalive_delay),
    )


def put_mount(
    *,
    base_url: str,
    auth_user: str,
    auth_password: str,
    node_id: str,
    host: str,
    port: int = 830,
    device_user: str = "dnroot",
    device_password: str = "dnroot",
    tcp_only: bool = False,
    keepalive_delay: int = 0,
    timeout: float = 20.0,
    verify: bool = True,
) -> Tuple[int, str]:
    """Create / replace one mounted node. Returns ``(http_status, body_text)``."""
    url = build_odl_mount_url(base_url, node_id)
    xml = render_mount_xml(
        node_id=node_id, host=host, port=port,
        user=device_user, password=device_password,
        tcp_only=tcp_only, keepalive_delay=keepalive_delay,
    )
    sc, _h, body, _el = http_request(
        method="PUT", url=url,
        user=auth_user, password=auth_password,
        verify=verify, timeout=timeout,
        xml_body=xml,
    )
    return sc, body.decode("utf-8", errors="replace")


def delete_mount(
    *,
    base_url: str,
    auth_user: str,
    auth_password: str,
    node_id: str,
    timeout: float = 10.0,
    verify: bool = True,
) -> Tuple[int, str]:
    url = build_odl_mount_url(base_url, node_id)
    sc, _h, body, _el = http_request(
        method="DELETE", url=url,
        user=auth_user, password=auth_password,
        verify=verify, timeout=timeout,
    )
    return sc, body.decode("utf-8", errors="replace")


def get_node_status(
    *,
    base_url: str,
    auth_user: str,
    auth_password: str,
    node_id: str,
    timeout: float = 10.0,
    verify: bool = True,
) -> Dict[str, Any]:
    """Return the operational ``node`` document for one mount.

    Yields a dict like::

        {
          "connection-status": "connected" | "connecting" | "unable-to-connect",
          "host": "...",
          "port": 830,
          "available_caps": int,
          "unavailable_caps": int,
          "raw": {...},
        }
    """
    url = build_odl_node_status_url(base_url, node_id)
    sc, _h, body, _el = http_request(
        method="GET", url=url,
        user=auth_user, password=auth_password,
        verify=verify, timeout=timeout,
        extra_headers={"Accept": "application/json"},
    )
    out: Dict[str, Any] = {
        "http_status": sc,
        "connection-status": None,
        "host": None,
        "port": None,
        "available_caps": 0,
        "unavailable_caps": 0,
        "raw": None,
    }
    if sc != 200:
        return out
    try:
        doc = json.loads(body.decode("utf-8"))
    except Exception:
        return out
    n = (doc.get("node") or [{}])[0]
    out["raw"] = n
    out["connection-status"] = n.get("netconf-node-topology:connection-status")
    out["host"] = n.get("netconf-node-topology:host")
    out["port"] = n.get("netconf-node-topology:port")
    av = n.get("netconf-node-topology:available-capabilities", {}) or {}
    ua = n.get("netconf-node-topology:unavailable-capabilities", {}) or {}
    out["available_caps"] = len(av.get("available-capability", []) or [])
    out["unavailable_caps"] = len(ua.get("unavailable-capability", []) or [])
    return out


def wait_until_connected(
    *,
    base_url: str,
    auth_user: str,
    auth_password: str,
    node_id: str,
    overall_timeout: float = 90.0,
    poll_interval: float = 5.0,
    verify: bool = True,
) -> Dict[str, Any]:
    """Poll until status reaches a terminal value or overall_timeout elapses.

    Terminal states: ``connected`` (success), ``unable-to-connect`` (error).
    Returns the last status dict from :func:`get_node_status`, plus
    ``elapsed_s`` showing how long polling took.
    """
    t0 = time.monotonic()
    last: Dict[str, Any] = {"connection-status": None}
    while True:
        last = get_node_status(
            base_url=base_url, auth_user=auth_user, auth_password=auth_password,
            node_id=node_id, verify=verify,
        )
        cs = last.get("connection-status")
        last["elapsed_s"] = round(time.monotonic() - t0, 1)
        if cs in ("connected", "unable-to-connect"):
            return last
        if (time.monotonic() - t0) >= overall_timeout:
            return last
        time.sleep(poll_interval)
