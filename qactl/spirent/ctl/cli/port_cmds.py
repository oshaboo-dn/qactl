"""``qactl spirent port ...`` — physical-port reserve / release / status.

Tool functions are imported lazily inside each handler so building the parser
and ``--help`` never needs ``stcrestclient``.
"""

from __future__ import annotations

import argparse

from qactl.spirent.ctl.cli.common import confirm_or_exit
from qactl.spirent.ctl.core.output import emit


def _reserve(args: argparse.Namespace) -> int:
    if args.force:
        rc = confirm_or_exit(
            args, kind="spirent_port_reserve",
            action=f"Reserve {args.location} with RevokeOwner "
                   f"(kicks any current owner)",
        )
        if rc is not None:
            return rc
    from qactl.spirent.tools.port import spirent_reserve_port
    env = spirent_reserve_port(
        host=args.host, port=args.port, user=args.user,
        location=args.location, name=args.name, force=args.force,
        wait_up=not args.no_wait, timeout=args.reserve_timeout,
    )
    return emit(env, as_json=args.json)


def _release(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="spirent_port_release",
        action=f"Release port {args.location}",
    )
    if rc is not None:
        return rc
    from qactl.spirent.tools.port import spirent_release_port
    env = spirent_release_port(
        host=args.host, port=args.port, user=args.user, location=args.location,
    )
    return emit(env, as_json=args.json)


def _status(args: argparse.Namespace) -> int:
    from qactl.spirent.tools.port import spirent_port_status
    env = spirent_port_status(host=args.host, port=args.port, user=args.user)
    return emit(env, as_json=args.json)


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("port", help="physical port reserve / release / status")
    sub = grp.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser(
        "reserve", parents=[parent],
        help="reserve (attach) a physical port at //chassis/slot/port",
    )
    r.add_argument("--location", required=True, metavar="//CHASSIS/SLOT/PORT",
                   help="port location, chassis best given as IP "
                        "(e.g. //100.64.3.238/6/13)")
    r.add_argument("--name", default=None, help="STC port object name")
    r.add_argument("--force", action="store_true",
                   help="RevokeOwner — take the port even if someone holds it "
                        "(confirm-gated; use --yes off a TTY)")
    r.add_argument("--no-wait", action="store_true",
                   help="don't wait for link UP after attach")
    r.add_argument("--reserve-timeout", type=int, default=40, metavar="SECONDS",
                   help="seconds to wait for link UP (default 40)")
    r.set_defaults(func=_reserve)

    rel = sub.add_parser(
        "release", parents=[parent], help="release a reserved physical port",
    )
    rel.add_argument("--location", required=True, metavar="//CHASSIS/SLOT/PORT",
                     help="port location to release")
    rel.set_defaults(func=_release)

    sub.add_parser(
        "status", parents=[parent],
        help="list ports in this session with location + link state",
    ).set_defaults(func=_status)
