"""``qactl spirent device ...`` — emulated-device create / list / start / stop / delete."""

from __future__ import annotations

import argparse

from qactl.spirent.ctl.cli.common import confirm_or_exit
from qactl.spirent.ctl.core.output import emit


def _create(args: argparse.Namespace) -> int:
    from qactl.spirent.tools.device import spirent_device_create
    env = spirent_device_create(
        host=args.host, port=args.port, user=args.user,
        port_location=args.port_location, name=args.name, ip=args.ip,
        prefix=args.prefix, gateway=args.gateway, vlan=args.vlan,
        mac=args.mac, router_id=args.router_id,
    )
    return emit(env, as_json=args.json)


def _list(args: argparse.Namespace) -> int:
    from qactl.spirent.tools.device import spirent_device_list
    return emit(spirent_device_list(host=args.host, port=args.port, user=args.user),
                as_json=args.json)


def _start(args: argparse.Namespace) -> int:
    from qactl.spirent.tools.device import spirent_device_start
    return emit(spirent_device_start(args.host, args.port, args.user, name=args.name),
                as_json=args.json)


def _stop(args: argparse.Namespace) -> int:
    from qactl.spirent.tools.device import spirent_device_stop
    return emit(spirent_device_stop(args.host, args.port, args.user, name=args.name),
                as_json=args.json)


def _delete(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(args, kind="spirent_device_delete",
                         action=f"Delete emulated device {args.name!r}")
    if rc is not None:
        return rc
    from qactl.spirent.tools.device import spirent_device_delete
    return emit(spirent_device_delete(args.host, args.port, args.user, name=args.name),
                as_json=args.json)


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("device", help="emulated device create/list/start/stop/delete")
    sub = grp.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", parents=[parent],
                       help="create an IPv4 emulated device on a reserved port")
    c.add_argument("--port-location", required=True, metavar="//CHASSIS/SLOT/PORT",
                   help="reserved port to host the device")
    c.add_argument("--name", required=True, help="device name (handle for other cmds)")
    c.add_argument("--ip", required=True, help="device IPv4 address")
    c.add_argument("--prefix", type=int, default=24, help="IPv4 prefix length (default 24)")
    c.add_argument("--gateway", required=True, help="IPv4 gateway (usually the DUT/peer)")
    c.add_argument("--vlan", type=int, default=None, help="single VLAN tag (omit for untagged)")
    c.add_argument("--mac", default=None, help="source MAC (default STC-assigned)")
    c.add_argument("--router-id", default=None, help="router-id (default = --ip)")
    c.set_defaults(func=_create)

    sub.add_parser("list", parents=[parent],
                   help="list emulated devices with IP/VLAN/gateway state").set_defaults(func=_list)

    for verb, fn, helptext in (("start", _start, "start (bring up) a device's protocols"),
                               ("stop", _stop, "stop a device's protocols")):
        s = sub.add_parser(verb, parents=[parent], help=helptext)
        s.add_argument("--name", required=True, help="device name")
        s.set_defaults(func=fn)

    d = sub.add_parser("delete", parents=[parent], help="delete an emulated device")
    d.add_argument("--name", required=True, help="device name")
    d.set_defaults(func=_delete)
