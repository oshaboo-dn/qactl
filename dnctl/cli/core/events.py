"""Parsing + alert rules for the DNOS ``system-events`` log.

The event collector (``qactl cli monitor tick``) reads the platform's
``system-events.log`` — the same source as :func:`get_system_events` —
and needs to turn its free-text lines into structured, rankable events,
then decide which ones are worth waking a human for. That policy lives
here so it stays testable in isolation from any device / SSH / Slack.

Line shape (space-separated; see ``log_read.get_system_events``)::

    local7.warning 2026-04-14T18:43:04.189Z OHADZS-CL System - - - \
NCF_STATE_CHANGE_DISCONNECTED:NCF 0 state has changed from versioning \
to disconnected

    <facility>.<severity> <timestamp> <host> <subsystem> - - - \
<EVENT_CODE>:<human message>

Everything here is pure (no I/O), so the collector and its tests can
parse fixture lines without a device.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Sequence

# syslog severity keywords, most severe first. Lower rank = more urgent.
_SEVERITIES: Sequence[str] = (
    "emerg", "alert", "crit", "err", "warning", "notice", "info", "debug",
)
SEVERITY_RANK: Dict[str, int] = {name: i for i, name in enumerate(_SEVERITIES)}
# common aliases DNOS / syslog emit
SEVERITY_RANK.update({
    "emergency": SEVERITY_RANK["emerg"],
    "critical": SEVERITY_RANK["crit"],
    "error": SEVERITY_RANK["err"],
    "warn": SEVERITY_RANK["warning"],
})

DEFAULT_SEVERITY = "warning"
_UNKNOWN_RANK = SEVERITY_RANK["debug"] + 1

# Event-code / message substrings that are always alert-worthy regardless
# of syslog severity — DNOS logs several of these at notice/info level even
# though an operator very much wants to know (a BGP session bouncing or an
# NCF dropping out is "informational" to the platform, urgent to us).
# Matched case-insensitively against the EVENT_CODE and the message.
DEFAULT_MATCH: Sequence[str] = (
    "BGP",
    "STATE_CHANGE",
    "LINK_DOWN",
    "LINKDOWN",
    "OPER_STATUS_DOWN",
    "CRASH",
    "CORE_DUMP",
    "COREDUMP",
    "OOM",
    "OUT_OF_MEMORY",
    "REBOOT",
    "RESTART",
    "FAILOVER",
    "DISCONNECT",
    "UNREACHABLE",
)


def severity_rank(severity: Optional[str]) -> int:
    """Rank a severity keyword (lower = more urgent); unknown sorts last."""
    if not severity:
        return _UNKNOWN_RANK
    return SEVERITY_RANK.get(severity.strip().lower(), _UNKNOWN_RANK)


def parse_event_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse one ``system-events.log`` line into a structured event dict.

    Returns ``None`` for blank lines. Lines that don't match the expected
    shape are still returned with best-effort fields (``raw`` always set)
    so nothing is silently dropped — an unparseable urgent line should
    still be surfaceable.

    Keys: ``facility``, ``severity``, ``severity_rank``, ``timestamp``,
    ``host``, ``subsystem``, ``event_code``, ``message``, ``raw``.
    """
    raw = (line or "").rstrip("\n")
    if not raw.strip():
        return None

    ev: Dict[str, Any] = {
        "facility": None, "severity": None, "severity_rank": _UNKNOWN_RANK,
        "timestamp": None, "host": None, "subsystem": None,
        "event_code": None, "message": raw.strip(), "raw": raw,
    }

    tokens = raw.split()
    # token 0 = facility.severity
    if tokens:
        fac = tokens[0]
        if "." in fac:
            facility, _, sev = fac.rpartition(".")
            ev["facility"] = facility or None
            ev["severity"] = sev or None
            ev["severity_rank"] = severity_rank(sev)
    if len(tokens) >= 2:
        ev["timestamp"] = tokens[1]
    if len(tokens) >= 3:
        ev["host"] = tokens[2]
    if len(tokens) >= 4:
        ev["subsystem"] = tokens[3]

    # The human payload is everything after the "- - -" separator that DNOS
    # places between the subsystem and the EVENT_CODE:message. Fall back to
    # "after the first 4 tokens" if the separator isn't present.
    payload = ""
    if " - - - " in raw:
        payload = raw.split(" - - - ", 1)[1].strip()
    elif len(tokens) > 4:
        payload = " ".join(tokens[4:]).strip()
    if payload:
        code, sep, msg = payload.partition(":")
        if sep and code and " " not in code.strip():
            ev["event_code"] = code.strip()
            ev["message"] = msg.strip()
        else:
            ev["message"] = payload
    return ev


def is_alertworthy(
    ev: Dict[str, Any],
    *,
    max_rank: int,
    match: Sequence[str],
    exclude: Sequence[str] = (),
) -> bool:
    """Decide whether an event should fire a notification.

    Alert when the event is at least as severe as ``max_rank`` **or** its
    code/message contains any ``match`` substring (case-insensitive).
    ``exclude`` substrings veto a match regardless. ``match``/``exclude``
    are matched against ``event_code`` + ``message``.
    """
    hay = f"{ev.get('event_code') or ''} {ev.get('message') or ''}".lower()
    for ex in exclude:
        if ex and ex.lower() in hay:
            return False
    if ev.get("severity_rank", _UNKNOWN_RANK) <= max_rank:
        return True
    return any(m and m.lower() in hay for m in match)


def event_fingerprint(device: str, ev: Dict[str, Any]) -> str:
    """Stable dedupe key for an event on a device.

    Built from device + timestamp + code + message so the same line read
    twice (overlapping ``since`` windows) dedupes, while two genuinely
    distinct events never collide.
    """
    basis = "|".join((
        device or "",
        str(ev.get("timestamp") or ""),
        str(ev.get("event_code") or ""),
        str(ev.get("message") or ""),
    ))
    return hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()


def format_slack(device: str, ev: Dict[str, Any]) -> str:
    """One-line Slack mrkdwn summary for an alert-worthy event."""
    sev = (ev.get("severity") or "?").upper()
    code = ev.get("event_code") or "EVENT"
    ts = ev.get("timestamp") or ""
    msg = ev.get("message") or ev.get("raw") or ""
    if len(msg) > 300:
        msg = msg[:297] + "..."
    return f":rotating_light: *{device}* `{sev}` *{code}* {ts}\n{msg}"


def parse_events(text: str) -> List[Dict[str, Any]]:
    """Parse a multi-line ``system-events`` blob into structured events."""
    out: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        ev = parse_event_line(line)
        if ev is not None:
            out.append(ev)
    return out


__all__ = [
    "SEVERITY_RANK",
    "DEFAULT_SEVERITY",
    "DEFAULT_MATCH",
    "severity_rank",
    "parse_event_line",
    "parse_events",
    "is_alertworthy",
    "event_fingerprint",
    "format_slack",
]
