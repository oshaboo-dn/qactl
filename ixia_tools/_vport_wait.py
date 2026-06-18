"""Vport readiness helpers — shared by ``config.py`` and ``run.py``.

When ``ixia_load_config`` returns "loaded" IxNetwork is done parsing the
``.ixncfg`` and re-applying vport assignments, but the chassis ports
themselves typically need another 30-60 s to come up:
``ConnectionState`` walks ``unconnected`` → ``connecting`` →
``connectedLinkDown`` → ``connectedLinkUp`` and ``State`` (link state)
goes ``up``. Calling ``StartAllProtocols`` before that completes
returns the cryptic IxNetwork error
``"No IP Address for Parent found!"`` — the protocol stack can't find
the IPv4 layer because the underlying physical link isn't up yet.

This module:
1. snapshots vport state in one place,
2. classifies a vport as "ready" (assigned + ConnectionState=connectedLinkUp
   + State=up) or "stuck",
3. blocks-with-timeout for all assigned vports to reach "ready",
4. provides a known marker so the run-time start tools can recognise the
   IxNetwork error and surface it as "vports not ready".
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

# Final readiness states. ``ConnectionState`` and ``State`` are the
# ground-truth fields IxNetwork exposes on a Vport (see GUI: Test
# Configuration → Ports). All other intermediates (``connecting``,
# ``connectedLinkDown``, ``unassigned``, ``assignedUnconnected``,
# ``assignedInUseByOther``) mean the port is not usable for protocol
# bring-up.
READY_CONNECTION_STATE = "connectedLinkUp"
READY_LINK_STATE = "up"

# IxNetwork emits this error string from ``StartAllProtocols`` /
# ``Topology.Start()`` when the parent IPv4 stack has no addressable
# link below it — typically because the vport is still rebooting.
# Substring match (case-sensitive — IxNetwork is consistent here).
NOT_READY_ERROR_MARKER = "No IP Address for Parent"


def vport_state_snapshot(s) -> List[Dict[str, Any]]:
    """One-shot read of every vport in the session.

    Read-only — no write lock needed. Re-reads attributes from the API
    server on each call so it is safe to use in a polling loop.
    """
    out: List[Dict[str, Any]] = []
    for v in s.ixn.Vport.find():
        out.append({
            "name": getattr(v, "Name", ""),
            "assigned_to": getattr(v, "AssignedTo", "") or None,
            "connection_state": getattr(v, "ConnectionState", ""),
            "link_state": getattr(v, "State", ""),
            "is_available": bool(getattr(v, "IsAvailable", False)),
            "href": getattr(v, "href", ""),
        })
    return out


def _is_ready(v: Dict[str, Any]) -> bool:
    """A vport without a chassis assignment is treated as ready (it
    contributes nothing to protocol bring-up). An assigned vport is
    ready only when both ``ConnectionState`` and ``State`` are at their
    final values."""
    if not v.get("assigned_to"):
        return True
    return (
        v.get("connection_state") == READY_CONNECTION_STATE
        and v.get("link_state") == READY_LINK_STATE
    )


def vports_not_ready(snapshot: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return the assigned vports from ``snapshot`` that aren't
    connectedLinkUp + up yet."""
    return [v for v in snapshot if not _is_ready(v)]


def filter_vports(
    snapshot: List[Dict[str, Any]],
    *,
    hrefs: Optional[List[str]] = None,
    names: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Subset ``snapshot`` to only vports matching ``hrefs`` or ``names``.

    When both args are ``None`` returns the full snapshot. When either
    is provided, matches are by membership (case-sensitive). Useful for
    "wait only on the vports this topology actually uses" before a
    ``Topology.Start()``.
    """
    if hrefs is None and names is None:
        return list(snapshot)
    href_set = set(hrefs or [])
    name_set = set(names or [])
    out: List[Dict[str, Any]] = []
    for v in snapshot:
        if href_set and v.get("href") in href_set:
            out.append(v)
            continue
        if name_set and v.get("name") in name_set:
            out.append(v)
    return out


def wait_for_vports_ready(
    s,
    *,
    timeout_s: float,
    poll_interval_s: float = 10.0,
    only_hrefs: Optional[List[str]] = None,
    only_names: Optional[List[str]] = None,
) -> Tuple[bool, List[Dict[str, Any]], float]:
    """Poll vport state until every assigned vport is connectedLinkUp + up.

    Args:
        timeout_s: Hard deadline in seconds.
        poll_interval_s: Seconds between polls (default 10 — chassis
            transitions are multi-second, 1 s polls just generate REST
            chatter).
        only_hrefs: When given, restrict the readiness check to vports
            whose href is in this list. Use this from
            ``ixia_topology_start`` to wait only for the topology's
            vports, not every vport on the session.
        only_names: Same as ``only_hrefs`` but matched by name.

    Returns ``(ready, snapshot, elapsed_s)``:
        ready: True if all matching assigned vports reached the final
            state, False on timeout (or if the filter matched zero
            vports — that's also "not ready" because we asked for
            specific ports and got nothing).
        snapshot: most recent **filtered** snapshot.
        elapsed_s: wall-clock seconds spent polling, rounded to 2 dp.

    Read-only. With defaults (``timeout_s=60``, ``poll_interval_s=10``)
    the polls land at t≈10, 20, 30, 40, 50, 60.
    """
    t0 = time.time()
    deadline = t0 + max(0.0, float(timeout_s))
    interval = max(0.05, float(poll_interval_s))
    filtered: List[Dict[str, Any]] = []

    while True:
        time.sleep(interval)
        full = vport_state_snapshot(s)
        filtered = filter_vports(full, hrefs=only_hrefs, names=only_names)
        if filtered and not vports_not_ready(filtered):
            return True, filtered, round(time.time() - t0, 2)
        if time.time() >= deadline:
            return False, filtered, round(time.time() - t0, 2)


def stuck_vport_summary(
    snapshot: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Compact ``[{name, connection_state, link_state}, …]`` view of the
    not-yet-ready vports, for embedding in ``warnings`` / ``next_actions``
    without dumping the full snapshot."""
    return [
        {
            "name": v.get("name", ""),
            "connection_state": v.get("connection_state", ""),
            "link_state": v.get("link_state", ""),
        }
        for v in vports_not_ready(snapshot)
    ]
