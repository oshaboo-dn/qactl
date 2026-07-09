"""Confirm gate for destructive / mutating subcommands.

The MCP layer's central guardrail is gone, so every mutating command
re-implements the gate here:

* ``--yes`` always proceeds (fully suppresses the prompt; this is how an
  agent runs non-interactively).
* No ``--yes`` + a TTY → interactive ``[y/N]`` prompt.
* No ``--yes`` + non-TTY (piped / CI / agent) → **refuse**, never block.

:func:`ensure` returns ``True`` to proceed; on refusal it prints a
machine-stable error envelope (text or ``--json``) and returns ``False``
so the caller can ``raise typer.Exit(2)``.
"""

from __future__ import annotations

import sys

from qactl.dnos.core import output


REFUSAL_EXIT = 2


def ensure(action: str, *, yes: bool, as_json: bool) -> bool:
    """Gate a destructive op. Returns True to proceed, False to abort."""
    if yes:
        return True

    if sys.stdin.isatty() and sys.stderr.isatty():
        # Prompt on stderr so a piped ``--json`` still surfaces it without
        # polluting stdout (and so we never block on a swallowed prompt when
        # stdout is redirected). Keys interactivity on stdin+stderr to match
        # the native qactl / qactl.ixia.ctl gates.
        sys.stderr.write(f"About to: {action}\nProceed? [y/N] ")
        sys.stderr.flush()
        try:
            resp = input()
        except (EOFError, KeyboardInterrupt):
            resp = ""
        if resp.strip().lower() in ("y", "yes"):
            return True
        _emit_refusal(action, as_json, reason="declined at prompt")
        return False

    _emit_refusal(action, as_json, reason="non-interactive; pass --yes to proceed")
    return False


def _emit_refusal(action: str, as_json: bool, *, reason: str) -> None:
    payload = {
        "status": "error",
        "errors": [f"refused destructive op ({reason}): {action}"],
        "next_actions": ["re-run with --yes to confirm"],
    }
    output.emit(payload, as_json=as_json)
