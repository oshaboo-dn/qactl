"""Read-only inspectors for NetworkGroups, BGP peers, and the session
as a whole.

These three tools answer "what's actually configured?" without needing
the agent to chain ``ixia_rest_get`` calls through the NGPF tree:

- ``ixia_get_network_group`` — per-line breakdown of one
  NetworkGroup (multiplier, pools, route ranges, label start, RFC 8277
  flag, attached VRF, current ``Active`` mask). The thing
  ``ixia_route_action`` callers consult to confirm the multiplier and
  the per-line baseline before flipping a line.
- ``ixia_get_bgp_peer`` — capabilities, RX filters, bgpVrfs,
  optional cumulative route counts. Surfaces what the existing
  ``ixia_get_topology`` deliberately glosses over (capabilities,
  GR/LLGR, AF filters).
- ``ixia_describe_session`` — composes everything above plus vport
  state, traffic-item state, and the global ``applyOnTheFlyState`` so
  one call gives an agent enough context to act.

Implementation notes
--------------------
- All three are **read-only** — no write lock, no ``confirm`` gate.
- Multivalue reads go through ``read_multivalue`` (RestPy-aware) for
  scalar attributes and through raw ``ixn._connection._read`` for the
  per-instance ``values`` arrays we need broadcast or compared
  per-line.
- Best-effort: a missing attribute on one stack (e.g. an LLGR scalar
  that wasn't set in this config) returns ``None`` rather than tanking
  the whole inspect.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from ixia.models import IxiaError, IxiaNotFoundError
from ixia._helpers import read_multivalue

from ixia_core.envelope import make_envelope, error_envelope
from ixia_core.session import (
    DEFAULT_PORT, DEFAULT_USER,
    get_session, session_id_of,
)
from ixia_tools._ngpf_lookup import (
    POOL_ATTRS,
    find_bgp_peer,
    list_bgp_peers,
    resolve_device_group,
    resolve_network_group,
    resolve_topology,
)


# ----------------------------------------------------------------------
# Multivalue per-line helpers
# ----------------------------------------------------------------------

def _mv_line_values(mv_obj, ixn, *, length: int) -> List[Any]:
    """Read a multivalue and return a list of length ``length``,
    broadcasting a single value across all lines.

    The ``Active`` / ``Label*`` / ``NumberOfAddressesAsy`` multivalues
    can be ``singleValue`` (one entry → broadcast) or ``valueList``
    (one entry per line). Either way the caller wants a per-line list.
    Missing / unreadable multivalues return ``[None] * length``.
    """
    raw = read_multivalue(mv_obj, ixn) if mv_obj is not None else None
    if raw is None:
        return [None] * length
    if isinstance(raw, list):
        if len(raw) == length:
            return list(raw)
        if len(raw) == 1:
            return [raw[0]] * length
        # Mismatch — return what we got, padded/truncated to length so
        # callers can still align by index.
        if len(raw) < length:
            return list(raw) + [None] * (length - len(raw))
        return list(raw[:length])
    return [raw] * length


def _mv_scalar(mv_obj, ixn) -> Any:
    """Read a multivalue, collapsing to a single value if the underlying
    pattern is uniform. Returns ``None`` on failure."""
    if mv_obj is None:
        return None
    try:
        v = read_multivalue(mv_obj, ixn)
    except Exception:
        return None
    if isinstance(v, list) and len(set(map(str, v))) == 1:
        return v[0]
    return v


def _safe_bool(v: Any) -> Optional[bool]:
    """Coerce a multivalue-string ``"true"``/``"false"`` (or Python
    bool) to ``Optional[bool]``. Returns ``None`` if neither shape
    fits, which lets callers distinguish "absent" from "False"."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


# ----------------------------------------------------------------------
# NetworkGroup inspector
# ----------------------------------------------------------------------

