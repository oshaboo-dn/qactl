"""Shared response envelope used by every cli-mcp tool.

All tools return the same JSON shape:

    {
        "status": "ok" | "error" | "connect_error" | "timeout",
        "device": <str or null>,
        "host":   <str>,
        "command": <str>,
        "stdout": <str>,
        "warnings": [str, ...],
        "errors":   [str, ...],
        "next_actions": [str, ...],
        ...<tool-specific extras>...
    }

``make_response`` builds the base shape; ``error_response`` is the common
short-hand for a single-error envelope. Any extra keyword becomes an extra
field on the envelope (see ``list_devices``, ``manage_device``).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


def make_response(
    *,
    status: str = "ok",
    device: Optional[str] = None,
    host: Optional[str] = "",
    command: str = "",
    stdout: str = "",
    warnings: Optional[Iterable[str]] = None,
    errors: Optional[Iterable[str]] = None,
    next_actions: Optional[Iterable[str]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    response: Dict[str, Any] = {
        "status": status,
        "device": device,
        "host": host or "",
        "command": command,
        "stdout": stdout,
        "warnings": list(warnings or []),
        "errors": list(errors or []),
        "next_actions": list(next_actions or []),
    }
    response.update(extra)
    return response


def error_response(
    message: str,
    *,
    device: Optional[str] = None,
    host: Optional[str] = None,
    command: str = "",
    next_action: Optional[str] = None,
) -> Dict[str, Any]:
    return make_response(
        status="error",
        device=device,
        host=host,
        command=command,
        errors=[message],
        next_actions=[next_action] if next_action else [],
    )


__all__ = ["make_response", "error_response"]
