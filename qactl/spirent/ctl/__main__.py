"""``qactl.spirent.ctl`` entry point — builds the argparse tree and dispatches.

Sibling of ``qactl.ixia.ctl.__main__``. Every leaf subcommand inherits the
global option block via the shared parent parser, so global flags can appear
after the subcommand. The session reattach policy is recorded once from those
globals before the handler runs.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from qactl.spirent.ctl import __version__
from qactl.spirent.ctl.cli import session_cmds
from qactl.spirent.ctl.cli.common import apply_session_policy, global_parent


def build_parser(prog: str = "qactl spirent") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Command-line control for a Spirent TestCenter REST "
                    "server (reattach-aware; pairs with qactl).",
    )
    parser.add_argument(
        "--version", action="version", version=f"qactl.spirent.ctl {__version__}",
    )
    sub = parser.add_subparsers(dest="group", required=True)
    parent = global_parent()
    session_cmds.register(sub, parent)
    return parser


def main(argv: Optional[List[str]] = None, prog: str = "qactl spirent") -> int:
    parser = build_parser(prog)
    args = parser.parse_args(argv)

    rc = apply_session_policy(args)
    if rc is not None:
        return rc

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as e:  # last-resort: never dump a raw traceback at a user
        from qactl.spirent.core.envelope import error_envelope
        from qactl.spirent.ctl.core.output import emit
        env = error_envelope(
            f"{type(e).__name__}: {str(e)[:240]}",
            kind=getattr(args, "group", "spirent") or "spirent",
            host=getattr(args, "host", None),
            port=getattr(args, "port", None),
        )
        return emit(env, as_json=getattr(args, "json", False))


if __name__ == "__main__":
    raise SystemExit(main())