def _ip_offset(start: str, step: str, n: int) -> Optional[str]:
    """Compute ``start + step * n`` for an IPv4/IPv6 dotted address.

    Used to surface ``first_prefix`` per route-range line. Best-effort —
    returns ``None`` if either input doesn't parse as a numeric address.
    The IxNetwork ``NetworkAddress`` is shared across lines, so we
    offset by the cumulative ``count`` of earlier lines * ``step``.
    """
    if start is None or step is None:
        return None
    try:
        if ":" in str(start):
            import ipaddress
            return str(
                ipaddress.IPv6Address(int(ipaddress.IPv6Address(start)) +
                                     int(ipaddress.IPv6Address(step)) * n)
            )
        parts_a = [int(p) for p in str(start).split(".")]
        parts_s = [int(p) for p in str(step).split(".")]
        if len(parts_a) != 4 or len(parts_s) != 4:
            return None
        ai = (parts_a[0] << 24) | (parts_a[1] << 16) | (parts_a[2] << 8) | parts_a[3]
        si = (parts_s[0] << 24) | (parts_s[1] << 16) | (parts_s[2] << 8) | parts_s[3]
        v = ai + si * n
        return ".".join(str((v >> shift) & 0xFF) for shift in (24, 16, 8, 0))
    except Exception:
        return None


def _connector_target(pool, ixn) -> Optional[str]:
    """Return the href the pool's ``Connector`` is wired to (or None)."""
    try:
        conn = pool.Connector.find()
        if not conn:
            return None
        target = getattr(conn, "ConnectedTo", None)
        if not target:
            return None
        return str(target)
    except Exception:
        return None


def _route_property_lines(
    pool, ixn, *, multiplier: int, family: str,
) -> Dict[str, Any]:
    """Per-line route-range info for one prefix pool.

    The "two lines under one cloud" pattern (NG ``Multiplier`` > 1)
    supports two shapes for distinguishing lines:

    1. **Single ``NetworkAddress``** + per-line
       ``NumberOfAddressesAsy``: line N starts at
       ``NetworkAddress + addr_step * sum(prior line counts)``.
    2. **Per-line ``NetworkAddress``** (a valueList of length=multiplier):
       line N's first prefix is ``NetworkAddress[N]`` directly. Used
       on ``bgp-lu-stale-bug.ixncfg``.

    We detect which shape is in play by reading ``NetworkAddress``: if
    it comes back as a list of length ``multiplier``, take the
    per-line entry; otherwise fall back to the cumulative-offset
    calculation. Either way per-line ``count`` comes from
    ``NumberOfAddressesAsy``.
    """
    rp_attr = "BgpIPRouteProperty" if family == "ipv4" else "BgpV6IPRouteProperty"
    coll = getattr(pool, rp_attr, None)
    rps = list(coll.find()) if coll is not None else []
    rp = rps[0] if rps else None

    counts_per_line = _mv_line_values(
        getattr(pool, "NumberOfAddressesAsy", None), ixn, length=multiplier,
    )
    network_addr_per_line = _mv_line_values(
        getattr(pool, "NetworkAddress", None), ixn, length=multiplier,
    )
    network_address = _mv_scalar(getattr(pool, "NetworkAddress", None), ixn)
    prefix_len = _mv_scalar(getattr(pool, "PrefixLength", None), ixn)
    addr_step = _mv_scalar(getattr(pool, "PrefixAddrStep", None), ixn)

    if rp is None:
        active = [None] * multiplier
        label_start = [None] * multiplier
        label_step = [None] * multiplier
        rfc8277_broadcast: Any = None
        no_of_labels_broadcast: Any = None
    else:
        active = _mv_line_values(getattr(rp, "Active", None), ixn, length=multiplier)
        label_start = _mv_line_values(getattr(rp, "LabelStart", None), ixn, length=multiplier)
        label_step = _mv_line_values(getattr(rp, "LabelStep", None), ixn, length=multiplier)
        # ``advertiseAsRfc8277`` and ``noOfLabels`` are *scalars* on the
        # route property (not multivalues — confirmed by GET on
        # /…/bgpIPRouteProperty/N), so RestPy exposes them as plain
        # Python attributes. Read once and broadcast across all lines.
        rfc8277_broadcast = getattr(rp, "AdvertiseAsRfc8277", None)
        no_of_labels_broadcast = getattr(rp, "NoOfLabels", None)
    rfc8277 = [rfc8277_broadcast] * multiplier
    no_of_labels = [no_of_labels_broadcast] * multiplier

    lines: List[Dict[str, Any]] = []
    cumulative = 0
    for i in range(multiplier):
        line_count = 0
        try:
            line_count = int(counts_per_line[i]) if counts_per_line[i] is not None else 0
        except Exception:
            line_count = 0
        per_line_addr = network_addr_per_line[i]
        if per_line_addr:
            first_prefix = str(per_line_addr)
        elif isinstance(network_address, str):
            first_prefix = _ip_offset(network_address, addr_step, cumulative)
        else:
            first_prefix = None
        prefix_str = (
            f"{first_prefix}/{prefix_len}"
            if first_prefix and prefix_len is not None
            else None
        )
        lines.append({
            "index": i,
            "active": _safe_bool(active[i]),
            "first_prefix": prefix_str,
            "count": line_count,
            "label_start": _coerce_int(label_start[i]),
            "label_step": _coerce_int(label_step[i]),
            "rfc8277": _safe_bool(rfc8277[i]),
            "no_of_labels": _coerce_int(no_of_labels[i]),
        })
        cumulative += line_count
    return {
        "network_address": network_address,
        "prefix_length": _coerce_int(prefix_len),
        "addr_step": addr_step,
        "lines": lines,
        "route_property_href": getattr(rp, "href", None) if rp is not None else None,
    }


