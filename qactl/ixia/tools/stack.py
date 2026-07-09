"""Per-layer NGPF stack builders: ethernet, IPv4, BGP peer, BGP-VRF.

These tools sit one layer below ``ixia_create_device_group`` and one
layer above ``ixia_create_network_group`` — they let an agent assemble
a full DG protocol stack without dropping into ``ixia_rest_patch`` for
each multivalue.

Layered against the NGPF tree:

    Topology
      └─ DeviceGroup            ← ixia_create_device_group
          └─ Ethernet           ← ixia_create_ethernet  (this module)
              └─ Ipv4           ← ixia_create_ipv4
                  └─ BgpIpv4Peer ← ixia_create_bgp_peer
                       └─ BgpVrf ← ixia_create_bgp_vrf
          └─ NetworkGroup        ← ixia_create_network_group
              └─ Ipv4PrefixPool
                  └─ bgpIPRouteProperty / bgpL3VpnRouteProperty

Why these and not "one big builder"
-----------------------------------
Per the ``mcp-tool-policy.mdc`` rule (general primitives only): one
verb per object class keeps the surface composable. Agents that need a
full eBGP-on-VLAN pipe call the four tools in sequence; agents that
only need to add a VRF to an existing peer call just one. A single
``ixia_create_full_stack`` helper would either bake in a specific
scenario (anti-rule) or grow as many parameters as the four tools
combined (no win).

Implementation notes shared by every tool here
----------------------------------------------
- **Multivalues via raw REST.** Same pattern as
  ``ixia_create_network_group``: ``RestPy.add()`` for the parent
  object (which uses ``_create()`` — not the licence-gated
  ``_add_xpath()``), then PATCH ``<mv_href>/singleValue`` for each
  attribute. RestPy's ``mv.Single()`` works for most attributes on
  this lab but has hit Batch Assistance gates on ``bgpIPRouteProperty``
  multivalues; staying consistent with the build.py path means new
  attributes can be added without re-discovering that landmine.
  See lesson ``2026-05-05-route-toggle-resolved.md`` and the
  shared :func:`qactl.ixia.tools._ngpf_lookup.patch_singlevalue` helper.
- **No silent-bounce on plain create.** Adding a stack under a running
  topology is accepted by IxNetwork — the new sub-stack just stays in
  ``notStarted`` until the parent DG is restarted (or
  ``ixia_route_apply_pending`` pulses ``ApplyOnTheFly``). This matches
  ``ixia_create_device_group``'s policy. The ``mcp-tool-policy.mdc``
  bounce rule kicks in only for connector/multivalue mutations
  IxNetwork rejects mid-flight; pure ``.add()`` on a fresh child is
  not in that set.
- **Up-front parent resolution.** Bad topology / DG / ethernet / ipv4
  / peer names fail before any ``.add()`` POST so we don't leave
  half-built children behind. Children that ARE created and then
  partially fail are rolled back via best-effort ``remove()`` (same
  pattern as ``ixia_create_network_group``).

Scope of this pass
------------------
- IPv4 / BGP IPv4 only. IPv6 + BGP IPv6 follow the same shape but
  hit different RestPy classes (``Ipv6``, ``BgpIpv6Peer``); add as
  sibling tools when needed instead of overloading the IPv4 path.
- One Ethernet / IPv4 / Peer per ``.add()`` call. Per-line override
  (mac list, address list, etc.) is left to ``ixia_rest_patch`` until
  a real use case shows up — keeping the surface boring until we know
  the actual shape avoids growing API tax for nobody.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Union

from qactl.ixia.client.models import IxiaError, IxiaNotFoundError, IxiaOperationError

from qactl.ixia.core.envelope import make_envelope, error_envelope
from qactl.ixia.core.session import (
    DEFAULT_PORT, DEFAULT_USER,
    get_session, write_lock, session_id_of,
)
from qactl.ixia.tools._ngpf_lookup import (
    confirm_guard,
    extract_self_href,
    find_bgp_peer,
    patch_singlevalue,
    patch_singlevalue_if_set,
    resolve_device_group,
    resolve_ethernet,
    resolve_ipv4,
    resolve_topology,
)
# ``_bounce_if_running`` is the silent stop-mutate-start context
# manager every mutating tool in this codebase uses on a running parent
# topology, per ``.cursor/rules/ixia/mcp-tool-policy.mdc``. Sourced
# from build.py (where the lifecycle concept already lives) instead of
# duplicated; the cross-module import is fine — ``build.py`` doesn't
# import anything from ``stack.py``, so no cycle.
from qactl.ixia.tools.build import _bounce_if_running, _topology_is_running


# ----------------------------------------------------------------------
# Capability label → RestPy attribute mapping
# ----------------------------------------------------------------------
# Keys are the labels callers of ``ixia_create_bgp_peer`` use in the
# ``capabilities`` dict; values are the multivalue field name in the
# bgpIpv4Peer body (i.e. the camelCase REST attribute).
_PEER_CAPABILITY_FIELDS: Dict[str, str] = {
    "ipv4_unicast": "capabilityIpV4Unicast",
    "ipv4_mpls": "capabilityIpV4Mpls",
    "ipv4_mpls_vpn": "capabilityIpV4MplsVpn",
    "ipv4_multicast": "capabilityIpV4Multicast",
    "ipv6_unicast": "capabilityIpV6Unicast",
    "ipv6_mpls": "capabilityIpV6Mpls",
    "ipv6_mpls_vpn": "capabilityIpV6MplsVpn",
    "vpls": "capabilityVpls",
    "evpn": "capabilityEvpn",
    "route_refresh": "capabilityRouteRefresh",
    "rt_constraint": "capabilityRouteConstraint",
    "graceful_restart": "enableGracefulRestart",
    "long_lived_gr": "enableLlgr",
}

# Scalar (non-multivalue) capability bools on bgpIpv4Peer. Set via
# direct PATCH on the parent body (no /singleValue suffix).
# See lesson 2026-05-04-lu-capability-correction.md — these are NOT
# the same as their multivalue siblings.
_PEER_SCALAR_CAPABILITY_FIELDS: Dict[str, str] = {
    "ipv4_multi_mpls_labels": "ipv4MultipleMplsLabelsCapability",
    "ipv6_multi_mpls_labels": "ipv6MultipleMplsLabelsCapability",
}

# Valid peer types — what IxNetwork's bgpIpv4Peer.Type multivalue
# accepts. We narrow to the two sane cases for now; users that need
# the SDN variants can call ``ixia_rest_patch`` directly.
_PEER_TYPES = {"internal", "external"}

# Valid ``modeOfBfdOperations`` enum values on bgpIpv4Peer (the
# IxNetwork server spells them lowercase: ``0=multihop 1=singlehop``).
# Single-hop is the default for a directly-connected eBGP peer;
# multi-hop is for BGP-over-BFD across more than one IP hop.
_BFD_MODES = {"singlehop", "multihop"}


# ----------------------------------------------------------------------
# Ethernet
# ----------------------------------------------------------------------

def ixia_create_ethernet(
    host: str,
    topology: str,
    device_group: Union[str, int],
    name: str,
    mac: Optional[str] = None,
    mtu: Optional[int] = None,
    vlan_id: Optional[int] = None,
    vlan_priority: Optional[int] = None,
    vlan_tpid: Optional[str] = None,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Add an Ethernet stack under a DeviceGroup.

    The Ethernet stack is the bottom of every NGPF protocol pipe — IPv4,
    IPv6, BGP, OSPF, and traffic items all chain onto it. Most of the
    time you only need ``name`` plus the parent DG; ``mac`` and
    ``mtu`` get IxNetwork defaults if omitted (auto-incrementing MAC
    starting at ``00:11:01:00:00:01`` per session, MTU 1500). Pass
    ``vlan_id`` to enable a single 802.1Q tag — multi-tag stacks
    (Q-in-Q, multi-VLAN per session) are out of scope for this tool;
    use ``ixia_rest_patch`` after creation to add additional Vlan
    children.

    Args:
        topology: Parent topology name (exact match).
        device_group: Parent DG. Pass the exact name (str) or a 1-based
            index (int) into ``Topology.DeviceGroup.find()``.
        name: Ethernet stack name. Must be unique inside the DG.
        mac: Optional source MAC — single value, applied to every
            session in the DG. Format: ``"aa:bb:cc:dd:ee:ff"``. Omit
            to keep IxNetwork's auto-generated default.
        mtu: Optional L2 MTU in bytes (68-14000). Omit for IxNetwork
            default (1500).
        vlan_id: Optional 12-bit 802.1Q VLAN ID (1-4094). Setting this
            enables ``EnableVlans=True`` and ``VlanCount=1`` on the
            ethernet stack and PATCHes the auto-created ``vlan/1`` child
            with the requested ID. Omit to leave the stack untagged.
        vlan_priority: Optional 3-bit PCP value (0-7). Only honoured
            when ``vlan_id`` is also set. Omit to keep IxNetwork
            default (0).
        vlan_tpid: Optional Tag Protocol Identifier as IxNetwork enum
            string. Common values: ``"ethertype8100"`` (default,
            standard 802.1Q), ``"ethertype88a8"`` (S-VLAN / 802.1ad),
            ``"ethertype9100"``, ``"ethertype9200"``. Only honoured
            when ``vlan_id`` is set.

    Returns envelope with
    ``result = {topology, device_group, name, href, mac, mtu, vlan}``
    where ``vlan`` is ``None`` (untagged) or
    ``{vlan_href, vlan_id, priority, tpid}``.

    Notes:
        - This tool does NOT bounce a running topology. The new
          ethernet stack lands in ``notStarted``; restart the parent
          DG (``ixia_dg_start``) or topology to bring it up.
        - VlanCount > 1 (multi-tag) is intentionally not supported
          here — it changes which ethernet attributes apply per-line
          and is rare enough in DriveNets test work that the tax of
          adding it isn't worth paying yet.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group, "name": name,
        "mac": mac, "mtu": mtu, "vlan_id": vlan_id,
        "vlan_priority": vlan_priority, "vlan_tpid": vlan_tpid,
    }

    if vlan_id is not None and (
        not isinstance(vlan_id, int) or vlan_id < 1 or vlan_id > 4094
    ):
        return error_envelope(
            "vlan_id must be an integer in [1, 4094].",
            kind="create_ethernet", host=host, port=port,
            status="bad_argument",
        )
    if vlan_priority is not None and (
        not isinstance(vlan_priority, int) or vlan_priority < 0 or vlan_priority > 7
    ):
        return error_envelope(
            "vlan_priority must be an integer in [0, 7].",
            kind="create_ethernet", host=host, port=port,
            status="bad_argument",
        )
    if mtu is not None and (
        not isinstance(mtu, int) or mtu < 68 or mtu > 14000
    ):
        return error_envelope(
            "mtu must be an integer in [68, 14000].",
            kind="create_ethernet", host=host, port=port,
            status="bad_argument",
        )

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="create_ethernet",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="create_ethernet", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        ixn = s.ixn
        with write_lock(host, port, user):
            topo = resolve_topology(ixn, topology)
            dg = resolve_device_group(topo, device_group)

            # Set VlanCount + UseVlans at create time: the server then
            # auto-instantiates VlanCount child Vlan rows. Adding a
            # Vlan after the fact would have to go through
            # ``Vlan.add()`` which uses ``_add_xpath`` (Batch
            # Assistance — not on this lab licence).
            add_kwargs: Dict[str, Any] = {"Name": name}
            if vlan_id is not None:
                add_kwargs["UseVlans"] = True
                add_kwargs["VlanCount"] = 1
            eth = dg.Ethernet.add(**add_kwargs)
            eth_href = getattr(eth, "href", "")
            if not eth_href:
                raise IxiaOperationError(
                    "ethernet href missing after creation."
                )

            try:
                eth_body = ixn._connection._read(eth_href)
                if not isinstance(eth_body, dict):
                    raise IxiaOperationError(
                        f"Unexpected ethernet body shape for {eth_href}: "
                        f"{type(eth_body).__name__}"
                    )

                patch_singlevalue_if_set(ixn, eth_body["mac"], mac)
                patch_singlevalue_if_set(ixn, eth_body["mtu"], mtu)

                vlan_result: Optional[Dict[str, Any]] = None
                if vlan_id is not None:
                    # The auto-created Vlan row lives under ethernet/N/vlan/1.
                    # Use RestPy .find() to discover its href so we can
                    # PATCH the multivalues directly.
                    vlans = list(eth.Vlan.find())
                    if not vlans:
                        raise IxiaOperationError(
                            "Ethernet was created with VlanCount=1 but no "
                            "Vlan child appeared. Check the IxNetwork API "
                            "server logs."
                        )
                    vlan = vlans[0]
                    vlan_href = getattr(vlan, "href", "")
                    if not vlan_href:
                        raise IxiaOperationError(
                            "vlan child href missing after auto-create."
                        )
                    vlan_body = ixn._connection._read(vlan_href)
                    patch_singlevalue(ixn, vlan_body["vlanId"], int(vlan_id))
                    if vlan_priority is not None:
                        patch_singlevalue(
                            ixn, vlan_body["priority"], int(vlan_priority),
                        )
                    if vlan_tpid is not None:
                        patch_singlevalue(ixn, vlan_body["tpid"], vlan_tpid)
                    vlan_result = {
                        "vlan_href": vlan_href,
                        "vlan_id": int(vlan_id),
                        "priority": vlan_priority,
                        "tpid": vlan_tpid,
                    }
            except Exception:
                # Roll back the half-built ethernet stack so the DG
                # doesn't accumulate zombies. Best-effort — rollback
                # failure must not mask the original error.
                try:
                    eth.remove()
                except Exception:
                    pass
                raise

        env["result"] = {
            "topology": topology,
            "device_group": getattr(dg, "Name", str(device_group)),
            "name": getattr(eth, "Name", name),
            "href": eth_href,
            "mac": mac,
            "mtu": mtu,
            "vlan": vlan_result,
        }
        return env
    except IxiaNotFoundError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        env["next_actions"].append(
            "Run `qactl ixia session describe` to confirm topology / DG names."
        )
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


# ----------------------------------------------------------------------
# IPv4
# ----------------------------------------------------------------------

def ixia_create_ipv4(
    host: str,
    topology: str,
    device_group: Union[str, int],
    name: str,
    address: str,
    gateway: str,
    prefix_length: int = 24,
    ethernet: Union[str, int] = 1,
    resolve_gateway: bool = True,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Add an IPv4 stack on top of an existing Ethernet stack.

    The IPv4 stack carries one address per session (DG multiplier),
    one gateway, one prefix length. Per-line overrides (different IPs
    on different sessions) are left to ``ixia_rest_patch`` — see
    ``ixia_rest_patch`` + ``<mv_href>/valueList`` for that path.

    Args:
        topology: Parent topology name (exact match).
        device_group: Parent DG. Name (str) or 1-based index (int).
        ethernet: Parent Ethernet stack inside the DG. Name (str) or
            1-based index (int, default ``1`` — the first ethernet
            stack, which is the common case where each DG has exactly
            one ethernet).
        name: IPv4 stack name. Must be unique inside the parent
            ethernet.
        address: IPv4 address as ``"a.b.c.d"`` — applied as the
            singleValue across every session in the DG (each session
            increments by 1 by default, per IxNetwork behaviour).
        gateway: Default gateway IP as ``"a.b.c.d"``. Required —
            IxNetwork won't ARP without one. For unnumbered / no-gw
            scenarios, set to your local address and pass
            ``resolve_gateway=False`` so the stack doesn't try to ARP
            it.
        prefix_length: Subnet mask length in bits, default 24.
        resolve_gateway: When True (default), the IPv4 stack will ARP
            for the gateway MAC at start. Setting False suppresses
            ARP — useful for back-to-back tests where the DUT side is
            already known to be up and you want to avoid the ARP
            wait. Has no effect on traffic that uses
            ``ManualGatewayMac``.

    Returns envelope with
    ``result = {topology, device_group, ethernet, name, href,
    address, gateway, prefix_length, resolve_gateway}``.

    Notes:
        - No silent-bounce: adding a fresh IPv4 stack to a running DG
          is accepted by IxNetwork; the new stack stays
          ``notStarted`` until the DG is restarted.
        - If the parent ethernet does not exist, the call fails before
          any POST.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "ethernet": ethernet, "name": name,
        "address": address, "gateway": gateway,
        "prefix_length": prefix_length,
        "resolve_gateway": resolve_gateway,
    }

    if not isinstance(prefix_length, int) or prefix_length < 0 or prefix_length > 32:
        return error_envelope(
            "prefix_length must be an integer in [0, 32].",
            kind="create_ipv4", host=host, port=port,
            status="bad_argument",
        )
    if not address or not gateway:
        return error_envelope(
            "address and gateway are both required.",
            kind="create_ipv4", host=host, port=port,
            status="bad_argument",
        )

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="create_ipv4",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="create_ipv4", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        ixn = s.ixn
        with write_lock(host, port, user):
            topo = resolve_topology(ixn, topology)
            dg = resolve_device_group(topo, device_group)
            eth = resolve_ethernet(dg, ethernet)

            ipv4 = eth.Ipv4.add(Name=name)
            ipv4_href = getattr(ipv4, "href", "")
            if not ipv4_href:
                raise IxiaOperationError(
                    "ipv4 href missing after creation."
                )

            try:
                ipv4_body = ixn._connection._read(ipv4_href)
                if not isinstance(ipv4_body, dict):
                    raise IxiaOperationError(
                        f"Unexpected ipv4 body shape for {ipv4_href}: "
                        f"{type(ipv4_body).__name__}"
                    )
                patch_singlevalue(ixn, ipv4_body["address"], address)
                patch_singlevalue(ixn, ipv4_body["gatewayIp"], gateway)
                patch_singlevalue(ixn, ipv4_body["prefix"], int(prefix_length))
                if resolve_gateway is False:
                    patch_singlevalue(
                        ixn, ipv4_body["resolveGateway"], "false",
                    )
            except Exception:
                try:
                    ipv4.remove()
                except Exception:
                    pass
                raise

        env["result"] = {
            "topology": topology,
            "device_group": getattr(dg, "Name", str(device_group)),
            "ethernet": getattr(eth, "Name", str(ethernet)),
            "name": getattr(ipv4, "Name", name),
            "href": ipv4_href,
            "address": address,
            "gateway": gateway,
            "prefix_length": int(prefix_length),
            "resolve_gateway": bool(resolve_gateway),
        }
        return env
    except IxiaNotFoundError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        env["next_actions"].append(
            "Run `qactl ixia session describe` to confirm DG / ethernet names."
        )
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


# ----------------------------------------------------------------------
# BGP IPv4 peer
# ----------------------------------------------------------------------

def ixia_create_bgp_peer(
    host: str,
    topology: str,
    device_group: Union[str, int],
    name: str,
    dut_ip: str,
    local_as: int,
    peer_type: str = "external",
    ipv4: Union[str, int] = 1,
    hold_timer: Optional[int] = None,
    keepalive_timer: Optional[int] = None,
    capabilities: Optional[Dict[str, bool]] = None,
    bfd: Optional[bool] = None,
    bfd_mode: Optional[str] = None,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Add a bgpIpv4Peer on top of an existing IPv4 stack.

    Sets the peer identity (``DutIp``, ``LocalAs2Bytes``, ``Type``),
    optional hold/keepalive timers, and an optional capability vector.
    The default capability set leaves IxNetwork's defaults alone —
    pass ``capabilities={"ipv4_unicast": True}`` to lock that on, or
    ``{"ipv4_mpls": True, "ipv4_multi_mpls_labels": False}`` for plain
    BGP-LU (RFC 8277, ``NoOfLabels=1`` — the May-4 lesson recipe).

    Args:
        topology: Parent topology name (exact match).
        device_group: Parent DG. Name (str) or 1-based index (int).
        ipv4: Parent IPv4 stack inside the chosen ethernet. Name
            (str) or 1-based index (int, default ``1``). NOTE: this
            indexes into the IPv4 stacks under the *first* ethernet
            of the DG. If you have multiple ethernet stacks each with
            their own IPv4, look the IPv4 up by name instead.
        name: Peer name. Must be unique inside the IPv4 stack.
        dut_ip: DUT-side BGP neighbour IPv4 address.
        local_as: Local 2-byte ASN. For 4-byte ASNs above 65535, use
            ``ixia_rest_patch`` to set ``localAs4Bytes`` and
            ``enable4ByteAs`` after creation — this tool keeps the
            common 2-byte path.
        peer_type: ``"external"`` (eBGP, default) or ``"internal"``
            (iBGP). Other IxNetwork values (``externalsdn``,
            ``internalsdn``) are intentionally not exposed here —
            reach for ``ixia_rest_patch`` if you need them.
        hold_timer: Optional hold timer in seconds. Omit for
            IxNetwork default (90).
        keepalive_timer: Optional keepalive timer in seconds. Omit
            for IxNetwork default (30).
        capabilities: Optional ``{label: bool}`` dict. Recognised
            labels:

            - Multivalue capabilities: ``ipv4_unicast``, ``ipv4_mpls``,
              ``ipv4_mpls_vpn``, ``ipv4_multicast``, ``ipv6_unicast``,
              ``ipv6_mpls``, ``ipv6_mpls_vpn``, ``vpls``, ``evpn``,
              ``route_refresh``, ``rt_constraint``,
              ``graceful_restart``, ``long_lived_gr``.
            - Scalar capabilities (PATCHed on the parent body, NOT
              ``/singleValue``): ``ipv4_multi_mpls_labels``,
              ``ipv6_multi_mpls_labels``.

            See lesson ``2026-05-04-lu-capability-correction.md`` —
            ``ipv4_mpls`` (multivalue, AFI=1/SAFI=4) and
            ``ipv4_multi_mpls_labels`` (scalar, multi-labels-per-route
            extension) are NOT aliases. Set ``ipv4_mpls=True`` for
            standard BGP-LU; only set ``ipv4_multi_mpls_labels=True``
            when you actually want a label stack.
        bfd: Register this peer for BGP-over-BFD
            (``enableBfdRegistration``). ``True`` ties the peer's
            session liveness to a BFD session — pair it with a
            ``bfdv4Interface`` on the same IPv4 stack
            (``ixia_create_bfdv4_interface``) so there's an actual BFD
            session to track. ``False`` explicitly clears it; omit to
            leave the IxNetwork default.
        bfd_mode: ``modeOfBfdOperations`` — ``"singlehop"`` (default,
            directly-connected peer) or ``"multihop"`` (BFD across
            more than one IP hop). Only meaningful when ``bfd`` is
            registered; sets the multivalue regardless so a later
            registration picks it up.

    Returns envelope with
    ``result = {topology, device_group, ipv4, name, href, dut_ip,
    local_as, peer_type, hold_timer, keepalive_timer,
    capabilities_set}`` where ``capabilities_set`` is the dict that
    was actually applied (with unknown labels filtered out into
    ``warnings``).

    Notes:
        - No silent-bounce on create. Capability changes on a
          *running* peer don't renegotiate OPEN until the peer (or
          parent DG) is restarted — but this tool only sets caps on
          a brand-new peer that's still ``notStarted``, so that
          gotcha doesn't bite here.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "ipv4": ipv4, "name": name,
        "dut_ip": dut_ip, "local_as": local_as, "peer_type": peer_type,
        "hold_timer": hold_timer, "keepalive_timer": keepalive_timer,
        "capabilities": dict(capabilities or {}),
        "bfd": bfd, "bfd_mode": bfd_mode,
    }

    if bfd_mode is not None and bfd_mode not in _BFD_MODES:
        return error_envelope(
            f"bfd_mode must be one of {sorted(_BFD_MODES)}, got "
            f"{bfd_mode!r}.",
            kind="create_bgp_peer", host=host, port=port,
            status="bad_argument",
        )
    if peer_type not in _PEER_TYPES:
        return error_envelope(
            f"peer_type must be one of {sorted(_PEER_TYPES)}, got {peer_type!r}.",
            kind="create_bgp_peer", host=host, port=port,
            status="bad_argument",
        )
    if not isinstance(local_as, int) or local_as < 0 or local_as > 65535:
        return error_envelope(
            "local_as must be a 2-byte integer in [0, 65535]. "
            "For 4-byte ASNs use `qactl ixia rest patch` on localAs4Bytes "
            "after creation.",
            kind="create_bgp_peer", host=host, port=port,
            status="bad_argument",
        )
    if hold_timer is not None and (
        not isinstance(hold_timer, int) or hold_timer < 0
    ):
        return error_envelope(
            "hold_timer must be a non-negative integer (seconds).",
            kind="create_bgp_peer", host=host, port=port,
            status="bad_argument",
        )
    if keepalive_timer is not None and (
        not isinstance(keepalive_timer, int) or keepalive_timer < 0
    ):
        return error_envelope(
            "keepalive_timer must be a non-negative integer (seconds).",
            kind="create_bgp_peer", host=host, port=port,
            status="bad_argument",
        )

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="create_bgp_peer",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="create_bgp_peer", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    caps_in = dict(capabilities or {})
    caps_applied: Dict[str, bool] = {}
    unknown_caps: List[str] = []
    for label in caps_in:
        if label not in _PEER_CAPABILITY_FIELDS and label not in _PEER_SCALAR_CAPABILITY_FIELDS:
            unknown_caps.append(label)
    if unknown_caps:
        env["warnings"].append(
            f"Ignoring unknown capability label(s): {sorted(unknown_caps)}. "
            f"Valid: {sorted(list(_PEER_CAPABILITY_FIELDS) + list(_PEER_SCALAR_CAPABILITY_FIELDS))}."
        )

    try:
        ixn = s.ixn
        with write_lock(host, port, user):
            topo = resolve_topology(ixn, topology)
            dg = resolve_device_group(topo, device_group)
            # Walk the FIRST ethernet's IPv4 children. Multi-ethernet
            # DGs are rare on this lab; named lookup still works.
            eth = resolve_ethernet(dg, 1)
            ipv4_obj = resolve_ipv4(eth, ipv4)

            peer = ipv4_obj.BgpIpv4Peer.add(Name=name)
            peer_href = getattr(peer, "href", "")
            if not peer_href:
                raise IxiaOperationError(
                    "bgpIpv4Peer href missing after creation."
                )

            try:
                peer_body = ixn._connection._read(peer_href)
                if not isinstance(peer_body, dict):
                    raise IxiaOperationError(
                        f"Unexpected bgpIpv4Peer body shape for {peer_href}: "
                        f"{type(peer_body).__name__}"
                    )

                patch_singlevalue(ixn, peer_body["dutIp"], dut_ip)
                patch_singlevalue(
                    ixn, peer_body["localAs2Bytes"], int(local_as),
                )
                patch_singlevalue(ixn, peer_body["type"], peer_type)
                patch_singlevalue_if_set(
                    ixn, peer_body["holdTimer"], hold_timer,
                )
                patch_singlevalue_if_set(
                    ixn, peer_body["keepaliveTimer"], keepalive_timer,
                )

                # Multivalue capabilities — PATCH /singleValue
                for label, value in caps_in.items():
                    if label not in _PEER_CAPABILITY_FIELDS:
                        continue
                    field = _PEER_CAPABILITY_FIELDS[label]
                    mv_href = peer_body.get(field)
                    if not isinstance(mv_href, str) or not mv_href:
                        env["warnings"].append(
                            f"capability {label!r} expected multivalue href "
                            f"at peer body[{field!r}]; got {mv_href!r}. Skipped."
                        )
                        continue
                    patch_singlevalue(ixn, mv_href, "true" if value else "false")
                    caps_applied[label] = bool(value)

                # Scalar capabilities — PATCH the parent body directly
                scalar_patch: Dict[str, Any] = {}
                for label, value in caps_in.items():
                    if label not in _PEER_SCALAR_CAPABILITY_FIELDS:
                        continue
                    field = _PEER_SCALAR_CAPABILITY_FIELDS[label]
                    scalar_patch[field] = bool(value)
                    caps_applied[label] = bool(value)
                if scalar_patch:
                    ixn._connection._update(peer_href, scalar_patch)

                # BGP-over-BFD registration. ``enableBfdRegistration``
                # and ``modeOfBfdOperations`` are both multivalues on
                # the peer body.
                if bfd is not None:
                    patch_singlevalue(
                        ixn, peer_body["enableBfdRegistration"],
                        "true" if bfd else "false",
                    )
                if bfd_mode is not None:
                    patch_singlevalue(
                        ixn, peer_body["modeOfBfdOperations"], bfd_mode,
                    )
            except Exception:
                try:
                    peer.remove()
                except Exception:
                    pass
                raise

        env["result"] = {
            "topology": topology,
            "device_group": getattr(dg, "Name", str(device_group)),
            "ipv4": getattr(ipv4_obj, "Name", str(ipv4)),
            "name": getattr(peer, "Name", name),
            "href": peer_href,
            "dut_ip": dut_ip,
            "local_as": int(local_as),
            "peer_type": peer_type,
            "hold_timer": hold_timer,
            "keepalive_timer": keepalive_timer,
            "capabilities_set": caps_applied,
            "bfd": bfd,
            "bfd_mode": bfd_mode,
        }
        return env
    except IxiaNotFoundError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        env["next_actions"].append(
            "Run `qactl ixia session describe` to confirm DG / ipv4 names."
        )
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


# ----------------------------------------------------------------------
# BGP VRF
# ----------------------------------------------------------------------

# Acceptable formats for an RT entry passed to ixia_create_bgp_vrf:
# - ``"<asn>:<assigned>"`` (str) — TargetType=as
# - ``{"type": "as", "asn": int, "assigned": int}`` (dict)
# - ``{"type": "ip", "ip": "a.b.c.d", "assigned": int}`` (dict)
RtSpec = Union[str, Dict[str, Any]]


def _parse_rt(rt: RtSpec) -> Dict[str, Any]:
    """Normalise an RT spec into ``{type, asn, assigned, ip}``.

    Returns a dict with the exact keys IxNetwork's
    ``bgp{Im,Ex}portRouteTargetList`` multivalues consume:
    ``targetType`` (``"as"`` / ``"ip"``), ``targetAsNumber`` (int),
    ``targetAssignedNumber`` (int), ``targetIpAddress`` (str dotted-quad).
    """
    if isinstance(rt, str):
        bits = rt.split(":")
        if len(bits) != 2:
            raise ValueError(
                f"RT string {rt!r} must be '<asn>:<assigned>' (got "
                f"{len(bits)} parts)."
            )
        try:
            asn = int(bits[0])
            assigned = int(bits[1])
        except ValueError as ve:
            raise ValueError(
                f"RT string {rt!r} parts must be integers."
            ) from ve
        return {
            "type": "as", "asn": asn, "assigned": assigned, "ip": None,
        }
    if isinstance(rt, dict):
        rt_type = str(rt.get("type", "as")).lower()
        if rt_type not in ("as", "ip"):
            raise ValueError(
                f"RT dict {rt!r} type must be 'as' or 'ip', got "
                f"{rt_type!r}."
            )
        assigned = rt.get("assigned")
        if not isinstance(assigned, int):
            raise ValueError(
                f"RT dict {rt!r} 'assigned' must be an integer."
            )
        if rt_type == "as":
            asn = rt.get("asn")
            if not isinstance(asn, int):
                raise ValueError(
                    f"RT dict {rt!r} 'asn' must be an integer "
                    "for type='as'."
                )
            return {
                "type": "as", "asn": asn, "assigned": assigned, "ip": None,
            }
        ip = rt.get("ip")
        if not isinstance(ip, str) or not ip:
            raise ValueError(
                f"RT dict {rt!r} 'ip' must be a dotted-quad string "
                "for type='ip'."
            )
        return {"type": "ip", "asn": 0, "assigned": assigned, "ip": ip}
    raise ValueError(
        f"RT entry must be 'asn:assigned' string or dict, got "
        f"{type(rt).__name__}."
    )


def _patch_rt_list(ixn, rt_objs, rts: List[Dict[str, Any]]) -> None:
    """PATCH each RT row's targetType/targetAsNumber/targetAssignedNumber.

    ``rt_objs`` is the RestPy collection (already ``.find()``-ed).
    ``rts`` is the parsed list of dicts from :func:`_parse_rt`. The
    server auto-creates one RT row per ``NumRtIn{Im,Ex}port...``;
    we just walk both lists in order.
    """
    rt_list = list(rt_objs)
    if len(rt_list) != len(rts):
        raise IxiaOperationError(
            f"Expected {len(rts)} RT rows, server returned {len(rt_list)}. "
            "NumRtIn{Im,Ex}port... was not honoured at create time."
        )
    for rt_row, spec in zip(rt_list, rts):
        rt_href = getattr(rt_row, "href", "")
        if not rt_href:
            raise IxiaOperationError("RT row href missing after auto-create.")
        rt_body = ixn._connection._read(rt_href)
        if not isinstance(rt_body, dict):
            raise IxiaOperationError(
                f"Unexpected RT body shape for {rt_href}: "
                f"{type(rt_body).__name__}"
            )
        patch_singlevalue(ixn, rt_body["targetType"], spec["type"])
        patch_singlevalue(
            ixn, rt_body["targetAsNumber"], int(spec["asn"]),
        )
        patch_singlevalue(
            ixn, rt_body["targetAssignedNumber"], int(spec["assigned"]),
        )
        if spec["type"] == "ip":
            patch_singlevalue(
                ixn, rt_body["targetIpAddress"], spec["ip"],
            )


def ixia_create_bgp_vrf(
    host: str,
    topology: str,
    device_group: Union[str, int],
    peer: str,
    name: str,
    import_rts: List[RtSpec],
    export_rts: List[RtSpec],
    multiplier: int = 1,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Add a bgpVrf under an existing bgpIpv4Peer with import/export RTs.

    Creates the VRF via ``BgpVrf.add()`` with
    ``NumRtInImportRouteTargetList=len(import_rts)`` and
    ``NumRtInExportRouteTargetList=len(export_rts)``, which makes
    IxNetwork auto-instantiate that many import/export RT rows. We
    then PATCH each row's ``targetType`` / ``targetAsNumber`` /
    ``targetAssignedNumber`` (and ``targetIpAddress`` for type=ip)
    via raw REST.

    NGPF stacks one ``bgpVrf`` per ``bgpIpv4Peer`` per the ngpf-gotchas
    rule — for multiple VRFs use the ``multiplier`` argument here, not
    a second ``ixia_create_bgp_vrf`` call.

    The route distinguisher (RD) is **NOT** stored on ``bgpVrf`` —
    it's part of ``bgpL3VpnRouteProperty`` at the NetworkGroup level.
    To advertise VPNv4 routes for this VRF, follow up with
    ``ixia_create_network_group(... connect_to_peer=peer ...)`` and
    a separate ``ixia_rest_patch`` to add the L3VPN route property
    with the desired RD. (A future ``ixia_create_l3vpn_network_group``
    tool would close that gap; today it's still composed.)

    Args:
        topology: Parent topology name (exact match).
        device_group: Parent DG. Name (str) or 1-based index (int).
        peer: Exact name of the parent bgpIpv4Peer.
        name: VRF name. Must be unique inside the parent peer.
        import_rts: List of import-RT specs. Each entry is either:
            - ``"<asn>:<assigned>"`` string (TargetType=as), or
            - ``{"type": "as", "asn": N, "assigned": N}`` dict, or
            - ``{"type": "ip", "ip": "a.b.c.d", "assigned": N}`` dict.
        export_rts: Same shape as ``import_rts``.
        multiplier: Number of VRF instances per peer session
            (default 1). Use this for "many VRFs on one peer" instead
            of multiple ``add()`` calls.

    Returns envelope with
    ``result = {topology, device_group, peer, name, href, multiplier,
    import_rts, export_rts}`` (the RT lists echo back the parsed
    spec, not the raw input).

    Notes:
        - ``import_rts=[]`` and ``export_rts=[]`` are accepted — the
          VRF is created with zero RTs. Useful for templated builds
          that PATCH RTs in a later step.
        - This tool does NOT silent-bounce. The bgpVrf comes up the
          next time the parent peer is started (or the DG is
          restarted).
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "peer": peer, "name": name,
        "import_rts": list(import_rts or []),
        "export_rts": list(export_rts or []),
        "multiplier": multiplier,
    }

    if not isinstance(multiplier, int) or multiplier < 1:
        return error_envelope(
            "multiplier must be a positive integer.",
            kind="create_bgp_vrf", host=host, port=port,
            status="bad_argument",
        )

    # Parse RT specs eagerly so a typo fails before any IxNetwork POST.
    try:
        parsed_import = [_parse_rt(rt) for rt in (import_rts or [])]
        parsed_export = [_parse_rt(rt) for rt in (export_rts or [])]
    except ValueError as ve:
        return error_envelope(
            str(ve), kind="create_bgp_vrf", host=host, port=port,
            status="bad_argument",
        )

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="create_bgp_vrf",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="create_bgp_vrf", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        ixn = s.ixn
        with write_lock(host, port, user):
            topo = resolve_topology(ixn, topology)
            dg = resolve_device_group(topo, device_group)
            try:
                peer_obj, _ipv4 = find_bgp_peer(dg, peer)
            except IxiaNotFoundError as e:
                env["status"] = "error"
                env["errors"].append(str(e))
                env["next_actions"].append(
                    "Run `qactl ixia topo get` / `qactl ixia session describe` "
                    "to see available BGP peer names under this DG."
                )
                return env

            vrf = peer_obj.BgpVrf.add(
                Name=name,
                Multiplier=int(multiplier),
                NumRtInImportRouteTargetList=len(parsed_import),
                NumRtInExportRouteTargetList=len(parsed_export),
            )
            vrf_href = getattr(vrf, "href", "")
            if not vrf_href:
                raise IxiaOperationError(
                    "bgpVrf href missing after creation."
                )

            try:
                if parsed_import:
                    _patch_rt_list(
                        ixn,
                        vrf.BgpImportRouteTargetList.find(),
                        parsed_import,
                    )
                if parsed_export:
                    _patch_rt_list(
                        ixn,
                        vrf.BgpExportRouteTargetList.find(),
                        parsed_export,
                    )
            except Exception:
                try:
                    vrf.remove()
                except Exception:
                    pass
                raise

        env["result"] = {
            "topology": topology,
            "device_group": getattr(dg, "Name", str(device_group)),
            "peer": peer,
            "name": getattr(vrf, "Name", name),
            "href": vrf_href,
            "multiplier": int(multiplier),
            "import_rts": parsed_import,
            "export_rts": parsed_export,
        }
        return env
    except IxiaNotFoundError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


# ----------------------------------------------------------------------
# Delete twins
# ----------------------------------------------------------------------
#
# All four tools share the same shape:
#
# 1. ``confirm=True`` gate via ``confirm_guard`` — same wording as the
#    rest of the MCP, so the agent learns the pattern once.
# 2. Resolve the parent chain (topology → DG → ethernet/ipv4/peer)
#    BEFORE bouncing the topology, so a typo in the name doesn't cost
#    a needless stop/start.
# 3. ``_bounce_if_running`` around the actual ``.remove()`` —
#    matches ``ixia_delete_device_group`` / ``ixia_delete_network_group``
#    (per ``.cursor/rules/ixia/mcp-tool-policy.mdc``: any tool that
#    mutates topology/DG/NG/peer state and that IxNetwork rejects on a
#    running topology must silent-bounce, surfacing ``bounced`` /
#    ``bounce_elapsed_s`` in the result).
# 4. ``IxNetwork.remove()`` cascades — deleting an ethernet stack
#    deletes every IPv4 / BGP peer / BGP VRF underneath. We document
#    that in each docstring so the caller doesn't expect a "no
#    children allowed" guard.

def _resolve_ipv4_in_dg(dg, ipv4_name: str, ethernet: Union[str, int]):
    """Resolve an IPv4 stack by name under a chosen ethernet inside ``dg``.

    Mirrors ``ixia_create_ipv4``'s ``ethernet`` arg semantics: name
    (str) or 1-based index (int, default 1 — the first ethernet,
    which is the common case where each DG has exactly one ethernet).
    """
    eth = resolve_ethernet(dg, ethernet)
    return eth, resolve_ipv4(eth, ipv4_name), eth


def ixia_delete_ethernet(
    host: str,
    topology: str,
    device_group: Union[str, int],
    name: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Remove an ethernet stack (and every IPv4 / BGP / VRF beneath it).

    IxNetwork's ``ethernet.remove()`` cascades: every IPv4 stack
    chained on top, every BGP peer on top of that, and every bgpVrf
    under each peer goes away in one POST. There's no per-child
    safety net — ``confirm=True`` is your only checkpoint.

    Args:
        topology: Parent topology name (exact match).
        device_group: Parent DG. Name (str) or 1-based index (int).
        name: Exact ethernet stack name to delete.
        confirm: Must be ``True`` or the call returns
            ``status="confirmation_required"`` without touching
            IxNetwork.

    Returns envelope with ``result = {topology, device_group,
    deleted, bounced, bounce_elapsed_s}``.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "name": name, "confirm": confirm,
    }
    guard = confirm_guard(
        kind="delete_ethernet", host=host, port=port, confirm=confirm,
    )
    if guard is not None:
        guard["request"] = request
        return guard

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="delete_ethernet",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="delete_ethernet", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ixn = s.ixn
        with write_lock(host, port, user):
            tp = resolve_topology(ixn, topology)
            dg = resolve_device_group(tp, device_group)
            eth = resolve_ethernet(dg, name)
            with _bounce_if_running(ixn, tp) as (running, t0):
                eth.remove()
        env["result"] = {
            "topology": topology,
            "device_group": getattr(dg, "Name", str(device_group)),
            "deleted": name,
            "bounced": running,
            "bounce_elapsed_s": (
                round(time.time() - t0, 2) if running else 0.0
            ),
        }
        return env
    except IxiaNotFoundError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_delete_ipv4(
    host: str,
    topology: str,
    device_group: Union[str, int],
    name: str,
    ethernet: Union[str, int] = 1,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Remove an IPv4 stack (and the BGP peers / VRFs on top).

    The parent ethernet stays put. To delete the ethernet too, call
    ``ixia_delete_ethernet`` instead.

    Args:
        topology: Parent topology name (exact match).
        device_group: Parent DG. Name (str) or 1-based index (int).
        ethernet: Parent ethernet inside the DG. Name (str) or
            1-based index (int, default ``1`` — the first ethernet).
        name: Exact IPv4 stack name to delete.
        confirm: Must be ``True``.

    Returns envelope with ``result = {topology, device_group,
    ethernet, deleted, bounced, bounce_elapsed_s}``.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "ethernet": ethernet, "name": name, "confirm": confirm,
    }
    guard = confirm_guard(
        kind="delete_ipv4", host=host, port=port, confirm=confirm,
    )
    if guard is not None:
        guard["request"] = request
        return guard

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="delete_ipv4",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="delete_ipv4", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ixn = s.ixn
        with write_lock(host, port, user):
            tp = resolve_topology(ixn, topology)
            dg = resolve_device_group(tp, device_group)
            eth, ipv4_obj, _ = _resolve_ipv4_in_dg(dg, name, ethernet)
            with _bounce_if_running(ixn, tp) as (running, t0):
                ipv4_obj.remove()
        env["result"] = {
            "topology": topology,
            "device_group": getattr(dg, "Name", str(device_group)),
            "ethernet": getattr(eth, "Name", str(ethernet)),
            "deleted": name,
            "bounced": running,
            "bounce_elapsed_s": (
                round(time.time() - t0, 2) if running else 0.0
            ),
        }
        return env
    except IxiaNotFoundError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_delete_bgp_peer(
    host: str,
    topology: str,
    device_group: Union[str, int],
    name: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Remove a BGP IPv4 peer (and every bgpVrf attached to it).

    Walks the DG's Ethernet → IPv4 → BgpIpv4Peer tree to find the
    peer by name (peer names are unique inside a DG by NGPF rule).
    The parent IPv4 stack stays — only the peer and its VRF children
    go away.

    Args:
        topology: Parent topology name (exact match).
        device_group: Parent DG. Name (str) or 1-based index (int).
        name: Exact peer name to delete.
        confirm: Must be ``True``.

    Returns envelope with ``result = {topology, device_group,
    deleted, bounced, bounce_elapsed_s}``.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "name": name, "confirm": confirm,
    }
    guard = confirm_guard(
        kind="delete_bgp_peer", host=host, port=port, confirm=confirm,
    )
    if guard is not None:
        guard["request"] = request
        return guard

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="delete_bgp_peer",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="delete_bgp_peer", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ixn = s.ixn
        with write_lock(host, port, user):
            tp = resolve_topology(ixn, topology)
            dg = resolve_device_group(tp, device_group)
            peer, _ipv4 = find_bgp_peer(dg, name)
            with _bounce_if_running(ixn, tp) as (running, t0):
                peer.remove()
        env["result"] = {
            "topology": topology,
            "device_group": getattr(dg, "Name", str(device_group)),
            "deleted": name,
            "bounced": running,
            "bounce_elapsed_s": (
                round(time.time() - t0, 2) if running else 0.0
            ),
        }
        return env
    except IxiaNotFoundError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        env["next_actions"].append(
            "Run `qactl ixia session describe` to see available BGP peer "
            "names under this DG."
        )
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_delete_bgp_vrf(
    host: str,
    topology: str,
    device_group: Union[str, int],
    peer: str,
    name: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Remove a bgpVrf from under a bgpIpv4Peer.

    The parent peer stays running. RT lists under the VRF are
    auto-removed by IxNetwork's cascade.

    Note that the route distinguisher (RD) for VPNv4 advertisement is
    NOT stored on ``bgpVrf`` — it lives on a sibling NetworkGroup's
    ``bgpL3VpnRouteProperty``. Deleting the VRF here does NOT clean
    up that NG; if you also want the VPN routes gone, follow up with
    ``ixia_delete_network_group``.

    Args:
        topology: Parent topology name (exact match).
        device_group: Parent DG. Name (str) or 1-based index (int).
        peer: Exact name of the parent bgpIpv4Peer.
        name: Exact VRF name to delete.
        confirm: Must be ``True``.

    Returns envelope with ``result = {topology, device_group, peer,
    deleted, bounced, bounce_elapsed_s}``.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "peer": peer, "name": name, "confirm": confirm,
    }
    guard = confirm_guard(
        kind="delete_bgp_vrf", host=host, port=port, confirm=confirm,
    )
    if guard is not None:
        guard["request"] = request
        return guard

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="delete_bgp_vrf",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="delete_bgp_vrf", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ixn = s.ixn
        with write_lock(host, port, user):
            tp = resolve_topology(ixn, topology)
            dg = resolve_device_group(tp, device_group)
            peer_obj, _ipv4 = find_bgp_peer(dg, peer)
            target = None
            for vrf in peer_obj.BgpVrf.find():
                if getattr(vrf, "Name", "") == name:
                    target = vrf
                    break
            if target is None:
                raise IxiaNotFoundError(
                    f"BGP VRF {name!r} not found under peer "
                    f"{peer!r} in DG {getattr(dg, 'Name', '?')!r}"
                )
            with _bounce_if_running(ixn, tp) as (running, t0):
                target.remove()
        env["result"] = {
            "topology": topology,
            "device_group": getattr(dg, "Name", str(device_group)),
            "peer": peer,
            "deleted": name,
            "bounced": running,
            "bounce_elapsed_s": (
                round(time.time() - t0, 2) if running else 0.0
            ),
        }
        return env
    except IxiaNotFoundError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        env["next_actions"].append(
            "Run `qactl ixia bgp peer get` to see existing bgpVrf names "
            "under this peer."
        )
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------

def register(mcp) -> None:
    mcp.tool()(ixia_create_ethernet)
    mcp.tool()(ixia_create_ipv4)
    mcp.tool()(ixia_create_bgp_peer)
    mcp.tool()(ixia_create_bgp_vrf)
    mcp.tool()(ixia_delete_ethernet)
    mcp.tool()(ixia_delete_ipv4)
    mcp.tool()(ixia_delete_bgp_peer)
    mcp.tool()(ixia_delete_bgp_vrf)


__all__ = [
    "ixia_create_ethernet",
    "ixia_create_ipv4",
    "ixia_create_bgp_peer",
    "ixia_create_bgp_vrf",
    "ixia_delete_ethernet",
    "ixia_delete_ipv4",
    "ixia_delete_bgp_peer",
    "ixia_delete_bgp_vrf",
    "register",
]
