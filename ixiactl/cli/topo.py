"""``ixiactl topo ...`` — topology / device-group / stack management.

list / get / create / delete / start / stop, plus the nested
``dg``, ``eth``, ``ipv4``, and ``netgroup`` sub-groups.
"""

from __future__ import annotations

import argparse

from ixiactl.core.output import emit
from ixiactl.cli.common import (
    confirm_or_exit, name_or_index, primary_timeout,
)


# ----------------------------------------------------------------- topology

def _list(args: argparse.Namespace) -> int:
    from ixia_tools.topology import ixia_list_topologies
    env = ixia_list_topologies(host=args.host, port=args.port, user=args.user)
    return emit(env, as_json=args.json)


def _get(args: argparse.Namespace) -> int:
    from ixia_tools.topology import ixia_get_topology
    env = ixia_get_topology(
        host=args.host, name=args.name, port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _create(args: argparse.Namespace) -> int:
    from ixia_tools.build import ixia_create_topology
    env = ixia_create_topology(
        host=args.host, name=args.name,
        vport_hrefs=args.vport or None,
        port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _delete(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="delete_topology",
        action=f"delete topology {args.name!r} (and every DG / protocol below it).",
    )
    if rc is not None:
        return rc
    from ixia_tools.build import ixia_delete_topology
    env = ixia_delete_topology(
        host=args.host, name=args.name, port=args.port, user=args.user,
        confirm=True,
    )
    return emit(env, as_json=args.json)


def _start(args: argparse.Namespace) -> int:
    from ixia_tools.run import ixia_topology_start
    env = ixia_topology_start(
        host=args.host, name=args.name, port=args.port, user=args.user,
        wait_for_vports_ready_ms=args.wait_for_vports_ready_ms,
        apply_changes=not args.no_apply_changes,
        apply_changes_timeout_s=args.apply_changes_timeout_s,
        force=args.force,
    )
    return emit(env, as_json=args.json)


def _stop(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="topology_stop",
        action=f"stop protocols on topology {args.name!r}.",
    )
    if rc is not None:
        return rc
    from ixia_tools.run import ixia_topology_stop
    env = ixia_topology_stop(
        host=args.host, name=args.name, port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


# --------------------------------------------------------------- device group

def _dg_create(args: argparse.Namespace) -> int:
    from ixia_tools.build import ixia_create_device_group
    env = ixia_create_device_group(
        host=args.host, topology=args.topology, name=args.name,
        multiplier=args.multiplier, port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _dg_delete(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="delete_device_group",
        action=f"delete device group {args.name!r} from topology "
               f"{args.topology!r}.",
    )
    if rc is not None:
        return rc
    from ixia_tools.build import ixia_delete_device_group
    env = ixia_delete_device_group(
        host=args.host, topology=args.topology, name=args.name,
        port=args.port, user=args.user, confirm=True,
    )
    return emit(env, as_json=args.json)


def _dg_start(args: argparse.Namespace) -> int:
    from ixia_tools.run import ixia_dg_start
    env = ixia_dg_start(
        host=args.host, topology=args.topology, name=args.name,
        port=args.port, user=args.user,
        wait_for_vports_ready_ms=args.wait_for_vports_ready_ms,
        apply_changes=not args.no_apply_changes,
        apply_changes_timeout_s=args.apply_changes_timeout_s,
        force=args.force,
    )
    return emit(env, as_json=args.json)


def _dg_stop(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="dg_stop",
        action=f"stop device group {args.name!r} in topology {args.topology!r}.",
    )
    if rc is not None:
        return rc
    from ixia_tools.run import ixia_dg_stop
    env = ixia_dg_stop(
        host=args.host, topology=args.topology, name=args.name,
        port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


# -------------------------------------------------------------------- ethernet

def _eth_create(args: argparse.Namespace) -> int:
    from ixia_tools.stack import ixia_create_ethernet
    env = ixia_create_ethernet(
        host=args.host, topology=args.topology,
        device_group=name_or_index(args.device_group), name=args.name,
        mac=args.mac, mtu=args.mtu, vlan_id=args.vlan_id,
        vlan_priority=args.vlan_priority, vlan_tpid=args.vlan_tpid,
        port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _eth_delete(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="delete_ethernet",
        action=f"delete ethernet {args.name!r} (cascades to IPv4 / BGP / VRF "
               f"beneath it) in DG {args.device_group!r}.",
    )
    if rc is not None:
        return rc
    from ixia_tools.stack import ixia_delete_ethernet
    env = ixia_delete_ethernet(
        host=args.host, topology=args.topology,
        device_group=name_or_index(args.device_group), name=args.name,
        port=args.port, user=args.user, confirm=True,
    )
    return emit(env, as_json=args.json)


# ------------------------------------------------------------------------ ipv4

def _ipv4_create(args: argparse.Namespace) -> int:
    from ixia_tools.stack import ixia_create_ipv4
    env = ixia_create_ipv4(
        host=args.host, topology=args.topology,
        device_group=name_or_index(args.device_group), name=args.name,
        address=args.address, gateway=args.gateway,
        prefix_length=args.prefix_length,
        ethernet=name_or_index(args.ethernet),
        resolve_gateway=not args.no_resolve_gateway,
        port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _ipv4_delete(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="delete_ipv4",
        action=f"delete IPv4 stack {args.name!r} (and BGP peers / VRFs on "
               f"top) in DG {args.device_group!r}.",
    )
    if rc is not None:
        return rc
    from ixia_tools.stack import ixia_delete_ipv4
    env = ixia_delete_ipv4(
        host=args.host, topology=args.topology,
        device_group=name_or_index(args.device_group), name=args.name,
        ethernet=name_or_index(args.ethernet),
        port=args.port, user=args.user, confirm=True,
    )
    return emit(env, as_json=args.json)


# ------------------------------------------------------------------- netgroup

def _ng_create(args: argparse.Namespace) -> int:
    from ixia_tools.build import ixia_create_network_group
    env = ixia_create_network_group(
        host=args.host, topology=args.topology,
        device_group=args.device_group, name=args.name,
        prefix=args.prefix, prefix_len=args.prefix_len, count=args.count,
        connect_to_peer=args.connect_to_peer,
        connect_to_href=args.connect_to_href,
        advertise_as_rfc8277=args.advertise_as_rfc8277,
        label_start=args.label_start, label_step=args.label_step,
        port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _ng_get(args: argparse.Namespace) -> int:
    from ixia_tools.inspect import ixia_get_network_group
    env = ixia_get_network_group(
        host=args.host, topology=args.topology,
        network_group=args.name,
        device_group=name_or_index(args.device_group),
        port=args.port, user=args.user,
    )
    return emit(env, as_json=args.json)


def _ng_delete(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="delete_network_group",
        action=f"delete network group {args.name!r} from DG "
               f"{args.device_group!r}.",
    )
    if rc is not None:
        return rc
    from ixia_tools.build import ixia_delete_network_group
    env = ixia_delete_network_group(
        host=args.host, topology=args.topology,
        device_group=args.device_group, name=args.name,
        port=args.port, user=args.user, confirm=True,
    )
    return emit(env, as_json=args.json)


# ---------------------------------------------------------------- registration

def _add_start_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--wait-for-vports-ready-ms", type=int, default=60_000,
                   help="chassis-readiness preflight wait in ms (default 60000)")
    p.add_argument("--no-apply-changes", action="store_true",
                   help="skip the implicit Apply Changes before Start")
    p.add_argument("--apply-changes-timeout-s", type=int, default=60,
                   help="deadline for the implicit Apply Changes (default 60)")
    p.add_argument("--force", action="store_true",
                   help="skip the vport-readiness preflight wait")


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("topo", help="topology / stacks")
    sub = grp.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", parents=[parent],
                   help="list topologies").set_defaults(func=_list)

    g = sub.add_parser("get", parents=[parent], help="deep-read a topology")
    g.add_argument("name")
    g.set_defaults(func=_get)

    c = sub.add_parser("create", parents=[parent], help="create a topology")
    c.add_argument("--name", required=True)
    c.add_argument("--vport", action="append", default=[], metavar="HREF",
                   help="vport href to bind (repeatable)")
    c.set_defaults(func=_create)

    d = sub.add_parser("delete", parents=[parent],
                       help="delete a topology (--yes)")
    d.add_argument("name")
    d.set_defaults(func=_delete)

    st = sub.add_parser("start", parents=[parent],
                        help="start protocols on a topology")
    st.add_argument("name")
    _add_start_opts(st)
    st.set_defaults(func=_start)

    sp = sub.add_parser("stop", parents=[parent],
                        help="stop protocols on a topology (--yes)")
    sp.add_argument("name")
    sp.set_defaults(func=_stop)

    # ---- dg ----
    dg = sub.add_parser("dg", help="device-group create/delete/start/stop")
    dgs = dg.add_subparsers(dest="dgcmd", required=True)

    dgc = dgs.add_parser("create", parents=[parent], help="add a device group")
    dgc.add_argument("--topology", required=True)
    dgc.add_argument("--name", required=True)
    dgc.add_argument("--multiplier", type=int, default=1)
    dgc.set_defaults(func=_dg_create)

    dgd = dgs.add_parser("delete", parents=[parent],
                         help="remove a device group (--yes)")
    dgd.add_argument("--topology", required=True)
    dgd.add_argument("--name", required=True)
    dgd.set_defaults(func=_dg_delete)

    dgst = dgs.add_parser("start", parents=[parent], help="start a device group")
    dgst.add_argument("--topology", required=True)
    dgst.add_argument("--name", required=True)
    _add_start_opts(dgst)
    dgst.set_defaults(func=_dg_start)

    dgsp = dgs.add_parser("stop", parents=[parent],
                          help="stop a device group (--yes)")
    dgsp.add_argument("--topology", required=True)
    dgsp.add_argument("--name", required=True)
    dgsp.set_defaults(func=_dg_stop)

    # ---- eth ----
    eth = sub.add_parser("eth", help="ethernet stack create/delete")
    eths = eth.add_subparsers(dest="ethcmd", required=True)

    ec = eths.add_parser("create", parents=[parent], help="add an ethernet stack")
    ec.add_argument("--topology", required=True)
    ec.add_argument("--device-group", required=True)
    ec.add_argument("--name", required=True)
    ec.add_argument("--mac", default=None)
    ec.add_argument("--mtu", type=int, default=None)
    ec.add_argument("--vlan-id", type=int, default=None)
    ec.add_argument("--vlan-priority", type=int, default=None)
    ec.add_argument("--vlan-tpid", default=None)
    ec.set_defaults(func=_eth_create)

    ed = eths.add_parser("delete", parents=[parent],
                         help="delete an ethernet stack (--yes)")
    ed.add_argument("--topology", required=True)
    ed.add_argument("--device-group", required=True)
    ed.add_argument("--name", required=True)
    ed.set_defaults(func=_eth_delete)

    # ---- ipv4 ----
    ipv4 = sub.add_parser("ipv4", help="IPv4 stack create/delete")
    ipv4s = ipv4.add_subparsers(dest="ipv4cmd", required=True)

    ic = ipv4s.add_parser("create", parents=[parent], help="add an IPv4 stack")
    ic.add_argument("--topology", required=True)
    ic.add_argument("--device-group", required=True)
    ic.add_argument("--name", required=True)
    ic.add_argument("--address", required=True)
    ic.add_argument("--gateway", required=True)
    ic.add_argument("--prefix-length", type=int, default=24)
    ic.add_argument("--ethernet", default="1",
                    help="parent ethernet: name or 1-based index (default 1)")
    ic.add_argument("--no-resolve-gateway", action="store_true",
                    help="suppress gateway ARP at start")
    ic.set_defaults(func=_ipv4_create)

    idl = ipv4s.add_parser("delete", parents=[parent],
                           help="delete an IPv4 stack (--yes)")
    idl.add_argument("--topology", required=True)
    idl.add_argument("--device-group", required=True)
    idl.add_argument("--name", required=True)
    idl.add_argument("--ethernet", default="1",
                     help="parent ethernet: name or 1-based index (default 1)")
    idl.set_defaults(func=_ipv4_delete)

    # ---- netgroup ----
    ng = sub.add_parser("netgroup", help="network-group create/get/delete")
    ngs = ng.add_subparsers(dest="ngcmd", required=True)

    ngc = ngs.add_parser("create", parents=[parent], help="add a network group")
    ngc.add_argument("--topology", required=True)
    ngc.add_argument("--device-group", required=True)
    ngc.add_argument("--name", required=True)
    ngc.add_argument("--prefix", required=True, help="network address, e.g. 4.1.4.0")
    ngc.add_argument("--prefix-len", type=int, required=True)
    ngc.add_argument("--count", type=int, default=1, help="NG multiplier")
    ngc.add_argument("--connect-to-peer", default=None,
                     help="peer name to wire the pool to")
    ngc.add_argument("--connect-to-href", default=None,
                     help="raw bgpIpv4Peer href to wire the pool to")
    ngc.add_argument("--advertise-as-rfc8277", action="store_true",
                     help="BGP-LU (RFC 8277) advertisement")
    ngc.add_argument("--label-start", type=int, default=None)
    ngc.add_argument("--label-step", type=int, default=1)
    ngc.set_defaults(func=_ng_create)

    ngg = ngs.add_parser("get", parents=[parent],
                         help="per-line breakdown of a network group")
    ngg.add_argument("--topology", required=True)
    ngg.add_argument("--name", required=True, help="network-group name")
    ngg.add_argument("--device-group", default="1",
                     help="DG name or 1-based index (default 1)")
    ngg.set_defaults(func=_ng_get)

    ngd = ngs.add_parser("delete", parents=[parent],
                         help="delete a network group (--yes)")
    ngd.add_argument("--topology", required=True)
    ngd.add_argument("--device-group", required=True)
    ngd.add_argument("--name", required=True)
    ngd.set_defaults(func=_ng_delete)
