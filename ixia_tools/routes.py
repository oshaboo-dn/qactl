"""Per-line route-range control on a running NGPF topology.

Background
----------
On 2026-05-03 the user discovered that none of the obvious "withdraw a
single route" levers actually emitted a BGP UPDATE on the wire:
``rp.Active.Single(False)``, ``rp.Stop()``, ``ng.Stop()``,
``AgeOutRoutes(Percentage=100)``, ``rp.Stop()`` + ``rp.Start()`` — all
returned 200 OK and produced **zero UPDATEs**. See lesson
``2026-05-03-route-toggle-levers.md``.

On 2026-05-05 we closed the lesson: the missing piece is
``ApplyOnTheFly``. Multivalue PATCHes on a running topology are
*passive* — the config record updates, but the CPF/test-engine doesn't
consult it until ``operations/applyonthefly`` fires. Working REST
sequence (verified end-to-end against ``bgp-lu-stale-bug.ixncfg``):

  1. Write the per-line ``values`` vector at
     ``/…/multivalue/{N}/valueList``. The valueList child is a
     singleton:
       * ``pattern == singleValue`` → POST creates it and flips
         ``multivalue.pattern`` to ``valueList``.
       * ``pattern == valueList``  → PATCH updates the existing
         singleton's ``values``. POSTing again raises
         ``NullReferenceException`` from ``SDMObject.CreateChild``.
  2. ``POST /…/globals/topology/operations/applyonthefly`` with body
     ``{"arg1": "/…/globals/topology"}``.
  3. ``GET /…/globals/topology`` poll until
     ``applyOnTheFlyState == "nothingToApply"``.

Step 2 is the mandatory bit — every May-3 lever skipped it. This
module exposes:

- ``ixia_route_action`` — advertise / withdraw selected lines under a
  NetworkGroup. Reads the current ``Active`` valueList, mutates only
  the targeted indices, writes back the full vector, then runs steps
  1 + 2 + 3.
- ``ixia_route_apply_pending`` — just runs step 2 + 3, for callers
  who staged several edits with ``apply=False``.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Union

from ixia.models import (
    IxiaError,
    IxiaNotFoundError,
    IxiaOperationError,
)

from ixia_core.envelope import make_envelope, error_envelope
from ixia_core.session import (
    DEFAULT_PORT, DEFAULT_USER,
    get_session, write_lock, session_id_of,
)
from ixia_tools._ngpf_lookup import (
    ROUTE_PROPERTY_ATTRS,
    POOL_ATTRS,
    resolve_topology,
    resolve_device_group,
    resolve_network_group,
    resolve_pool,
    resolve_route_property,
)


# Wire-format strings IxNetwork's valueList endpoint expects for the
# ``Active`` bool multivalue. Python ``True``/``False`` would JSON-
# serialise to ``true``/``false`` but mixing bool and str on the wire
# is fragile — emit the lower-case string form everywhere.
def _bool_to_mv(v: bool) -> str:
    return "true" if bool(v) else "false"


def _mv_to_bool(v: Any) -> bool:
    """Coerce a multivalue cell (``"true"``, ``"false"``, ``True``,
    ``False``, ``None``) back to a Python bool. Default-True matches
    IxNetwork's documented default for the ``Active`` flag — an
    untouched route range advertises."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return True


def _read_active_mv(rp, ixn) -> Dict[str, Any]:
    """Return the ``{href, pattern, count, values}`` dict for the
    route-property's ``Active`` multivalue.

    RestPy's ``rp.Active.href`` raises
    ``"Multivalue object has no encapsulated resources"`` until the
    multivalue has been materialised by a prior ``.Values`` /
    ``.Pattern`` access — which is too brittle for our entry path.
    Instead, read the parent route-property body via raw REST and pull
    the ``active`` field, which IxNetwork serialises as the multivalue
    href string. Then GET the multivalue body for ``pattern`` and
    ``values``, the same shape ``ixia_get_network_group`` consumes.
    """
    rp_href = getattr(rp, "href", None) or getattr(rp, "Href", None)
    if not rp_href:
        raise IxiaOperationError("route-property has no href")
    rp_body = ixn._connection._read(rp_href)
    if not isinstance(rp_body, dict):
        raise IxiaOperationError(
            f"Unexpected route-property body shape for {rp_href}: "
            f"{type(rp_body).__name__}"
        )
    mv_href = rp_body.get("active")
    if not isinstance(mv_href, str) or not mv_href:
        raise IxiaOperationError(
            f"Route property {rp_href} body has no string 'active' "
            f"multivalue href (got {mv_href!r})"
        )
    body = ixn._connection._read(mv_href)
    if not isinstance(body, dict):
        raise IxiaOperationError(
            f"Unexpected multivalue body shape for {mv_href}: "
            f"{type(body).__name__}"
        )
    return {
        "href": mv_href,
        "pattern": body.get("pattern"),
        "count": int(body.get("count") or 0),
        "values": list(body.get("values") or []),
    }