def _coerce_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(str(v))
    except Exception:
        return None


def _ng_l3vpn_attachments(ng, ixn) -> List[Dict[str, Any]]:
    """If the NG carries a ``bgpL3VpnRouteProperty``, surface the
    VRF-side metadata (RD, RTs, target-VRF connector). Returns ``[]``
    when the NG isn't VPN-enabled. Best-effort."""
    out: List[Dict[str, Any]] = []
    try:
        coll = getattr(ng, "BgpL3VpnRouteProperty", None)
        if coll is None:
            return out
        for l3 in coll.find():
            entry: Dict[str, Any] = {
                "href": getattr(l3, "href", None),
                "distinguisher_type": _mv_scalar(
                    getattr(l3, "DistinguisherType", None), ixn,
                ),
                "asNumber": _mv_scalar(
                    getattr(l3, "DistinguisherAsNumber", None), ixn,
                ),
                "ipAddress": _mv_scalar(
                    getattr(l3, "DistinguisherIpAddress", None), ixn,
                ),
                "assignedNumber": _mv_scalar(
                    getattr(l3, "DistinguisherAssignedNumber", None), ixn,
                ),
                "enable_ipv4_sender": _safe_bool(_mv_scalar(
                    getattr(l3, "EnableIpv4Sender", None), ixn,
                )),
            }
            try:
                conn = l3.Connector.find()
                target = getattr(conn, "ConnectedTo", None) if conn else None
                entry["connector_to"] = str(target) if target else None
            except Exception:
                entry["connector_to"] = None
            out.append(entry)
    except Exception:
        pass
    return out


def ixia_get_network_group(
    host: str,
    topology: str,
    network_group: str,
    device_group: Union[str, int] = 1,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Per-line breakdown of one NetworkGroup.

    Returns the multiplier, the pool(s) underneath, and one entry per
    "route-range line" with its ``active`` flag, first prefix, count,
    label start/step, RFC 8277 flag, ``NoOfLabels``, and any attached
    L3VPN route-property metadata. Read-only.

    ``device_group`` accepts a name (str) or 1-based index (int,
    default 1 — the first DG in the topology).
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "network_group": network_group,
    }
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="get_network_group",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="get_network_group", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ixn = s.ixn
        topo = resolve_topology(ixn, topology)
        dg = resolve_device_group(topo, device_group)
        ng = resolve_network_group(dg, network_group)

        multiplier = int(getattr(ng, "Multiplier", 1) or 1)
        pools_out: List[Dict[str, Any]] = []
        for family, attr in POOL_ATTRS.items():
            coll = getattr(ng, attr, None)
            if coll is None:
                continue
            for pool in coll.find():
                routes = _route_property_lines(
                    pool, ixn, multiplier=multiplier, family=family,
                )
                pools_out.append({
                    "family": family,
                    "href": getattr(pool, "href", None),
                    "connector_to": _connector_target(pool, ixn),
                    "network_address": routes["network_address"],
                    "prefix_length": routes["prefix_length"],
                    "addr_step": routes["addr_step"],
                    "route_property_href": routes["route_property_href"],
                    "lines": routes["lines"],
                })
        env["result"] = {
            "name": getattr(ng, "Name", network_group),
            "href": getattr(ng, "href", None),
            "multiplier": multiplier,
            "pools": pools_out,
            "l3vpn_route_properties": _ng_l3vpn_attachments(ng, ixn),
        }
        return env
    except IxiaNotFoundError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


