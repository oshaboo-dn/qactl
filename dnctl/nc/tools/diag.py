"""Diagnostic tools (``netconf_ping``, ``netconf_capabilities``).

Both tools open a NETCONF session and immediately close it; ``ping``
just measures round-trip latency, ``capabilities`` returns the hello
XML the server advertised.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from dnctl.nc.core.device_log import _begin, _log_action, _log_event
from dnctl.nc.core.netconf_rpc import render_hello_xml
from dnctl.nc.core.results import _base_result, _error_result
from dnctl.nc.core.session import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    _connect_device,
    _session_id,
)


def netconf_capabilities(
    host: Optional[str] = None,
    device: Optional[str] = None,
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    no_verify: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Return NETCONF server capabilities as hello XML."""
    sid = _session_id()
    try:
        with _connect_device(host, device, port, user, password, no_verify, timeout) as cr:
            log_path = _begin(cr, sid, "capabilities", device=device)
            caps = [str(c) for c in cr.mgr.server_capabilities]
            hello_xml = render_hello_xml(caps)
            _log_action(log_path, "action", action="capabilities", result="ok")
            _log_event(log_path, sid, "end", status="ok")
            return _base_result(
                "capabilities", cr, sid,
                {"status": "ok", "capabilities_count": len(caps), "hello_xml": hello_xml},
            )
    except Exception as e:
        return _error_result("capabilities", sid, e)


def netconf_ping(
    host: Optional[str] = None,
    device: Optional[str] = None,
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    no_verify: bool = True,
    timeout: int = 30,
) -> Dict[str, Any]:
    """Lightweight connectivity and auth check. Connects and immediately disconnects."""
    sid = _session_id()
    t0 = time.monotonic()
    try:
        with _connect_device(host, device, port, user, password, no_verify, timeout) as cr:
            latency_ms = round((time.monotonic() - t0) * 1000)
            return _base_result(
                "ping", cr, sid,
                {"status": "ok", "latency_ms": latency_ms},
            )
    except Exception as e:
        latency_ms = round((time.monotonic() - t0) * 1000)
        result = _error_result("ping", sid, e)
        result["latency_ms"] = latency_ms
        return result


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(netconf_capabilities)
    mcp.tool()(netconf_ping)
