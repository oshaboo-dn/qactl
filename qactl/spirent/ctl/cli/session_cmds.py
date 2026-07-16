"""``qactl spirent session ...`` — session lifecycle (scaffold surface).

connect / sessions / describe. Tool functions are imported lazily inside each
handler so building the parser and ``--help`` never needs ``stcrestclient``.
"""

from __future__ import annotations

import argparse

from qactl.spirent.ctl.core.output import emit


def _connect(args: argparse.Namespace) -> int:
    from qactl.spirent.tools.diag import spirent_connect_check
    env = spirent_connect_check(host=args.host, port=args.port, user=args.user)
    return emit(env, as_json=args.json)


def _sessions(args: argparse.Namespace) -> int:
    from qactl.spirent.tools.diag import spirent_list_sessions
    env = spirent_list_sessions(host=args.host, port=args.port, user=args.user)
    return emit(env, as_json=args.json)


def _describe(args: argparse.Namespace) -> int:
    from qactl.spirent.tools.diag import spirent_describe_session
    env = spirent_describe_session(host=args.host, port=args.port, user=args.user)
    return emit(env, as_json=args.json)


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("session", help="session lifecycle")
    sub = grp.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "connect", parents=[parent],
        help="reattach-first probe; reports whether the session existed",
    ).set_defaults(func=_connect)

    sub.add_parser(
        "sessions", parents=[parent],
        help="list STC sessions on the REST server (no join)",
    ).set_defaults(func=_sessions)

    sub.add_parser(
        "describe", parents=[parent],
        help="connect (reattach) + server / system / BLL info snapshot",
    ).set_defaults(func=_describe)
