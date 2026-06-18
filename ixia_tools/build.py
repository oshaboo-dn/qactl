"""Create / delete tools for topologies, device groups, traffic items.

Strategy
--------
These tools are thin wrappers over the bundled ``ixia`` package where
possible, and drop down to raw ``ixn.*`` RestPy calls where the wrapper
is too narrow (e.g. binding a topology to specific vports at creation
time — ``s.topology.create(name)`` doesn't accept a ``Vports`` kwarg
today, so we call ``ixn.Topology.add(Name=, Vports=[...])`` directly).

All mutating tools:
- take ``write_lock`` so two concurrent MCP calls into the same session
  don't race,
- require ``confirm=True`` on delete tools (create tools don't — the
  user named it, they mean it).

Scope of pass 2
---------------
- Topology create/delete with optional vport binding
- Device group create/delete (multiplier only; protocol stacks come
  later)
- Traffic item create/delete — raw type only (src/dst are vport hrefs).
  IPv4 / BGP endpoint-set traffic waits for protocol stack tools.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ixia.models import IxiaError, IxiaNotFoundError, IxiaOperationError

from ixia_core.envelope import make_envelope, error_envelope
from ixia_core.session import (
    DEFAULT_PORT, DEFAULT_USER,
    get_session, write_lock, session_id_of,
)
from ixia_tools._ngpf_lookup import (
    confirm_guard,
    extract_self_href,
    find_bgp_peer,
    patch_singlevalue,
)


def _resolve_topology(ixn, name: str):
    """Find a topology by exact name or raise IxiaOperationError."""
    for tp in ixn.Topology.find():
        if getattr(tp, "Name", "") == name:
            return tp
    raise IxiaOperationError(f"Topology {name!r} not found")


def _resolve_dg(topo, name: str):
    """Find a DG by exact name inside the given topology or raise."""
    for dg in topo.DeviceGroup.find():
        if getattr(dg, "Name", "") == name:
            return dg
    raise IxiaOperationError(
        f"Device group {name!r} not found in topology {getattr(topo, 'Name', '?')!r}"
    )


def _resolve_traffic_item(ixn, name: str):
    for ti in ixn.Traffic.TrafficItem.find():
        if getattr(ti, "Name", "") == name:
            return ti
    raise IxiaOperationError(f"Traffic item {name!r} not found")


_RUNNING_STATES = {"started", "starting", "mixed"}


def _topology_is_running(ixn, tp) -> bool:
    """True if the topology is currently in started / starting / mixed state.

    The IxNetwork ``topology.status`` field reports one of:
    ``notStarted``, ``stopped``, ``stopping``, ``started``, ``starting``,
    ``mixed``. ``mixed`` means some children are up and some are not —
    we treat that as running for the bounce-decision (any child being up
    is enough to break the connector PATCH).

    Reads via raw REST to avoid any RestPy-side caching: the ``Status``
    attribute on a long-lived ``Topology`` object can stay stale across
    background state changes (e.g. another agent starts protocols
    between when this object was resolved and now).
    """
    try:
        body = ixn._connection._read(tp.href)
        state = str(body.get("status") or "").lower()
    except Exception:
        state = str(getattr(tp, "Status", "") or "").lower()
    return state in _RUNNING_STATES


@contextmanager
def _bounce_if_running(ixn, tp) -> Iterator[Tuple[bool, float]]:
    """Stop the topology around a mutation, restart it afterwards.

    Pattern shared by every mutating tool that IxNetwork rejects on a
    running topology — connector PATCHes during NG create, NG/DG
    removes, etc. Caller policy in this codebase is silent bounce:
    don't ask, don't warn, just stop-mutate-start. Yields
    ``(was_running, t0)`` so the caller can populate ``bounced`` and
    ``bounce_elapsed_s`` in the response envelope.

    The restart happens in a ``finally`` so a mutation failure still
    leaves the topology back in its pre-call state (running) instead
    of stranded stopped.
    """
    running = _topology_is_running(ixn, tp)
    t0 = time.time()
    if running:
        tp.Stop()
    try:
        yield running, t0
    finally:
        if running:
            tp.Start()


# --------------------------------------------------------------------- topology

def ixia_create_topology(
    host: str,
    name: str,
    vport_hrefs: Optional[List[str]] = None,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Create a new topology bound to zero or more vports.

    Args:
        name: Topology name. Must be unique in the current session.
        vport_hrefs: Optional list of Vport ``href`` strings (e.g.
            ``/api/v1/sessions/1/ixnetwork/vport/3``) to bind on create.
            Use :func:`ixia_list_vports` to discover them. Binding at
            create time is preferred over doing it after; the REST API
            accepts ``Vports=[...]`` on ``Topology.add``.
    """
    request = {
        "host": host, "port": port, "user": user,
        "name": name, "vport_hrefs": vport_hrefs,
    }
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="create_topology",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="create_topology", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        with write_lock(host, port, user):
            if vport_hrefs:
                tp = s.ixn.Topology.add(Name=name, Vports=list(vport_hrefs))
            else:
                tp = s.ixn.Topology.add(Name=name)
        env["result"] = {
            "name": getattr(tp, "Name", name),
            "href": getattr(tp, "href", ""),
            "vports": list(vport_hrefs or []),
        }
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_delete_topology(
    host: str,
    name: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Delete a topology (and every DG / protocol below it) by exact name."""
    request = {
        "host": host, "port": port, "user": user,
        "name": name, "confirm": confirm,
    }
    guard = confirm_guard(
        kind="delete_topology", host=host, port=port, confirm=confirm
    )
    if guard is not None:
        guard["request"] = request
        return guard

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="delete_topology",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="delete_topology", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        with write_lock(host, port, user):
            tp = _resolve_topology(s.ixn, name)
            # Stop first if running. IxNetwork rejects topology.remove()
            # while protocols are up; no restart needed because the
            # topology won't exist after.
            running = _topology_is_running(s.ixn, tp)
            t0 = time.time()
            if running:
                tp.Stop()
            tp.remove()
        env["result"] = {
            "deleted": name,
            "bounced": running,
            "bounce_elapsed_s": round(time.time() - t0, 2) if running else 0.0,
        }
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


# ----------------------------------------------------------------- device group

def ixia_create_device_group(
    host: str,
    topology: str,
    name: str,
    multiplier: int = 1,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Add a device group under ``topology``.

    Args:
        topology: Parent topology name (exact match).
        name: DG name. Must be unique inside the topology.
        multiplier: Session count for the DG (default 1). More
            sessions => more copies of whatever protocol stack is added
            under the DG (ethernet, IPv4, BGP, ...).
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "name": name, "multiplier": multiplier,
    }
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="create_device_group",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="create_device_group", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        with write_lock(host, port, user):
            tp = _resolve_topology(s.ixn, topology)
            dg = tp.DeviceGroup.add(Name=name, Multiplier=int(multiplier))
        env["result"] = {
            "topology": topology,
            "name": getattr(dg, "Name", name),
            "multiplier": int(multiplier),
            "href": getattr(dg, "href", ""),
        }
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_delete_device_group(
    host: str,
    topology: str,
    name: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Remove a named device group from a topology."""
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "name": name, "confirm": confirm,
    }
    guard = confirm_guard(
        kind="delete_device_group", host=host, port=port, confirm=confirm
    )
    if guard is not None:
        guard["request"] = request
        return guard

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="delete_device_group",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="delete_device_group", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        with write_lock(host, port, user):
            tp = _resolve_topology(s.ixn, topology)
            dg = _resolve_dg(tp, name)
            with _bounce_if_running(s.ixn, tp) as (running, t0):
                dg.remove()
        env["result"] = {
            "topology": topology, "deleted": name,
            "bounced": running,
            "bounce_elapsed_s": round(time.time() - t0, 2) if running else 0.0,
        }
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


