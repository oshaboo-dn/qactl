"""Event collector — bounded ``monitor tick`` over the device fleet.

This is the workspace-level piece the ``gnmi subscribe`` / log-read docs
keep pointing at: a single *tick* that, for each device,

  1. reads its ``system-events`` log **since the last tick** (a persisted
     per-device cursor; first run uses ``lookback``),
  2. parses the lines into structured events,
  3. keeps the **alert-worthy** ones (severity threshold OR an interesting
     event-code/message substring — BGP, link-down, crash, HA failover …),
  4. **dedupes** against what earlier ticks already handled, and
  5. optionally pushes each new alert to **Slack**,

then advances the cursor and exits. A cron/loop runs the tick on an
interval; nothing here holds a connection open.

Why syslog and not gNMI for these events: DNOS gNMI Subscribe works for
paths in its on-change registry (interface oper-status/admin-state,
transceivers, PSU/fan/temp, LACP, ...) — those are better consumed as a
push stream via ``gnmi subscribe``. But BGP neighbor state and most
``EVENT_CODE`` platform notifications are NOT in that registry, so the
syslog ``system-events`` log is the source of truth for them. A future
revision can fold gNMI on-change link/platform signals into the same
spool alongside these syslog events.

Why a one-shot instead of a daemon: it matches the agent-safe CLI
contract (``--json`` envelope, real exit codes, ``--yes`` gating for the
side-effecting notify) and survives process restarts via the on-disk
spool, while a loop/cron supplies the "keep running" part.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from dnctl.cli.core import event_spool as _spool
from dnctl.cli.core import events as _events
from dnctl.cli.core import slack_notify
from dnctl.cli.core.envelope import make_response
from dnctl.cli.core.session import (
    DEFAULT_CMD_TIMEOUT,
    DEFAULT_PASSWORD,
    DEFAULT_USER,
)
from dnctl.cli.tools.log_read import get_system_events
from dnctl.core import devices as _dn_devices

_TICK_NEXT_ACTION = (
    "Re-run `qactl cli monitor tick` on an interval (cron / loop) to keep "
    "collecting. Tune with --severity / --match / --exclude, target a "
    "subset with -d/--device (repeatable), and add --notify <#channel|@user> "
    "--yes to push alerts to Slack. Use --dry-run to preview without "
    "advancing the cursor or notifying."
)

_MAX_EVENTS_DEFAULT = 200


def _newest_timestamp(parsed: List[Dict[str, Any]]) -> Optional[str]:
    """Largest ISO-8601 timestamp among parsed events (lexical), or None."""
    best: Optional[str] = None
    for ev in parsed:
        ts = ev.get("timestamp")
        if isinstance(ts, str) and ts and (best is None or ts > best):
            best = ts
    return best


def monitor_tick(
    devices: Optional[List[str]] = None,
    severity: str = _events.DEFAULT_SEVERITY,
    match: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    use_default_rules: bool = True,
    lookback: str = "15m",
    notify_slack: str = "",
    max_events_per_device: int = _MAX_EVENTS_DEFAULT,
    dry_run: bool = False,
    state_path: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Run one collection tick across one or more devices.

    Args:
        devices: device names/aliases to poll. ``None``/empty = every
            registered device.
        severity: minimum syslog severity that alerts on its own
            (``emerg|alert|crit|err|warning|notice|info|debug``). Anything
            at least this severe is kept regardless of ``match``.
        match: extra case-insensitive substrings (event-code or message)
            that mark an event alert-worthy even below the severity
            threshold. Added to the built-in rules unless
            ``use_default_rules`` is False.
        exclude: case-insensitive substrings that veto an event.
        use_default_rules: include the built-in interesting-code list
            (:data:`events.DEFAULT_MATCH`).
        lookback: how far back to read on a device's **first** tick (no
            cursor yet). Relative (``30s``/``10m``/``2h``/``1d``) or ISO.
        notify_slack: Slack channel/``@user`` to post new alerts to. Empty
            disables notification (collection still happens).
        max_events_per_device: cap on new alerts surfaced/notified per
            device per tick (newest kept), so a log storm can't flood.
        dry_run: parse + report but do NOT notify or advance the cursor /
            dedupe state. Safe to run repeatedly.
        state_path: override the spool file path (tests).
        user/password/timeout: SSH params for the log read.

    Returns:
        Envelope with ``ticked`` (per-device summaries), ``new_events``
        (flattened, each tagged with ``device``), ``new_event_count``,
        ``notified``, and ``notify_errors``.
    """
    sev = (severity or "").strip().lower()
    if sev not in _events.SEVERITY_RANK:
        return make_response(
            status="error", device=None,
            errors=[
                f"severity must be one of {sorted(_events.SEVERITY_RANK)} "
                f"(got {severity!r})."
            ],
            next_actions=[_TICK_NEXT_ACTION],
            operation="monitor-tick",
        )
    max_rank = _events.severity_rank(sev)

    if not isinstance(max_events_per_device, int) or max_events_per_device < 1:
        return make_response(
            status="error", device=None,
            errors=["max_events_per_device must be a positive integer."],
            next_actions=[_TICK_NEXT_ACTION], operation="monitor-tick",
        )

    match_terms = list(_events.DEFAULT_MATCH) if use_default_rules else []
    match_terms += [m for m in (match or []) if m]
    exclude_terms = [e for e in (exclude or []) if e]

    if devices:
        targets = list(devices)
    else:
        targets = _dn_devices.list_device_aliases()
    if not targets:
        return make_response(
            status="warning", device=None,
            warnings=["no devices to poll (none registered)."],
            next_actions=[_TICK_NEXT_ACTION], operation="monitor-tick",
            ticked=[], new_events=[], new_event_count=0,
            notified=0, notify_errors=[],
        )

    with _spool._LOCK:
        state = _spool.load(state_path)

        ticked: List[Dict[str, Any]] = []
        all_new: List[Dict[str, Any]] = []
        warnings: List[str] = []
        notified = 0
        notify_errors: List[str] = []
        any_read_error = False

        for name in targets:
            canonical = _dn_devices.resolve_canonical(name) or name
            cursor = _spool.get_cursor(state, canonical)
            since = cursor or lookback
            # DEVICE_HOSTS (the SSH-host cache get_system_events resolves
            # against) is keyed by canonical name, so read with the resolved
            # key — a secondary alias like "cl" isn't a key there.
            resp = get_system_events(
                tail_lines=None, since=since,
                device=canonical, user=user, password=password, timeout=timeout,
            )
            summary: Dict[str, Any] = {
                "device": canonical, "since": since,
                "read_status": resp.get("status"),
                "alert_count": 0, "new_count": 0,
            }
            if resp.get("status") != "ok":
                any_read_error = True
                errs = resp.get("errors") or ["read failed"]
                summary["error"] = errs[0]
                warnings.append(f"{canonical}: system-events read failed: {errs[0]}")
                ticked.append(summary)
                continue

            parsed = _events.parse_events(resp.get("stdout") or "")
            alert = [
                ev for ev in parsed
                if _events.is_alertworthy(
                    ev, max_rank=max_rank,
                    match=match_terms, exclude=exclude_terms,
                )
            ]
            summary["alert_count"] = len(alert)

            new_events: List[Dict[str, Any]] = []
            alert_fps: List[str] = []
            for ev in alert:
                fp = _events.event_fingerprint(canonical, ev)
                alert_fps.append(fp)
                if _spool.is_new(state, canonical, fp):
                    tagged = dict(ev)
                    tagged["device"] = canonical
                    tagged["fingerprint"] = fp
                    new_events.append(tagged)

            # Newest first, capped — a storm can't flood the notifier.
            new_events.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
            if len(new_events) > max_events_per_device:
                summary["truncated"] = True
                new_events = new_events[:max_events_per_device]
            summary["new_count"] = len(new_events)

            if notify_slack and not dry_run:
                for ev in new_events:
                    r = slack_notify.post(
                        notify_slack, _events.format_slack(canonical, ev),
                    )
                    if r.get("ok"):
                        notified += 1
                    else:
                        notify_errors.append(
                            f"{canonical}: {r.get('error') or 'slack failed'}"
                        )

            if not dry_run:
                _spool.record(
                    state, canonical, alert_fps,
                    cursor=_newest_timestamp(parsed),
                )

            all_new.extend(new_events)
            ticked.append(summary)

        if not dry_run:
            _spool.save(state, state_path)

    if notify_errors:
        warnings.extend(notify_errors)

    status = "warning" if (any_read_error or notify_errors) else "ok"
    return make_response(
        status=status, device=None,
        warnings=warnings,
        next_actions=[_TICK_NEXT_ACTION] if any_read_error else [],
        operation="monitor-tick",
        dry_run=dry_run,
        severity=sev,
        ticked=ticked,
        new_events=all_new,
        new_event_count=len(all_new),
        notified=notified,
        notify_errors=notify_errors,
    )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(monitor_tick)
