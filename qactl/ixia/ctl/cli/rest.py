"""``ixiactl rest ...`` — raw IxNetwork REST escape hatch."""

from __future__ import annotations

import argparse

from qactl.ixia.ctl.core.output import emit, parse_json_payload
from qactl.ixia.ctl.cli.common import confirm_or_exit


def _get(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.rest import ixia_rest_get
    env = ixia_rest_get(
        host=args.host, path=args.url, method=args.method,
        port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _patch(args: argparse.Namespace) -> int:
    try:
        body = parse_json_payload(args.body, args.file)
    except (ValueError, OSError) as e:
        from qactl.ixia.core.envelope import error_envelope
        return emit(error_envelope(str(e), kind="rest_patch",
                                   host=args.host, port=args.port,
                                   status="bad_argument"), as_json=args.json)
    rc = confirm_or_exit(
        args, kind="rest_patch",
        action=f"{args.method} {args.url} (raw REST write — no undo).",
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.rest import ixia_rest_patch
    env = ixia_rest_patch(
        host=args.host, path=args.url, body=body, method=args.method,
        port=args.port, user=args.user, confirm=True,
    )
    return emit(env, as_json=args.json)


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("rest", help="raw REST escape hatch")
    sub = grp.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get", parents=[parent], help="GET/OPTIONS a REST path")
    g.add_argument("url", help="IxNetwork REST path (relative to session root)")
    g.add_argument("--method", default="GET", choices=["GET", "OPTIONS"])
    g.set_defaults(func=_get)

    p = sub.add_parser("patch", parents=[parent],
                       help="POST/PATCH/PUT/DELETE a REST path (--yes)")
    p.add_argument("url", help="IxNetwork REST path (relative to session root)")
    p.add_argument("body", nargs="?", default=None,
                   help="JSON body inline, or '-' to read stdin")
    p.add_argument("--file", default=None, help="read JSON body from a file")
    p.add_argument("--method", default="PATCH",
                   choices=["POST", "PATCH", "PUT", "DELETE"])
    p.set_defaults(func=_patch)
