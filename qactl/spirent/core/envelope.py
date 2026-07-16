"""Standard envelope shape for every ``qactl spirent`` tool response.

Mirrors the ``qactl.ixia`` envelope contract so the agent only has to
remember one shape across every traffic-generator group. The one Spirent
difference from ixia: STC REST sessions are addressed by **name**
(``"<name> - <user>"``), not by an integer id, so the locus key is
``session`` (a string) rather than ``session_id``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def make_envelope(
    *,
    kind: str,
    host: Optional[str] = None,
    port: Optional[int] = None,
    session: Optional[str] = None,
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
        "session": session,
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
    session: Optional[str] = None,
    next_actions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Convenience for early-return errors before any STC REST traffic."""
    env = make_envelope(kind=kind, host=host, port=port, session=session)
    env["status"] = status
    env["errors"].append(message)
    if next_actions:
        env["next_actions"].extend(next_actions)
    return env
