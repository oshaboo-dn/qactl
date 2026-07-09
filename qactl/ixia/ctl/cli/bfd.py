"""``qactl.ixia.ctl bfd ...`` — BFD-over-IPv4 interface create/get/delete."""

from __future__ import annotations

import argparse

from qactl.ixia.ctl.core.output import emit
from qactl.ixia.ctl.cli.common import confirm_or_exit, name_or_index


def _bfd_create(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.bfd import ixia_create_bfdv4_interface
    env = ixia_create_bfdv4_interface(
        host=args.host, topology=args.topology,
        device_group=name_or_index(args.device_group), name=args.name,
        ethernet=name_or_index(args.ethernet), ipv4=name_or_index(args.ipv4),
        tx_interval=args.tx_interval, rx_interval=args.rx_interval,
        detect_multiplier=args.detect_multiplier, admin_state=args.admin_state,
        control_plane_independent=args.control_plane_independent,
        aggregate=args.aggregate, no_of_sessions=args.no_of_sessions,
        port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _bfd_get(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.bfd import ixia_get_bfdv4_interface
    env = ixia_get_bfdv4_interface(
        host=args.host, topology=args.topology, name=args.name,
        device_group=name_or_index(args.device_group),
        ethernet=name_or_index(args.ethernet), ipv4=name_or_index(args.ipv4),
        port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _bfd_delete(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="delete_bfdv4_interface",
        action=f"delete BFD interface {args.name!r} in DG "
               f"{args.device_group!r}.",
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.bfd import ixia_delete_bfdv4_interface
    env = ixia_delete_bfdv4_interface(
        host=args.host, topology=args.topology,
        device_group=name_or_index(args.device_group), name=args.name,
        ethernet=name_or_index(args.ethernet), ipv4=name_or_index(args.ipv4),
        port=args.port, user=args.user, confirm=True,
    )
    return emit(env, as_json=args.json)


def _add_admin_state(parser: argparse.ArgumentParser) -> None:
    """Tri-state ``--admin-state`` / ``--no-admin-state`` (default: leave)."""
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--admin-state", dest="admin_state",
                     action="store_true", default=None,
                     help="administratively enable the BFD session "
                          "(active=true).")
    grp.add_argument("--no-admin-state", dest="admin_state",
                     action="store_false",
                     help="administratively disable the session "
                          "(active=false).")


def _add_cpi(parser: argparse.ArgumentParser) -> None:
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--control-plane-independent",
                     dest="control_plane_independent",
                     action="store_true", default=None,
                     help="keep BFD up across a control-plane restart.")
    grp.add_argument("--no-control-plane-independent",
                     dest="control_plane_independent",
                     action="store_false",
                     help="disable control-plane-independent mode.")


def _add_aggregate(parser: argparse.ArgumentParser) -> None:
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--aggregate", dest="aggregate",
                     action="store_true", default=None,
                     help="aggregate all sessions onto one interface.")
    grp.add_argument("--no-aggregate", dest="aggregate",
                     action="store_false",
                     help="disable session aggregation.")


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("bfd", help="BFD-over-IPv4 interface")
    sub = grp.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", parents=[parent],
                       help="add a bfdv4Interface to an IPv4 stack")
    c.add_argument("--topology", required=True)
    c.add_argument("--device-group", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--ethernet", default="1",
                   help="parent ethernet: name or 1-based index (default 1)")
    c.add_argument("--ipv4", default="1",
                   help="parent IPv4 stack: name or 1-based index (default 1)")
    c.add_argument("--tx-interval", type=int, default=None,
                   help="desired min TX interval in ms (default 1000)")
    c.add_argument("--rx-interval", type=int, default=None,
                   help="required min RX interval in ms (default 1000)")
    c.add_argument("--detect-multiplier", type=int, default=None,
                   help="detection time multiplier (default 3)")
    c.add_argument("--no-of-sessions", type=int, default=None,
                   help="number of configured BFD sessions")
    _add_admin_state(c)
    _add_cpi(c)
    _add_aggregate(c)
    c.set_defaults(func=_bfd_create)

    g = sub.add_parser("get", parents=[parent],
                       help="inspect a bfdv4Interface (config + session state)")
    g.add_argument("--topology", required=True)
    g.add_argument("--name", required=True, help="BFD interface name")
    g.add_argument("--device-group", default="1",
                   help="DG name or 1-based index (default 1)")
    g.add_argument("--ethernet", default="1",
                   help="parent ethernet: name or 1-based index (default 1)")
    g.add_argument("--ipv4", default="1",
                   help="parent IPv4 stack: name or 1-based index (default 1)")
    g.set_defaults(func=_bfd_get)

    d = sub.add_parser("delete", parents=[parent],
                       help="delete a bfdv4Interface (--yes)")
    d.add_argument("--topology", required=True)
    d.add_argument("--device-group", required=True)
    d.add_argument("--name", required=True)
    d.add_argument("--ethernet", default="1",
                   help="parent ethernet: name or 1-based index (default 1)")
    d.add_argument("--ipv4", default="1",
                   help="parent IPv4 stack: name or 1-based index (default 1)")
    d.set_defaults(func=_bfd_delete)
