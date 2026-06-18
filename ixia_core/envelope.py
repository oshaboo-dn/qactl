"""Standard envelope shape for every ixia-mcp tool response.

Mirrors the gnmi-mcp / cli-mcp / netconf-mcp envelope contract so the agent
only has to remember one shape across the whole monorepo. Ixia doesn't have
a DUT concept (it's a traffic generator), so we drop `device` and
`tls_mode` and add `session_id` — which IxNetwork session the call targeted.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def make_envelope(
    *,
    kind: str,
    host: Optional[str] = None,
    port: Optional[int] = None,
    session_id: Optional[int] = None,
    request: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a fresh envelope with all top-level keys present.

    Tool body fills in ``result`` (or ``errors`` / ``warnings``) and flips
    ``status`` away from the default ``ok`` only on failure paths.
    """
    return {
        "status": "ok",
        "host": host,
        "port": port,
        "session_id": session_id,
        "kind": kind,
        "request": dict(request or {}),
        "result": None,
        "warnings": [],
        "errors": [],
        "next_actions": [],
    }


def error_envelope(
    message: str,
    *,
    kind: str,
    status: str = "error",
    host: Optional[str] = None,
    port: Optional[int] = None,
    session_id: Optional[int] = None,
    next_actions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Convenience for early-return errors before any IxNetwork traffic."""
    env = make_envelope(
        kind=kind, host=host, port=port, session_id=session_id,
    )
    env["status"] = status
    env["errors"].append(message)
    if next_actions:
        env["next_actions"].extend(next_actions)
    return env