def _expand_active_to_lines(mv: Dict[str, Any], multiplier: int) -> List[bool]:
    """Turn the raw ``Active`` multivalue body into a length-``multiplier``
    bool vector regardless of pattern.

    - ``singleValue``: one entry → broadcast across every line.
    - ``valueList``: one entry per line → take as-is, pad if short.
    - any other pattern (counter, etc.) is unusual on ``Active`` but we
      best-effort broadcast / pad with the documented default (True).
    """
    raw = mv.get("values") or []
    pattern = (mv.get("pattern") or "").lower()
    default = True
    if pattern == "singlevalue" or len(raw) == 1:
        return [_mv_to_bool(raw[0]) if raw else default] * multiplier
    out = [_mv_to_bool(v) for v in raw[:multiplier]]
    if len(out) < multiplier:
        out.extend([default] * (multiplier - len(out)))
    return out


def _resolve_lines(
    lines: Union[str, int, List[int]], multiplier: int,
) -> List[int]:
    """Normalise the ``lines`` argument to a sorted list of unique
    1-based indices in ``[1, multiplier]``.

    Raises ``ValueError`` with a caller-facing message on bad input
    (handled by ``ixia_route_action`` and converted to a
    ``status='bad_argument'`` envelope).
    """
    if isinstance(lines, str):
        if lines.strip().lower() == "all":
            return list(range(1, multiplier + 1))
        raise ValueError(
            f"lines must be 'all', a 1-based int, or a list of "
            f"1-based ints — got string {lines!r}."
        )
    # Reject bool here: bool is an int subclass in Python and `lines=True`
    # would silently map to line 1. Almost certainly a caller mistake.
    if isinstance(lines, bool):
        raise ValueError(
            f"lines={lines!r} (bool) is not a valid line selector. "
            f"Use 'all', a 1-based int, or a list of ints."
        )
    if isinstance(lines, int):
        if lines < 1 or lines > multiplier:
            raise ValueError(
                f"line {lines} out of range — NetworkGroup has "
                f"multiplier={multiplier} (valid 1..{multiplier})."
            )
        return [lines]
    if isinstance(lines, list):
        if not lines:
            raise ValueError(
                "lines=[] selects nothing. Pass 'all', an int, or a "
                "non-empty list of 1-based ints."
            )
        if any(isinstance(x, bool) or not isinstance(x, int) for x in lines):
            raise ValueError(
                "lines list entries must all be ints (got "
                f"{[type(x).__name__ for x in lines]})."
            )
        bad = [n for n in lines if n < 1 or n > multiplier]
        if bad:
            raise ValueError(
                f"line(s) {bad} out of range — NetworkGroup has "
                f"multiplier={multiplier} (valid 1..{multiplier})."
            )
        return sorted(set(lines))
    raise ValueError(
        f"lines must be 'all', a 1-based int, or a list of ints — got "
        f"{type(lines).__name__}."
    )


def _write_value_list(
    ixn, mv_href: str, values: List[str], *, current_pattern: str,
) -> Any:
    """Write the per-line ``values`` vector to ``{mv_href}/valueList``.

    The valueList child is a singleton resource — its existence depends
    on the parent multivalue's ``pattern``:

    - ``pattern == "singleValue"`` (or anything other than valueList):
      the child doesn't exist yet. POST creates it and atomically flips
      the parent's ``pattern`` to ``valueList``. POSTing when the child
      already exists raises ``NullReferenceException`` from
      ``SDMObject.CreateChild`` because IxNetwork tries to add a second
      singleton.
    - ``pattern == "valueList"``: the child exists. PATCH updates its
      ``values`` array in place. POST in this state is a hard error.

    RestPy's typed ``mv_obj.ValueList(values)`` papers over this on
    happy paths but has hit cases where the auto-pattern flip is
    dropped silently — direct REST is what we verified end-to-end
    on ``bgp-lu-stale-bug.ixncfg``.
    """
    target = mv_href.rstrip("/") + "/valueList"
    payload = {"values": values}
    if (current_pattern or "").lower() == "valuelist":
        return ixn._connection._update(target, payload)
    return ixn._connection._create(target, payload)