# ----------------------------------------------------------------------
# BGP peer inspector
# ----------------------------------------------------------------------

# Capabilities + filters we want to surface. (Tool-side label, RestPy
# attribute name). Each is a multivalue bool; missing attributes
# resolve to ``None``.
_CAPABILITY_ATTRS = [
    ("ipv4_unicast", "CapabilityIpV4Unicast"),
    ("ipv4_mpls", "CapabilityIpV4Mpls"),
    ("ipv4_mpls_vpn", "CapabilityIpV4MplsVpn"),
    ("ipv6_unicast", "CapabilityIpV6Unicast"),
    ("ipv6_mpls", "CapabilityIpV6Mpls"),
    ("ipv6_mpls_vpn", "CapabilityIpV6MplsVpn"),
    ("vpls", "CapabilityVpls"),
    ("evpn", "CapabilityEvpn"),
    ("rt_constraint", "CapabilityRtConstraint"),
    ("graceful_restart", "EnableGracefulRestart"),
    ("long_lived_gr", "EnableLlgr"),
]

_FILTER_ATTRS = [
    ("ipv4_unicast", "FilterIpV4Unicast"),
    ("ipv4_mpls", "FilterIpV4Mpls"),
    ("ipv4_mpls_vpn", "FilterIpV4MplsVpn"),
    ("ipv6_unicast", "FilterIpV6Unicast"),
    ("ipv6_mpls", "FilterIpV6Mpls"),
    ("ipv6_mpls_vpn", "FilterIpV6MplsVpn"),
    ("vpls", "FilterVpls"),
    ("evpn", "FilterEvpn"),
]


def _peer_vrfs(peer, ixn, *, parent_dg=None) -> List[Dict[str, Any]]:
    """Return ``[{name, href, rd, import_rt, export_rt}, ...]`` for every
    bgpVrf under ``peer``. Best-effort — missing children on individual
    VRFs degrade to ``None`` entries instead of failing the inspect.

    The RD is **not** stored on ``bgpVrf`` itself — it lives on the
    NetworkGroup-level ``bgpL3VpnRouteProperty`` whose ``Connector``
    points at this VRF. When ``parent_dg`` is given we walk its NGs to
    find that match. Without it (or no match) ``rd`` is ``None`` and
    the caller can drill into ``ixia_get_network_group`` to see
    the per-NG L3VPN attach.
    """
    rd_by_vrf_href: Dict[str, Optional[str]] = {}
    if parent_dg is not None:
        try:
            rd_by_vrf_href = _rd_by_vrf_from_ngs(parent_dg, ixn)
        except Exception:
            rd_by_vrf_href = {}

    out: List[Dict[str, Any]] = []
    try:
        for vrf in peer.BgpVrf.find():
            href = getattr(vrf, "href", None)
            entry = {
                "name": getattr(vrf, "Name", "") or None,
                "href": href,
                "rd": rd_by_vrf_href.get(str(href)) if href else None,
                "import_rt": _read_rt_list(
                    getattr(vrf, "BgpImportRouteTargetList", None), ixn,
                ),
                "export_rt": _read_rt_list(
                    getattr(vrf, "BgpExportRouteTargetList", None), ixn,
                ),
            }
            out.append(entry)
    except Exception:
        pass
    return out


