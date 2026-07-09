"""MCP-level request logger (shim around dnctl.core.request_log).

Concrete instance + module-level wrappers so existing imports
(``from dnctl.nc.core.request_log import log_mcp_call``) keep working. The
heavy lifting is in :class:`dnctl.core.request_log.RequestLogger`.

Writes one JSONL line per tool invocation (request + response) to
``mcp-logs/<YYYY-MM-DD>-requests.jsonl`` (under ``netconf-mcp/``).
Fully isolated from the per-device NETCONF action logs under
``netconf-logs/`` (different folder, different format, different
purpose).

Intended for cross-tool debugging (who called what, with which args,
how long it took, what the outcome was). Device-level NETCONF action
history stays in ``netconf-logs/`` and is surfaced by
``netconf_extract_logs``.
"""

from __future__ import annotations

from typing import Any, Callable

from qactl.dnctl.core.request_log import RequestLogger

from .session import IL_TZ, ROOT_DIR


MCP_LOG_DIR = ROOT_DIR / "mcp-logs"

_logger = RequestLogger(MCP_LOG_DIR, IL_TZ)


def log_mcp_call(tool_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: log req/resp JSONL entries around a tool function."""
    return _logger.log_mcp_call(tool_name)


def mcp_log_event(event: str, **fields: Any) -> None:
    """Emit a structured debug event tagged with the in-flight tool's rid."""
    _logger.log_event(event, **fields)


__all__ = ["log_mcp_call", "mcp_log_event", "MCP_LOG_DIR"]
