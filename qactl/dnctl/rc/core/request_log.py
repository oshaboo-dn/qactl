"""JSONL request logger for restconf-mcp (shim around dnctl.core.request_log).

Same JSONL contract netconf-mcp / gnmi-mcp use: per-tool req + resp lines
correlated by an 8-char ``rid``, with response-size telemetry. Writes
to ``<state_dir>/rc/mcp-logs/<YYYY-MM-DD>-requests.jsonl``.

Also re-exports the legacy ``log_mcp_call`` callable signature so
existing decorators in ``restconf_mcp_server.py`` and tool modules
keep compiling.
"""

from __future__ import annotations

from typing import Any, Callable

from qactl.dnctl.core.paths import state_dir
from qactl.dnctl.core.request_log import RequestLogger


MCP_LOG_DIR = state_dir("rc") / "mcp-logs"

_logger = RequestLogger(MCP_LOG_DIR)


def log_mcp_call(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: log req/resp JSONL entries around a tool function."""
    return _logger.log_mcp_call(name)


def mcp_log_event(event: str, **fields: Any) -> None:
    """Emit a structured debug event tagged with the in-flight tool's rid."""
    _logger.log_event(event, **fields)


__all__ = ["log_mcp_call", "mcp_log_event", "MCP_LOG_DIR"]
