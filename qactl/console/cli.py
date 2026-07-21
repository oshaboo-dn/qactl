"""``qactl console [<device>] [--server CS --port N]`` — interactive serial console.

With a device **name or serial** the console server + port are looked up in
Device42; with explicit ``--server``/``--port`` it connects manually, bypassing
Device42 (also the fallback for a device whose Device42 console field is too
free-form to parse). Connect only runs on an interactive TTY — with ``--json``
or off a TTY it resolves the server/port and stops.
"""

from __future__ import annotations

import argparse
import sys

from qactl.core.creds import ConsoleServerConfig
from qactl.core.envelope import error_envelope
from qactl.core.output import emit
from qactl.console import connect as connect_mod
from qactl.console import tools


def _normalize_cs(name: str) -> str:
    s = (name or "").strip().upper()
    return s if s.startswith("CONSOLE-") else "CONSOLE-" + s


def _console(args):
    manual = args.server is not None or args.port is not None
    if manual:
        if not (args.server and args.port):
            return emit(error_envelope(
                "manual connect needs both --server and --port.",
                kind="console", status="bad_argument"), as_json=args.json)
        server, port = _normalize_cs(args.server), args.port
    else:
        if not args.query:
            return emit(error_envelope(
                "give a device name/serial, or --server and --port for a "
                "manual connect.", kind="console", status="bad_argument"),
                as_json=args.json)
        env = tools.console_resolve(args.query)
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
        return connect_mod.connect(server, int(port), cfg)
    except connect_mod.ConsoleError as e:
        return emit(error_envelope(str(e), kind="console"), as_json=args.json)


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    p = subparsers.add_parser(
        "console", parents=[parent],
        help="open an interactive serial console (Device42 lookup, or manual "
             "--server/--port)")
    p.add_argument("query", nargs="?",
                   help="device name or serial (looks up the console in Device42)")
    p.add_argument("--server", help="console server name (manual; e.g. B10 or "
                                    "CONSOLE-B10) — bypass the Device42 lookup")
    p.add_argument("--port", type=int, help="console-server port number (manual)")
    p.add_argument("--cs-user", default=None, help="override $CONSOLE_CS_USER")
    p.add_argument("--cs-password", default=None, help="override $CONSOLE_CS_PASSWORD")
    p.set_defaults(func=_console)
