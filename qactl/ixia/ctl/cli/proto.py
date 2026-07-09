"""``qactl.ixia.ctl proto ...`` — protocol start/stop/summary + per-line routes."""

from __future__ import annotations

import argparse

from qactl.ixia.ctl.core.output import emit
from qactl.ixia.ctl.cli.common import (
    confirm_or_exit, name_or_index, parse_lines, primary_timeout,
)


def _start_all(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.run import ixia_protocols_start_all
    env = ixia_protocols_start_all(
        host=args.host, port=args.port, user=args.user, sync=not args.no_sync,
        wait_for_vports_ready_ms=args.wait_for_vports_ready_ms,
        apply_changes=not args.no_apply_changes,
        apply_changes_timeout_s=args.apply_changes_timeout_s,
        force=args.force,
    )
    return emit(env, as_json=args.json)


def _stop_all(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="protocols_stop_all",
        action="stop ALL protocols in the session.",
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.run import ixia_protocols_stop_all
    env = ixia_protocols_stop_all(
        host=args.host, port=args.port, user=args.user, sync=not args.no_sync,
    )
    return emit(env, as_json=args.json)


def _summary(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.run import ixia_protocols_summary
    env = ixia_protocols_summary(
        host=args.host, port=args.port, user=args.user,
        timeout=primary_timeout(args, 10),
    )
    return emit(env, as_json=args.json)


def _route_action(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="route_action",
        action=f"{args.action} routes on NG {args.network_group!r} "
               f"(lines={args.lines or 'all'}) — changes the wire.",
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.routes import ixia_route_action
    env = ixia_route_action(
        host=args.host, topology=args.topology,
        network_group=args.network_group, action=args.action,
        lines=parse_lines(args.lines, args.line),
        device_group=name_or_index(args.device_group),
        pool_index=args.pool_index, family=args.family,
        route_property=args.route_property,
        apply=not args.no_apply,
        apply_timeout_s=args.apply_timeout_s,
        port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _route_apply_pending(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.routes import ixia_route_apply_pending
    env = ixia_route_apply_pending(
        host=args.host, port=args.port, user=args.user,
        timeout_s=primary_timeout(args, 30),
    )
    return emit(env, as_json=args.json)


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("proto", help="protocols + routes")
    sub = grp.add_subparsers(dest="cmd", required=True)

    sa = sub.add_parser("start-all", parents=[parent],
                        help="start every protocol in the session")
    sa.add_argument("--no-sync", action="store_true",
                    help="return immediately instead of blocking")
    sa.add_argument("--wait-for-vports-ready-ms", type=int, default=60_000)
    sa.add_argument("--no-apply-changes", action="store_true")
    sa.add_argument("--apply-changes-timeout-s", type=int, default=60)
    sa.add_argument("--force", action="store_true",
                    help="skip the vport-readiness preflight wait")
    sa.set_defaults(func=_start_all)

    sp = sub.add_parser("stop-all", parents=[parent],
                        help="stop every protocol (--yes)")
    sp.add_argument("--no-sync", action="store_true")
    sp.set_defaults(func=_stop_all)

    sub.add_parser("summary", parents=[parent],
                   help="per-protocol session counts").set_defaults(func=_summary)

    # ---- route ----
    route = sub.add_parser("route", help="per-line route advertise/withdraw")
    rs = route.add_subparsers(dest="routecmd", required=True)

    ra = rs.add_parser("action", parents=[parent],
                       help="advertise/withdraw NG route lines (--yes)")
    ra.add_argument("action", choices=["advertise", "withdraw"])
    ra.add_argument("--topology", required=True)
    ra.add_argument("--network-group", required=True)
    ra.add_argument("--lines", default=None,
                    help="'all' (default), or comma-separated 1-based ints")
    ra.add_argument("--line", action="append", type=int, default=[],
                    metavar="N", help="single 1-based line (repeatable)")
    ra.add_argument("--device-group", default="1",
                    help="DG name or 1-based index (default 1)")
    ra.add_argument("--pool-index", type=int, default=1)
    ra.add_argument("--family", default="ipv4", choices=["ipv4", "ipv6"])
    ra.add_argument("--route-property", default="bgpIPRouteProperty")
    ra.add_argument("--no-apply", action="store_true",
                    help="stage only; do not pulse ApplyOnTheFly")
    ra.add_argument("--apply-timeout-s", type=int, default=30)
    ra.set_defaults(func=_route_action)

    rap = rs.add_parser("apply-pending", parents=[parent],
                        help="push staged NGPF edits onto the wire")
    rap.set_defaults(func=_route_apply_pending)