# ---------------------------------------------------------------- network group


def _patch_counter(ixn, mv_href: str, *, start: str, step: str) -> None:
    """Set a multivalue's ``counter`` (per-line increment) via raw REST.

    Used for LU label-per-line where ``count > 1``: each route-range
    line gets its own MPLS label, starting at ``start`` and stepping
    by ``step``.
    """
    ixn._connection._update(
        mv_href + "/counter",
        {"start": str(start), "step": str(step), "direction": "increment"},
    )




def ixia_create_network_group(
    host: str,
    topology: str,
    device_group: str,
    name: str,
    prefix: str,
    prefix_len: int,
    count: int = 1,
    connect_to_peer: Optional[str] = None,
    connect_to_href: Optional[str] = None,
    advertise_as_rfc8277: bool = False,
    label_start: Optional[int] = None,
    label_step: int = 1,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Add a NetworkGroup with an IPv4 prefix pool wired to a specific stack.

    Closes the layered gap between ``ixia_create_device_group`` and the
    raw 5-call dance (``POST networkGroup`` → ``POST ipv4PrefixPools`` →
    PATCH multivalues → ``POST bgpIPRouteProperty`` → PATCH connector).
    The connector PATCH is the load-bearing bit: by default IxNetwork
    wires the prefix pool to the parent ``ethernet``, which advertises
    nothing — we re-point it at the chosen ``bgpIpv4Peer`` so the
    routes actually go out on the wire.

    Address family
    --------------
    IPv4 only in this pass. IPv6 / VPNv4 are deferred — when needed,
    add a sibling tool rather than overloading this one.

    Multiplier / per-line behaviour
    -------------------------------
    ``count`` becomes the NG's ``Multiplier``. With the default
    ``NumberOfAddresses=1`` per line, this produces ``count`` route-range
    lines whose network addresses auto-increment by the natural
    ``/prefix_len`` step (e.g. ``count=3, prefix='4.1.4.0', prefix_len=24``
    → 4.1.4.0/24, 4.1.5.0/24, 4.1.6.0/24). For LU, ``label_start`` /
    ``label_step`` drive a matching counter on ``LabelStart`` so each
    line gets a distinct label. To toggle individual lines after
    creation, use ``ixia_route_action``.

    Connect-to addressing
    ---------------------
    Pass exactly one of:

    - ``connect_to_peer`` — peer name, looked up under the same DG.
      Walks ``Ethernet/N/Ipv4/N/BgpIpv4Peer/N``. Errors with the actual
      list of peers under the DG inline if not found.
    - ``connect_to_href`` — raw ``/api/v1/sessions/.../bgpIpv4Peer/N``
      escape hatch for callers that already have the href.

    Topology bounce (gotcha this hides)
    -----------------------------------
    PATCHing ``connector.connectedTo`` is rejected by IxNetwork while
    the parent topology is in a ``started``-ish state. When this tool
    detects that, it stops the topology before any mutation and starts
    it again at the end. ``result.bounced`` reports whether that
    happened. On a stopped topology no bounce occurs and routes will
    start advertising the next time you run ``ixia_topology_start``.

    Implementation note (Batch Assistance)
    --------------------------------------
    ``BgpIPRouteProperty`` and several multivalue writes go through
    raw REST (POST/PATCH) instead of RestPy's high-level helpers. The
    helpers route through ``_add_xpath`` / auto-apply paths that fail
    with ``"This feature is only available with Batch Assistance"`` on
    lab licences without Config Assistant. The raw-REST sequence
    matches the verified manual repro and stays within the basic tier.

    Args:
        topology: Parent topology name (exact match).
        device_group: Parent DG name (must already exist).
        name: NG name (must be unique inside the DG).
        prefix: Network address, e.g. ``"4.1.4.0"``.
        prefix_len: 0-32 (e.g. 24).
        count: NG multiplier (number of route-range lines, default 1).
        connect_to_peer / connect_to_href: One required, the other left None.
        advertise_as_rfc8277: True for BGP-LU advertisement (RFC 8277).
            ``label_start`` becomes mandatory in that case.
        label_start: Starting MPLS label when ``advertise_as_rfc8277``.
        label_step: Per-line label step (default 1).

    Returns envelope with ``result = {ng_href, prefix_pool_href,
    route_property_href, peer_href, bounced, bounce_elapsed_s}``.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group, "name": name,
        "prefix": prefix, "prefix_len": prefix_len, "count": count,
        "connect_to_peer": connect_to_peer,
        "connect_to_href": connect_to_href,
        "advertise_as_rfc8277": advertise_as_rfc8277,
        "label_start": label_start, "label_step": label_step,
    }

    # ---- argument validation (fail fast, no IxNetwork session needed)
    if not isinstance(prefix_len, int) or prefix_len < 0 or prefix_len > 32:
        return error_envelope(
            "prefix_len must be an integer in [0, 32].",
            kind="create_network_group", host=host, port=port,
            status="bad_argument",
        )
    if not isinstance(count, int) or count < 1:
        return error_envelope(
            "count must be a positive integer (NG multiplier).",
            kind="create_network_group", host=host, port=port,
            status="bad_argument",
        )
    if (connect_to_peer is None) == (connect_to_href is None):
        return error_envelope(
            "Pass exactly one of connect_to_peer or connect_to_href.",
            kind="create_network_group", host=host, port=port,
            status="bad_argument",
        )
    if advertise_as_rfc8277 and label_start is None:
        return error_envelope(
            "advertise_as_rfc8277=True requires label_start to be set.",
            kind="create_network_group", host=host, port=port,
            status="bad_argument",
        )
    if label_start is not None and (
        not isinstance(label_start, int) or label_start < 0
    ):
        return error_envelope(
            "label_start must be a non-negative integer.",
            kind="create_network_group", host=host, port=port,
            status="bad_argument",
        )
    if not isinstance(label_step, int) or label_step < 1:
        return error_envelope(
            "label_step must be a positive integer.",
            kind="create_network_group", host=host, port=port,
            status="bad_argument",
        )

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="create_network_group",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="create_network_group", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        with write_lock(host, port, user):
            tp = _resolve_topology(s.ixn, topology)
            dg = _resolve_dg(tp, device_group)

            # Resolve connector target up-front so we fail before
            # creating anything if the peer name is wrong.
            if connect_to_peer is not None:
                try:
                    peer, _ = find_bgp_peer(dg, connect_to_peer)
                except IxiaNotFoundError as e:
                    env["status"] = "error"
                    env["errors"].append(str(e))
                    env["next_actions"].append(
                        "Call ixia_get_topology to see the available "
                        "BGP peer names under this DG."
                    )
                    return env
                peer_href = getattr(peer, "href", "")
            else:
                peer_href = str(connect_to_href)

            # Bounce-up-front: the connector PATCH at the end is
            # rejected on a running topology. The user opted into silent
            # bounce, so front-load it for the whole sequence.
            with _bounce_if_running(s.ixn, tp) as (running, t0):
                # NG comes up cleanly via RestPy ``.add()`` (it goes
                # through ``_add``, not the licence-gated
                # ``_add_xpath``). Everything after this point that
                # fails must roll back the NG so the topology doesn't
                # accumulate half-built zombies.
                ng = dg.NetworkGroup.add(Name=name, Multiplier=int(count))
                ng_href = ng.href
                try:
                    pool = ng.Ipv4PrefixPools.add()
                    pool_href = pool.href
                    if not pool_href:
                        raise IxiaOperationError(
                            "ipv4PrefixPool href missing after creation."
                        )

                    # bgpIPRouteProperty MUST be created via raw POST.
                    # RestPy's ``pool.BgpIPRouteProperty.add()`` routes
                    # through ``_add_xpath`` which raises
                    # ``"This feature is only available with Batch
                    # Assistance"`` on lab licences without Config
                    # Assistant. Verified via /tmp/probe_ng.py on
                    # 2026-05-05.
                    rp_resp = s.ixn._connection._create(
                        pool_href + "/bgpIPRouteProperty", {}
                    )
                    rp_href = extract_self_href(rp_resp) or (
                        pool_href + f"/bgpIPRouteProperty/{rp_resp['id']}"
                        if isinstance(rp_resp, dict) and "id" in rp_resp
                        else None
                    )
                    if not rp_href:
                        raise IxiaOperationError(
                            "Could not extract bgpIPRouteProperty href "
                            f"from POST response: {str(rp_resp)[:200]}"
                        )

                    # Multivalue mutation via raw REST PATCH on
                    # ``<mv>/singleValue`` — NOT RestPy's ``.Single()``
                    # helper, which also routes through the
                    # licence-gated path on some attributes.
                    pool_body = s.ixn._connection._read(pool_href)
                    patch_singlevalue(
                        s.ixn, pool_body["networkAddress"], prefix
                    )
                    patch_singlevalue(
                        s.ixn, pool_body["prefixLength"], str(int(prefix_len))
                    )

                    if advertise_as_rfc8277:
                        # ``advertiseAsRfc8277`` is a direct boolean
                        # field on the routeProperty. ``labelStart`` is
                        # a multivalue.
                        s.ixn._connection._update(
                            rp_href, {"advertiseAsRfc8277": True}
                        )
                        rp_body = s.ixn._connection._read(rp_href)
                        if count == 1 or label_step == 0:
                            patch_singlevalue(
                                s.ixn,
                                rp_body["labelStart"],
                                str(int(label_start)),
                            )
                        else:
                            _patch_counter(
                                s.ixn,
                                rp_body["labelStart"],
                                start=str(int(label_start)),
                                step=str(int(label_step)),
                            )

                    # Connector PATCH (the load-bearing bit) — points
                    # the prefix pool at the chosen peer instead of the
                    # default ethernet child, so the routes actually
                    # advertise.
                    s.ixn._connection._update(
                        pool_href + "/connector",
                        {"connectedTo": peer_href},
                    )
                except Exception:
                    # Roll back the half-built NG so the topology
                    # doesn't accumulate zombies. Best-effort:
                    # rollback failure must not mask the original
                    # error.
                    try:
                        ng.remove()
                    except Exception:
                        pass
                    raise

        env["result"] = {
            "ng_href": ng_href,
            "prefix_pool_href": pool_href,
            "route_property_href": rp_href,
            "peer_href": peer_href,
            "bounced": running,
            "bounce_elapsed_s": round(time.time() - t0, 2) if running else 0.0,
        }
        return env

    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def _resolve_ng(dg, name: str):
    """Find a NetworkGroup by exact name inside the given DG or raise."""
    for ng in dg.NetworkGroup.find():
        if getattr(ng, "Name", "") == name:
            return ng
    raise IxiaOperationError(
        f"Network group {name!r} not found in device group "
        f"{getattr(dg, 'Name', '?')!r}"
    )


