"""Shared CLI plumbing for ``qactl spirent``: global options + confirm gate.

Mirrors ``qactl.ixia.ctl.cli.common``. Every subcommand inherits the global
option block via an argparse parent parser, so the global flags may appear
after the subcommand (``qactl spirent session connect --host X --json``).
The per-install target lives in ``$SPIRENT_HOST`` so ``--host`` is optional.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, Optional

from qactl.spirent.core.envelope import error_envelope
from qactl.spirent.core.session import DEFAULT_PORT, DEFAULT_USER, set_session_policy
from qactl.spirent.ctl.core.output import emit


Handler = Callable[[argparse.Namespace], int]


def _env_host() -> Optional[str]:
    h = os.environ.get("SPIRENT_HOST")
    return (h.strip() or None) if h else None


def _env_user() -> str:
    return os.environ.get("SPIRENT_USER") or DEFAULT_USER


def _env_port() -> int:
    raw = os.environ.get("SPIRENT_PORT")
    try:
        return int(raw) if raw else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def global_parent() -> argparse.ArgumentParser:
    """Parent parser carrying the global options shared by every command."""
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("global options")
    g.add_argument("--host", default=_env_host(),
                   help="Spirent TestCenter REST server hostname or IP "
                        "(default: $SPIRENT_HOST).")
    g.add_argument("--port", type=int, default=_env_port(),
                   help=f"STC REST port (default {DEFAULT_PORT}, or $SPIRENT_PORT).")
    g.add_argument("--user", default=_env_user(),
                   help=f"session user (default {DEFAULT_USER!r}, or $SPIRENT_USER).")
    g.add_argument("--password", default=None,
                   help="password for auth-required REST servers "
                        "(default: $SPIRENT_PASSWORD; usually none).")
    g.add_argument("--session", default=None, metavar="NAME",
                   help="STC session name to attach to instead of the default "
                        "(default: $SPIRENT_SESSION or 'qactl-session').")
    g.add_argument("--new-session", action="store_true",
                   help="Force a fresh STC session instead of reattaching.")
    g.add_argument("--timeout", type=int, default=None, metavar="SECONDS",
                   help="Override the REST client timeout.")
    g.add_argument("--json", action="store_true", dest="json",
                   help="Emit the raw result envelope as JSON (pipe to jq).")
    g.add_argument("--yes", action="store_true",
                   help="Skip the confirmation prompt on destructive ops.")
    return p


def apply_session_policy(args: argparse.Namespace) -> Optional[int]:
    """Validate globals and record the reattach + credential choice.

    Returns an exit code on a bad-argument condition (missing host), else
    ``None``.
    """
    if not args.host:
        env = error_envelope(
            "No host: pass --host or set SPIRENT_HOST.",
            kind="bad_argument", host=None, port=args.port,
            status="bad_argument",
            next_actions=["export SPIRENT_HOST=<your-stc-rest-server>",
                          "or pass --host <server> on the command line."],
        )
        return emit(env, as_json=args.json)
    password = (
        args.password if args.password is not None
        else os.environ.get("SPIRENT_PASSWORD")
    )
    set_session_policy(
        session_name=args.session, new_session=args.new_session,
        password=password, timeout=args.timeout,
    )
    return None


def confirm_or_exit(
    args: argparse.Namespace, *, kind: str, action: str
) -> Optional[int]:
    """Gate a destructive operation (``--yes`` / TTY prompt / off-TTY refuse)."""
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
    sys.stderr.write(f"{action}\nProceed? [y/N] ")
    sys.stderr.flush()
    try:
        resp = input()
    except EOFError:
        resp = ""
    if resp.strip().lower() in ("y", "yes"):
        return None
    env = error_envelope(
        f"Aborted by user: {action}",
        kind=kind, host=args.host, port=args.port, status="aborted",
    )
    return emit(env, as_json=args.json)
