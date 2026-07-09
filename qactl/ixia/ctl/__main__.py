"""``qactl.ixia.ctl`` entry point — builds the argparse tree and dispatches.

Every leaf subcommand inherits the global option block (``--host``,
``--port``, ``--user``, ``--session``, ``--new-session``, ``--timeout``,
``--json``, ``--yes``) via the shared parent parser, so the global flags
can appear after the subcommand. The session reattach policy is recorded
once from those globals before the handler runs.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from qactl.ixia.ctl import __version__
from qactl.ixia.ctl.cli import bfd, bgp, proto, rest, session_cmds, topo
from qactl.ixia.ctl.cli import traffic as traffic_cli
from qactl.ixia.ctl.cli.common import apply_session_policy, global_parent


def build_parser(prog: str = "qactl ixia") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Command-line control for an IxNetwork REST API server "
                    "(reattach-aware; pairs with qactl).",
    )
    parser.add_argument(
        "--version", action="version", version=f"qactl.ixia.ctl {__version__}",
    )
    sub = parser.add_subparsers(dest="group", required=True)
    parent = global_parent()
    session_cmds.register(sub, parent)
    topo.register(sub, parent)
    bgp.register(sub, parent)
    bfd.register(sub, parent)
    proto.register(sub, parent)
    traffic_cli.register(sub, parent)
    rest.register(sub, parent)
    return parser


def main(argv: Optional[List[str]] = None, prog: str = "qactl ixia") -> int:
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
        from qactl.ixia.ctl.core.output import emit
        from qactl.ixia.core.envelope import error_envelope
        env = error_envelope(
            f"{type(e).__name__}: {str(e)[:240]}",
            kind=getattr(args, "group", "ixia") or "ixia",
            host=getattr(args, "host", None),
            port=getattr(args, "port", None),
            status="error",
        )
        return emit(env, as_json=getattr(args, "json", False))


if __name__ == "__main__":
    raise SystemExit(main())