def ixia_delete_network_group(
    host: str,
    topology: str,
    device_group: str,
    name: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Remove a named NetworkGroup from a DG.

    Twin of ``ixia_create_network_group``. Routes advertised by this
    NG stop appearing on the wire as soon as the parent topology
    re-applies (or immediately if the NG was already disabled). The
    DELETE itself is accepted whether the topology is running or
    stopped — IxNetwork only blocks edits to ``connector.connectedTo``,
    not full-resource removals.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "name": name, "confirm": confirm,
    }
    guard = confirm_guard(
        kind="delete_network_group", host=host, port=port, confirm=confirm
    )
    if guard is not None:
        guard["request"] = request
        return guard

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="delete_network_group",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="delete_network_group", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        with write_lock(host, port, user):
            tp = _resolve_topology(s.ixn, topology)
            dg = _resolve_dg(tp, device_group)
            ng = _resolve_ng(dg, name)
            with _bounce_if_running(s.ixn, tp) as (running, t0):
                ng.remove()
        env["result"] = {
            "topology": topology, "device_group": device_group,
            "deleted": name,
            "bounced": running,
            "bounce_elapsed_s": round(time.time() - t0, 2) if running else 0.0,
        }
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


# ---------------------------------------------------------------- traffic item

DEFAULT_TRACK_BY = ["sourceDestEndpointPair0"]


