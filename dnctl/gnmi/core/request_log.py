"""JSONL request logger for gnmi-mcp (shim around dnctl.core.request_log).

Same JSONL contract netconf-mcp uses. Per-tool req + resp lines
correlated by an 8-char ``rid``; password-bearing kwargs are
``repr``'d in args at the dnctl.core layer (no field-name redaction
yet — caller-side responsibility).

Writes to ``<state_dir>/gnmi/mcp-logs/<YYYY-MM-DD>-requests.jsonl``.
"""

from __future__ import annotations

from typing import Any, Callable

from dnctl.core.paths import state_dir
from dnctl.core.request_log import RequestLogger


MCP_LOG_DIR = state_dir("gnmi") / "mcp-logs"

_logger = RequestLogger(MCP_LOG_DIR)


def log_mcp_call(tool_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: log req/resp JSONL entries around a tool function."""
    return _logger.log_mcp_call(tool_name)


def mcp_log_event(event: str, **fields: Any) -> None:
    """Emit a structured debug event tagged with the in-flight tool's rid."""
    _logger.log_event(event, **fields)


__all__ = ["log_mcp_call", "mcp_log_event", "MCP_LOG_DIR"]
