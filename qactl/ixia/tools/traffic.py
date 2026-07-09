"""Read-only traffic-item tools.

First pass: list + glob-find + deep inspect. Mutating ops (start / stop
/ modify-rate / enable / disable / regenerate) land in a follow-up pass
— they all need the per-session write lock from ``ixia_core.session``.
"""

from __future__ import annotations

from dataclasses import asdict
from fnmatch import fnmatch
from typing import Any, Dict, Optional

from qactl.ixia.client.models import IxiaError

from qactl.ixia.core.envelope import make_envelope, error_envelope
from qactl.ixia.core.session import (
    DEFAULT_PORT, DEFAULT_USER,
    get_session, session_id_of,
)


def ixia_list_traffic_items(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    pattern: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """List configured traffic items with name / state / enabled flag.

    Args:
        host: IxNetwork API server hostname or IP.
        port: REST port (default 11009 on Windows API Server).
        user: API username — ``dn`` on the DriveNets Windows clients.
        pattern: Optional glob (``*``, ``?``) filter against item names;
            ``None`` returns every item. Matches fnmatch semantics, so
            ``"*VM-ROW*"`` and ``"INDIA-v?"`` both work.
        limit: Cap on returned items (defaults to 200; use 0 to mean
            "no limit").

    Returns envelope with ``result = {count, total, returned, items:
    [{name, state, enabled}]}``. ``total`` is the unfiltered count, so the
    agent can tell whether their pattern / limit dropped entries.
    """
    request = {
        "host": host, "port": port, "user": user,
        "pattern": pattern, "limit": limit,
    }

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}",
            kind="list_traffic_items", host=host, port=port,
            status="connect_error",
        )

    env = make_envelope(
        kind="list_traffic_items", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        summaries = s.traffic.list()
        total = len(summaries)
        if pattern:
            summaries = [x for x in summaries if fnmatch(x.name, pattern)]
        returned = summaries
        if limit and limit > 0:
            returned = summaries[:limit]
        env["result"] = {
            "total": total,
            "matched": len(summaries),
            "returned": len(returned),
            "items": [
                {"name": x.name, "state": x.state, "enabled": x.enabled}
                for x in returned
            ],
        }
        if total == 0:
            env["warnings"].append(
                "No traffic items configured in this IxNetwork session. "
                "Open the saved .ixncfg via File > Open in the API Server "
                "window, or create items via the API."
            )
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_get_traffic_item(
    host: str,
    name: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    max_streams: int = 5,
) -> Dict[str, Any]:
    """Deep-read a single traffic item by exact name.

    Returns rate, frame size, endpoints with prefix-pool resolution, and
    up to ``max_streams`` per-stream details (packet headers + resolved
    hex). Uses ``s.traffic(name).inspect()`` which already falls back to
    raw REST for the bits RestPy can't see (scalable endpoints, prefix
    pool values, ConfigElement rate/frameSize).

    Args:
        name: Exact traffic-item name. Use ``ixia_list_traffic_items`` +
            pattern to discover names first.
        max_streams: Hard cap on per-stream detail in the response.
            Defaults to 5. Streams beyond this are silently dropped to
            keep the envelope small.

    Returns envelope with ``result`` ≈ the ``TrafficItemInfo`` dataclass:
    ``{name, state, enabled, rate, frame_size, endpoints[], streams[]}``.
    """
    request = {
        "host": host, "port": port, "user": user,
        "name": name, "max_streams": max_streams,
    }

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}",
            kind="get_traffic_item", host=host, port=port,
            status="connect_error",
        )

    env = make_envelope(
        kind="get_traffic_item", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        info = s.traffic(name).inspect(max_streams=max_streams)
        # asdict() turns the whole nested dataclass tree into plain dicts.
        env["result"] = asdict(info)
        return env
    except IxiaError as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {e}")
        env["next_actions"].append(
            "Run `qactl ixia traffic list` to confirm the exact item name "
            "(names are case- and whitespace-sensitive)."
        )
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(ixia_list_traffic_items)
    mcp.tool()(ixia_get_traffic_item)
