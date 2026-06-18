"""Reusable Typer option aliases + the call/emit glue.

Global flags apply to *every* subcommand and are accepted **after** the
leaf command (e.g. ``dnctl cli system -d sa --json``), so they are
declared on each leaf via these ``Annotated`` aliases rather than on a
parent callback. :func:`build_ctx` packs them into a :class:`Ctx`;
:func:`call` invokes the lifted tool function with only the kwargs it
actually accepts (dropping ``None`` so each tool's own defaults win);
:func:`finish` renders the envelope and exits with the right code.
"""

from __future__ import annotations

import inspect
import json as _json
from typing import Annotated, Any, Callable, Optional

import typer

from dnctl.core import output
from dnctl.core.context import Ctx

# --- global flag aliases ---------------------------------------------------

Device = Annotated[Optional[str], typer.Option("--device", "-d", help="Device alias from the registry.")]
Host = Annotated[Optional[str], typer.Option("--host", help="Override mgmt IP/host (skip alias resolution).")]
User = Annotated[Optional[str], typer.Option("--user", help="Login user (default: dnroot).")]
Password = Annotated[Optional[str], typer.Option("--password", help="Login password (default: dnroot).")]
Port = Annotated[Optional[int], typer.Option("--port", help="Protocol port (NETCONF 830, gNMI 50051, ...).")]
Timeout = Annotated[Optional[int], typer.Option("--timeout", help="Per-call timeout in seconds.")]
NoVerify = Annotated[bool, typer.Option("--no-verify/--verify", help="Skip TLS/host-key verification (default: on).")]
Json = Annotated[bool, typer.Option("--json", help="Emit the raw structured payload (jq-friendly).")]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Confirm a destructive op; required when non-interactive.")]


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
) -> Ctx:
    return Ctx(
        device=device, host=host, user=user, password=password,
        port=port, timeout=timeout, no_verify=no_verify,
        json=as_json, yes=yes,
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
    from dnctl.core import devices as _devices

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
    }
    merged = {**conn, **extra}
    params = inspect.signature(fn).parameters
    kwargs = {k: v for k, v in merged.items() if k in params and v is not None}
    return fn(**kwargs)


def finish(result: Any, c: Ctx) -> None:
    """Render ``result`` per ``--json`` and exit with its status code."""
    raise typer.Exit(output.emit(result, as_json=c.json))


def read_body(positional: Optional[str], file: Optional[str], c: Ctx, *, required: bool = True) -> Optional[str]:
    """Resolve a payload (inline / ``--file`` / stdin ``-``).

    On failure, emit a clean error envelope and exit instead of raising
    an uncaught traceback.
    """
    from dnctl.core.payload import PayloadError, resolve_body

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
