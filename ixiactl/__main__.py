"""``ixiactl`` entry point — builds the argparse tree and dispatches.

Every leaf subcommand inherits the global option block (``--host``,
``--port``, ``--user``, ``--session``, ``--new-session``, ``--timeout``,
``--json``, ``--yes``) via the shared parent parser, so the global flags
can appear after the subcommand. The session reattach policy is recorded
once from those globals before the handler runs.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from ixiactl import __version__
from ixiactl.cli import bfd, bgp, proto, rest, session_cmds, topo
from ixiactl.cli import traffic as traffic_cli
from ixiactl.cli.common import apply_session_policy, global_parent


def build_parser(prog: str = "ixiactl") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Command-line control for an IxNetwork REST API server "
                    "(reattach-aware; pairs with dnctl).",
    )
    parser.add_argument(
        "--version", action="version", version=f"ixiactl {__version__}",
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


def _warn_if_standalone() -> None:
    """Nudge direct callers toward the umbrella ``qactl`` CLI.

    ``qactl`` delegates here with ``prog="qactl ixia"`` and leaves
    ``sys.argv[0]`` as ``qactl``; only a leftover standalone ``ixiactl`` on
    PATH still presents as ``ixiactl``. Warn on stderr so stdout stays
    lossless for ``--json`` piping.
    """
    invoked = os.path.basename(sys.argv[0] or "").removesuffix(".exe")
    if invoked == "ixiactl":
        print(
            "ixiactl: the standalone `ixiactl` command is deprecated; "
            "use `qactl ixia ...` instead.",
            file=sys.stderr,
        )


def main(argv: Optional[List[str]] = None, prog: str = "ixiactl") -> int:
    _warn_if_standalone()
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
        from ixiactl.core.output import emit
        from ixia_core.envelope import error_envelope
        env = error_envelope(
            f"{type(e).__name__}: {str(e)[:240]}",
            kind=getattr(args, "group", "ixiactl") or "ixiactl",
            host=getattr(args, "host", None),
            port=getattr(args, "port", None),
            status="error",
        )
        return emit(env, as_json=getattr(args, "json", False))


if __name__ == "__main__":
    raise SystemExit(main())
