"""``ixiactl traffic ...`` — traffic item CRUD + run control + stats."""

from __future__ import annotations

import argparse

from qactl.ixia.ctl.core.output import emit
from qactl.ixia.ctl.cli.common import confirm_or_exit, primary_timeout


def _list(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.traffic import ixia_list_traffic_items
    env = ixia_list_traffic_items(
        host=args.host, port=args.port, user=args.user,
        pattern=args.pattern, limit=args.limit,
    )
    return emit(env, as_json=args.json)


def _get(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.traffic import ixia_get_traffic_item
    env = ixia_get_traffic_item(
        host=args.host, name=args.name, port=args.port, user=args.user,
        max_streams=args.max_streams,
    )
    return emit(env, as_json=args.json)


def _create(args: argparse.Namespace) -> int:
    track_by = args.track_by or None
    if args.no_track:
        track_by = []
    from qactl.ixia.tools.build import ixia_create_traffic_item
    env = ixia_create_traffic_item(
        host=args.host, name=args.name,
        src_refs=args.src, dst_refs=args.dst,
        rate_fps=args.rate_fps, frame_size=args.frame_size,
        traffic_type=args.traffic_type, track_by=track_by,
        port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _delete(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="delete_traffic_item",
        action=f"delete traffic item {args.name!r}.",
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.build import ixia_delete_traffic_item
    env = ixia_delete_traffic_item(
        host=args.host, name=args.name, port=args.port, user=args.user,
        confirm=True,
    )
    return emit(env, as_json=args.json)


def _generate(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.run import ixia_traffic_generate
    env = ixia_traffic_generate(host=args.host, port=args.port, user=args.user)
    return emit(env, as_json=args.json)


def _apply(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.run import ixia_traffic_apply
    env = ixia_traffic_apply(host=args.host, port=args.port, user=args.user)
    return emit(env, as_json=args.json)


def _start(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="traffic_start",
        action=f"start traffic ({args.name or 'ALL items'}).",
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.run import ixia_traffic_start
    env = ixia_traffic_start(
        host=args.host, port=args.port, user=args.user, name=args.name,
    )
    return emit(env, as_json=args.json)


def _stop(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="traffic_stop",
        action=f"stop traffic ({args.name or 'ALL items'}).",
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.run import ixia_traffic_stop
    env = ixia_traffic_stop(
        host=args.host, port=args.port, user=args.user, name=args.name,
    )
    return emit(env, as_json=args.json)


def _stats(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.run import ixia_get_traffic_stats
    env = ixia_get_traffic_stats(
        host=args.host, port=args.port, user=args.user,
        timeout=primary_timeout(args, 10),
    )
    return emit(env, as_json=args.json)


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("traffic", help="traffic items + stats")
    sub = grp.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", parents=[parent], help="list traffic items")
    ls.add_argument("--pattern", default=None, help="glob filter on item names")
    ls.add_argument("--limit", type=int, default=200,
                    help="max items (0 = no limit)")
    ls.set_defaults(func=_list)

    g = sub.add_parser("get", parents=[parent], help="deep-read a traffic item")
    g.add_argument("name")
    g.add_argument("--max-streams", type=int, default=5)
    g.set_defaults(func=_get)

    c = sub.add_parser("create", parents=[parent], help="create a traffic item")
    c.add_argument("--name", required=True)
    c.add_argument("--src", action="append", default=[], required=True,
                   metavar="REF", help="source endpoint ref (repeatable)")
    c.add_argument("--dst", action="append", default=[], required=True,
                   metavar="REF", help="destination endpoint ref (repeatable)")
    c.add_argument("--rate-fps", type=int, default=None)
    c.add_argument("--frame-size", type=int, default=None)
    c.add_argument("--traffic-type", default="ipv4")
    c.add_argument("--track-by", action="append", default=[], metavar="FIELD",
                   help="flow-tracking field (repeatable; default "
                        "sourceDestEndpointPair0)")
    c.add_argument("--no-track", action="store_true",
                   help="disable flow tracking (track_by=[])")
    c.set_defaults(func=_create)

    d = sub.add_parser("delete", parents=[parent],
                       help="delete a traffic item (--yes)")
    d.add_argument("name")
    d.set_defaults(func=_delete)

    sub.add_parser("generate", parents=[parent],
                   help="regenerate flow groups").set_defaults(func=_generate)
    sub.add_parser("apply", parents=[parent],
                   help="apply traffic config to hardware").set_defaults(func=_apply)

    st = sub.add_parser("start", parents=[parent], help="start traffic (--yes)")
    st.add_argument("--name", default=None,
                    help="single item name (default: all items)")
    st.set_defaults(func=_start)

    sp = sub.add_parser("stop", parents=[parent], help="stop traffic (--yes)")
    sp.add_argument("--name", default=None,
                    help="single item name (default: all items)")
    sp.set_defaults(func=_stop)

    sub.add_parser("stats", parents=[parent],
                   help="read Traffic Item Statistics").set_defaults(func=_stats)
