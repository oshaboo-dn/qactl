"""``ixiactl bgp ...`` — BGP peer + VRF create/get/delete."""

from __future__ import annotations

import argparse

from qactl.ixia.ctl.core.output import emit
from qactl.ixia.ctl.cli.common import (
    confirm_or_exit, name_or_index, parse_capabilities, parse_rt,
)


def _peer_create(args: argparse.Namespace) -> int:
    try:
        caps = parse_capabilities(args.capability)
    except ValueError as e:
        from qactl.ixia.core.envelope import error_envelope
        return emit(error_envelope(str(e), kind="create_bgp_peer",
                                   host=args.host, port=args.port,
                                   status="bad_argument"), as_json=args.json)
    from qactl.ixia.tools.stack import ixia_create_bgp_peer
    env = ixia_create_bgp_peer(
        host=args.host, topology=args.topology,
        device_group=name_or_index(args.device_group), name=args.name,
        dut_ip=args.dut_ip, local_as=args.local_as, peer_type=args.peer_type,
        ipv4=name_or_index(args.ipv4),
        hold_timer=args.hold_timer, keepalive_timer=args.keepalive_timer,
        capabilities=caps or None, bfd=args.bfd, bfd_mode=args.bfd_mode,
        port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _peer_get(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.inspect import ixia_get_bgp_peer
    env = ixia_get_bgp_peer(
        host=args.host, topology=args.topology, peer=args.name,
        device_group=name_or_index(args.device_group),
        include_route_counts=args.route_counts,
        port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _peer_delete(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="delete_bgp_peer",
        action=f"delete BGP peer {args.name!r} (and every bgpVrf on it) in "
               f"DG {args.device_group!r}.",
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.stack import ixia_delete_bgp_peer
    env = ixia_delete_bgp_peer(
        host=args.host, topology=args.topology,
        device_group=name_or_index(args.device_group), name=args.name,
        port=args.port, user=args.user, confirm=True,
    )
    return emit(env, as_json=args.json)


def _vrf_create(args: argparse.Namespace) -> int:
    try:
        import_rts = [parse_rt(x) for x in (args.import_rt or [])]
        export_rts = [parse_rt(x) for x in (args.export_rt or [])]
    except ValueError as e:
        from qactl.ixia.core.envelope import error_envelope
        return emit(error_envelope(str(e), kind="create_bgp_vrf",
                                   host=args.host, port=args.port,
                                   status="bad_argument"), as_json=args.json)
    from qactl.ixia.tools.stack import ixia_create_bgp_vrf
    env = ixia_create_bgp_vrf(
        host=args.host, topology=args.topology,
        device_group=name_or_index(args.device_group), peer=args.peer,
        name=args.name, import_rts=import_rts, export_rts=export_rts,
        multiplier=args.multiplier, port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _vrf_delete(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="delete_bgp_vrf",
        action=f"delete bgpVrf {args.name!r} from peer {args.peer!r} in DG "
               f"{args.device_group!r}.",
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.stack import ixia_delete_bgp_vrf
    env = ixia_delete_bgp_vrf(
        host=args.host, topology=args.topology,
        device_group=name_or_index(args.device_group), peer=args.peer,
        name=args.name, port=args.port, user=args.user, confirm=True,
    )
    return emit(env, as_json=args.json)


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("bgp", help="BGP peer + VRF")
    sub = grp.add_subparsers(dest="cmd", required=True)

    # ---- peer ----
    peer = sub.add_parser("peer", help="BGP peer create/get/delete")
    ps = peer.add_subparsers(dest="peercmd", required=True)

    pc = ps.add_parser("create", parents=[parent], help="add a BGP IPv4 peer")
    pc.add_argument("--topology", required=True)
    pc.add_argument("--device-group", required=True)
    pc.add_argument("--name", required=True)
    pc.add_argument("--dut-ip", required=True)
    pc.add_argument("--local-as", type=int, required=True)
    pc.add_argument("--peer-type", default="external",
                    choices=["external", "internal"])
    pc.add_argument("--ipv4", default="1",
                    help="parent IPv4 stack: name or 1-based index (default 1)")
    pc.add_argument("--hold-timer", type=int, default=None)
    pc.add_argument("--keepalive-timer", type=int, default=None)
    pc.add_argument("--capability", action="append", default=[],
                    metavar="LABEL=BOOL",
                    help="capability flag, e.g. ipv4_mpls=true (repeatable)")
    bfd_grp = pc.add_mutually_exclusive_group()
    bfd_grp.add_argument("--bfd", dest="bfd", action="store_true",
                         default=None,
                         help="register the peer for BGP-over-BFD "
                              "(enableBfdRegistration). Pair with "
                              "`qactl ixia bfd create`.")
    bfd_grp.add_argument("--no-bfd", dest="bfd", action="store_false",
                         help="explicitly clear BGP-over-BFD registration.")
    pc.add_argument("--bfd-mode", default=None,
                    choices=["singlehop", "multihop"],
                    help="modeOfBfdOperations (default singlehop).")
    pc.set_defaults(func=_peer_create)

    pg = ps.add_parser("get", parents=[parent], help="inspect a BGP peer")
    pg.add_argument("--topology", required=True)
    pg.add_argument("--name", required=True, help="peer name")
    pg.add_argument("--device-group", default="1",
                    help="DG name or 1-based index (default 1)")
    pg.add_argument("--route-counts", action="store_true",
                    help="include cumulative advertised/withdrawn counts")
    pg.set_defaults(func=_peer_get)

    pd = ps.add_parser("delete", parents=[parent],
                       help="delete a BGP peer (--yes)")
    pd.add_argument("--topology", required=True)
    pd.add_argument("--device-group", required=True)
    pd.add_argument("--name", required=True)
    pd.set_defaults(func=_peer_delete)

    # ---- vrf ----
    vrf = sub.add_parser("vrf", help="BGP VRF create/delete")
    vs = vrf.add_subparsers(dest="vrfcmd", required=True)

    vc = vs.add_parser("create", parents=[parent], help="add a bgpVrf")
    vc.add_argument("--topology", required=True)
    vc.add_argument("--device-group", required=True)
    vc.add_argument("--peer", required=True)
    vc.add_argument("--name", required=True)
    vc.add_argument("--import-rt", action="append", default=[],
                    metavar="RT", help="import RT 'asn:assigned' or JSON "
                                       "(repeatable)")
    vc.add_argument("--export-rt", action="append", default=[],
                    metavar="RT", help="export RT 'asn:assigned' or JSON "
                                       "(repeatable)")
    vc.add_argument("--multiplier", type=int, default=1)
    vc.set_defaults(func=_vrf_create)

    vd = vs.add_parser("delete", parents=[parent], help="delete a bgpVrf (--yes)")
    vd.add_argument("--topology", required=True)
    vd.add_argument("--device-group", required=True)
    vd.add_argument("--peer", required=True)
    vd.add_argument("--name", required=True)
    vd.set_defaults(func=_vrf_delete)
