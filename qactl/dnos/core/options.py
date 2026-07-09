"""Reusable Typer option aliases + the call/emit glue.

Global flags apply to *every* subcommand and are accepted **after** the
leaf command (e.g. ``qactl cli system -d sa --json``), so they are
declared on each leaf via these ``Annotated`` aliases rather than on a
parent callback. :func:`build_ctx` packs them into a :class:`Ctx`;
:func:`call` invokes the lifted tool function with only the kwargs it
actually accepts (dropping ``None`` so each tool's own defaults win);
:func:`finish` renders the envelope and exits with the right code.
"""

from __future__ import annotations

import inspect
import json as _json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Optional

import typer

from qactl.dnos.core import output
from qactl.dnos.core.context import Ctx

# --- global flag aliases ---------------------------------------------------

Device = Annotated[Optional[str], typer.Option("--device", "-d", help="Device alias from the registry.")]
Host = Annotated[Optional[str], typer.Option("--host", help="Override mgmt IP/host (skip alias resolution).")]
User = Annotated[Optional[str], typer.Option("--user", help="Login user (default: dnroot).")]
Password = Annotated[Optional[str], typer.Option("--password", help="Login password (default: dnroot).")]
Port = Annotated[Optional[int], typer.Option("--port", help="Protocol port (NETCONF 830, gNMI 50051, ...).")]
Timeout = Annotated[Optional[int], typer.Option("--timeout", help="Per-call timeout in seconds.")]
NoVerify = Annotated[bool, typer.Option("--no-verify/--verify", help="Skip TLS/host-key verification (default: on).")]
NoVerifyMgmt0 = Annotated[bool, typer.Option("--no-verify-mgmt0/--verify-mgmt0", help="Skip the live mgmt0 pre-check against the chassis and use the cached address as-is (default: verify; an unverifiable mgmt0 refuses the session).")]
Json = Annotated[bool, typer.Option("--json", help="Emit the raw structured payload (jq-friendly).")]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Confirm a destructive op; required when non-interactive.")]
Log = Annotated[Optional[str], typer.Option("--log", help="Append the full raw command output (with a timestamp/device/cmd header) to this file, in addition to normal output. Append mode — repeated calls accumulate; usable as standalone QA evidence.")]


def build_ctx(
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    port: Optional[int] = None,
    timeout: Optional[int] = None,
    no_verify: bool = True,
    as_json: bool = False,
    yes: bool = False,
    log: Optional[str] = None,
    no_verify_mgmt0: bool = False,
) -> Ctx:
    return Ctx(
        device=device, host=host, user=user, password=password,
        port=port, timeout=timeout, no_verify=no_verify,
        no_verify_mgmt0=no_verify_mgmt0,
        json=as_json, yes=yes, log=log,
    )


def _canonical_device(device: Optional[str]) -> Optional[str]:
    """Resolve a ``-d`` value (canonical key or secondary alias) to the
    canonical device name, so every downstream tool, SSH-host lookup and
    backup folder is keyed by the one true name. Unknown names pass
    through untouched (the tool then surfaces the usual "unknown device"
    error).
    """
    if not device:
        return device
    from qactl.dnos.core import devices as _devices

    return _devices.resolve_canonical(device) or device


def call(fn: Callable[..., Any], c: Ctx, **extra: Any) -> Any:
    """Invoke a lifted tool function with connection + command kwargs.

    The connection flags are offered under every name the tools use
    (``timeout`` and ``timeout_s``); :func:`call` keeps only the kwargs
    in ``fn``'s signature and drops ``None`` so each tool's own default
    applies untouched. ``extra`` carries the command-specific args.
    """
    conn = {
        "device": _canonical_device(c.device),
        "host": c.host,
        "user": c.user,
        "password": c.password,
        "port": c.port,
        "timeout": c.timeout,
        "timeout_s": c.timeout,
        "no_verify": c.no_verify,
        "verify_mgmt0": not c.no_verify_mgmt0,
    }
    merged = {**conn, **extra}
    params = inspect.signature(fn).parameters
    # A tool that declares **kwargs accepts anything, so forward every
    # non-None key; otherwise keep only the kwargs it names. (Dropping None
    # everywhere lets each tool's own defaults win.)
    accepts_var_kw = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    kwargs = {
        k: v for k, v in merged.items()
        if v is not None and (accepts_var_kw or k in params)
    }
    return fn(**kwargs)


def _summarize_paths(paths: Any) -> str:
    """First path plus a count, so multi-path ops stay one-line labels."""
    if not isinstance(paths, list) or not paths:
        return ""
    first = str(paths[0])
    return first if len(paths) == 1 else f"{first} (+{len(paths) - 1} more)"


def _derive_command(env: dict) -> str:
    """Best-effort operation label for envelopes without a raw ``command``.

    The nc / gnmi / rc envelopes never carry a device CLI command string,
    so entries used to land as ``cmd=''``. Each family is recognised by
    structural keys its envelope builder always emits (rc:
    ``endpoint``+``base_url``; gnmi: ``tls_mode``; nc: ``action``+
    ``session_id``) and labelled with the operation and its target path.
    Unrecognised shapes return ''.
    """
    kind = env.get("kind") or ""
    request = env.get("request") if isinstance(env.get("request"), dict) else {}
    if "endpoint" in env and "base_url" in env:
        target = request.get("url") or ""
        if not target and isinstance(request.get("segments"), list):
            target = "/" + "/".join(str(s) for s in request["segments"])
        return " ".join(p for p in ("rc", kind, target) if p)
    if "tls_mode" in env:
        if kind == "set":
            paths = [
                e.get("path")
                for key in ("update", "replace")
                for e in (request.get(key) or [])
                if isinstance(e, dict) and e.get("path")
            ]
            paths += [p for p in (request.get("delete") or []) if p]
            target = _summarize_paths(paths)
        else:
            target = (
                request.get("path")
                or request.get("list_path")
                or _summarize_paths(request.get("paths"))
            )
        return " ".join(p for p in ("gnmi", kind, target) if p)
    if "action" in env and "session_id" in env:
        label = f"nc {env['action']}"
        if kind and kind != env["action"]:
            label += f" ({kind})"
        op = env.get("op")
        if op and op != "merge":
            label += f" op={op}"
        return label
    return ""


