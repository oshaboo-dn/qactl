"""JSONL request logging for the MCP front (stdio servers).

Folded in from the old ``dnos-mcps`` ``dn_common.request_log`` so the
merged repo keeps one logger. It is the *only* piece of that shared
package we re-home here: device registry / credential / dnftp helpers are
already provided by ``qactl.core`` (the vendored tool tree the MCP front
reuses), so duplicating them would just create drift.

The CLI front stays unlogged (process-per-invocation, output already on
stdout). The MCP front wraps every registered tool with
:meth:`RequestLogger.log_mcp_call`, emitting two correlated JSONL lines
per call (``phase`` ``req`` / ``resp`` sharing one ``rid``)::

    {"ts":"...", "rid":"abcd1234", "tool":"gnmi_ping", "phase":"req",  "args":{...}}
    {"ts":"...", "rid":"abcd1234", "tool":"gnmi_ping", "phase":"resp", "status":"ok", "ms":287, ...}

No secrets are written: only argument *reprs* (capped) and response size
telemetry. Timestamps default to UTC.
"""

from __future__ import annotations

import functools
import json
import os
import time
import uuid
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional


_ARG_REPR_CAP = 2000


def default_log_dir(group: str) -> Path:
    """Per-group log directory, override the root with ``QACTL_MCP_LOG_DIR``."""
    root = os.environ.get("QACTL_MCP_LOG_DIR") or str(Path.home() / ".qactl" / "mcp-logs")
    return Path(root) / group


def _safe_repr(value: Any, limit: int = _ARG_REPR_CAP) -> str:
    try:
        text = repr(value)
    except Exception as e:  # pragma: no cover - defensive
        return f"<unrepr:{type(value).__name__}:{e}>"
    if len(text) <= limit:
        return text
    return text[:limit] + f"...<+{len(text) - limit}b>"


def _measure_response(result: Any) -> dict:
    """Per-tool size telemetry: total bytes + per-top-level-field length."""
    if result is None:
        return {"resp_bytes": 0}
    try:
        total = len(json.dumps(result, default=str, ensure_ascii=False))
    except Exception:
        total = len(str(result))
    info: dict = {"resp_bytes": total}
    if isinstance(result, dict):
        fields: dict = {}
        for key, value in result.items():
            try:
                fields[str(key)] = len(json.dumps(value, default=str, ensure_ascii=False))
            except Exception:
                fields[str(key)] = len(str(value))
        info["resp_fields"] = fields
    return info


class RequestLogger:
    """Per-server JSONL request logger.

    Args:
        log_dir: Directory the daily ``YYYY-MM-DD-requests.jsonl`` file
            lives in. Created on demand.
        tz: Timezone for the ``ts`` field. ``None`` (default) uses UTC.
    """

    def __init__(self, log_dir: Path, tz=None) -> None:
        self.log_dir: Path = Path(log_dir)
        self._tz = tz
        self._rid_var: ContextVar[Optional[str]] = ContextVar(
            f"qactl_mcp_rid_{id(self)}", default=None,
        )

    def _now_iso(self) -> str:
        d = datetime.now(self._tz) if self._tz is not None else datetime.utcnow()
        return d.isoformat(timespec="milliseconds")

    def _today_path(self) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        day = (datetime.now(self._tz) if self._tz else datetime.utcnow()).strftime("%Y-%m-%d")
        return self.log_dir / f"{day}-requests.jsonl"

    def _append(self, entry: dict) -> None:
        try:
            with self._today_path().open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            # Never let logging failures break a tool call.
            pass

    def log_mcp_call(self, tool_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator: log req/resp JSONL entries around a tool function."""

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                rid = uuid.uuid4().hex[:8]
                started = time.monotonic()
                req_entry: dict = {
                    "ts": self._now_iso(),
                    "rid": rid,
                    "tool": tool_name,
                    "phase": "req",
                    "args": {k: _safe_repr(v) for k, v in kwargs.items()},
                }
                if args:
                    req_entry["positional"] = [_safe_repr(a) for a in args]
                self._append(req_entry)
                status = "ok"
                result: Any = None
                token = self._rid_var.set(rid)
                try:
                    result = fn(*args, **kwargs)
                    if isinstance(result, dict):
                        status = str(result.get("status", "ok"))
                    return result
                except BaseException as e:
                    status = f"exception:{type(e).__name__}:{e}"
                    raise
                finally:
                    self._rid_var.reset(token)
                    resp_entry: dict = {
                        "ts": self._now_iso(),
                        "rid": rid,
                        "tool": tool_name,
                        "phase": "resp",
                        "status": status,
                        "ms": round((time.monotonic() - started) * 1000),
                    }
                    resp_entry.update(_measure_response(result))
                    self._append(resp_entry)

            return wrapper

        return deco

    def log_event(self, event: str, **fields: Any) -> None:
        """Structured debug breadcrumb for the in-flight tool call (no-op if none)."""
        rid = self._rid_var.get()
        if rid is None:
            return
        entry: dict = {"ts": self._now_iso(), "rid": rid, "phase": "debug", "event": event}
        for key, value in fields.items():
            entry[str(key)] = value
        self._append(entry)


__all__ = ["RequestLogger", "default_log_dir"]
