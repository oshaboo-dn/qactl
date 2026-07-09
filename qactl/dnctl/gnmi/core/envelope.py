"""Standard envelope shape for every gnmi-mcp tool response.

Mirrors the cli-mcp envelope contract so the agent only has to remember one
shape across the three MCPs that talk to a device.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def make_envelope(
    *,
    kind: str,
    device: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    tls_mode: Optional[str] = None,
    request: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a fresh envelope with all top-level keys present.

    Tool body fills in `result` (or `errors` / `warnings`) and flips
    `status` away from the default `ok` only on failure paths.
    """
    return {
        "status": "ok",
        "device": device,
        "host": host,
        "port": port,
        "tls_mode": tls_mode,
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
    host: Optional[str] = None,
    port: Optional[int] = None,
    tls_mode: Optional[str] = None,
    next_actions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Convenience for early-return errors before any device traffic."""
    env = make_envelope(
        kind=kind, device=device, host=host, port=port, tls_mode=tls_mode,
    )
    env["status"] = status
    env["errors"].append(message)
    if next_actions:
        env["next_actions"].extend(next_actions)
    return env
