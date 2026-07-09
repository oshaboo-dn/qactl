"""``qactl`` entry point — one CLI over every QA surface.

`qactl` is a thin front dispatcher that routes the first token to the
right domain:

    cli / nc / gnmi / rc / setup     -> vendored dnctl    (DNOS devices)
    ixia                             -> vendored ixiactl  (IxNetwork)
    jira / confluence / jenkins /
    arista                           -> native argparse   (Atlassian / Jenkins / EOS)

The DNOS and Ixia groups are delegated to the existing dnctl / ixiactl
entrypoints unchanged, so their full command surface, help, behaviour,
and tests carry over verbatim. The Atlassian/Jenkins groups are native
qactl argparse commands. Every group keeps the shared contract:
``--json`` everywhere, real exit codes, ``--yes`` on destructive ops.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from qactl import __version__
from qactl.core.common import global_parent
from qactl.arista import cli as arista_cli
from qactl.confluence import cli as confluence_cli
from qactl.jenkins import cli as jenkins_cli
from qactl.jira import cli as jira_cli


NATIVE_GROUPS = {"jira", "confluence", "jenkins", "arista"}
DNCTL_GROUPS = {"cli", "nc", "gnmi", "rc", "setup"}
IXIA_GROUP = "ixia"
MCP_GROUP = "mcp"


TOP_HELP = """\
usage: qactl <group> <command> [options]

One agent-shaped CLI for a QA workflow. Every command supports --json
(exact structured envelope), returns real exit codes, and gates
destructive operations behind --yes.

DNOS devices (delegated to dnctl):
  cli           SSH -> DNOS CLI (show/config/backup/recovery/...)
  nc            NETCONF
  gnmi          gNMI
  rc            RESTCONF
  setup         configure device registry / credentials

Traffic generation (delegated to ixiactl):
  ixia          IxNetwork sessions / topology / bgp / protocols / traffic

Atlassian + CI (native):
  jira          Jira watchers / attachments / comments / transitions / status
  confluence    Confluence comments / attachments
  jenkins       Jenkins builds: trigger / inspect / stop

Arista EOS switches (native, read-only over SSH):
  arista        interfaces / lldp / config / version

MCP front (same tools, over stdio):
  mcp           run a local stdio MCP server: `qactl mcp <group>` / `qactl mcp all`

Options:
  -h, --help     show this help
  -V, --version  show qactl version

Run `qactl <group> --help` for a group's commands
(e.g. `qactl cli --help`, `qactl ixia --help`, `qactl jenkins --help`).
Run `qactl mcp --help` for the MCP front (and `qactl mcp --list` for its tools).
"""


def build_native_parser() -> argparse.ArgumentParser:
    """argparse tree for the natively-implemented groups."""
    parser = argparse.ArgumentParser(
        prog="qactl",
        description="qactl native groups: jira, confluence, jenkins, arista.",
    )
    sub = parser.add_subparsers(dest="group", required=True)
    parent = global_parent()
    jira_cli.register(sub, parent)
    confluence_cli.register(sub, parent)
    jenkins_cli.register(sub, parent)
    arista_cli.register(sub, parent)
    return parser


def _run_native(argv: List[str]) -> int:
    parser = build_native_parser()
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


def _delegate_dnctl(argv: List[str]) -> int:
    """Invoke the vendored dnctl typer app with ``argv`` (group token first)."""
    from qactl.dnctl.__main__ import main as dnctl_main
    saved = sys.argv
    sys.argv = ["qactl"] + argv
    try:
        dnctl_main()
        return 0
    except SystemExit as e:
        if e.code is None:
            return 0
        return e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv = saved


def _delegate_ixia(argv: List[str]) -> int:
    """Invoke the vendored ixiactl argparse main with everything after ``ixia``."""
    from qactl.ixia.ctl.__main__ import main as ixia_main
    return int(ixia_main(argv[1:], prog="qactl ixia"))


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(TOP_HELP)
        return 0
    if argv[0] in ("-V", "--version", "--Version"):
        print(f"qactl {__version__}")
        return 0
    group = argv[0]
    if group == MCP_GROUP:
        from qactl.mcp.server import main as mcp_main
        return mcp_main(argv[1:])
    if group in NATIVE_GROUPS:
        return _run_native(argv)
    if group in DNCTL_GROUPS:
        return _delegate_dnctl(argv)
    if group == IXIA_GROUP:
        return _delegate_ixia(argv)
    print(f"qactl: unknown group {group!r}\n", file=sys.stderr)
    print(TOP_HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
