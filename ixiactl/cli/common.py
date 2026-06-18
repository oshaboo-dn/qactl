"""Shared CLI plumbing: global options, the confirm gate, arg parsers.

Every ``ixiactl`` subcommand inherits the same global option block via an
argparse *parent* parser (:func:`global_parent`). argparse attaches those
options to each leaf subparser, which is exactly what lets the global
flags appear **after** the subcommand on the command line —
``ixiactl topo list --host X --json`` — the way the acceptance smoke test
expects.

The destructive-op confirm gate (:func:`confirm_or_exit`) replaces the
MCP's central ``confirm=True`` guardrail: off a TTY it refuses without
``--yes`` (non-zero exit); on a TTY it prompts. ``--yes`` suppresses the
prompt entirely.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional

from ixia_core.envelope import error_envelope
from ixia_core.session import DEFAULT_PORT, DEFAULT_USER, set_session_policy

from ixiactl.core.output import emit


# Handler signature: takes parsed args, returns a process exit code.
Handler = Callable[[argparse.Namespace], int]


def _env_host() -> Optional[str]:
    """Default target host for this client, from ``IXIA_HOST``.

    ``ixiactl`` is a general program, but each install ("client") drives
    one user against one API server. Rather than bake a site-specific host
    into the source, the per-client default lives in the environment:
    ``export IXIA_HOST=<your-host>`` makes ``--host`` optional for that
    client. An explicit ``--host`` always wins.
    """
    h = os.environ.get("IXIA_HOST")
    return h.strip() or None if h else None


def _env_user() -> str:
    return os.environ.get("IXIA_USER") or DEFAULT_USER


def _env_port() -> int:
    raw = os.environ.get("IXIA_PORT")
    try:
        return int(raw) if raw else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def global_parent() -> argparse.ArgumentParser:
    """Parent parser carrying the global options shared by every command."""
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("global options")
    g.add_argument("--host", default=_env_host(),
                   help="IxNetwork REST API server hostname or IP "
                        "(default: $IXIA_HOST).")
    g.add_argument("--port", type=int, default=_env_port(),
                   help=f"REST port (default {DEFAULT_PORT}, or $IXIA_PORT).")
    g.add_argument("--user", default=_env_user(),
                   help=f"API username (default {DEFAULT_USER!r}, or "
                        "$IXIA_USER).")
    g.add_argument("--password", default=None,
                   help="API password for auth-required servers "
                        "(default: $IXIA_PASSWORD). Windows 11009 API "
                        "servers usually need none.")
    g.add_argument("--api-key", default=None, dest="api_key", metavar="KEY",
                   help="Pre-issued IxNetwork apiKey, used instead of a "
                        "password (default: $IXIA_API_KEY).")
    g.add_argument("--session", type=int, default=None, metavar="ID",
                   help="Attach to this IxNetwork session id instead of "
                        "reattaching to the most recent one.")
    g.add_argument("--new-session", action="store_true",
                   help="Force a fresh IxNetwork session (Linux servers "
                        "only; Windows servers share one session).")
    g.add_argument("--timeout", type=int, default=None, metavar="SECONDS",
                   help="Override the primary timeout for commands that "
                        "have one (stats, apply, vport wait).")
    g.add_argument("--json", action="store_true", dest="json",
                   help="Emit the raw result envelope as JSON (pipe to jq).")
    g.add_argument("--yes", action="store_true",
                   help="Skip the confirmation prompt on destructive ops.")
    return p


def apply_session_policy(args: argparse.Namespace) -> Optional[int]:
    """Validate globals and record the reattach + credential choice.

    Returns an exit code on a bad-argument condition (missing host, or
    ``--session`` / ``--new-session`` conflict), else ``None``.
    """
    if not args.host:
        env = error_envelope(
            "No host: pass --host or set IXIA_HOST.",
            kind="bad_argument", host=None, port=args.port,
            status="bad_argument",
            next_actions=["export IXIA_HOST=<your-host>",
                          "or pass --host <your-host> on the command line."],
        )
        return emit(env, as_json=args.json)
    if args.session is not None and args.new_session:
        env = error_envelope(
            "--session and --new-session are mutually exclusive.",
            kind="bad_argument", host=args.host, port=args.port,
            status="bad_argument",
        )
        return emit(env, as_json=args.json)
    password = (
        args.password if args.password is not None
        else os.environ.get("IXIA_PASSWORD")
    )
    api_key = (
        args.api_key if args.api_key is not None
        else os.environ.get("IXIA_API_KEY")
    )
    set_session_policy(
        session_id=args.session, new_session=args.new_session,
        password=password, api_key=api_key,
    )
    return None


def confirm_or_exit(
    args: argparse.Namespace, *, kind: str, action: str
) -> Optional[int]:
    """Gate a destructive operation.

    Returns ``None`` to proceed. Otherwise emits a refusal/abort envelope
    and returns its (non-zero) exit code, which the caller should return.

    Rules:
      - ``--yes`` → proceed silently.
      - no TTY    → refuse (never block a pipeline on input).
      - TTY       → prompt; anything but y/yes aborts.
    """
    if args.yes:
        return None
    interactive = sys.stdin.isatty() and sys.stderr.isatty()
    if not interactive:
        env = error_envelope(
            f"Refusing destructive operation without --yes: {action}",
            kind=kind, host=args.host, port=args.port,
            status="confirmation_required",
            next_actions=["Re-run with --yes to proceed."],
        )
        return emit(env, as_json=args.json)
    try:
        resp = input(f"{action}\nProceed? [y/N] ")
    except EOFError:
        resp = ""
    if resp.strip().lower() in ("y", "yes"):
        return None
    env = error_envelope(
        f"Aborted by user: {action}",
        kind=kind, host=args.host, port=args.port, status="aborted",
    )
    return emit(env, as_json=args.json)


# --------------------------------------------------------------------------
# Small value parsers used by several command groups
# --------------------------------------------------------------------------

def parse_bool(value: str) -> bool:
    v = str(value).strip().lower()
    if v in ("true", "1", "yes", "on", "y"):
        return True
    if v in ("false", "0", "no", "off", "n"):
        return False
    raise ValueError(f"expected a boolean, got {value!r}")


def parse_capabilities(pairs: Optional[List[str]]) -> Dict[str, bool]:
    """Turn ``["ipv4_mpls=true", "evpn=false"]`` into ``{label: bool}``."""
    out: Dict[str, bool] = {}
    for item in pairs or []:
        if "=" not in item:
            raise ValueError(
                f"capability {item!r} must be of the form label=true|false"
            )
        key, _, val = item.partition("=")
        out[key.strip()] = parse_bool(val)
    return out


def parse_rt(item: str) -> Any:
    """An RT entry is either ``"asn:assigned"`` or a JSON object string."""
    s = item.strip()
    if s.startswith("{"):
        return json.loads(s)
    return s


def parse_lines(lines: Optional[str], line: Optional[List[int]]) -> Any:
    """Build the ``lines`` selector for route actions.

    Precedence: repeated ``--line N`` flags win and yield a list. Else a
    ``--lines`` value of ``all`` (default) stays ``"all"``; a
    comma-separated value becomes a list of ints; a bare int stays int.
    """
    if line:
        return list(line)
    if lines is None or lines.strip().lower() == "all":
        return "all"
    parts = [p.strip() for p in lines.split(",") if p.strip()]
    ints = [int(p) for p in parts]
    return ints[0] if len(ints) == 1 else ints


def primary_timeout(args: argparse.Namespace, default: int) -> int:
    """Resolve a command's primary timeout: ``--timeout`` if set, else default."""
    return int(args.timeout) if args.timeout is not None else int(default)


def name_or_index(value: Optional[str]) -> Any:
    """Coerce a ``str | int`` selector the way the MCP tools expect.

    The NGPF resolvers treat an ``int`` as a 1-based index and a ``str``
    as an exact name. The CLI only has strings, so a bare-digit value
    (``--device-group 1``) becomes the integer index 1 — matching the
    MCP defaults the agent already knows — while any non-numeric value
    stays a name.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value)
    return int(s) if s.isdigit() else s
