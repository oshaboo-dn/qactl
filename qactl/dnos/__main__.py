"""qactl — one agent-shaped CLI over DriveNets DNOS devices.

Root Typer app wiring the four subcommand groups:

    qactl cli  ...   SSH→DNOS CLI          (from user-cli-mcp)
    qactl nc   ...   NETCONF               (from user-netconf-mcp)
    qactl gnmi ...   gNMI                  (from user-gnmi-mcp)
    qactl rc   ...   RESTCONF              (from user-restconf-mcp)

Every subcommand defaults to readable text and supports ``--json`` for
the exact structured payload (jq-friendly). Destructive subcommands
require ``--yes``. See ``qactl <group> --help`` for the full surface.
"""

from __future__ import annotations

import typer

from qactl.dnos import __version__
from qactl.dnos.cli.app import app as cli_app
from qactl.dnos.core.setup_cmd import setup as _setup_cmd
from qactl.dnos.gnmi.app import app as gnmi_app
from qactl.dnos.nc.app import app as nc_app
from qactl.dnos.rc.app import app as rc_app

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "qactl — one agent-shaped CLI over DriveNets DNOS devices. "
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
        typer.echo(f"qactl {__version__}")
        raise typer.Exit(0)


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version_cb, is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    """qactl — collapse the four DNOS MCP servers into one CLI."""


def main() -> None:
    app()


if __name__ == "__main__":
    main()