def ixia_create_traffic_item(
    host: str,
    name: str,
    src_refs: List[str],
    dst_refs: List[str],
    rate_fps: Optional[int] = None,
    frame_size: Optional[int] = None,
    traffic_type: str = "ipv4",
    track_by: Optional[List[str]] = None,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Create a new traffic item with flow tracking enabled by default.

    Args:
        name: Item name — must be unique in the session.
        src_refs: Endpoint references for the transmit side. Meaning
            depends on ``traffic_type``:
              - ``ipv4`` / ``ipv4WithSrv6`` / typed traffic → DG hrefs
                or NetworkGroup/PrefixPool hrefs (e.g.
                ``/api/v1/sessions/1/ixnetwork/topology/1/deviceGroup/1/networkGroup/1/ipv4PrefixPools/1``)
              - ``raw`` → ``/vport:{id}/protocols`` path-style refs
                (note: NOT ``/api/v1/.../vport/{id}`` hrefs — IxNetwork
                is picky here)
        dst_refs: Same shape as ``src_refs`` for the receive side.
        rate_fps: Frames-per-second rate. Omit to keep RestPy default.
        frame_size: Fixed frame size in bytes. Omit to keep default.
        traffic_type: IxNetwork ``trafficType``. Defaults to ``ipv4``
            (endpoint-based). Use ``raw`` for pure vport-to-vport.
        track_by: Flow-tracking fields. Defaults to
            ``["sourceDestEndpointPair0"]`` — matches what bgp-leak
            uses and gives per-flow rows in Traffic Item Statistics.
            Pass ``[]`` to disable tracking (then stats aggregate into
            one row — usually not what you want).

    Auto-calls ``Generate()`` after creation so flow groups resolve.
    The item stays in ``unapplied`` state — call
    ``ixia_traffic_apply`` + ``ixia_traffic_start`` to run it.
    """
    tb = list(DEFAULT_TRACK_BY) if track_by is None else list(track_by)
    request = {
        "host": host, "port": port, "user": user,
        "name": name,
        "src_refs": list(src_refs or []),
        "dst_refs": list(dst_refs or []),
        "rate_fps": rate_fps, "frame_size": frame_size,
        "traffic_type": traffic_type,
        "track_by": tb,
    }
    if not src_refs or not dst_refs:
        return error_envelope(
            "src_refs and dst_refs are both required and must be non-empty.",
            kind="create_traffic_item", host=host, port=port,
            status="bad_argument",
        )

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="create_traffic_item",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="create_traffic_item", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        with write_lock(host, port, user):
            ixn = s.ixn
            ti = ixn.Traffic.TrafficItem.add(
                Name=name, TrafficType=traffic_type,
            )
            ti.EndpointSet.add(
                Sources=list(src_refs),
                Destinations=list(dst_refs),
            )
            # Flow tracking — set BEFORE Generate() so the flow groups
            # are built with the tracking columns baked in.
            if tb:
                try:
                    ti.Tracking.find().TrackBy = tb
                except Exception as e:
                    env["warnings"].append(
                        f"Tracking setup failed (item created without "
                        f"tracking): {type(e).__name__}: {str(e)[:160]}"
                    )
            config = ti.ConfigElement.find()
            if config:
                if rate_fps is not None:
                    config.FrameRate.update(
                        Type="framesPerSecond", Rate=rate_fps,
                    )
                if frame_size is not None:
                    config.FrameSize.update(
                        Type="fixed", FixedSize=frame_size,
                    )
            ti.Generate()
        env["result"] = {
            "name": name,
            "traffic_type": traffic_type,
            "rate_fps": rate_fps,
            "frame_size": frame_size,
            "track_by": tb,
            "href": getattr(ti, "href", ""),
        }
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_delete_traffic_item(
    host: str,
    name: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Delete a traffic item by exact name."""
    request = {
        "host": host, "port": port, "user": user,
        "name": name, "confirm": confirm,
    }
    guard = confirm_guard(
        kind="delete_traffic_item", host=host, port=port, confirm=confirm
    )
    if guard is not None:
        guard["request"] = request
        return guard

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="delete_traffic_item",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="delete_traffic_item", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        with write_lock(host, port, user):
            ti = _resolve_traffic_item(s.ixn, name)
            ti.remove()
        env["result"] = {"deleted": name}
        return env
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def register(mcp) -> None:
    mcp.tool()(ixia_create_topology)
    mcp.tool()(ixia_delete_topology)
    mcp.tool()(ixia_create_device_group)
    mcp.tool()(ixia_delete_device_group)
    mcp.tool()(ixia_create_network_group)
    mcp.tool()(ixia_delete_network_group)
    mcp.tool()(ixia_create_traffic_item)
    mcp.tool()(ixia_delete_traffic_item)
