"""``qactl spirent bgp ...`` — BGP router add / status on an emulated device.

``--strict`` advertises BGP-BFD strict-mode (capability code 74).
"""

from __future__ import annotations

import argparse

from qactl.spirent.ctl.core.output import emit


def _add(args: argparse.Namespace) -> int:
    from qactl.spirent.tools.bgp import spirent_bgp_add
    env = spirent_bgp_add(
        host=args.host, port=args.port, user=args.user,
        device=args.device, local_as=args.local_as, peer_as=args.peer_as,
        peer=args.peer, use_gateway=not args.no_use_gateway,
        bfd=args.bfd, strict=args.strict,
    )
    return emit(env, as_json=args.json)


def _status(args: argparse.Namespace) -> int:
    from qactl.spirent.tools.bgp import spirent_bgp_status
    env = spirent_bgp_status(host=args.host, port=args.port, user=args.user,
                             device=args.device)
    return emit(env, as_json=args.json)


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("bgp", help="BGP router add / status on a device")
    sub = grp.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", parents=[parent],
                       help="add / reconfigure a BGP router on an emulated device")
    a.add_argument("--device", required=True, help="target device name")
    a.add_argument("--local-as", type=int, required=True,
                   help="local ASN (2- or 4-byte; 4-byte handled automatically)")
    a.add_argument("--peer-as", type=int, default=None,
                   help="peer/DUT ASN (default = --local-as, i.e. iBGP)")
    a.add_argument("--peer", default=None, metavar="IP",
                   help="explicit peer IPv4 (with --no-use-gateway)")
    a.add_argument("--no-use-gateway", action="store_true",
                   help="don't use the device gateway as the peer IP; needs --peer")
    a.add_argument("--bfd", action="store_true", help="enable BFD for the session")
    a.add_argument("--strict", action="store_true",
                   help="advertise BGP-BFD strict-mode (capability code 74); "
                        "implies --bfd")
    a.set_defaults(func=_add)

    s = sub.add_parser("status", parents=[parent], help="BGP router state per device")
    s.add_argument("--device", default=None, help="limit to one device name")
    s.set_defaults(func=_status)