def _rd_by_vrf_from_ngs(dg, ixn) -> Dict[str, Optional[str]]:
    """Build ``{bgpVrf_href: rd_string}`` for every NetworkGroup under
    ``dg`` that carries a ``bgpL3VpnRouteProperty``.

    The wiring rule: a VPNv4 NG's prefix pool's
    ``Connector.ConnectedTo`` points at the ``bgpVrf`` href, and the
    same NG carries a ``bgpL3VpnRouteProperty`` (one per NG) whose
    distinguisher fields hold the RD. So we walk the NG, find the
    pool whose connector targets a VRF, and pair the RD from the
    sibling L3VPN route-property with that target.
    """
    out: Dict[str, Optional[str]] = {}
    try:
        for ng in dg.NetworkGroup.find():
            l3_coll = getattr(ng, "BgpL3VpnRouteProperty", None)
            l3s = list(l3_coll.find()) if l3_coll is not None else []
            if not l3s:
                continue
            rd = _format_rd(l3s[0], ixn)
            for family, attr in POOL_ATTRS.items():
                pcoll = getattr(ng, attr, None)
                if pcoll is None:
                    continue
                for pool in pcoll.find():
                    target = _connector_target(pool, ixn)
                    if target:
                        out[str(target)] = rd
    except Exception:
        pass
    return out


def _format_rd(obj, ixn) -> Optional[str]:
    """Best-effort ``"<as>:<assigned>"`` (or ``"<ip>:<assigned>"``)
    string from an object that exposes ``DistinguisherType``,
    ``DistinguisherAsNumber``, ``DistinguisherIpAddress``,
    ``DistinguisherAssignedNumber`` (e.g. ``bgpL3VpnRouteProperty``)."""
    dt = _mv_scalar(getattr(obj, "DistinguisherType", None), ixn)
    asn = _mv_scalar(getattr(obj, "DistinguisherAsNumber", None), ixn)
    ip = _mv_scalar(getattr(obj, "DistinguisherIpAddress", None), ixn)
    assigned = _mv_scalar(
        getattr(obj, "DistinguisherAssignedNumber", None), ixn,
    )
    if dt is None and asn is None and ip is None and assigned is None:
        return None
    if dt in ("ip", "1") and ip is not None and assigned is not None:
        return f"{ip}:{assigned}"
    if asn is not None and assigned is not None:
        return f"{asn}:{assigned}"
    return None


def _read_rt_list(coll, ixn) -> List[str]:
    """Read a route-target list (auto-created child of bgpVrf) and
    return ``"<as>:<assigned>"`` formatted strings."""
    if coll is None:
        return []
    out: List[str] = []
    try:
        for rt in coll.find():
            asn = _mv_scalar(getattr(rt, "TargetAsNumber", None), ixn)
            assigned = _mv_scalar(
                getattr(rt, "TargetAssignedNumber", None), ixn,
            )
            if asn is not None and assigned is not None:
                out.append(f"{asn}:{assigned}")
    except Exception:
        pass
    return out


def _peer_capabilities(peer, ixn) -> Dict[str, Optional[bool]]:
    out: Dict[str, Optional[bool]] = {}
    for label, attr in _CAPABILITY_ATTRS:
        out[label] = _safe_bool(_mv_scalar(getattr(peer, attr, None), ixn))
    # The scalar (NOT multivalue) "multi-labels-per-route" capability —
    # explicit sibling of ipv4_mpls.
    try:
        v = getattr(peer, "Ipv4MultipleMplsLabelsCapability", None)
    except Exception:
        v = None
    out["ipv4_multi_mpls_labels"] = _safe_bool(v)
    return out


def _peer_filters(peer, ixn) -> Dict[str, Optional[bool]]:
    out: Dict[str, Optional[bool]] = {}
    for label, attr in _FILTER_ATTRS:
        out[label] = _safe_bool(_mv_scalar(getattr(peer, attr, None), ixn))
    return out


