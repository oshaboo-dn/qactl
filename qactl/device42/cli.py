"""``qactl d42 ...`` — read-only Device42 CMDB lookups.

Device42 is the lab's authoritative CMDB; this group reads it live so the
hostname migration to ``{Site}{NN}-{ROLE}-{RACK}`` can't leave us on stale
cached names. Lookups (``device`` / ``rack`` / ``power``) take a device
**name or serial** and are read-only shows.

``console`` is the one interactive command: it resolves a device's console
server + port from Device42 and opens the session — or, with explicit
``--server``/``--port``, connects manually (bypassing Device42, which also
covers devices whose Device42 console field is too free-form to parse).
"""

from __future__ import annotations

import argparse
import sys

from qactl.core.creds import ConsoleServerConfig
from qactl.core.envelope import error_envelope
from qactl.core.output import emit
from qactl.device42 import console as console_mod
from qactl.device42 import tools


def _normalize_cs(name: str) -> str:
    s = (name or "").strip().upper()
    return s if s.startswith("CONSOLE-") else "CONSOLE-" + s


def _device(args):
    return emit(tools.d42_device(args.query), as_json=args.json)


def _rack(args):
    return emit(tools.d42_rack(args.query), as_json=args.json)


def _power(args):
    return emit(tools.d42_power(args.query), as_json=args.json)


def _console(args):
    manual = args.server is not None or args.port is not None
    if manual:
        if not (args.server and args.port):
            return emit(error_envelope(
                "manual connect needs both --server and --port.",
                kind="d42_console", status="bad_argument"), as_json=args.json)
        server, port = _normalize_cs(args.server), args.port
    else:
        if not args.query:
            return emit(error_envelope(
                "give a device name/serial, or --server and --port for a "
                "manual connect.", kind="d42_console", status="bad_argument"),
                as_json=args.json)
        env = tools.d42_console(args.query)
        result = env.get("result") or {}
        server, port = result.get("console_server"), result.get("port")
        # Resolve-only (no connect) when asked for JSON, when not on a TTY, or
        # when Device42 has no clean mapping to connect to.
        if (args.json or not sys.stdin.isatty()
                or env["status"] not in ("ok", "warning") or not server):
            return emit(env, as_json=args.json)

    try:
        cfg = ConsoleServerConfig.resolve(user=args.cs_user,
                                          password=args.cs_password)
        return console_mod.connect(server, int(port), cfg)
    except console_mod.ConsoleError as e:
        return emit(error_envelope(str(e), kind="d42_console"),
                    as_json=args.json)


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser(
        "d42", help="Device42 CMDB (read-only: device inventory / rack lookup)")
    sub = grp.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("device", parents=[parent],
                       help="device inventory + owner, by name or serial")
    d.add_argument("query", help="device name or serial (e.g. WDY1A17P0001A or a hostname)")
    d.set_defaults(func=_device)

    r = sub.add_parser("rack", parents=[parent],
                       help="physical placement: rack / row / room / building / U")
    r.add_argument("query", help="device name or serial")
    r.set_defaults(func=_rack)

    p = sub.add_parser("power", parents=[parent],
                       help="PDU power feed(s): pdu / outlet / model (read-only)")
    p.add_argument("query", help="device name or serial")
    p.set_defaults(func=_power)

    co = sub.add_parser(
        "console", parents=[parent],
        help="open an interactive serial console (from Device42, or manual "
             "--server/--port)")
    co.add_argument("query", nargs="?",
                    help="device name or serial (looks up the console in Device42)")
    co.add_argument("--server", help="console server name (manual; e.g. B10 or "
                                     "CONSOLE-B10) — bypass the Device42 lookup")
    co.add_argument("--port", type=int, help="console-server port number (manual)")
    co.add_argument("--cs-user", default=None, help="override $CONSOLE_CS_USER")
    co.add_argument("--cs-password", default=None, help="override $CONSOLE_CS_PASSWORD")
    co.set_defaults(func=_console)
