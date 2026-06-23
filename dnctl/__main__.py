"""dnctl — one agent-shaped CLI over DriveNets DNOS devices.

Root Typer app wiring the four subcommand groups:

    dnctl cli  ...   SSH→DNOS CLI          (from user-cli-mcp)
    dnctl nc   ...   NETCONF               (from user-netconf-mcp)
    dnctl gnmi ...   gNMI                  (from user-gnmi-mcp)
    dnctl rc   ...   RESTCONF              (from user-restconf-mcp)

Every subcommand defaults to readable text and supports ``--json`` for
the exact structured payload (jq-friendly). Destructive subcommands
require ``--yes``. See ``dnctl <group> --help`` for the full surface.
"""

from __future__ import annotations

import os
import sys

import typer

from dnctl import __version__
from dnctl.cli.app import app as cli_app
from dnctl.core.setup_cmd import setup as _setup_cmd
from dnctl.gnmi.app import app as gnmi_app
from dnctl.nc.app import app as nc_app
from dnctl.rc.app import app as rc_app

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "dnctl — one agent-shaped CLI over DriveNets DNOS devices. "
        "Groups: cli (SSH→DNOS CLI), nc (NETCONF), gnmi (gNMI), rc (RESTCONF). "
        "Every command supports --json (exact structured payload); destructive "
        "ones require --yes."
    ),
    rich_markup_mode=None,
)

app.add_typer(cli_app, name="cli")
app.add_typer(nc_app, name="nc")
app.add_typer(gnmi_app, name="gnmi")
app.add_typer(rc_app, name="rc")
app.command("setup")(_setup_cmd)


def _version_cb(value: bool) -> None:
    if value:
        typer.echo(f"dnctl {__version__}")
        raise typer.Exit(0)


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version_cb, is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    """dnctl — collapse the four DNOS MCP servers into one CLI."""


def _warn_if_standalone() -> None:
    """Nudge direct callers toward the umbrella ``qactl`` CLI.

    When ``qactl`` delegates here it rewrites ``sys.argv[0]`` to ``qactl``,
    so a leftover standalone ``dnctl`` on PATH is the only thing that still
    presents as ``dnctl``. Warn on stderr only — stdout stays lossless so
    ``--json`` piping is unaffected.
    """
    invoked = os.path.basename(sys.argv[0] or "").removesuffix(".exe")
    if invoked == "dnctl":
        print(
            "dnctl: the standalone `dnctl` command is deprecated; "
            "use `qactl <cli|nc|gnmi|rc|setup> ...` instead.",
            file=sys.stderr,
        )


def main() -> None:
    _warn_if_standalone()
    app()


if __name__ == "__main__":
    main()
