"""Tree-walk helpers for the NGPF object hierarchy.

Topology -> DeviceGroup -> Ethernet -> Ipv4 -> BgpIpv4Peer (peers, vrfs)
                       \\-> NetworkGroup -> Ipv4PrefixPools / Ipv6PrefixPools
                                         \\-> bgpL3VpnRouteProperty (NG-level)

Several tools (``ixia_route_action``, the inspect-* family,
``ixia_describe_session``) need to look these up by name or 1-based
index. Centralising the lookups keeps the tool bodies focused on
"what to do once you've got the handle" instead of yet-another tree
walk.

Design notes
------------
- Resolvers raise :class:`ixia.models.IxiaNotFoundError` with the same
  message shape so tool wrappers can convert them to a uniform
  ``status="error"`` envelope.
- ``device_group`` accepts either ``str`` (exact name) or ``int``
  (1-based index into ``Topology.DeviceGroup.find()``). Index 1 picks
  the first DG, which matches user-facing schemas like
  ``device_group: str | int = 1`` in ``ixia_route_action``.
- The walks deliberately stay shallow: the route-property is reached
  via ``ipv4PrefixPools[N]/<route_property>``, no nested DG recursion.
  The current scenarios all live one DG deep; if/when we start nesting
  DGs, extend ``_resolve_dg`` with a recursive variant.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple, Union

from qactl.ixia.client.models import IxiaNotFoundError


# ----------------------------------------------------------------------
# Confirm-gate helper (shared by every delete tool)
# ----------------------------------------------------------------------

def confirm_guard(
    *, kind: str, host: str, port: int, confirm: bool,
) -> Optional[dict]:
    """Standard ``confirm=True``-required envelope for delete tools.

    Returns ``None`` when ``confirm is True`` (caller proceeds).
    Returns a ``status="confirmation_required"`` envelope otherwise,
    so the caller can short-circuit without needing to import the
    envelope module here.

    Lives in ``_ngpf_lookup.py`` (the shared-helper bag) instead of
    each tool module so the wording / status string stays uniform —
    agents shouldn't have to learn a per-tool variant.
    """
    if confirm is True:
        return None
    # Local import to avoid envelope <-> lookup cycle (envelope is in
    # qactl.ixia.core, lookup is in qactl.ixia.tools — unrelated, but kept local
    # for symmetry with the build.py original).
    from qactl.ixia.core.envelope import error_envelope
    return error_envelope(
        f"{kind} deletes live IxNetwork objects and has no undo. "
        "Re-call with confirm=True after reviewing the arguments.",
        kind=kind, host=host, port=port,
        status="confirmation_required",
        next_actions=["Re-invoke with confirm=True to proceed."],
    )


# ----------------------------------------------------------------------
# Multivalue write helper (shared by build.py / stack.py)
# ----------------------------------------------------------------------

def patch_singlevalue(ixn, mv_href: str, value: Any) -> None:
    """Set a multivalue's ``singleValue`` via raw REST PATCH.

    Equivalent to RestPy's ``mv.Single(value)`` but bypasses the
    helper's auto-apply path, which on this lab triggers IxNetwork's
    "Batch Assistance" licence check on a few attributes (notably
    ``bgpIPRouteProperty`` multivalues — see lesson
    ``2026-05-05-route-toggle-resolved.md``). Pattern: PATCH
    ``<mv_href>/singleValue`` body ``{"value": "..."}``. Verified
    end-to-end against the manual 12-call repro on
    ``bgp-leak-2026-05-04.ixncfg``.

    The valueList POST/PATCH dance from ``routes.py`` doesn't apply
    here — fresh multivalues start at ``pattern=singleValue`` and
    builder tools only ever write singleValue at create time.
    """
    ixn._connection._update(mv_href + "/singleValue", {"value": str(value)})


def patch_singlevalue_if_set(ixn, mv_href: str, value: Optional[Any]) -> bool:
    """Same as :func:`patch_singlevalue` but a no-op when ``value`` is None.

    Lets builder tools cleanly express "only set this attribute if the
    caller passed a value" without an ``if foo is not None`` ladder
    around every PATCH.

    Returns ``True`` when a PATCH actually happened, ``False`` when
    skipped — useful for the ``warnings``/``result`` payload.
    """
    if value is None:
        return False
    patch_singlevalue(ixn, mv_href, value)
    return True


def extract_self_href(resp: Any) -> Optional[str]:
    """Pull the ``self`` link href out of a raw IxNetwork POST response.

    Body shape: ``{"id": N, ..., "links": [{"rel": "self", "href": "..."}, ...]}``
    """
    if not isinstance(resp, dict):
        return None
    for link in resp.get("links") or []:
        if isinstance(link, dict) and link.get("rel") == "self":
            href = link.get("href")
            if isinstance(href, str) and href:
                return href
    return None


# Recognised route-property children of a prefix pool. Each maps to the
# RestPy attribute name on the pool object. Keys are what an MCP caller
# would type. Values are how RestPy spells them.
ROUTE_PROPERTY_ATTRS = {
    "bgpIPRouteProperty": "BgpIPRouteProperty",
    "bgpV6IPRouteProperty": "BgpV6IPRouteProperty",
    "bgpL3VpnRouteProperty": "BgpL3VpnRouteProperty",
    "bgpV6L3VpnRouteProperty": "BgpV6L3VpnRouteProperty",
}

# Recognised prefix-pool families on a NetworkGroup.
POOL_ATTRS = {
    "ipv4": "Ipv4PrefixPools",
    "ipv6": "Ipv6PrefixPools",
}


def resolve_topology(ixn, name: str):
    """Find a topology by exact name. Raises ``IxiaNotFoundError``."""
    for tp in ixn.Topology.find():
        if getattr(tp, "Name", "") == name:
            return tp
    raise IxiaNotFoundError(f"Topology {name!r} not found")


def resolve_device_group(topo, ref: Union[str, int]):
    """Find a DG inside ``topo`` by name (str) or 1-based index (int).

    The 1-based default in ``ixia_route_action`` is meant for the common
    "the topology has one DG, just give me that one" case — passing
    ``device_group=1`` (default) picks ``DeviceGroup.find()[0]``.
    """
    dgs = list(topo.DeviceGroup.find())
    if not dgs:
        raise IxiaNotFoundError(
            f"Topology {getattr(topo, 'Name', '?')!r} has no device groups"
        )
    if isinstance(ref, int):
        if ref < 1 or ref > len(dgs):
            raise IxiaNotFoundError(
                f"Device-group index {ref} out of range "
                f"(topology {getattr(topo, 'Name', '?')!r} has {len(dgs)} DG)"
            )
        return dgs[ref - 1]
    for dg in dgs:
        if getattr(dg, "Name", "") == ref:
            return dg
    raise IxiaNotFoundError(
        f"Device group {ref!r} not found in topology "
        f"{getattr(topo, 'Name', '?')!r}"
    )


def resolve_network_group(dg, name: str):
    """Find a NG by exact name under ``dg``. Raises ``IxiaNotFoundError``."""
    for ng in dg.NetworkGroup.find():
        if getattr(ng, "Name", "") == name:
            return ng
    raise IxiaNotFoundError(
        f"Network group {name!r} not found under DG "
        f"{getattr(dg, 'Name', '?')!r}"
    )


def resolve_pool(ng, *, family: str = "ipv4", index: int = 1):
    """Find a prefix pool under ``ng`` by 1-based ``index`` and family.

    Most NGs have exactly one pool; the explicit index makes it
    deterministic for the rare two-pool case (e.g. dual-stack).
    """
    if family not in POOL_ATTRS:
        raise IxiaNotFoundError(
            f"Unknown family {family!r}. Valid: {sorted(POOL_ATTRS)}."
        )
    coll = getattr(ng, POOL_ATTRS[family], None)
    if coll is None:
        raise IxiaNotFoundError(
            f"NetworkGroup {getattr(ng, 'Name', '?')!r} has no "
            f"{POOL_ATTRS[family]!r} attribute"
        )
    pools = list(coll.find())
    if not pools:
        raise IxiaNotFoundError(
            f"NetworkGroup {getattr(ng, 'Name', '?')!r} has no "
            f"{family} prefix pools"
        )
    if index < 1 or index > len(pools):
        raise IxiaNotFoundError(
            f"Pool index {index} out of range (NG "
            f"{getattr(ng, 'Name', '?')!r} has {len(pools)} {family} pool(s))"
        )
    return pools[index - 1]


def resolve_route_property(pool, *, kind: str = "bgpIPRouteProperty"):
    """Find a route-property child on ``pool`` by RestPy attribute name."""
    if kind not in ROUTE_PROPERTY_ATTRS:
        raise IxiaNotFoundError(
            f"Unknown route_property {kind!r}. Valid: "
            f"{sorted(ROUTE_PROPERTY_ATTRS)}."
        )
    attr = ROUTE_PROPERTY_ATTRS[kind]
    coll = getattr(pool, attr, None)
    if coll is None:
        raise IxiaNotFoundError(
            f"Pool {getattr(pool, 'href', '?')} has no "
            f"{kind!r} route-property collection"
        )
    rps = list(coll.find())
    if not rps:
        raise IxiaNotFoundError(
            f"Pool {getattr(pool, 'href', '?')} has zero "
            f"{kind!r} entries"
        )
    return rps[0]


def resolve_ethernet(dg, ref: Union[str, int]):
    """Find an Ethernet stack inside ``dg`` by name (str) or 1-based index (int).

    The 1-based default in the stack-builder tools (``ixia_create_ipv4``)
    is meant for the common "DG has one ethernet, just give me that one"
    case — passing ``ethernet=1`` (default) picks
    ``Ethernet.find()[0]``.
    """
    eths = list(dg.Ethernet.find())
    if not eths:
        raise IxiaNotFoundError(
            f"DG {getattr(dg, 'Name', '?')!r} has no ethernet stacks"
        )
    if isinstance(ref, int):
        if ref < 1 or ref > len(eths):
            raise IxiaNotFoundError(
                f"Ethernet index {ref} out of range "
                f"(DG {getattr(dg, 'Name', '?')!r} has {len(eths)} ethernet)"
            )
        return eths[ref - 1]
    for eth in eths:
        if getattr(eth, "Name", "") == ref:
            return eth
    raise IxiaNotFoundError(
        f"Ethernet {ref!r} not found under DG "
        f"{getattr(dg, 'Name', '?')!r}"
    )


def resolve_ipv4(eth, ref: Union[str, int]):
    """Find an IPv4 stack under ``eth`` by name (str) or 1-based index (int).

    Default-1 picks the first IPv4 stack — the common case where each
    Ethernet has exactly one IPv4 stack on top.
    """
    ipv4s = list(eth.Ipv4.find())
    if not ipv4s:
        raise IxiaNotFoundError(
            f"Ethernet {getattr(eth, 'Name', '?')!r} has no IPv4 stacks"
        )
    if isinstance(ref, int):
        if ref < 1 or ref > len(ipv4s):
            raise IxiaNotFoundError(
                f"IPv4 index {ref} out of range "
                f"(Ethernet {getattr(eth, 'Name', '?')!r} has "
                f"{len(ipv4s)} IPv4 stacks)"
            )
        return ipv4s[ref - 1]
    for ipv4 in ipv4s:
        if getattr(ipv4, "Name", "") == ref:
            return ipv4
    raise IxiaNotFoundError(
        f"IPv4 {ref!r} not found under Ethernet "
        f"{getattr(eth, 'Name', '?')!r}"
    )


def find_bgp_peer(dg, name: str):
    """Locate a ``bgpIpv4Peer`` by name under ``dg`` (walks Ethernet/IPv4).

    Returns ``(peer, ipv4)`` so callers that need the parent IPv4 stack
    (for address/gateway readouts) don't re-walk the tree.
    """
    for eth in dg.Ethernet.find():
        for ipv4 in eth.Ipv4.find():
            for peer in ipv4.BgpIpv4Peer.find():
                if getattr(peer, "Name", "") == name:
                    return peer, ipv4
    raise IxiaNotFoundError(
        f"BGP peer {name!r} not found under DG "
        f"{getattr(dg, 'Name', '?')!r}"
    )


def list_bgp_peers(dg) -> List[Tuple[Any, Any]]:
    """Return ``[(peer, ipv4), …]`` for every BGP peer under ``dg``."""
    out: List[Tuple[Any, Any]] = []
    for eth in dg.Ethernet.find():
        for ipv4 in eth.Ipv4.find():
            for peer in ipv4.BgpIpv4Peer.find():
                out.append((peer, ipv4))
    return out
