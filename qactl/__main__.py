"""``qactl`` entry point — builds the argparse tree and dispatches.

Each domain registers its own subcommand group (``jira`` / ``confluence``
/ ``jenkins``); every leaf inherits the global option block (``--json``,
``--yes``, ``--timeout``) via the shared parent parser so those flags can
appear after the subcommand.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from qactl import __version__
from qactl.core.common import global_parent
from qactl.confluence import cli as confluence_cli
from qactl.jenkins import cli as jenkins_cli
from qactl.jira import cli as jira_cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qactl",
        description="One agent-shaped CLI for Jira, Confluence, and Jenkins.",
    )
    parser.add_argument("--version", action="version", version=f"qactl {__version__}")
    sub = parser.add_subparsers(dest="group", required=True)
    parent = global_parent()
    jira_cli.register(sub, parent)
    confluence_cli.register(sub, parent)
    jenkins_cli.register(sub, parent)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as e:  # last resort — never dump a raw traceback at a user
        from qactl.core.envelope import error_envelope
        from qactl.core.output import emit
        env = error_envelope(
            f"{type(e).__name__}: {str(e)[:240]}",
            kind=getattr(args, "group", "qactl") or "qactl",
        )
        return emit(env, as_json=getattr(args, "json", False))


if __name__ == "__main__":
    raise SystemExit(main())