def _apply_on_the_fly(ixn) -> str:
    """``POST /…/globals/topology/operations/applyonthefly`` and return
    the topology root href so the caller can poll
    ``applyOnTheFlyState``."""
    topo_root = "/api/v1/sessions/" + str(_session_id(ixn)) + "/ixnetwork/globals/topology"
    op = topo_root + "/operations/applyonthefly"
    ixn._connection._create(op, {"arg1": topo_root})
    return topo_root


def apply_changes(ixn, *, timeout_s: int = 60) -> Dict[str, Any]:
    """Push every pending NGPF config edit to the test engine.

    Implementation: ``POST /…/globals/topology/operations/applyonthefly``
    + poll ``applyOnTheFlyState`` until ``"nothingToApply"`` (or the
    deadline). Despite the REST operation being named ``applyonthefly``,
    this is the same operation IxNetwork's GUI exposes as the **Apply
    Changes** button — the OPTIONS schema for ``applyOnTheFlyState``
    even describes it as "Checks whether the apply changes operation is
    allowed". On IxNetwork 10.x there is no separate ``applychanges``
    operation: the two GUI buttons funnel into this one REST call.

    Required before ``Topology.Start`` / ``DeviceGroup.Start`` /
    ``StartAllProtocols`` whenever there are pending NGPF edits — the
    Start otherwise silently no-ops on the affected DGs (status stays
    ``notStarted``, ``elapsed_s ≈ 0.03``). Safe to call when nothing is
    pending: the pre-check below skips the round-trip.

    Does NOT flap running protocols on unrelated DGs — that's the whole
    point of the on-the-fly variant; the test engine only re-pushes
    objects that actually changed.

    Returns:
      dict ``{applied, skipped, state, elapsed_s, polls}``
        - applied: True iff the operation succeeded (or there was
          nothing to apply).
        - skipped: True iff the pre-check found
          ``applyOnTheFlyState in {"nothingToApply", "notAllowed"}``
          and the POST was not issued.
        - state: last observed ``applyOnTheFlyState``.
        - elapsed_s: total wall time for the helper call.
        - polls: number of GET polls taken to reach steady state
          (0 when skipped or when the POST itself reported success
          before the first poll).
    """
    t0 = time.time()
    topo_root = (
        "/api/v1/sessions/" + str(_session_id(ixn))
        + "/ixnetwork/globals/topology"
    )
    pre_state = ""
    try:
        body = ixn._connection._read(topo_root)
        if isinstance(body, dict):
            pre_state = str(body.get("applyOnTheFlyState") or "")
    except Exception:
        pass
    # nothingToApply → no edits pending; notAllowed → another op in
    # flight (e.g. ApplyOnTheFly already running on another thread).
    # Either way, posting again would be wasteful or rejected.
    if pre_state in ("nothingToApply", "notAllowed"):
        return {
            "applied": pre_state == "nothingToApply",
            "skipped": True,
            "state": pre_state,
            "elapsed_s": round(time.time() - t0, 2),
            "polls": 0,
        }
    _apply_on_the_fly(ixn)
    wait = _wait_apply_on_the_fly(
        ixn, topo_root=topo_root,
        deadline=time.time() + max(1, int(timeout_s)),
    )
    return {
        "applied": wait.get("state") == "nothingToApply",
        "skipped": False,
        "state": wait.get("state"),
        "elapsed_s": round(time.time() - t0, 2),
        "polls": wait.get("polls"),
    }


def _session_id(ixn) -> int:
    """Pull session id out of ``ixn.href`` (e.g. ``/api/v1/sessions/1/ixnetwork/``)."""
    href = getattr(ixn, "href", "") or ""
    for part in href.strip("/").split("/"):
        if part.isdigit():
            return int(part)
    raise IxiaOperationError(f"Cannot derive session id from ixn.href={href!r}")


def _wait_apply_on_the_fly(
    ixn, *, topo_root: str, deadline: float, poll_s: float = 0.5,
) -> Dict[str, Any]:
    """Poll ``GET {topo_root}`` until ``applyOnTheFlyState=='nothingToApply'``
    or ``deadline`` expires.

    Returns ``{state, elapsed_s, polls}``. The state field is whatever
    IxNetwork reported on the last poll — useful for distinguishing
    ``"nothingToApply"`` from ``"applying"`` (still in flight) or
    ``"errorOccurred"`` (caller should investigate).
    """
    t0 = time.time()
    polls = 0
    state = ""
    while True:
        polls += 1
        body = ixn._connection._read(topo_root)
        state = str(body.get("applyOnTheFlyState") if isinstance(body, dict) else "")
        if state == "nothingToApply":
            return {"state": state, "elapsed_s": round(time.time() - t0, 2),
                    "polls": polls}
        if time.time() >= deadline:
            return {"state": state, "elapsed_s": round(time.time() - t0, 2),
                    "polls": polls, "timed_out": True}
        time.sleep(max(0.05, poll_s))


