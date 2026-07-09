"""Launch a stdio FastMCP server exposing one or more qactl groups.

Entry point for ``qactl mcp <group> [<group> ...]`` (and ``qactl mcp all``).
The server speaks MCP JSON-RPC over **stdio** -- it runs locally, in the
same place as the agent, so tools that touch local files behave exactly as
they do under the CLI. Credentials resolve from the environment
(``ATLASSIAN_*`` / ``JENKINS_*`` / device creds via ``qactl setup``), not
from request headers; there is no HTTP listener and no systemd unit.
"""

from __future__ import annotations

import json
import sys
from typing import List, Optional, Sequence

from qactl.mcp.registry import ALL_GROUPS, list_group_tools, register_group


USAGE = """\
usage: qactl mcp <group> [<group> ...]

Launch a local stdio MCP server exposing the selected groups' tools.

Groups:
  jira confluence jenkins        Atlassian + CI (native)
  cli nc gnmi rc                 DNOS devices (vendored qactl)
  ixia                           IxNetwork traffic (vendored qactl.ixia.ctl)
  all                            every group above

Notes:
  - Transport is stdio: register in mcp.json with
      {"command": "qactl", "args": ["mcp", "<group>"]}
  - Heavy dnftp / large-artifact tools (device + netconf backups,
    tech-support, tar-load, scale-deploy) and `setup` stay CLI-only.
  - Credentials come from the environment, not request headers.

Options:
  --list        print each group's exposed MCP tools as JSON and exit
  -h, --help    show this help
"""


def _resolve_groups(tokens: Sequence[str]) -> List[str]:
    """Expand/validate group tokens; ``all`` expands to every group."""
    if any(t == "all" for t in tokens):
        return list(ALL_GROUPS)
    out: List[str] = []
    for t in tokens:
        if t not in ALL_GROUPS:
            raise ValueError(f"unknown MCP group {t!r}; choose from {', '.join(ALL_GROUPS)}, all")
        if t not in out:
            out.append(t)
    return out


def build_server(groups: Sequence[str], *, name: Optional[str] = None, log: bool = True):
    """Build a FastMCP server with ``groups`` registered (stdio transport)."""
    from mcp.server.fastmcp import FastMCP

    groups = list(groups)
    server_name = name or ("qactl-" + "-".join(groups) if groups else "qactl")
    mcp = FastMCP(server_name)

    wrap = None
    if log:
        from qactl.core.request_log import RequestLogger, default_log_dir
        logger = RequestLogger(default_log_dir("-".join(groups) or "all"))
        wrap = lambda fn: logger.log_mcp_call(fn.__name__)(fn)  # noqa: E731

    for group in groups:
        register_group(group, mcp, wrap=wrap)
    return mcp


def main(argv: Optional[List[str]] = None) -> int:
    """``qactl mcp ...`` entry point."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if argv[0] == "--list":
        tokens = argv[1:] or list(ALL_GROUPS)
        try:
            groups = _resolve_groups(tokens)
        except ValueError as e:
            print(f"qactl mcp: {e}", file=sys.stderr)
            return 2
        print(json.dumps({g: list_group_tools(g) for g in groups}, indent=2))
        return 0

    try:
        groups = _resolve_groups(argv)
    except ValueError as e:
        print(f"qactl mcp: {e}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

    # Serve over stdio. Nothing may write to stdout except the MCP protocol,
    # so diagnostics go to stderr.
    print(f"qactl mcp: serving groups [{', '.join(groups)}] over stdio", file=sys.stderr)
    server = build_server(groups)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
