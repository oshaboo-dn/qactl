"""Topology + vport read tools.

Topologies, device groups, and the virtual ports (vports) they bind to.
Deeper DG inspection (IPv4 / BGP / ISIS protocol stacks) and create /
delete mutating tools land in a follow-up pass.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ixia.models import IxiaError, IxiaNotFoundError, IxiaOperationError
from ixia._helpers import read_multivalue

from ixia_core.envelope import make_envelope, error_envelope
from ixia_core.session import (
    DEFAULT_PORT, DEFAULT_USER,
    get_session, session_id_of,
)


def ixia_list_topologies(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """List topologies with their bound vports and top-level device groups.

    This is the lightweight summary returned by
    ``s.topology.list()`` — it walks topologies, resolves port refs to
    vport names, and collects the full (recursive) DG-name tree, but it
    does *not* dereference protocol stacks. Use ``ixia_get_device_group``
    (coming next pass) for IPv4 / BGP / ISIS detail.

    Returns envelope with ``result = {count, topologies: [{name, ports,
    device_groups, href}]}``.
    """
    request = {"host": host, "port": port, "user": user}

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}",
            kind="list_topologies", host=host, port=port,
            status="connect_error",
        )

    env = make_envelope(
        kind="list_topologies", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        topos = s.topology.list()
        env["result"] = {
            "count": len(topos),
            "topologies": [
                {
                    "name": t.name,
                    "ports": list(t.ports),
                    "device_groups": list(t.device_groups),
                    "href": t.href,
                }
                for t in topos
            ],
        }
        if not topos:
            env["warnings"].append(
                "No topologies configured in this IxNetwork session. "
                "Load a saved .ixncfg to populate."
            )
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_list_vports(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """List virtual ports (``Vport``) — the virtual-port objects that bind
    topologies to physical Ixia chassis ports.

    Each vport carries: its name, assigned state, connection state, link
    state, and the chassis/card/port triple it points to (if assigned).
    A loaded config without chassis connectivity will still list vports
    (state=``unassigned``); they become usable once physical assignment
    happens.

    Returns envelope with ``result = {count, vports: [{name,
    assigned_to, connection_state, link_state, href}]}``.
    """
    request = {"host": host, "port": port, "user": user}

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}",
            kind="list_vports", host=host, port=port,
            status="connect_error",
        )

    env = make_envelope(
        kind="list_vports", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        vports = s.ixn.Vport.find()
        items: list[dict] = []
        for v in vports:
            items.append({
                "name": getattr(v, "Name", ""),
                "assigned_to": getattr(v, "AssignedTo", "") or None,
                "connection_state": getattr(v, "ConnectionState", ""),
                "connection_status": getattr(v, "ConnectionStatus", ""),
                "link_state": getattr(v, "State", ""),
                "is_available": bool(getattr(v, "IsAvailable", False)),
                "href": getattr(v, "href", ""),
            })
        env["result"] = {"count": len(items), "vports": items}
        if not items:
            env["warnings"].append(
                "No vports in this session. Either the config is empty, "
                "or no vports were defined. Load an .ixncfg that has ports."
            )
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def _safe_mv(mv_obj, ixn):
    """``read_multivalue`` swallows REST failures by returning None; this
    adds attribute-missing safety (the attribute may not exist on all
    RestPy object shapes) and returns None on any trouble."""
    if mv_obj is None:
        return None
    try:
        return read_multivalue(mv_obj, ixn)
    except Exception:
        return None


def _inspect_dg(dg, ixn) -> Dict[str, Any]:
    """Pull ipv4 / bgp / network-group detail out of one RestPy DG object.

    Failures on any sub-stack are reported as ``None`` rather than
    bubbling up — the goal is "learn the shape"; we don't want one
    missing attribute to tank the whole read.
    """
    info: Dict[str, Any] = {
        "name": getattr(dg, "Name", ""),
        "multiplier": int(getattr(dg, "Multiplier", 1)),
        "href": getattr(dg, "href", ""),
        "ipv4": None,
        "bgp": None,
        "network_groups": [],
    }

    # IPv4 stack — at most one per ethernet (the common case). If the
    # DG has no ethernet/ipv4 layer, leave ``ipv4`` as None.
    try:
        for eth in dg.Ethernet.find():
            for ipv4 in eth.Ipv4.find():
                info["ipv4"] = {
                    "address": _safe_mv(ipv4.Address, ixn),
                    "gateway": _safe_mv(ipv4.GatewayIp, ixn),
                    "prefix_length": _safe_mv(ipv4.Prefix, ixn),
                }
                peers = ipv4.BgpIpv4Peer.find()
                if peers:
                    peer_list = []
                    for p in peers:
                        peer_list.append({
                            "name": getattr(p, "Name", ""),
                            "dut_ip": _safe_mv(p.DutIp, ixn),
                            "local_as": _safe_mv(
                                getattr(p, "LocalAs2Bytes", None), ixn,
                            ),
                            "dut_as": _safe_mv(
                                getattr(p, "DutAs2Bytes", None), ixn,
                            ),
                            "type": _safe_mv(
                                getattr(p, "Type", None), ixn,
                            ),
                        })
                    info["bgp"] = {
                        "peer_count": len(peer_list),
                        "peers": peer_list,
                    }
                break
            if info["ipv4"] is not None:
                break
    except Exception:
        pass

    # NetworkGroups (route ranges) attached to the DG.
    try:
        for ng in dg.NetworkGroup.find():
            pools = []
            try:
                for p in ng.Ipv4PrefixPools.find():
                    pools.append({
                        "family": "ipv4",
                        "network": _safe_mv(p.NetworkAddress, ixn),
                        "prefix_length": _safe_mv(p.PrefixLength, ixn),
                        "count": int(getattr(p, "NumberOfAddresses", 0) or 0),
                    })
            except Exception:
                pass
            info["network_groups"].append({
                "name": getattr(ng, "Name", ""),
                "multiplier": int(getattr(ng, "Multiplier", 1)),
                "pools": pools,
            })
    except Exception:
        pass

    return info


def ixia_get_topology(
    host: str,
    name: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Deep-read a topology by exact name.

    Returns the topology, its bound vports, and each DG's IPv4 + BGP +
    NetworkGroup detail. Best-effort per DG — missing stacks are
    reported as ``None`` rather than failing the whole call. Useful for
    answering "what BGP peers / route ranges does bgp-leak actually
    configure?" without round-tripping through the GUI.
    """
    request = {"host": host, "port": port, "user": user, "name": name}
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="get_topology",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="get_topology", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        target = None
        for tp in s.ixn.Topology.find():
            if getattr(tp, "Name", "") == name:
                target = tp
                break
        if target is None:
            env["status"] = "error"
            env["errors"].append(f"Topology {name!r} not found.")
            env["next_actions"].append(
                "Call ixia_list_topologies to see available names."
            )
            return env

        vport_hrefs = list(getattr(target, "Vports", []) or [])
        vport_names: list[str] = []
        for v in s.ixn.Vport.find():
            if v.href in vport_hrefs:
                vport_names.append(v.Name)

        dgs = [_inspect_dg(dg, s.ixn) for dg in target.DeviceGroup.find()]

        env["result"] = {
            "name": getattr(target, "Name", name),
            "href": getattr(target, "href", ""),
            "vports": [
                {"name": n, "href": h}
                for n, h in zip(vport_names, vport_hrefs)
            ],
            "device_groups": dgs,
        }
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_list_chassis(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """List Ixia chassis the API server knows about and their ports.

    Returns every chassis under ``AvailableHardware.Chassis`` plus a
    flat ``ports`` list of ``{chassis, card, port, owner, state,
    type, vport_href}`` rows — the authoritative answer to "which
    physical interfaces exist, which are mine, which are taken?".
    """
    request = {"host": host, "port": port, "user": user}
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="list_chassis",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="list_chassis", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    # Map physical assignment -> vport for cross-reference. Format is
    # ``chassis:card:port`` (same string the GUI shows).
    vport_by_assignment: Dict[str, Dict[str, Any]] = {}
    try:
        for v in s.ixn.Vport.find():
            ass = getattr(v, "AssignedTo", "") or ""
            if ass:
                vport_by_assignment[ass] = {
                    "name": getattr(v, "Name", ""),
                    "href": getattr(v, "href", ""),
                }
    except Exception:
        pass

    try:
        chassis_rows: list[dict] = []
        port_rows: list[dict] = []
        ch_root = getattr(s.ixn, "AvailableHardware", None)
        if ch_root is None:
            env["warnings"].append(
                "No AvailableHardware root on this session (API Server "
                "may not have completed chassis discovery yet)."
            )
            env["result"] = {"chassis": [], "ports": []}
            return env

        for ch in ch_root.Chassis.find():
            ch_host = getattr(ch, "Hostname", "") or getattr(ch, "Ip", "")
            ch_state = getattr(ch, "State", "")
            chassis_rows.append({
                "hostname": ch_host,
                "state": ch_state,
                "chain_topology": getattr(ch, "ChainTopology", ""),
                "ix_os_ver": getattr(ch, "IxosVersion", ""),
            })
            try:
                for card in ch.Card.find():
                    card_num = getattr(card, "CardId", None)
                    card_type = getattr(card, "Description", "") or getattr(
                        card, "Type", ""
                    )
                    for pt in card.Port.find():
                        port_num = getattr(pt, "PortId", None)
                        assign_key = f"{ch_host}:{card_num}:{port_num}"
                        owner = getattr(pt, "Owner", "") or ""
                        port_rows.append({
                            "chassis": ch_host,
                            "card": card_num,
                            "card_type": card_type,
                            "port": port_num,
                            "type": getattr(pt, "Type", ""),
                            "state": getattr(pt, "State", ""),
                            "owner": owner,
                            "vport": vport_by_assignment.get(assign_key),
                        })
            except Exception as e:
                env["warnings"].append(
                    f"Card/port walk failed for chassis {ch_host!r}: "
                    f"{type(e).__name__}: {str(e)[:160]}"
                )

        env["result"] = {
            "chassis": chassis_rows,
            "ports": port_rows,
            "port_count": len(port_rows),
        }
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(ixia_list_topologies)
    mcp.tool()(ixia_list_vports)
    mcp.tool()(ixia_get_topology)
    mcp.tool()(ixia_list_chassis)