def _peer_route_counts(s, peer_name: str) -> Dict[str, Any]:
    """Snapshot ``Routes Advertised`` / ``Routes Withdrawn`` from
    ``/statistics/view/13`` for the row whose ``Port`` matches the
    peer's vport. Cumulative since session-up — they include every
    re-advertise and never decrement, so for "what's currently
    installed" ask the DUT directly.

    The view is keyed by **port name**, not peer name, so we return the
    raw row + a ``port`` field; multiple peers on the same vport
    aggregate. Returns ``{}`` if the view isn't available."""
    try:
        row = _read_view_row(s, "BGP Aggregated Statistics")
        if row:
            return row
    except Exception:
        pass
    return {}


def _read_view_row(s, view_name: str) -> Dict[str, Any]:
    """One-shot read of the first row of an IxNetwork stat view."""
    try:
        sv = s._session.StatViewAssistant(view_name)
        rows = sv.Rows
        if rows is None:
            return {}
        try:
            return dict(rows[0]) if hasattr(rows, "__getitem__") else {}
        except Exception:
            return {}
    except Exception:
        return {}


def ixia_get_bgp_peer(
    host: str,
    topology: str,
    peer: str,
    device_group: Union[str, int] = 1,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    include_route_counts: bool = False,
) -> Dict[str, Any]:
    """Surface BGP-peer detail that ``ixia_get_topology`` glosses over.

    Returns the peer's identity (``name``, ``type``, ``local_as``,
    ``dut_ip``), timer values, every relevant capability flag (incl.
    the standalone ``ipv4_multi_mpls_labels`` scalar), the AF RX
    filters, and one entry per attached ``bgpVrf``.

    ``include_route_counts=True`` adds a cumulative ``Routes Advertised``
    / ``Routes Withdrawn`` snapshot from ``/statistics/view/13``
    (~2-5 s extra). Default off so the per-peer inspect stays cheap
    enough to call inside ``ixia_describe_session``.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "peer": peer, "include_route_counts": include_route_counts,
    }
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="get_bgp_peer",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="get_bgp_peer", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ixn = s.ixn
        topo = resolve_topology(ixn, topology)
        dg = resolve_device_group(topo, device_group)
        peer_obj, _ipv4 = find_bgp_peer(dg, peer)

        env["result"] = _build_peer_view(
            s, ixn, peer_obj, parent_dg=dg,
            include_route_counts=include_route_counts,
        )
        return env
    except IxiaNotFoundError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def _build_peer_view(
    s, ixn, peer, *, include_route_counts: bool, parent_dg=None,
) -> Dict[str, Any]:
    """Shared body — also used by ``ixia_describe_session`` to embed
    full peer detail under each DG. ``parent_dg`` enables the RD
    lookup against sibling NetworkGroups' L3VPN route properties."""
    view: Dict[str, Any] = {
        "name": getattr(peer, "Name", "") or None,
        "href": getattr(peer, "href", None),
        "type": _mv_scalar(getattr(peer, "Type", None), ixn),
        "local_as": _mv_scalar(getattr(peer, "LocalAs2Bytes", None), ixn),
        "dut_ip": _mv_scalar(getattr(peer, "DutIp", None), ixn),
        "dut_as": _mv_scalar(getattr(peer, "DutAs2Bytes", None), ixn),
        "hold_timer_s": _coerce_int(
            _mv_scalar(getattr(peer, "HoldTimer", None), ixn)
        ),
        "keepalive_timer_s": _coerce_int(
            _mv_scalar(getattr(peer, "KeepaliveTimer", None), ixn)
        ),
        "capabilities": _peer_capabilities(peer, ixn),
        "rx_filters": _peer_filters(peer, ixn),
        "bfd_registered": _safe_bool(
            _mv_scalar(getattr(peer, "EnableBfdRegistration", None), ixn)
        ),
        "bfd_mode": _mv_scalar(
            getattr(peer, "ModeOfBfdOperations", None), ixn
        ),
        "bgp_vrfs": _peer_vrfs(peer, ixn, parent_dg=parent_dg),
    }
    if include_route_counts:
        view["route_counts"] = _peer_route_counts(s, str(view["name"] or ""))
    return view


# ----------------------------------------------------------------------
# Session-level mega-inspector
# ----------------------------------------------------------------------

