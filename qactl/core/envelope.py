"""The single response envelope shape shared by every qactl command.

One shape across all domains (jira / confluence / jenkins) so an agent
only has to learn it once and ``--json`` is lossless and greppable:

    {
      "status": "ok" | "warning" | "error" | "aborted"
                 | "bad_argument" | "confirmation_required",
      "kind":   "<domain>_<action>",   # e.g. "jira_list_watchers"
      "result": <payload | null>,
      "warnings": [...],
      "errors":   [...],
      "next_actions": [...],
    }

``status in {"ok", "warning"}`` exits 0; anything else exits non-zero
(see :mod:`qactl.core.output`).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def make_envelope(*, kind: str, request: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A fresh success envelope with every top-level key present."""
    return {
        "status": "ok",
        "kind": kind,
        "request": dict(request or {}),
        "result": None,
        "warnings": [],
        "errors": [],
        "next_actions": [],
    }


def ok_envelope(
    *,
    kind: str,
    result: Any = None,
    next_actions: Optional[List[str]] = None,
    warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    env = make_envelope(kind=kind)
    env["result"] = result
    if next_actions:
        env["next_actions"].extend(next_actions)
    if warnings:
        env["warnings"].extend(warnings)
        env["status"] = "warning"
    return env


def error_envelope(
    message: str,
    *,
    kind: str,
    status: str = "error",
    next_actions: Optional[List[str]] = None,
    result: Any = None,
) -> Dict[str, Any]:
    """Convenience for early-return errors (bad args, auth, HTTP failures)."""
    env = make_envelope(kind=kind)
    env["status"] = status
    env["errors"].append(message)
    env["result"] = result
    if next_actions:
        env["next_actions"].extend(next_actions)
    return env
