"""Per-device transcript log + per-tool JSONL request log."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo


IL_TZ = ZoneInfo("Asia/Jerusalem")

from qactl.dnctl.core import paths as _paths

_ROOT = str(_paths.state_dir("cli"))
_LOGS_DIR = os.path.join(_ROOT, "logs")
_MCP_LOGS_DIR = os.path.join(_ROOT, "mcp-logs")

_write_lock = threading.Lock()


def _now() -> str:
    return datetime.now(IL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _safe_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "unknown"


def _date() -> str:
    return datetime.now(IL_TZ).strftime("%Y-%m-%d")


def transcript_path(device: Optional[str], host: str) -> str:
    token = _safe_token(device) if device else _safe_token(host)
    return os.path.join(_LOGS_DIR, f"{_date()}-{token}.log")


def append_transcript(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _write_lock, open(path, "a", encoding="utf-8") as f:
        f.write(text)


def log_invocation(
    device: Optional[str],
    host: str,
    command: str,
    output: str,
    head_prompt_line: str = "",
    tail_prompt: str = "",
    steps: Optional[Iterable[Any]] = None,
) -> None:
    """Append one complete tool invocation to the per-device transcript.

    Each call is a self-contained block — the new model opens a fresh
    channel per request, so we emit an invocation header followed by the
    terminal-style exchange as a human would see it on the device:

        === 2026-04-20 21:23:40 | device=sa host=WW61... ===
        HOST(20-Apr-2026-20:21:25)# show system
        <output ...>
        HOST(20-Apr-2026-20:21:27)#

    When ``steps`` is provided (a list of ``session.StepCapture``), we
    render EVERY step's echo + output + trailing prompt in order, so a
    multi-step sequence like ``configure ; rollback 1 ; commit`` reads
    end-to-end instead of just showing the ``commit`` tail. ``steps`` is
    duck-typed (any object with ``head_prompt_line`` / ``output`` /
    ``tail_prompt`` / ``command`` attrs works) to keep this module free
    of a session-layer import cycle.

    When ``steps`` is absent / empty we fall back to the single-step
    rendering using ``head_prompt_line`` / ``output`` / ``tail_prompt``.

    - ``head_prompt_line`` is the full rendered prompt+command line as DNOS
      echoed it back. When absent (couldn't be captured) we fall back to
      just the command text, prefixed with ``# `` so it still reads sensibly.
    - ``tail_prompt`` is the trailing ``HOST(timestamp)#`` line. When
      absent (e.g. timeout) we omit it.

    Both are kept out of the agent-facing tool response; logging only.
    """
    path = transcript_path(device, host)
    header = f"\n=== {_now()} | device={device or '-'} host={host} ===\n"
    body = _render_steps(steps) if steps else _render_single(
        command, output, head_prompt_line, tail_prompt,
    )
    append_transcript(path, f"{header}{body}")


def _render_single(
    command: str, output: str, head_prompt_line: str, tail_prompt: str,
) -> str:
    head = head_prompt_line if head_prompt_line else f"# {command}"
    tail = f"{tail_prompt}\n" if tail_prompt else ""
    return f"{head}\n{output}{tail}"


def _render_steps(steps: Iterable[Any]) -> str:
    parts = []
    for s in steps:
        head = getattr(s, "head_prompt_line", "") or f"# {getattr(s, 'command', '')}"
        out = getattr(s, "output", "") or ""
        tail = getattr(s, "tail_prompt", "") or ""
        parts.append(f"{head}\n{out}{(tail + chr(10)) if tail else ''}")
    return "".join(parts)


def request_log_path() -> str:
    return os.path.join(_MCP_LOGS_DIR, f"{_date()}-requests.jsonl")


def log_request(tool: str, request: Dict[str, Any], response: Dict[str, Any]) -> None:
    path = request_log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {
        "ts": _now(),
        "tool": tool,
        "request": _redact(request),
        "response": _summarise_response(response),
    }
    with _write_lock, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _redact(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in payload.items():
        if k.lower() in {"password", "pass", "secret"}:
            out[k] = "***"
        else:
            out[k] = v
    return out


def _summarise_response(resp: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(resp)
    stdout = out.get("stdout")
    if isinstance(stdout, str) and len(stdout) > 2000:
        out["stdout"] = stdout[:2000] + f"\n... [truncated, full {len(stdout)} chars]"
    return out
