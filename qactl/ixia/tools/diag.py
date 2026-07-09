"""Diagnostic tools: connect check, session list.

These are the first smoke tests — they work against an empty IxNetwork
session (no config loaded, no topologies, no traffic items) and validate
the MCP ↔ wrapper ↔ REST path end-to-end.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from qactl.ixia.client.models import IxiaError

from qactl.ixia.core.envelope import make_envelope, error_envelope
from qactl.ixia.core.session import (
    DEFAULT_PORT, DEFAULT_USER,
    get_session, drop_session, session_id_of,
)


def ixia_connect_check(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Cheap reachability probe against an IxNetwork REST API server.

    Opens (or reuses) the cached ``IxiaSession`` for ``(host, port, user)``,
    reads the IxNetwork build number + the active session list, and
    returns latency. Use before any expensive traffic / stats call.

    The IxNetwork API Server must be running on ``host:port`` — that's
    the binary launched from the "IxNetwork API Server" Start menu
    shortcut (not the interactive GUI). On Windows with
    ``-restOnAllInterfaces``, REST is HTTPS-only on port 11009; the
    wrapper auto-selects the scheme.

    Returns envelope with ``result = {connected, api_version, session_href,
    session_id, sessions, latency_ms, user}``.
    """
    request = {"host": host, "port": port, "user": user}

    t0 = time.time()
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}",
            kind="connect_check", host=host, port=port,
            status="connect_error",
            next_actions=[
                "Verify the IxNetwork API Server is running on the target host "
                "(IxNetwork.exe -restPort <port> -restOnAllInterfaces).",
                "Verify TCP reachability: `curl -sk https://<host>:<port>/api/v1/sessions`.",
            ],
        )
    except Exception as e:
        return error_envelope(
            f"{type(e).__name__}: {str(e)[:240]}",
            kind="connect_check", host=host, port=port,
            status="error",
        )
    elapsed_ms = int((time.time() - t0) * 1000)

    env = make_envelope(
        kind="connect_check", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        ixn = s.ixn
        api_version = getattr(ixn.Globals, "BuildNumber", None)
        session_href = getattr(ixn, "href", None)
        try:
            sessions = s.config.sessions()
        except Exception as sub:
            sessions = []
            env["warnings"].append(
                f"config.sessions() failed: {type(sub).__name__}: {sub}"
            )
        env["result"] = {
            "connected": True,
            "api_version": api_version,
            "session_href": session_href,
            "session_id": env["session_id"],
            "sessions": sessions,
            "latency_ms": elapsed_ms,
            "user": user,
        }
        return env
    except Exception as e:
        # Session probably went stale; evict for next call.
        drop_session(host, port, user)
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        env["next_actions"].append(
            "Session cache evicted; retry this tool — it will reconnect."
        )
        return env


def ixia_list_sessions(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """List all IxNetwork sessions on the target API server.

    On Windows API servers this is usually a single ``ACTIVE`` session
    with ``id=1``. Linux / container servers may run multiple. This
    tool never creates or removes sessions — it's a pure inventory.
    """
    request = {"host": host, "port": port, "user": user}

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}",
            kind="list_sessions", host=host, port=port,
            status="connect_error",
        )

    env = make_envelope(
        kind="list_sessions", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        sessions = s.config.sessions()
        env["result"] = {
            "count": len(sessions),
            "sessions": sessions,
        }
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(ixia_connect_check)
    mcp.tool()(ixia_list_sessions)