def _fallback_body(env: dict) -> str:
    """Lossless JSON dump of the envelope, for tools with no raw text
    payload (gnmi / rc / nc edits) — the request and response are the
    evidence, so persist all of it."""
    try:
        return _json.dumps(env, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _evidence_fields(result: Any, c: Ctx) -> tuple[str, str, str, str]:
    """Pull the (device, command, status, body) we record for a call.

    ``body`` is the verbatim raw text payload (``stdout``, or
    ``result_xml`` for config reads) regardless of ``--json``; envelopes
    with no raw text payload (gnmi / rc / nc edits) are dumped whole as
    JSON so the request + response survive as evidence. ``command`` falls
    back to a derived operation label (see :func:`_derive_command`); the
    rest fall back to the context when the envelope omits them.
    """
    body = ""
    device = c.device or c.host or ""
    command = ""
    status = ""
    if isinstance(result, dict):
        for key in ("stdout", "result_xml"):
            v = result.get(key)
            if isinstance(v, str) and v:
                body = v
                break
        if not body:
            body = _fallback_body(result)
        device = result.get("device") or result.get("host") or device
        command = result.get("command") or _derive_command(result)
        status = str(result.get("status") or "")
    return device, command, status, body


def _evidence_chunk(device: str, command: str, body: str, status: str = "") -> str:
    """A self-describing header + fenced verbatim body, append-ready.

    Header: ``# ===== <ISO-ts> | device=<dev> | cmd=<cmd> [| status=..] =====``.
    The body is wrapped in a code fence so it renders as a block in
    markdown; a longer fence is used when the body itself contains a
    backtick run, so it can't terminate the block prematurely.
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = f"# ===== {ts} | device={device} | cmd={command!r}"
    if status:
        header += f" | status={status}"
    header += " ====="
    longest_run = max((len(m) for m in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest_run + 1)
    chunk = f"{header}\n\n{fence}\n{body}"
    if not chunk.endswith("\n"):
        chunk += "\n"
    chunk += f"{fence}\n"
    return chunk


def _safe_name(name: str) -> str:
    """Filesystem-safe device key for the journal directory."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "_"


def _journal_dir() -> Path:
    """Root of the always-on per-device journal (override with QACTL_DEVICE_LOG_DIR)."""
    root = os.environ.get("QACTL_DEVICE_LOG_DIR") or str(Path.home() / ".qactl" / "device-logs")
    return Path(root)


def _append_log(result: Any, c: Ctx) -> None:
    """Append the full raw device output to ``c.log`` (the ``--log`` file).

    Tee-like evidence capture, in append mode so repeated calls
    accumulate; usable as standalone QA evidence. A logging failure never
    fails the command — it is downgraded to a warning on the envelope.
    """
    path = c.log
    if not path:
        return

    device, command, _status, body = _evidence_fields(result, c)
    chunk = _evidence_chunk(device, command, body)

    try:
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(chunk)
    except OSError as exc:
        if isinstance(result, dict):
            result.setdefault("warnings", []).append(
                f"could not append --log to {path!r}: {exc}"
            )


def _append_journal(result: Any, c: Ctx) -> None:
    """Always-on per-device daily journal: a full raw record of every
    device command, keyed by device under ``<root>/<device>/<YYYY-MM-DD>.md``.

    Unlike ``--log`` (opt-in, you pick the file), this captures *all*
    work against a device without anyone remembering a flag. It only
    fires when a device can be identified (skips local/registry-only
    commands) and degrades silently on write failure — the journal must
    never break a command.
    """
    device, command, status, body = _evidence_fields(result, c)
    if not device:
        return

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    chunk = _evidence_chunk(device, command, body, status=status)
    try:
        dir_ = _journal_dir() / _safe_name(device)
        dir_.mkdir(parents=True, exist_ok=True)
        with (dir_ / f"{day}.md").open("a", encoding="utf-8") as fh:
            fh.write(chunk)
    except OSError:
        # The journal is best-effort; never let it fail a command.
        pass


def finish(result: Any, c: Ctx) -> None:
    """Render ``result`` per ``--json`` and exit with its status code."""
    _append_log(result, c)
    _append_journal(result, c)
    raise typer.Exit(output.emit(result, as_json=c.json))


def read_body(positional: Optional[str], file: Optional[str], c: Ctx, *, required: bool = True) -> Optional[str]:
    """Resolve a payload (inline / ``--file`` / stdin ``-``).

    On failure, emit a clean error envelope and exit instead of raising
    an uncaught traceback.
    """
    from qactl.dnos.core.payload import PayloadError, resolve_body

    try:
        return resolve_body(positional, file, required=required)
    except (PayloadError, OSError) as exc:
        finish({"status": "error", "errors": [str(exc)]}, c)
        return None  # unreachable: finish raises


def rc_payload(body: str) -> Any:
    """Coerce a raw RESTCONF body into what the write tools expect.

    XML (leading ``<``) is passed through as a string; otherwise we try
    to parse JSON into an object (so it isn't double-encoded), falling
    back to the raw string.
    """
    s = body.strip()
    if s.startswith("<"):
        return body
    try:
        return _json.loads(body)
    except ValueError:
        return body
