"""``qactl arista ...`` — read-only Arista EOS queries over SSH.

Thin argparse front over :mod:`qactl.arista.tools` (the same envelope
layer the stdio MCP server exposes). All commands are read-only shows,
so nothing here takes the ``--yes`` gate.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict

from qactl.core.common import resolve_timeout
from qactl.core.output import emit
from qactl.arista import tools


def _creds(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "timeout": resolve_timeout(args, 30.0),
        "user": args.user,
        "password": args.password,
        "port": args.port,
    }


# ---- handlers ------------------------------------------------------------

def _interfaces(args):
    return emit(tools.arista_interfaces(args.host, **_creds(args)), as_json=args.json)


def _lldp(args):
    return emit(tools.arista_lldp(args.host, **_creds(args)), as_json=args.json)


def _config(args):
    return emit(tools.arista_config(args.host, interfaces=args.interface,
                                    **_creds(args)), as_json=args.json)


def _version(args):
    return emit(tools.arista_version(args.host, **_creds(args)), as_json=args.json)


# ---- registration --------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("host", help="Arista switch hostname or IP (e.g. arista410)")
    g = p.add_argument_group("ssh credentials (default: environment)")
    g.add_argument("--user", default=None, help="override $ARISTA_USER (default admin)")
    g.add_argument("--password", default=None, help="override $ARISTA_PASSWORD")
    g.add_argument("--port", type=int, default=None, help="SSH port (default 22)")


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("arista",
                                help="Arista EOS switches (read-only, over SSH)")
    sub = grp.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("interfaces", parents=[parent],
                       help="interface status + free-port candidates")
    _add_common(i); i.set_defaults(func=_interfaces)

    n = sub.add_parser("lldp", parents=[parent],
                       help="LLDP neighbors (local port -> peer device/port)")
    _add_common(n); n.set_defaults(func=_lldp)

    c = sub.add_parser("config", parents=[parent],
                       help="running config (whole box, or --interface sections)")
    _add_common(c)
    c.add_argument("--interface", action="append", metavar="IFACE",
                   help="limit to one interface section (repeatable, e.g. Ethernet10)")
    c.set_defaults(func=_config)

    v = sub.add_parser("version", parents=[parent],
                       help="model / EOS version / serial (connectivity sanity check)")
    _add_common(v); v.set_defaults(func=_version)
