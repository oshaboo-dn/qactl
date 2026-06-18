"""Shared CLI plumbing: the global option block and the confirm gate.

Every qactl leaf subcommand inherits the same global options via an
argparse *parent* parser (:func:`global_parent`) so ``--json`` / ``--yes``
/ ``--timeout`` can appear after the subcommand:
``qactl jira whoami --json``.

:func:`confirm_or_exit` is the destructive-op guardrail that replaces the
MCP's central ``confirm=True``: off a TTY it refuses without ``--yes``
(non-zero exit); on a TTY it prompts. ``--yes`` suppresses the prompt.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable, Optional

from qactl.core.envelope import error_envelope
from qactl.core.output import emit


Handler = Callable[[argparse.Namespace], int]


def global_parent() -> argparse.ArgumentParser:
    """Parent parser carrying the global options shared by every command."""
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("global options")
    g.add_argument("--json", action="store_true", dest="json",
                   help="Emit the raw result envelope as JSON (pipe to jq).")
    g.add_argument("--yes", action="store_true",
                   help="Skip the confirmation prompt on destructive ops.")
    g.add_argument("--timeout", type=float, default=None, metavar="SECONDS",
                   help="Override the HTTP timeout for this command.")
    return p


def confirm_or_exit(args: argparse.Namespace, *, kind: str, action: str) -> Optional[int]:
    """Gate a destructive operation.

    Returns ``None`` to proceed; otherwise emits a refusal/abort envelope
    and returns its (non-zero) exit code, which the caller should return.

    Rules:
      - ``--yes`` → proceed silently.
      - no TTY    → refuse (never block a pipeline waiting for input).
      - TTY       → prompt; anything but y/yes aborts.
    """
    if getattr(args, "yes", False):
        return None
    interactive = sys.stdin.isatty() and sys.stderr.isatty()
    if not interactive:
        env = error_envelope(
            f"Refusing destructive operation without --yes: {action}",
            kind=kind, status="confirmation_required",
            next_actions=["Re-run with --yes to proceed."],
        )
        return emit(env, as_json=getattr(args, "json", False))
    try:
        resp = input(f"{action}\nProceed? [y/N] ")
    except EOFError:
        resp = ""
    if resp.strip().lower() in ("y", "yes"):
        return None
    env = error_envelope(f"Aborted by user: {action}", kind=kind, status="aborted")
    return emit(env, as_json=getattr(args, "json", False))


def resolve_timeout(args: argparse.Namespace, default: float) -> float:
    t = getattr(args, "timeout", None)
    return float(t) if t is not None else float(default)
