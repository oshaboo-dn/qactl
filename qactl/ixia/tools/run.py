"""Run-time control tools: protocols + traffic apply / start / stop / stats.

Mutating operations — take the per-session write lock so two concurrent
MCP calls don't trample each other. These are **routine operations**
(start/stop traffic during a test run), not destructive config edits,
so they do NOT require ``confirm=True``. Only config-surface-changing
tools (delete topology, new/load/save_config) carry confirmation gates.

The three Start tools (``ixia_dg_start``, ``ixia_topology_start``,
``ixia_protocols_start_all``) run two preflights before the actual
Start: a chassis vport-readiness wait (default 60 s, polled every
10 s) and an Apply Changes pulse (the same
``operations/applyonthefly`` POST IxNetwork's GUI calls "Apply
Changes" — auto-skipped when ``applyOnTheFlyState`` is already
``nothingToApply``). Both are gated behind explicit flags
(``force=True`` skips vport wait, ``apply_changes=False`` skips the
apply) for callers that want bare-Start semantics.

Stat-view reads (``protocols_summary``, ``get_traffic_stats``) wrap the
IxNetwork ``StatViewAssistant`` call in a bounded worker thread — the
default 180 s existence-wait is a footgun for MCP latency.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict
from typing import Any, Dict, Optional

from qactl.ixia.client.models import IxiaError, IxiaOperationError

from qactl.ixia.core.envelope import make_envelope, error_envelope
from qactl.ixia.core.session import (
    DEFAULT_PORT, DEFAULT_USER, STAT_VIEW_WAIT_SECONDS,
    get_session, write_lock, session_id_of,
)
from qactl.ixia.tools._vport_wait import (
    NOT_READY_ERROR_MARKER,
    filter_vports,
    stuck_vport_summary,
    vport_state_snapshot,
    vports_not_ready,
    wait_for_vports_ready,
)
from qactl.ixia.tools.routes import apply_changes as _apply_changes


# Default vport-readiness preflight wait for the start tools.
# Matches the load_config default — same chassis bring-up window
# (~30-60 s after a load); polled every 10 s.
DEFAULT_START_WAIT_MS = 60_000

# Default deadline for the auto-applyChanges call wired into the
# start tools and ``ixia_apply_changes``. The POST itself blocks
# until the test engine reports SUCCESS (we've seen 25-30 s on a
# session with ~60 NGPF objects); the poll afterwards usually
# returns ``nothingToApply`` immediately. 60 s is comfortable for
# the session sizes this lab actually runs.
DEFAULT_APPLY_CHANGES_TIMEOUT_S = 60


def _run_apply_changes(env: Dict[str, Any], s, *, timeout_s: int) -> bool:
    """Wrap :func:`qactl.ixia.tools.routes.apply_changes` for the start tools.

    Attaches the result dict to ``env["result"]["apply_changes"]`` so
    callers can see whether anything was actually pushed and how long
    it took. On a hard failure (state ended somewhere other than
    ``nothingToApply`` after the deadline) the envelope is left at
    ``status="warning"`` and the caller still proceeds to Start —
    historically the Start would then surface the real "Cannot start
    X. Please use Apply Changes first" error from IxNetwork, which
    is more actionable than a generic apply-timeout.

    Returns True so the caller chains naturally (mirrors the shape of
    :func:`_preflight_vport_wait`).
    """
    try:
        ac = _apply_changes(s.ixn, timeout_s=int(timeout_s))
    except Exception as e:
        env.setdefault("warnings", []).append(
            f"Auto Apply Changes failed: {type(e).__name__}: {str(e)[:200]}. "
            "Continuing to Start anyway — IxNetwork will surface the "
            "real reason if Start can't proceed."
        )
        return True
    result = env.get("result") or {}
    result["apply_changes"] = ac
    env["result"] = result
    if not ac.get("applied") and not ac.get("skipped"):
        env.setdefault("warnings", []).append(
            f"Apply Changes did not reach 'nothingToApply' within "
            f"{timeout_s}s — last state: {ac.get('state')!r}. "
            "Continuing to Start; if Start no-ops, retry "
            "`qactl ixia session apply` with a larger --timeout."
        )
    return True


def _enrich_vport_not_ready(env: Dict[str, Any], s, exc: BaseException) -> bool:
    """If ``exc`` looks like the IxNetwork vports-not-ready error, attach
    a vport-state diagnosis to ``env`` and return True.

    The IxNetwork-side message ``"No IP Address for Parent found!"`` is
    cryptic — it really means "the protocol stack couldn't find an IPv4
    layer because the underlying vport isn't connectedLinkUp yet".
    Re-reading vport state at the moment of failure lets us swap that
    for a precise "vport X is in state Y" diagnosis.

    Returns True if the error was recognised and ``env`` was enriched;
    False otherwise (caller should leave the original error in place).
    """
    if NOT_READY_ERROR_MARKER not in str(exc):
        return False
    try:
        snapshot = vport_state_snapshot(s)
    except Exception:
        return False
    stuck = stuck_vport_summary(snapshot)
    if not stuck:
        # The error matched the marker but every vport is actually
        # ready — leave the original error unenriched; this is a
        # different bug.
        return False
    env["status"] = "error"
    env["errors"].append(
        f"vports_not_ready: {len(stuck)} of {len(snapshot)} vport(s) are "
        f"not connectedLinkUp+up: {stuck}. IxNetwork raised "
        f"'{NOT_READY_ERROR_MARKER}' because the protocol stack has no "
        "physical link below it yet."
    )
    env["next_actions"].append(
        "Wait for chassis ports to finish booting "
        "(typically 30-60 s after `qactl ixia session load`), then retry. "
        "`qactl ixia session load` blocks for vport readiness by default — "
        "if you skipped that wait (--wait-for-vports-ms 0) re-poll "
        "`qactl ixia session vports` until every vport is connectedLinkUp+up."
    )
    env_result = env.get("result") or {}
    env_result["vports_ready"] = False
    env_result["vports_stuck"] = stuck
    env["result"] = env_result
    return True


def _preflight_vport_wait(
    env: Dict[str, Any],
    s,
    *,
    wait_ms: int,
    force: bool,
    only_hrefs: Optional[list] = None,
    only_names: Optional[list] = None,
) -> bool:
    """Run the chassis-bring-up wait before a Start op.

    On success: attaches ``vport_wait`` to ``env["result"]`` (creating
    the dict if needed) and returns ``True`` so the caller continues
    with the actual Start. On timeout: flips ``env["status"]`` to
    ``"error"``, populates ``errors`` + ``next_actions``, and returns
    ``False`` so the caller bails before issuing the doomed Start.

    When ``force=True`` or ``wait_ms<=0`` the wait is skipped and the
    function returns ``True`` immediately.
    """
    if force or wait_ms <= 0:
        return True
    try:
        ready, snapshot, elapsed_s = wait_for_vports_ready(
            s, timeout_s=wait_ms / 1000.0,
            only_hrefs=only_hrefs, only_names=only_names,
        )
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(
            f"Vport-readiness preflight failed: "
            f"{type(e).__name__}: {str(e)[:200]}"
        )
        return False
    result = env.get("result") or {}
    result["vport_wait"] = {
        "ready": ready,
        "elapsed_s": elapsed_s,
        "vports": snapshot,
    }
    env["result"] = result
    if ready:
        return True
    stuck = stuck_vport_summary(snapshot)
    env["status"] = "error"
    env["errors"].append(
        f"vports_not_ready: {len(stuck)} of {len(snapshot)} vport(s) are "
        f"not connectedLinkUp+up after {wait_ms} ms preflight wait: "
        f"{stuck}. Skipping Start (would either silently no-op or raise "
        f"'{NOT_READY_ERROR_MARKER}')."
    )
    env["next_actions"].append(
        "Either bump --wait-for-vports-ready-ms (chassis "
        "may need >60 s after `qactl ixia session load`), or run "
        "`qactl ixia session wait-vports` / `qactl ixia session vports` "
        "first to confirm. Pass --force to skip the preflight when you "
        "really know the vports are up."
    )
    return False


def ixia_wait_vports_ready(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    timeout_ms: int = DEFAULT_START_WAIT_MS,
    only_vport_names: Optional[list] = None,
    only_vport_hrefs: Optional[list] = None,
) -> Dict[str, Any]:
    """Block until every (filtered) vport is connectedLinkUp + up.

    Standalone version of the preflight wait baked into
    ``ixia_protocols_start_all`` / ``ixia_topology_start``. Useful when
    the caller wants explicit control — e.g. wait between
    ``ixia_load_config(wait_for_vports_ms=0)`` and a custom orchestration
    step.

    Args:
        timeout_ms: Hard deadline (default 60 000 ms; polled every 10 s
            — first check at t≈10 s, last at t≈60 s).
        only_vport_names / only_vport_hrefs: Optional whitelist filters.
            When both are ``None`` waits on every assigned vport on the
            session.

    Returns envelope with ``result = {ready, elapsed_s, vports}``;
    ``status`` is ``"warning"`` on timeout (load succeeded, just slow).
    """
    request = {
        "host": host, "port": port, "user": user,
        "timeout_ms": timeout_ms,
        "only_vport_names": list(only_vport_names or []) or None,
        "only_vport_hrefs": list(only_vport_hrefs or []) or None,
    }
    if not isinstance(timeout_ms, int) or timeout_ms < 0:
        return error_envelope(
            "timeout_ms must be a non-negative integer (milliseconds).",
            kind="wait_vports_ready", host=host, port=port,
            status="bad_argument",
        )
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="wait_vports_ready",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="wait_vports_ready", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ready, snapshot, elapsed_s = wait_for_vports_ready(
            s, timeout_s=timeout_ms / 1000.0,
            only_hrefs=only_vport_hrefs, only_names=only_vport_names,
        )
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env

    env["result"] = {
        "ready": ready,
        "elapsed_s": elapsed_s,
        "vports": snapshot,
    }
    if not ready:
        env["status"] = "warning"
        env["warnings"].append(
            f"{len(stuck_vport_summary(snapshot))} of {len(snapshot)} "
            f"vport(s) did not reach connectedLinkUp+up within "
            f"{timeout_ms} ms."
        )
    return env


def _run_bounded(fn, *, timeout: float):
    """Run ``fn()`` in a worker thread, return (done, value_or_exc).

    If ``done`` is False we've hit the timeout; the worker keeps running
    in the background (we don't cancel IxNetwork requests mid-flight —
    RestPy doesn't expose a clean cancel).
    """
    holder: Dict[str, Any] = {}

    def _target():
        try:
            holder["value"] = fn()
        except BaseException as e:
            holder["exc"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return False, None
    if "exc" in holder:
        return True, holder["exc"]
    return True, holder.get("value")


# ----------------------------------------------------------------- protocols

def ixia_protocols_start_all(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    sync: bool = True,
    wait_for_vports_ready_ms: int = DEFAULT_START_WAIT_MS,
    apply_changes: bool = True,
    apply_changes_timeout_s: int = DEFAULT_APPLY_CHANGES_TIMEOUT_S,
    force: bool = False,
) -> Dict[str, Any]:
    """Start every protocol in the current session (BGP, OSPF, IPv4, …).

    Args:
        sync: If True (default), IxNetwork blocks until protocols are
            brought up or fail. If False, returns immediately and the
            caller should poll with ``ixia_protocols_summary``.
        wait_for_vports_ready_ms: Pre-flight chassis-readiness wait
            (default 60 000 ms; polled every 10 s). The actual Start
            only runs after every assigned vport reaches
            ``connectedLinkUp + up``. This eliminates the silent no-op
            and the ``"No IP Address for Parent found!"`` misdirection
            seen when starting against a port still in ``Rebooting``.
        apply_changes: When True (default), auto-runs IxNetwork's
            **Apply Changes** (the ``operations/applyonthefly`` POST,
            same one as :func:`ixia_apply_changes`) just before the
            Start. Without this, a Start issued while NGPF edits are
            pending silently no-ops on the affected DGs (status stays
            ``notStarted``, ``elapsed_s ≈ 0.03 s``); IxNetwork only
            surfaces the real "Cannot start X. Please use Apply
            Changes first" message on a subsequent ``start_all``.
            Skipped when ``apply_changes_state == "nothingToApply"``
            so the cost is negligible on hot paths.
        apply_changes_timeout_s: Deadline for the auto Apply Changes
            (default 60 s — covers ~60 NGPF objects on this lab).
        force: When True, skips the preflight wait and Starts whatever
            state the chassis is currently in. Use only when you've
            already confirmed readiness another way.

    Wall-clock: expect 15-60 s on a loaded config like bgp-leak. First
    call after a fresh load is the slowest (chassis port bring-up +
    ARP + BGP session negotiation). Blocking ``sync=True`` may exceed
    the MCP streaming keepalive — use ``sync=False`` if the caller
    wants to watch progress.
    """
    request = {
        "host": host, "port": port, "user": user, "sync": sync,
        "wait_for_vports_ready_ms": wait_for_vports_ready_ms,
        "apply_changes": apply_changes,
        "apply_changes_timeout_s": apply_changes_timeout_s,
        "force": force,
    }
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="protocols_start_all",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="protocols_start_all", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    if not _preflight_vport_wait(
        env, s, wait_ms=int(wait_for_vports_ready_ms), force=bool(force),
    ):
        return env

    if apply_changes:
        _run_apply_changes(
            env, s, timeout_s=int(apply_changes_timeout_s),
        )

    t0 = time.time()
    try:
        with write_lock(host, port, user):
            s.protocols.start_all(sync=sync)
        result = env.get("result") or {}
        result.update({
            "sync": sync,
            "elapsed_s": round(time.time() - t0, 2),
        })
        env["result"] = result
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        _enrich_vport_not_ready(env, s, e)
        return env


def ixia_topology_start(
    host: str,
    name: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    wait_for_vports_ready_ms: int = DEFAULT_START_WAIT_MS,
    apply_changes: bool = True,
    apply_changes_timeout_s: int = DEFAULT_APPLY_CHANGES_TIMEOUT_S,
    force: bool = False,
) -> Dict[str, Any]:
    """Start protocols on a single topology by exact name.

    Prefer this over ``ixia_protocols_start_all`` — ``Start`` on a
    specific topology only brings up that topology's DGs / ethernet /
    IPv4 / BGP stack. Safe to call when the session may hold other
    topologies you must not touch.

    Args:
        wait_for_vports_ready_ms: Pre-flight wait restricted to this
            topology's vports (default 60 000 ms; polled every 10 s).
            On a freshly-loaded config calling Start while a vport is
            still ``Rebooting`` is a silent no-op (``elapsed_s≈0.03``,
            protocols stay ``not_started``); the wait makes the call
            block until the chassis is ready.
        apply_changes: When True (default), auto-runs IxNetwork's
            **Apply Changes** (``operations/applyonthefly``, same as
            :func:`ixia_apply_changes`) right before the Start. Without
            this, a Start issued while NGPF edits are pending — e.g.
            you just added a DG / Ethernet / IPv4 / BGP peer — silently
            no-ops (``elapsed_s ≈ 0.03 s``, status stays
            ``notStarted``). The skip-if-nothing-pending check makes
            it cheap on hot paths.
        apply_changes_timeout_s: Deadline for the auto Apply Changes
            (default 60 s).
        force: When True, skip the preflight wait. Use only when you
            already know the chassis is up (e.g. you ran
            ``ixia_wait_vports_ready`` first).

    Blocking — returns once IxNetwork reports the topology reached a
    steady state. Typical wall time 10-40 s depending on DG count
    (plus the preflight wait).
    """
    request = {
        "host": host, "port": port, "user": user, "name": name,
        "wait_for_vports_ready_ms": wait_for_vports_ready_ms,
        "apply_changes": apply_changes,
        "apply_changes_timeout_s": apply_changes_timeout_s,
        "force": force,
    }
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="topology_start",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="topology_start", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    target = None
    target_vport_hrefs: list = []
    try:
        for tp in s.ixn.Topology.find():
            if getattr(tp, "Name", "") == name:
                target = tp
                target_vport_hrefs = list(getattr(tp, "Vports", []) or [])
                break
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(
            f"Topology lookup failed: {type(e).__name__}: {str(e)[:200]}"
        )
        return env
    if target is None:
        env["status"] = "error"
        env["errors"].append(f"Topology {name!r} not found.")
        env["next_actions"].append(
            "Run `qactl ixia topo list` to see available names."
        )
        return env

    if not _preflight_vport_wait(
        env, s, wait_ms=int(wait_for_vports_ready_ms), force=bool(force),
        only_hrefs=target_vport_hrefs or None,
    ):
        return env

    if apply_changes:
        _run_apply_changes(
            env, s, timeout_s=int(apply_changes_timeout_s),
        )

    t0 = time.time()
    try:
        with write_lock(host, port, user):
            target.Start()
        result = env.get("result") or {}
        result.update({
            "topology": name,
            "elapsed_s": round(time.time() - t0, 2),
        })
        env["result"] = result
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        _enrich_vport_not_ready(env, s, e)
        return env


def ixia_topology_stop(
    host: str,
    name: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Stop protocols on a single topology by exact name."""
    request = {"host": host, "port": port, "user": user, "name": name}
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="topology_stop",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="topology_stop", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    t0 = time.time()
    try:
        with write_lock(host, port, user):
            target = None
            for tp in s.ixn.Topology.find():
                if getattr(tp, "Name", "") == name:
                    target = tp
                    break
            if target is None:
                env["status"] = "error"
                env["errors"].append(f"Topology {name!r} not found.")
                return env
            target.Stop()
        env["result"] = {
            "topology": name,
            "elapsed_s": round(time.time() - t0, 2),
        }
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def _find_topology_dg(s, topology_name: str, dg_name: str):
    """Return ``(topology_obj, dg_obj, vport_hrefs)`` or raise LookupError.

    Looks at top-level device groups only. Sub-DGs (DGs nested inside
    another DG) are not in scope; if that becomes a real need, add a
    ``path`` arg or a tree-walk variant.
    """
    topology = None
    for tp in s.ixn.Topology.find():
        if getattr(tp, "Name", "") == topology_name:
            topology = tp
            break
    if topology is None:
        raise LookupError(f"Topology {topology_name!r} not found.")
    dg = None
    for candidate in topology.DeviceGroup.find():
        if getattr(candidate, "Name", "") == dg_name:
            dg = candidate
            break
    if dg is None:
        names = [getattr(c, "Name", "") for c in topology.DeviceGroup.find()]
        raise LookupError(
            f"Device group {dg_name!r} not found under topology "
            f"{topology_name!r}. Top-level DGs there: {names}."
        )
    vport_hrefs = list(getattr(topology, "Vports", []) or [])
    return topology, dg, vport_hrefs


def ixia_dg_start(
    host: str,
    topology: str,
    name: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    wait_for_vports_ready_ms: int = DEFAULT_START_WAIT_MS,
    apply_changes: bool = True,
    apply_changes_timeout_s: int = DEFAULT_APPLY_CHANGES_TIMEOUT_S,
    force: bool = False,
) -> Dict[str, Any]:
    """Start protocols on a single device group by exact name.

    Finer-grained sibling of ``ixia_topology_start`` — bring up just
    one DG (and the protocol stack underneath it) without touching the
    other DGs in the same topology. Useful for selective bounce: e.g.
    flap a CE-side DG to test withdraw / re-advert without restarting
    the PE side, or restart one DG after a multivalue edit.

    Args:
        topology: Exact topology name the DG lives under (DG names
            aren't globally unique — same ``CE`` can exist in ``CL``
            and ``SA``).
        name: Exact device-group name.
        wait_for_vports_ready_ms: Pre-flight wait restricted to the
            parent topology's vports (default 60 000 ms; polled every
            10 s). Same rationale as ``ixia_topology_start``: starting
            a DG while a vport is still ``Rebooting`` is a silent no-op.
        apply_changes: When True (default), auto-runs IxNetwork's
            **Apply Changes** (``operations/applyonthefly``, same as
            :func:`ixia_apply_changes`) right before the Start. This is
            the single most common cause of "DG.Start returned in 30 ms
            and ``status`` stayed ``notStarted``": the DG was just
            built (``ixia_create_device_group`` /
            ``ixia_create_ethernet`` / …) and IxNetwork rejects Start
            with "Cannot start <DG>. Please use Apply Changes first"
            until the new objects are pushed. Skipped when
            ``applyOnTheFlyState == "nothingToApply"``.
        apply_changes_timeout_s: Deadline for the auto Apply Changes
            (default 60 s).
        force: When True, skip the preflight wait. Use only when you
            already know the chassis is up.

    Top-level device groups only — sub-DGs (DGs nested inside another
    DG) are not addressable by this tool. Blocking; typical wall time
    5-30 s plus the preflight wait.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "name": name,
        "wait_for_vports_ready_ms": wait_for_vports_ready_ms,
        "apply_changes": apply_changes,
        "apply_changes_timeout_s": apply_changes_timeout_s,
        "force": force,
    }
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="dg_start",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="dg_start", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        _, dg, vport_hrefs = _find_topology_dg(s, topology, name)
    except LookupError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        env["next_actions"].append(
            "Run `qactl ixia topo list` / `qactl ixia topo get` to see "
            "the available topology + DG names."
        )
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(
            f"DG lookup failed: {type(e).__name__}: {str(e)[:200]}"
        )
        return env

    if not _preflight_vport_wait(
        env, s, wait_ms=int(wait_for_vports_ready_ms), force=bool(force),
        only_hrefs=vport_hrefs or None,
    ):
        return env

    if apply_changes:
        _run_apply_changes(
            env, s, timeout_s=int(apply_changes_timeout_s),
        )

    t0 = time.time()
    try:
        with write_lock(host, port, user):
            dg.Start()
        result = env.get("result") or {}
        result.update({
            "topology": topology,
            "device_group": name,
            "elapsed_s": round(time.time() - t0, 2),
        })
        env["result"] = result
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        _enrich_vport_not_ready(env, s, e)
        return env


def ixia_dg_stop(
    host: str,
    topology: str,
    name: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Stop protocols on a single device group by exact name.

    Finer-grained sibling of ``ixia_topology_stop``. Top-level DGs
    only — sub-DGs (DGs nested inside another DG) are not in scope.

    Args:
        topology: Exact topology name the DG lives under.
        name: Exact device-group name.
    """
    request = {
        "host": host, "port": port, "user": user,
        "topology": topology, "name": name,
    }
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="dg_stop",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="dg_stop", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    t0 = time.time()
    try:
        with write_lock(host, port, user):
            try:
                _, dg, _ = _find_topology_dg(s, topology, name)
            except LookupError as e:
                env["status"] = "error"
                env["errors"].append(str(e))
                env["next_actions"].append(
                    "Run `qactl ixia topo list` / `qactl ixia topo get` "
                    "to see the available topology + DG names."
                )
                return env
            dg.Stop()
        env["result"] = {
            "topology": topology,
            "device_group": name,
            "elapsed_s": round(time.time() - t0, 2),
        }
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_protocols_stop_all(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    sync: bool = True,
) -> Dict[str, Any]:
    """Stop every protocol in the session. Typically 5-15 s."""
    request = {"host": host, "port": port, "user": user, "sync": sync}
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="protocols_stop_all",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="protocols_stop_all", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    t0 = time.time()
    try:
        with write_lock(host, port, user):
            s.protocols.stop_all(sync=sync)
        env["result"] = {
            "sync": sync,
            "elapsed_s": round(time.time() - t0, 2),
        }
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_apply_changes(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    timeout_s: int = DEFAULT_APPLY_CHANGES_TIMEOUT_S,
) -> Dict[str, Any]:
    """Push every pending NGPF config edit to the test engine.

    Thin wrapper over :func:`qactl.ixia.tools.routes.apply_changes` — same
    operation IxNetwork's GUI exposes as the **Apply Changes** button
    (``POST /…/globals/topology/operations/applyonthefly`` under the
    hood; on IxNetwork 10.x there is no separate ``applychanges``
    operation, the two GUI buttons funnel into the same REST call).

    The three Start tools (``ixia_dg_start``, ``ixia_topology_start``,
    ``ixia_protocols_start_all``) auto-call this internally before
    Start, gated on ``apply_changes=True`` (the default). Use this
    tool when you want explicit control — e.g. you staged several
    builder calls (``ixia_create_device_group`` /
    ``ixia_create_ipv4`` / …) and want to push them in one pulse
    before Start, or you're scripting and prefer the Start tools to
    skip the implicit apply (``apply_changes=False``).

    Does NOT flap running protocols on unrelated DGs — the test
    engine only re-pushes objects that actually changed.

    Args:
        timeout_s: Deadline for the apply (default 60 s; covers
            ~60 NGPF objects on this lab — bump for very large
            sessions).

    Returns envelope with
    ``result = {applied, skipped, state, elapsed_s, polls}``:

    - ``applied``: True iff the test engine reached
      ``applyOnTheFlyState == "nothingToApply"`` (or already was there).
    - ``skipped``: True iff the pre-check found there was nothing to
      apply (or another apply was already in flight).
    - ``state``: last observed ``applyOnTheFlyState`` —
      ``"nothingToApply"`` on success.
    """
    request = {
        "host": host, "port": port, "user": user,
        "timeout_s": timeout_s,
    }
    if not isinstance(timeout_s, int) or timeout_s < 1:
        return error_envelope(
            "timeout_s must be a positive integer (seconds).",
            kind="apply_changes", host=host, port=port,
            status="bad_argument",
        )
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="apply_changes",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="apply_changes", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        with write_lock(host, port, user):
            ac = _apply_changes(s.ixn, timeout_s=int(timeout_s))
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env

    env["result"] = ac
    if not ac.get("applied") and not ac.get("skipped"):
        env["status"] = "warning"
        env["warnings"].append(
            f"Apply Changes did not reach 'nothingToApply' within "
            f"{timeout_s}s — last state: {ac.get('state')!r}. Retry "
            "with a larger timeout_s for very large sessions."
        )
    return env


def ixia_protocols_summary(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    timeout: int = STAT_VIEW_WAIT_SECONDS,
) -> Dict[str, Any]:
    """Per-protocol session counts (up / down / not-started).

    Backed by the "Protocols Summary" stat view. The view only exists
    after protocols have been started at least once; this tool bounds
    the wait to ``timeout`` seconds (default 10) instead of RestPy's
    default 180.
    """
    request = {
        "host": host, "port": port, "user": user, "timeout": timeout,
    }
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="protocols_summary",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="protocols_summary", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    done, value = _run_bounded(
        lambda: s.protocols.summary(), timeout=float(timeout),
    )
    if not done:
        env["status"] = "timeout"
        env["errors"].append(
            f"Protocols Summary view did not return within {timeout}s — "
            "either protocols are not started yet, or the view is still "
            "initialising. Try again after `qactl ixia proto start-all`."
        )
        return env
    if isinstance(value, BaseException):
        env["status"] = "error"
        env["errors"].append(f"{type(value).__name__}: {str(value)[:240]}")
        return env

    summaries = value or []
    env["result"] = {
        "count": len(summaries),
        "protocols": [asdict(x) for x in summaries],
    }
    return env


# -------------------------------------------------------------------- traffic

def ixia_traffic_apply(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Apply traffic config to the hardware.

    After ``load_config`` or any mutation to flow/endpoint config, items
    sit in ``state=unapplied`` until ``Traffic.Apply`` is called. Must
    run before ``start`` or the chassis won't have the flow groups.
    """
    request = {"host": host, "port": port, "user": user}
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="traffic_apply",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="traffic_apply", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    t0 = time.time()
    try:
        with write_lock(host, port, user):
            s.ixn.Traffic.Apply()
        env["result"] = {"elapsed_s": round(time.time() - t0, 2)}
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_traffic_generate(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Regenerate flow groups on every traffic item.

    Needed when endpoints change (e.g. after editing src/dst) so the
    per-stream config reflects the new topology. Does not apply — call
    ``ixia_traffic_apply`` afterwards.
    """
    request = {"host": host, "port": port, "user": user}
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="traffic_generate",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="traffic_generate", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        with write_lock(host, port, user):
            all_ti = s.ixn.Traffic.TrafficItem.find()
            if all_ti:
                all_ti.Generate()
                env["result"] = {"generated": True}
            else:
                env["result"] = {"generated": False}
                env["warnings"].append(
                    "No traffic items configured — nothing to generate."
                )
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_traffic_start(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """Start traffic.

    Args:
        name: Exact traffic-item name to start. If omitted, starts
            ALL traffic items in the session.

    Blocking — returns once IxNetwork confirms start. Typical wall-time
    3-10 s depending on flow group count.
    """
    request = {"host": host, "port": port, "user": user, "name": name}
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="traffic_start",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="traffic_start", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    t0 = time.time()
    try:
        with write_lock(host, port, user):
            if name:
                s.traffic(name).start()
            else:
                s.traffic.start_all()
        env["result"] = {
            "started": name or "ALL",
            "elapsed_s": round(time.time() - t0, 2),
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


def ixia_traffic_stop(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """Stop traffic.

    Args:
        name: Exact traffic-item name. If omitted, stops ALL traffic.
    """
    request = {"host": host, "port": port, "user": user, "name": name}
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="traffic_stop",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="traffic_stop", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    t0 = time.time()
    try:
        with write_lock(host, port, user):
            if name:
                s.traffic(name).stop()
            else:
                s.traffic.stop_all()
        env["result"] = {
            "stopped": name or "ALL",
            "elapsed_s": round(time.time() - t0, 2),
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


def ixia_clear_stats(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Clear all IxNetwork statistics counters."""
    request = {"host": host, "port": port, "user": user}
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="clear_stats",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="clear_stats", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        with write_lock(host, port, user):
            s.traffic.clear_stats()
        env["result"] = {"cleared": True}
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_get_traffic_stats(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    timeout: int = STAT_VIEW_WAIT_SECONDS,
) -> Dict[str, Any]:
    """Read the 'Traffic Item Statistics' view.

    Returns per-item tx/rx frame counts, rates, and loss. Bounded by
    ``timeout`` seconds (default 10) to dodge RestPy's 180 s view-wait
    when traffic hasn't started yet.
    """
    request = {
        "host": host, "port": port, "user": user, "timeout": timeout,
    }
    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="get_traffic_stats",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="get_traffic_stats", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    def _pull():
        return s.stats.all()

    done, value = _run_bounded(_pull, timeout=float(timeout))
    if not done:
        env["status"] = "timeout"
        env["errors"].append(
            f"Traffic Item Statistics view did not return within "
            f"{timeout}s — traffic may not be running yet."
        )
        return env
    if isinstance(value, BaseException):
        env["status"] = "error"
        env["errors"].append(f"{type(value).__name__}: {str(value)[:240]}")
        return env

    # StatsResult is a dataclass with .timestamp and .items (list of
    # TrafficItemStats). Flatten for the envelope.
    env["result"] = {
        "timestamp": getattr(value, "timestamp", None),
        "count": len(getattr(value, "items", []) or []),
        "items": [asdict(x) for x in (getattr(value, "items", []) or [])],
    }
    return env


def register(mcp) -> None:
    mcp.tool()(ixia_wait_vports_ready)
    mcp.tool()(ixia_apply_changes)
    mcp.tool()(ixia_topology_start)
    mcp.tool()(ixia_topology_stop)
    mcp.tool()(ixia_dg_start)
    mcp.tool()(ixia_dg_stop)
    mcp.tool()(ixia_protocols_start_all)
    mcp.tool()(ixia_protocols_stop_all)
    mcp.tool()(ixia_protocols_summary)
    mcp.tool()(ixia_traffic_apply)
    mcp.tool()(ixia_traffic_generate)
    mcp.tool()(ixia_traffic_start)
    mcp.tool()(ixia_traffic_stop)
    mcp.tool()(ixia_clear_stats)
    mcp.tool()(ixia_get_traffic_stats)
