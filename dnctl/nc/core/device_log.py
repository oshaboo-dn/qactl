"""Structured per-device operation logging."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from .session import ConnectResult, IL_TZ, ROOT_DIR, _utc_now
from .xml_payload import safe_log_token  # re-exported for existing callers

__all__ = [
    "il_now_ms",
    "format_log_event",
    "safe_log_token",
    "append_log",
    "append_logs",
    "device_log_file",
    "_log_start",
    "_log_event",
    "_log_action",
    "_device_log_path",
    "_begin",
]


# ---------------------------------------------------------------------------
# Low-level log line plumbing
# ---------------------------------------------------------------------------


def il_now_ms() -> str:
    """Return Israel local timestamp (ms) for log line prefix."""
    return datetime.now(IL_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def format_log_event(event: str, **fields: object) -> str:
    """Render one structured event line."""
    parts = [f"[{il_now_ms()}] {event}"]
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value).replace("\n", " ").strip()
        if not text:
            continue
        if " " in text:
            text = f"\"{text}\""
        parts.append(f"{key}={text}")
    return " ".join(parts)


def append_log(log_file: str, line: str) -> None:
    """Append a single line to the log file."""
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"{line}\n")


def append_logs(log_files: List[str], line: str) -> None:
    """Append one line to all target log files."""
    for log_file in log_files:
        append_log(log_file, line)


def device_log_file(device_name: Optional[str] = None, host: Optional[str] = None) -> str:
    """Return per-device-per-day log path: <netconf_root>/logs/<YYYY-MM-DD>-<device>.log"""
    logs_dir = str(ROOT_DIR / "netconf-logs")
    token = safe_log_token(device_name) if device_name else safe_log_token(host or "unknown")
    date = datetime.now(IL_TZ).strftime("%Y-%m-%d")
    return os.path.join(logs_dir, f"{date}-{token}.log")


# ---------------------------------------------------------------------------
# Session-level structured logging (used by MCP tool bodies)
# ---------------------------------------------------------------------------


def _log_start(log_file: Path, sid: str, cr: ConnectResult, action: str) -> None:
    _log_event(
        log_file,
        sid,
        "start",
        host=f"{cr.host}:{cr.port}",
        user=cr.user,
        action=action,
    )


def _log_event(log_file: Path, sid: str, event: str, **fields: Any) -> None:
    parts = [f"session={sid}", f"event={event}"]
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        parts.append(f"{key}={text}")
    append_log(str(log_file), f"[{_utc_now()}] {' '.join(parts)}")


def _log_action(log_file: Path, event: str, **fields: Any) -> None:
    """Log a lightweight one-line event (no XML bodies)."""
    append_log(str(log_file), format_log_event(event, **fields))


def _device_log_path(device: Optional[str], host: str) -> Path:
    """Return per-device-per-day log path."""
    return Path(device_log_file(device_name=device, host=host))


def _begin(
    cr: ConnectResult, sid: str, action: str, device: Optional[str] = None,
) -> Path:
    """Open a per-device log for a tool call: resolve path, emit 'start' and 'connect'.

    `device` is the tool's device argument (preserves per-device log filename when
    the caller passed both host= and device=).
    """
    log_path = _device_log_path(device if device is not None else cr.device, cr.host)
    _log_start(log_path, sid, cr, action)
    _log_action(log_path, "connect", host=f"{cr.host}:{cr.port}", user=cr.user)
    return log_path