def _vport_summary(ixn) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        for v in ixn.Vport.find():
            out.append({
                "name": getattr(v, "Name", ""),
                "assigned_to": getattr(v, "AssignedTo", "") or None,
                "connection_state": getattr(v, "ConnectionState", ""),
                "link_state": getattr(v, "State", ""),
                "is_available": bool(getattr(v, "IsAvailable", False)),
                "href": getattr(v, "href", ""),
            })
    except Exception:
        pass
    return out


def _ipv4_view(dg, ixn) -> Optional[Dict[str, Any]]:
    """Return the first IPv4 stack's address/gateway/prefix on this DG."""
    try:
        for eth in dg.Ethernet.find():
            vlan = None
            try:
                if _safe_bool(_mv_scalar(
                    getattr(eth, "UseVlans", None), ixn,
                )) is True:
                    for v in eth.Vlan.find():
                        vlan = _coerce_int(_mv_scalar(
                            getattr(v, "VlanId", None), ixn,
                        ))
                        break
            except Exception:
                pass
            mac = _mv_scalar(getattr(eth, "Mac", None), ixn)
            for ipv4 in eth.Ipv4.find():
                return {
                    "vlan": vlan,
                    "mac": mac,
                    "address": _mv_scalar(
                        getattr(ipv4, "Address", None), ixn,
                    ),
                    "gateway": _mv_scalar(
                        getattr(ipv4, "GatewayIp", None), ixn,
                    ),
                    "prefix_length": _coerce_int(_mv_scalar(
                        getattr(ipv4, "Prefix", None), ixn,
                    )),
                }
    except Exception:
        pass
    return None


def _dg_bfd_interfaces(dg, ixn) -> List[Dict[str, Any]]:
    """Every bfdv4Interface under ``dg`` with its config + session state.

    Walks Ethernet → IPv4 → Bfdv4Interface. Best-effort: a stack with
    no BFD child contributes nothing; a read error on one interface
    doesn't tank the describe.
    """
    from ixia_tools.bfd import _build_bfdv4_view
    out: List[Dict[str, Any]] = []
    try:
        for eth in dg.Ethernet.find():
            for ipv4 in eth.Ipv4.find():
                coll = getattr(ipv4, "Bfdv4Interface", None)
                if coll is None:
                    continue
                for bfd in coll.find():
                    try:
                        out.append(_build_bfdv4_view(bfd, ixn))
                    except Exception:
                        continue
    except Exception:
        pass
    return out


def _dg_view(dg, ixn, s, *, include_route_counts: bool) -> Dict[str, Any]:
    peers_out: List[Dict[str, Any]] = []
    for peer, _ipv4 in list_bgp_peers(dg):
        peers_out.append(_build_peer_view(
            s, ixn, peer, parent_dg=dg,
            include_route_counts=include_route_counts,
        ))
    ngs_out: List[Dict[str, Any]] = []
    try:
        for ng in dg.NetworkGroup.find():
            multiplier = int(getattr(ng, "Multiplier", 1) or 1)
            pools: List[Dict[str, Any]] = []
            for family, attr in POOL_ATTRS.items():
                coll = getattr(ng, attr, None)
                if coll is None:
                    continue
                for pool in coll.find():
                    routes = _route_property_lines(
                        pool, ixn, multiplier=multiplier, family=family,
                    )
                    pools.append({
                        "family": family,
                        "href": getattr(pool, "href", None),
                        "connector_to": _connector_target(pool, ixn),
                        "network_address": routes["network_address"],
                        "prefix_length": routes["prefix_length"],
                        "addr_step": routes["addr_step"],
                        "route_property_href": routes["route_property_href"],
                        "lines": routes["lines"],
                    })
            ngs_out.append({
                "name": getattr(ng, "Name", ""),
                "href": getattr(ng, "href", None),
                "multiplier": multiplier,
                "pools": pools,
                "l3vpn_route_properties": _ng_l3vpn_attachments(ng, ixn),
            })
    except Exception:
        pass
    return {
        "name": getattr(dg, "Name", ""),
        "href": getattr(dg, "href", None),
        "multiplier": int(getattr(dg, "Multiplier", 1) or 1),
        "ipv4": _ipv4_view(dg, ixn),
        "bgp_peers": peers_out,
        "bfd_interfaces": _dg_bfd_interfaces(dg, ixn),
        "network_groups": ngs_out,
    }