_ACTION_TO_BOOL = {"advertise": True, "withdraw": False}


def ixia_route_action(
    host: str,
    topology: str,
    network_group: str,
    action: str,
    lines: Union[str, int, List[int]] = "all",
    device_group: Union[str, int] = 1,
    pool_index: int = 1,
    family: str = "ipv4",
    route_property: str = "bgpIPRouteProperty",
    apply: bool = True,
    apply_timeout_s: int = 30,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Advertise or withdraw selected lines of a NetworkGroup route range.

    One action-named lever for the per-line ``Active`` flag. Reads the
    current ``Active`` valueList, mutates only the targeted lines,
    writes back the full vector, and (with ``apply=True``) pulses
    ``ApplyOnTheFly`` so the change actually reaches the wire.

    Args:
        topology: Exact topology name (e.g. ``"CL"``).
        network_group: Exact NG name (e.g. ``"CL-NG-LU"``). Looked up
            under the chosen device group.
        action: ``"advertise"`` (set selected lines' ``Active`` to True)
            or ``"withdraw"`` (set them to False). Required.
        lines: Which 1-based line indices under the NG to flip.
            - ``"all"`` (default) → every line ``[1..NG.multiplier]``.
            - ``int N`` → just line ``N``.
            - ``list[int]`` → those specific indices.
            Lines NOT named here are preserved at their current value
            (the tool reads the existing valueList first and re-writes
            the full vector).
        device_group: DG name or 1-based index inside ``topology``
            (default ``1`` — first DG).
        pool_index: 1-based prefix-pool index under the NG (default 1
            — most NGs only have one pool).
        family: ``"ipv4"`` or ``"ipv6"`` — picks ``Ipv4PrefixPools`` vs
            ``Ipv6PrefixPools``.
        route_property: One of ``ROUTE_PROPERTY_ATTRS`` keys (default
            ``"bgpIPRouteProperty"``: covers plain IPv4 unicast and LU
            RFC 8277). Use ``"bgpL3VpnRouteProperty"`` for VPNv4.
        apply: When ``True`` (default), runs ``ApplyOnTheFly`` and
            polls until ``applyOnTheFlyState=='nothingToApply'``. When
            ``False``, only stages the multivalue change — call
            ``ixia_route_apply_pending`` to push it.
        apply_timeout_s: Seconds to wait for ``ApplyOnTheFly`` to
            drain. 30 s is comfortably above what we've seen on
            ``bgp-lu-stale-bug.ixncfg`` (~2 s).

    Returns envelope with ``result = {action, multivalue, multiplier,
    targeted_lines, previous, new, changed_lines, applied,
    applyOnTheFlyState, apply_elapsed_s, apply_polls}``.

    On wire (verified 2026-05-05): with ``action='withdraw'`` against
    one line of ``CL-NG-LU`` the DUT prefix count drops by that line's
    instance count within ~1 s and a BGP UPDATE-withdraw is observed.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "device_group": device_group,
        "network_group": network_group, "pool_index": pool_index,
        "family": family, "route_property": route_property,
        "action": action, "lines": lines,
        "apply": apply, "apply_timeout_s": apply_timeout_s,
    }

    if action not in _ACTION_TO_BOOL:
        return error_envelope(
            f"action must be one of {sorted(_ACTION_TO_BOOL)}, got {action!r}.",
            kind="route_action", host=host, port=port,
            status="bad_argument",
        )
    if family not in POOL_ATTRS:
        return error_envelope(
            f"Unknown family {family!r}. Valid: {sorted(POOL_ATTRS)}.",
            kind="route_action", host=host, port=port,
            status="bad_argument",
        )
    if route_property not in ROUTE_PROPERTY_ATTRS:
        return error_envelope(
            f"Unknown route_property {route_property!r}. "
            f"Valid: {sorted(ROUTE_PROPERTY_ATTRS)}.",
            kind="route_action", host=host, port=port,
            status="bad_argument",
        )

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="route_action",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="route_action", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    target_bool = _ACTION_TO_BOOL[action]

    try:
        ixn = s.ixn
        with write_lock(host, port, user):
            topo = resolve_topology(ixn, topology)
            dg = resolve_device_group(topo, device_group)
            ng = resolve_network_group(dg, network_group)
            mult = int(getattr(ng, "Multiplier", 1) or 1)

            try:
                target_lines = _resolve_lines(lines, mult)
            except ValueError as ve:
                env["status"] = "error"
                env["errors"].append(str(ve))
                env["next_actions"].append(
                    "Call ixia_get_network_group to see the NG's "
                    "multiplier and current per-line Active mask."
                )
                return env

            pool = resolve_pool(ng, family=family, index=pool_index)
            rp = resolve_route_property(pool, kind=route_property)

            mv = _read_active_mv(rp, ixn)
            previous_bools = _expand_active_to_lines(mv, mult)
            new_bools = list(previous_bools)
            for n in target_lines:
                new_bools[n - 1] = target_bool

            changed_lines = [
                n for n in range(1, mult + 1)
                if new_bools[n - 1] != previous_bools[n - 1]
            ]

            previous_wire = [_bool_to_mv(b) for b in previous_bools]
            new_wire = [_bool_to_mv(b) for b in new_bools]

            _write_value_list(
                ixn, mv["href"], new_wire,
                current_pattern=str(mv.get("pattern") or ""),
            )

            if not apply:
                env["result"] = {
                    "action": action,
                    "multivalue": mv["href"],
                    "multiplier": mult,
                    "targeted_lines": target_lines,
                    "previous": previous_wire,
                    "new": new_wire,
                    "changed_lines": changed_lines,
                    "applied": False,
                    "applyOnTheFlyState": None,
                }
                env["next_actions"].append(
                    "Call ixia_route_apply_pending() to push the staged "
                    "change to the wire (mandatory — multivalue PATCH on "
                    "a running topology is passive until ApplyOnTheFly)."
                )
                return env

            topo_root = _apply_on_the_fly(ixn)
            wait = _wait_apply_on_the_fly(
                ixn, topo_root=topo_root,
                deadline=time.time() + max(1, int(apply_timeout_s)),
            )

        applied = wait.get("state") == "nothingToApply"
        if not applied:
            env["status"] = "warning"
            env["warnings"].append(
                f"ApplyOnTheFly did not reach 'nothingToApply' within "
                f"{apply_timeout_s}s — last state: {wait.get('state')!r} "
                f"(polls={wait.get('polls')}). The multivalue write "
                "succeeded; verify on the wire with cli-mcp / "
                "ixia_get_network_group."
            )
        env["result"] = {
            "action": action,
            "multivalue": mv["href"],
            "multiplier": mult,
            "targeted_lines": target_lines,
            "previous": previous_wire,
            "new": new_wire,
            "changed_lines": changed_lines,
            "applied": applied,
            "applyOnTheFlyState": wait.get("state"),
            "apply_elapsed_s": wait.get("elapsed_s"),
            "apply_polls": wait.get("polls"),
        }
        return env
    except IxiaNotFoundError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        env["next_actions"].append(
            "Call ixia_get_network_group / ixia_describe_session to "
            "see what topology / DG / NG names exist."
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


def ixia_route_apply_pending(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    timeout_s: int = 30,
) -> Dict[str, Any]:
    """Push every pending NGPF config edit onto the wire.

    Wraps the same ``operations/applyonthefly`` POST that
    ``ixia_route_action`` runs internally, but as a standalone call —
    useful when several actions or other multivalue edits were staged
    with ``apply=False`` and the caller wants to ship them in one
    pulse.

    Returns envelope with
    ``result = {applied, applyOnTheFlyState, elapsed_s, polls}``.
    """
    request = {"host": host, "port": port, "user": user,
               "timeout_s": timeout_s}
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="route_apply_pending",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="route_apply_pending", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ixn = s.ixn
        with write_lock(host, port, user):
            topo_root = _apply_on_the_fly(ixn)
            wait = _wait_apply_on_the_fly(
                ixn, topo_root=topo_root,
                deadline=time.time() + max(1, int(timeout_s)),
            )
        applied = wait.get("state") == "nothingToApply"
        if not applied:
            env["status"] = "warning"
            env["warnings"].append(
                f"ApplyOnTheFly did not reach 'nothingToApply' within "
                f"{timeout_s}s — last state: {wait.get('state')!r}."
            )
        env["result"] = {
            "applied": applied,
            "applyOnTheFlyState": wait.get("state"),
            "elapsed_s": wait.get("elapsed_s"),
            "polls": wait.get("polls"),
        }
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def register(mcp) -> None:
    mcp.tool()(ixia_route_action)
    mcp.tool()(ixia_route_apply_pending)


__all__ = [
    "ixia_route_action",
    "ixia_route_apply_pending",
    "apply_changes",
    "register",
]
