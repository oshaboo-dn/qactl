"""gNMI link-state source for the event collector.

DNOS gNMI Subscribe/Get *does* expose interface ``oper-status`` (it's in
the on-change registry), so link up/down is best learned over gNMI rather
than scraped from syslog. The collector's tick uses a bounded **Get** of

    /interfaces/interface[name=*]/state/oper-status

(one fast RPC per device) and turns it into edge events by **diffing**
the fresh snapshot against the one the previous tick stored in the spool.
The snapshot diff *is* the dedupe — an interface only produces an event
when its status actually changes — so these events deliberately skip the
syslog path's fingerprint ring (which would otherwise suppress a second
DOWN after a flap).

Everything here is pure: parsing a gNMI Get envelope and computing the
diff, no I/O. The tick supplies the live envelope and the stored snapshot.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dnctl.cli.core import events as _events

# OpenConfig keyed-wildcard path — the form DNOS accepts for Subscribe/Get
# (a bare/native subtree is rejected; see gnmi/tools/subscribe.py).
OPER_STATUS_PATH = "/interfaces/interface[name=*]/state/oper-status"

_NAME_RE = re.compile(r"\[name=([^\]]+)\]")


def _now_iso() -> str:
    """UTC timestamp matching the syslog event shape (``...Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_oper_status(envelope: Dict[str, Any]) -> Dict[str, str]:
    """Pull ``{interface_name: OPER_STATUS}`` out of a gNMI Get envelope.

    Tolerates the two shapes pygnmi yields — one notification with many
    ``update`` rows (the wildcard Get) or several notifications — and the
    value being a bare string or a ``{"oper-status": "..."}`` leaf dict.
    Returns ``{}`` if nothing parseable is present.
    """
    out: Dict[str, str] = {}
    result = envelope.get("result")
    if not isinstance(result, dict):
        return out
    for notif in result.get("notification") or []:
        if not isinstance(notif, dict):
            continue
        for upd in notif.get("update") or []:
            if not isinstance(upd, dict):
                continue
            path = upd.get("path") or ""
            m = _NAME_RE.search(path)
            if not m:
                continue
            name = m.group(1)
            val = upd.get("val")
            if isinstance(val, dict):
                val = val.get("oper-status") or val.get("state", {}).get("oper-status")
            if val is None:
                continue
            out[name] = str(val).upper()
    return out


def diff_link_states(
    device: str,
    old: Optional[Dict[str, str]],
    new: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Turn an oper-status snapshot change into structured events.

    Returns one event per interface whose status differs from ``old``.
    ``old`` of ``None``/empty means "no baseline yet" → no events (the
    caller stores ``new`` as the baseline so the *next* tick can diff).

    Event shape matches :func:`events.parse_event_line` so the same alert
    rules / fingerprint / Slack formatter apply. A transition **to** DOWN
    uses code ``OPER_STATUS_DOWN`` (in :data:`events.DEFAULT_MATCH`, so it
    alerts by default); a transition to UP uses ``OPER_STATUS_UP`` at
    ``notice`` (a recovery — surfaced but not alerted unless asked).
    """
    if not old:
        return []
    ts = _now_iso()
    out: List[Dict[str, Any]] = []
    for name, new_status in new.items():
        prev = old.get(name)
        if prev is None or prev == new_status:
            continue
        down = new_status != "UP"
        code = "OPER_STATUS_DOWN" if down else "OPER_STATUS_UP"
        severity = "warning" if down else "notice"
        msg = f"interface {name} oper-status {prev} -> {new_status}"
        out.append({
            "facility": "gnmi",
            "severity": severity,
            "severity_rank": _events.severity_rank(severity),
            "timestamp": ts,
            "host": device,
            "subsystem": "Interfaces",
            "event_code": code,
            "message": msg,
            "raw": f"{device} {code}:{msg}",
            "source": "gnmi-oper",
            "interface": name,
        })
    return out


__all__ = ["OPER_STATUS_PATH", "parse_oper_status", "diff_link_states"]