def _topology_view(topo, ixn, s, *, include_route_counts: bool) -> Dict[str, Any]:
    vport_hrefs = list(getattr(topo, "Vports", []) or [])
    vport_names: List[str] = []
    try:
        href_to_name = {
            getattr(v, "href", ""): getattr(v, "Name", "")
            for v in ixn.Vport.find()
        }
        vport_names = [href_to_name.get(h, h) for h in vport_hrefs]
    except Exception:
        pass
    return {
        "name": getattr(topo, "Name", ""),
        "href": getattr(topo, "href", None),
        "vports": vport_names,
        "device_groups": [
            _dg_view(dg, ixn, s, include_route_counts=include_route_counts)
            for dg in topo.DeviceGroup.find()
        ],
    }


def _traffic_summary(ixn) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        for ti in ixn.Traffic.TrafficItem.find():
            out.append({
                "name": getattr(ti, "Name", ""),
                "state": getattr(ti, "State", ""),
                "enabled": bool(getattr(ti, "Enabled", False)),
                "traffic_type": getattr(ti, "TrafficType", ""),
                "href": getattr(ti, "href", None),
            })
    except Exception:
        pass
    return out


def _session_id(ixn) -> int:
    """Session id from ``ixn.href`` (e.g. ``/api/v1/sessions/1/ixnetwork/``).
    Returns 1 if the href is unparseable — Windows API servers default
    to that anyway, so the inspector still returns useful data."""
    href = getattr(ixn, "href", "") or ""
    for part in href.strip("/").split("/"):
        if part.isdigit():
            return int(part)
    return 1


def _global_topology_state(ixn) -> Dict[str, Any]:
    """Read ``/globals/topology`` so callers can see
    ``applyOnTheFlyState`` / in-progress protocol actions."""
    try:
        sid = _session_id(ixn)
        body = ixn._connection._read(
            f"/api/v1/sessions/{sid}/ixnetwork/globals/topology"
        )
        if not isinstance(body, dict):
            return {}
        return {
            "applyOnTheFlyState": body.get("applyOnTheFlyState"),
            "protocolActionsInProgress":
                list(body.get("protocolActionsInProgress") or []),
            "status": body.get("status"),
        }
    except Exception:
        return {}


def ixia_describe_session(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    include_route_counts: bool = True,
    include_traffic: bool = True,
) -> Dict[str, Any]:
    """One-call snapshot of the entire IxNetwork session.

    Composes vport state + per-topology DG/Ethernet/IPv4/BGP-peer/NG
    detail (full ``ixia_get_bgp_peer`` body for each peer; full
    ``ixia_get_network_group`` body for each NG) + traffic-item
    summary + global ``applyOnTheFlyState``.

    Args:
        include_route_counts: Pass through to
            ``ixia_get_bgp_peer`` per peer (~2-5 s extra over the
            view fetch).
        include_traffic: Include traffic-item summary.

    Read-only — no write lock taken. Use it to bootstrap an agent into
    "what's running on this session" without prior knowledge.
    """
    request = {
        "host": host, "port": port, "user": user,
        "include_route_counts": include_route_counts,
        "include_traffic": include_traffic,
    }
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="describe_session",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="describe_session", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ixn = s.ixn
        topos = list(ixn.Topology.find())
        result: Dict[str, Any] = {
            "session_id": session_id_of(s),
            "vports": _vport_summary(ixn),
            "topologies": [
                _topology_view(
                    t, ixn, s, include_route_counts=include_route_counts,
                )
                for t in topos
            ],
            "globals": _global_topology_state(ixn),
        }
        if include_traffic:
            result["traffic_items"] = _traffic_summary(ixn)
        env["result"] = result
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def register(mcp) -> None:
    mcp.tool()(ixia_get_network_group)
    mcp.tool()(ixia_get_bgp_peer)
    mcp.tool()(ixia_describe_session)


__all__ = [
    "ixia_get_network_group",
    "ixia_get_bgp_peer",
    "ixia_describe_session",
    "register",
]
