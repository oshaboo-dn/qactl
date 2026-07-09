"""BFD-over-IPv4 NGPF builder + inspector.

IxNetwork models BFD as a ``bfdv4Interface`` stacked on top of an IPv4
stack — a sibling of ``bgpIpv4Peer`` under the same IPv4:

    DeviceGroup
      └─ Ethernet
          └─ Ipv4
              ├─ BgpIpv4Peer      ← ixia_create_bgp_peer (stack.py)
              └─ Bfdv4Interface   ← this module

Two distinct things wire BFD into a BGP test:

1. **The BFD session itself** — a ``bfdv4Interface`` with its own
   tx/rx intervals, detect multiplier, and admin state. That's what
   actually drives a BFD session up/down on the wire and what a DUT
   sees. Built/inspected/torn down here
   (:func:`ixia_create_bfdv4_interface` /
   :func:`ixia_get_bfdv4_interface` /
   :func:`ixia_delete_bfdv4_interface`).

2. **BGP-over-BFD registration** — the ``bgpIpv4Peer`` flag
   ``enableBfdRegistration`` (+ ``modeOfBfdOperations`` single/multi
   hop) that ties the peer's session liveness to BFD. That lives on
   the peer, so it's set through ``ixia_create_bgp_peer``'s ``bfd`` /
   ``bfd_mode`` arguments (see stack.py). Multihop on/off is a peer
   property in NGPF, not a ``bfdv4Interface`` one, which is why it's
   exposed there.

Implementation mirrors ``ixia_tools.stack``: ``.add()`` the parent then
PATCH ``<mv_href>/singleValue`` for each multivalue via the shared
:func:`patch_singlevalue` helper (avoids the Batch-Assistance licence
gate ``mv.Single()`` trips on this lab). Scalars (``aggregateBfdSession``,
``noOfSessions``, ``multiplier``) ride the ``.add()`` kwargs.

Scope: IPv4 only. IPv6 BFD (``bfdv6Interface``) follows the same shape;
add a sibling tool when a scenario needs it instead of overloading the
IPv4 path.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Union

from qactl.ixia.client.models import IxiaError, IxiaNotFoundError, IxiaOperationError

from qactl.ixia.core.envelope import make_envelope, error_envelope
from qactl.ixia.core.session import (
    DEFAULT_PORT, DEFAULT_USER,
    get_session, write_lock, session_id_of,
)
from qactl.ixia.tools._ngpf_lookup import (
    confirm_guard,
    patch_singlevalue,
    patch_singlevalue_if_set,
    resolve_device_group,
    resolve_ethernet,
    resolve_ipv4,
    resolve_topology,
)
from qactl.ixia.tools.build import _bounce_if_running


# IxNetwork's ``stateCounts`` is documented as
# ``dict(total, notStarted, down, up)`` but the REST server returns the
# values under positional keys ``arg1..arg4`` in that same order. Remap
# so a verdict read can do ``state_counts["up"]`` instead of guessing.
_STATE_COUNT_KEYS = ("total", "notStarted", "down", "up")


def _normalise_state_counts(counts: Any) -> Any:
    """Relabel a positional ``{arg1..arg4}`` stateCounts dict.

    Pass through anything that's already keyed by name (or isn't a
    dict) untouched, so a future server that returns the documented
    shape keeps working.
    """
    if not isinstance(counts, dict):
        return counts
    if all(k in counts for k in ("total", "up")):
        return dict(counts)
    out: Dict[str, Any] = {}
    for i, name in enumerate(_STATE_COUNT_KEYS, start=1):
        if f"arg{i}" in counts:
            out[name] = counts[f"arg{i}"]
    return out or dict(counts)


def _find_bfdv4(ipv4_obj, name: str):
    """Return the bfdv4Interface named ``name`` under ``ipv4_obj`` or None."""
    for bfd in ipv4_obj.Bfdv4Interface.find():
        if getattr(bfd, "Name", "") == name:
            return bfd
    return None


def _resolve_bfdv4(dg, name: str, ethernet: Union[str, int], ipv4: Union[str, int]):
    """Resolve a bfdv4Interface by name under a chosen ethernet/ipv4.

    Returns ``(eth, ipv4_obj, bfd)``. Raises ``IxiaNotFoundError`` if
    the interface name isn't found under the resolved IPv4 stack.
    """
    eth = resolve_ethernet(dg, ethernet)
    ipv4_obj = resolve_ipv4(eth, ipv4)
    bfd = _find_bfdv4(ipv4_obj, name)
    if bfd is None:
        raise IxiaNotFoundError(
            f"BFD interface {name!r} not found under IPv4 "
            f"{getattr(ipv4_obj, 'Name', '?')!r} in DG "
            f"{getattr(dg, 'Name', '?')!r}"
        )
    return eth, ipv4_obj, bfd


def ixia_create_bfdv4_interface(
    host: str,
    topology: str,
    device_group: Union[str, int],
    name: str,
    ethernet: Union[str, int] = 1,
    ipv4: Union[str, int] = 1,
    tx_interval: Optional[int] = None,
    rx_interval: Optional[int] = None,
    detect_multiplier: Optional[int] = None,
    admin_state: Optional[bool] = None,
    control_plane_independent: Optional[bool] = None,
    aggregate: Optional[bool] = None,
    no_of_sessions: Optional[int] = None,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Add a bfdv4Interface on top of an existing IPv4 stack.

    This builds the BFD session emulator itself. To make a BGP peer
    use it for liveness, also register the peer with
    ``ixia_create_bgp_peer(... bfd=True ...)`` (or set
    ``enableBfdRegistration`` on the existing peer).

    Args:
        topology: Parent topology name (exact match).
        device_group: Parent DG. Name (str) or 1-based index (int).
        ethernet: Parent ethernet inside the DG. Name (str) or 1-based
            index (int, default 1 — the first ethernet).
        ipv4: Parent IPv4 stack under the chosen ethernet. Name (str)
            or 1-based index (int, default 1).
        name: BFD interface name. Must be unique inside the IPv4 stack.
        tx_interval: Desired min TX interval in milliseconds
            (``txInterval``). Omit for IxNetwork default (1000).
        rx_interval: Required min RX interval in milliseconds
            (``minRxInterval``). Omit for IxNetwork default (1000).
        detect_multiplier: Detection time multiplier
            (``timeoutMultiplier``). Omit for IxNetwork default (3).
        admin_state: ``active`` flag. ``True`` enables the session,
            ``False`` keeps it administratively down. Omit to keep the
            IxNetwork default (enabled).
        control_plane_independent: ``enableControlPlaneIndependent`` —
            keep BFD up across a control-plane (BGP) restart. Omit to
            leave the default.
        aggregate: ``aggregateBfdSession`` scalar. When True all
            interfaces except VNI 0 are disabled (single aggregated
            session). Omit for the IxNetwork default.
        no_of_sessions: ``noOfSessions`` scalar — number of configured
            BFD sessions. Omit for the IxNetwork default.

    Returns envelope with ``result = {topology, device_group, ethernet,
    ipv4, name, href, tx_interval, rx_interval, detect_multiplier,
    admin_state, control_plane_independent, aggregate, no_of_sessions}``.

    Notes:
        - No silent-bounce: the new interface lands ``notStarted``;
          restart the parent DG / topology (or apply-on-the-fly) to
          bring the session up.
        - Multihop is a *peer* property in NGPF
          (``modeOfBfdOperations``), not a ``bfdv4Interface`` one — set
          it via ``ixia_create_bgp_peer(... bfd_mode="multihop" ...)``.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "ethernet": ethernet, "ipv4": ipv4, "name": name,
        "tx_interval": tx_interval, "rx_interval": rx_interval,
        "detect_multiplier": detect_multiplier, "admin_state": admin_state,
        "control_plane_independent": control_plane_independent,
        "aggregate": aggregate, "no_of_sessions": no_of_sessions,
    }

    for label, value in (
        ("tx_interval", tx_interval),
        ("rx_interval", rx_interval),
    ):
        if value is not None and (not isinstance(value, int) or value < 1):
            return error_envelope(
                f"{label} must be a positive integer (milliseconds).",
                kind="create_bfdv4_interface", host=host, port=port,
                status="bad_argument",
            )
    if detect_multiplier is not None and (
        not isinstance(detect_multiplier, int) or detect_multiplier < 1
    ):
        return error_envelope(
            "detect_multiplier must be a positive integer.",
            kind="create_bfdv4_interface", host=host, port=port,
            status="bad_argument",
        )
    if no_of_sessions is not None and (
        not isinstance(no_of_sessions, int) or no_of_sessions < 1
    ):
        return error_envelope(
            "no_of_sessions must be a positive integer.",
            kind="create_bfdv4_interface", host=host, port=port,
            status="bad_argument",
        )

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="create_bfdv4_interface",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="create_bfdv4_interface", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        ixn = s.ixn
        with write_lock(host, port, user):
            topo = resolve_topology(ixn, topology)
            dg = resolve_device_group(topo, device_group)
            eth = resolve_ethernet(dg, ethernet)
            ipv4_obj = resolve_ipv4(eth, ipv4)

            add_kwargs: Dict[str, Any] = {"Name": name}
            if aggregate is not None:
                add_kwargs["AggregateBfdSession"] = bool(aggregate)
            if no_of_sessions is not None:
                add_kwargs["NoOfSessions"] = int(no_of_sessions)
            bfd = ipv4_obj.Bfdv4Interface.add(**add_kwargs)
            bfd_href = getattr(bfd, "href", "")
            if not bfd_href:
                raise IxiaOperationError(
                    "bfdv4Interface href missing after creation."
                )

            try:
                bfd_body = ixn._connection._read(bfd_href)
                if not isinstance(bfd_body, dict):
                    raise IxiaOperationError(
                        f"Unexpected bfdv4Interface body shape for "
                        f"{bfd_href}: {type(bfd_body).__name__}"
                    )
                patch_singlevalue_if_set(
                    ixn, bfd_body["txInterval"], tx_interval,
                )
                patch_singlevalue_if_set(
                    ixn, bfd_body["minRxInterval"], rx_interval,
                )
                patch_singlevalue_if_set(
                    ixn, bfd_body["timeoutMultiplier"], detect_multiplier,
                )
                if admin_state is not None:
                    patch_singlevalue(
                        ixn, bfd_body["active"],
                        "true" if admin_state else "false",
                    )
                if control_plane_independent is not None:
                    patch_singlevalue(
                        ixn, bfd_body["enableControlPlaneIndependent"],
                        "true" if control_plane_independent else "false",
                    )
            except Exception:
                try:
                    bfd.remove()
                except Exception:
                    pass
                raise

        env["result"] = {
            "topology": topology,
            "device_group": getattr(dg, "Name", str(device_group)),
            "ethernet": getattr(eth, "Name", str(ethernet)),
            "ipv4": getattr(ipv4_obj, "Name", str(ipv4)),
            "name": getattr(bfd, "Name", name),
            "href": bfd_href,
            "tx_interval": tx_interval,
            "rx_interval": rx_interval,
            "detect_multiplier": detect_multiplier,
            "admin_state": admin_state,
            "control_plane_independent": control_plane_independent,
            "aggregate": aggregate,
            "no_of_sessions": no_of_sessions,
        }
        return env
    except IxiaNotFoundError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        env["next_actions"].append(
            "Run `qactl ixia session describe` to confirm DG / ethernet / "
            "ipv4 names."
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


def ixia_get_bfdv4_interface(
    host: str,
    topology: str,
    name: str,
    device_group: Union[str, int] = 1,
    ethernet: Union[str, int] = 1,
    ipv4: Union[str, int] = 1,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Inspect a bfdv4Interface — config + live session state.

    Surfaces the configured timers / detect multiplier / admin state
    plus the runtime ``session_status`` (per-session ``up`` / ``down`` /
    ``notStarted``) and the aggregate ``state_counts`` so a verdict read
    can assert "BFD is up" without dropping into raw REST. Read-only.

    ``device_group`` / ``ethernet`` / ``ipv4`` accept a name (str) or a
    1-based index (int, default 1 — the first of each).
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "ethernet": ethernet, "ipv4": ipv4, "name": name,
    }
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="get_bfdv4_interface",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="get_bfdv4_interface", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ixn = s.ixn
        topo = resolve_topology(ixn, topology)
        dg = resolve_device_group(topo, device_group)
        _eth, ipv4_obj, bfd = _resolve_bfdv4(dg, name, ethernet, ipv4)
        env["result"] = _build_bfdv4_view(bfd, ixn)
        return env
    except IxiaNotFoundError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def _build_bfdv4_view(bfd, ixn) -> Dict[str, Any]:
    """Shared read body for the bfdv4Interface inspector.

    Imported lazily by ``ixia_tools.inspect`` so the session/topology
    describe can embed BFD state per IPv4 stack without re-walking.
    """
    from qactl.ixia.tools.inspect import _mv_scalar, _coerce_int, _safe_bool

    status = getattr(bfd, "SessionStatus", None)
    if not isinstance(status, list):
        status = [status] if status is not None else []
    counts = _normalise_state_counts(getattr(bfd, "StateCounts", None))
    return {
        "name": getattr(bfd, "Name", "") or None,
        "href": getattr(bfd, "href", None),
        "tx_interval": _coerce_int(
            _mv_scalar(getattr(bfd, "TxInterval", None), ixn)
        ),
        "rx_interval": _coerce_int(
            _mv_scalar(getattr(bfd, "MinRxInterval", None), ixn)
        ),
        "detect_multiplier": _coerce_int(
            _mv_scalar(getattr(bfd, "TimeoutMultiplier", None), ixn)
        ),
        "admin_state": _safe_bool(
            _mv_scalar(getattr(bfd, "Active", None), ixn)
        ),
        "control_plane_independent": _safe_bool(
            _mv_scalar(getattr(bfd, "EnableControlPlaneIndependent", None), ixn)
        ),
        "aggregate": _safe_bool(getattr(bfd, "AggregateBfdSession", None)),
        "no_of_sessions": _coerce_int(getattr(bfd, "NoOfSessions", None)),
        "session_status": list(status),
        "state_counts": dict(counts) if isinstance(counts, dict) else counts,
    }


def ixia_delete_bfdv4_interface(
    host: str,
    topology: str,
    device_group: Union[str, int],
    name: str,
    ethernet: Union[str, int] = 1,
    ipv4: Union[str, int] = 1,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Remove a bfdv4Interface from an IPv4 stack.

    The parent IPv4 stack and any BGP peer on it stay put — only the
    BFD interface goes away. A BGP peer still carrying
    ``enableBfdRegistration`` will have nothing to register against
    until a new interface is added, so unregister the peer too if you
    no longer want BGP-over-BFD.

    Args:
        topology: Parent topology name (exact match).
        device_group: Parent DG. Name (str) or 1-based index (int).
        ethernet: Parent ethernet inside the DG. Name (str) or 1-based
            index (int, default 1).
        ipv4: Parent IPv4 stack. Name (str) or 1-based index (int,
            default 1).
        name: Exact BFD interface name to delete.
        confirm: Must be ``True`` or the call returns
            ``status="confirmation_required"`` without touching
            IxNetwork.

    Returns envelope with ``result = {topology, device_group, ethernet,
    ipv4, deleted, bounced, bounce_elapsed_s}``.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "ethernet": ethernet, "ipv4": ipv4, "name": name, "confirm": confirm,
    }
    guard = confirm_guard(
        kind="delete_bfdv4_interface", host=host, port=port, confirm=confirm,
    )
    if guard is not None:
        guard["request"] = request
        return guard

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="delete_bfdv4_interface",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="delete_bfdv4_interface", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ixn = s.ixn
        with write_lock(host, port, user):
            tp = resolve_topology(ixn, topology)
            dg = resolve_device_group(tp, device_group)
            eth, ipv4_obj, bfd = _resolve_bfdv4(dg, name, ethernet, ipv4)
            with _bounce_if_running(ixn, tp) as (running, t0):
                bfd.remove()
        env["result"] = {
            "topology": topology,
            "device_group": getattr(dg, "Name", str(device_group)),
            "ethernet": getattr(eth, "Name", str(ethernet)),
            "ipv4": getattr(ipv4_obj, "Name", str(ipv4)),
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
            "Run `qactl ixia session describe` to see BFD interface names."
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


def register(mcp) -> None:
    mcp.tool()(ixia_create_bfdv4_interface)
    mcp.tool()(ixia_get_bfdv4_interface)
    mcp.tool()(ixia_delete_bfdv4_interface)


__all__ = [
    "ixia_create_bfdv4_interface",
    "ixia_get_bfdv4_interface",
    "ixia_delete_bfdv4_interface",
    "register",
]
