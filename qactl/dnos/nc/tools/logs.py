"""``netconf_extract_logs`` tool.

Surfaces structured per-device session events from
``netconf-logs/<date>-<device>.log`` (the human-readable transcript
written by :mod:`qactl.nc.core.device_log`). Companion to the JSONL request
log under ``mcp-logs/`` which can be read directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from qactl.dnos.nc.core.device_log import safe_log_token
from qactl.dnos.nc.core.session import IL_TZ, ROOT_DIR


def netconf_extract_logs(
    device: str,
    date: Optional[str] = None,
) -> Dict[str, Any]:
    """Pull NETCONF session logs for a device and date from netconf-logs/.

    Date format: YYYY-MM-DD (defaults to today in Israel timezone).
    Returns log contents, or a list of available dates if the requested file is missing.
    """
    if date is None:
        date = datetime.now(IL_TZ).strftime("%Y-%m-%d")

    token = safe_log_token(device)
    log_file = ROOT_DIR / "netconf-logs" / f"{date}-{token}.log"

    if not log_file.exists():
        logs_dir = ROOT_DIR / "netconf-logs"
        available = sorted(
            (f.name for f in logs_dir.iterdir()
             if f.is_file() and f.suffix == ".log" and f"-{token}." in f.name),
            reverse=True,
        ) if logs_dir.is_dir() else []
        return {
            "action": "extract_logs",
            "status": "error",
            "error": f"No log file found: {log_file.name}",
            "device": device,
            "date": date,
            "available_dates": available,
        }

    content = log_file.read_text(encoding="utf-8")
    lines = content.count("\n")
    max_bytes = 100_000
    truncated = False
    if len(content) > max_bytes:
        content = content[-max_bytes:]
        first_nl = content.find("\n")
        if first_nl != -1:
            content = content[first_nl + 1:]
        truncated = True

    result: Dict[str, Any] = {
        "action": "extract_logs",
        "status": "ok",
        "log_file": str(log_file.relative_to(ROOT_DIR)),
        "device": device,
        "date": date,
        "lines": lines,
        "content": content,
    }
    if truncated:
        result["truncated"] = True
        result["note"] = "Log file exceeded 100KB — showing tail only"
    return result


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(netconf_extract_logs)
