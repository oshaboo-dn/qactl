"""Standard envelope shape for every restconf-mcp tool response.

Same contract as `gnmi-mcp` and `cli-mcp` so the agent only has to remember
one shape across the family of MCPs that talk to a device.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def make_envelope(
    *,
    kind: str,
    device: Optional[str] = None,
    endpoint: Optional[str] = None,
    base_url: Optional[str] = None,
    request: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a fresh envelope with all top-level keys present.

    `endpoint` is the RESTCONF speaker alias (e.g. ``odl-lab1``);
    `device` is the back-end node visible through that endpoint
    (e.g. ``cl`` mounted on ODL as ``OHADZS-CL``).
    """
    return {
        "status": "ok",
        "device": device,
        "endpoint": endpoint,
        "base_url": base_url,
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
    device: Optional[str] = None,
    endpoint: Optional[str] = None,
    base_url: Optional[str] = None,
    next_actions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Convenience for early-return errors before any RESTCONF traffic."""
    env = make_envelope(
        kind=kind, device=device, endpoint=endpoint, base_url=base_url,
    )
    env["status"] = status
    env["errors"].append(message)
    if next_actions:
        env["next_actions"].extend(next_actions)
    return env
