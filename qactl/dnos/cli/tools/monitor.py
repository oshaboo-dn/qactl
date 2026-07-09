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

import contextlib
import os
import sys
from typing import Any, Dict, List, Optional

from qactl.dnos.cli.core import event_spool as _spool
from qactl.dnos.cli.core import events as _events
from qactl.dnos.cli.core import gnmi_links as _links
from qactl.dnos.cli.core import slack_notify
from qactl.dnos.cli.core.envelope import make_response
from qactl.dnos.cli.core.session import (
    DEFAULT_CMD_TIMEOUT,
    DEFAULT_PASSWORD,
    DEFAULT_USER,
)
from qactl.dnos.cli.tools.log_read import get_system_events
from qactl.dnos.core import devices as _dn_devices

try:  # gNMI is an optional source; degrade cleanly if its deps are absent
    from qactl.dnos.gnmi.tools.rw import gnmi_get as _gnmi_get
except Exception:  # noqa: BLE001
    _gnmi_get = None


@contextlib.contextmanager
def _silence_fd1():
    """Redirect OS fd 1 to /dev/null for the duration of the block.

    gRPC's C core writes TLS notices ("ssl_target_name_override ...")
    straight to file descriptor 1, bypassing ``sys.stdout`` — which would
    corrupt the ``--json`` envelope this tool emits. The gNMI link read
    returns structured data, so it has no stdout we need to keep; mute fd 1
    around it. Native-side only; the vendored gNMI tree is untouched.
    """
    sys.stdout.flush()
    saved = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 1)
        yield
    finally:
        os.dup2(saved, 1)
        os.close(devnull)
        os.close(saved)

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
    links: bool = True,
    gnmi_tls_mode: str = "skip_verify",
    gnmi_port: Optional[int] = None,
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
        links: also collect interface oper-status over gNMI and emit
            up/down transition events (snapshot diff). Degrades to a
            warning if gNMI is unreachable; never blocks the syslog source.
        gnmi_tls_mode: TLS mode for the gNMI link read (DNOS lab default
            ``skip_verify``; also ``insecure`` / ``verify_ca`` / ``mtls``).
        gnmi_port: override the gNMI port (default from the registry/50051).
        dry_run: parse + report but do NOT notify or advance the cursor /
            dedupe state / link snapshot. Safe to run repeatedly.
        state_path: override the spool file path (tests).
        user/password/timeout: SSH (and gNMI) params.

    Returns:
        Envelope with ``ticked`` (per-device summaries), ``new_events``
        (flattened, each tagged with ``device`` and ``source``),
        ``new_event_count``, ``notified``, and ``notify_errors``.
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
            summary: Dict[str, Any] = {
                "device": canonical, "since": since,
                "alert_count": 0, "new_count": 0,
            }
            new_events: List[Dict[str, Any]] = []

            # --- source 1: syslog system-events (BGP + EVENT_CODE) ---------
            # DEVICE_HOSTS (the SSH-host cache get_system_events resolves
            # against) is keyed by canonical name, so read with the resolved
            # key — a secondary alias like "cl" isn't a key there.
            resp = get_system_events(
                tail_lines=None, since=since,
                device=canonical, user=user, password=password, timeout=timeout,
            )
            summary["read_status"] = resp.get("status")
            if resp.get("status") != "ok":
                any_read_error = True
                errs = resp.get("errors") or ["read failed"]
                summary["error"] = errs[0]
                warnings.append(f"{canonical}: system-events read failed: {errs[0]}")
            else:
                parsed = _events.parse_events(resp.get("stdout") or "")
                alert = [
                    ev for ev in parsed
                    if _events.is_alertworthy(
                        ev, max_rank=max_rank,
                        match=match_terms, exclude=exclude_terms,
                    )
                ]
                summary["alert_count"] = len(alert)
                alert_fps: List[str] = []
                for ev in alert:
                    fp = _events.event_fingerprint(canonical, ev)
                    alert_fps.append(fp)
                    if _spool.is_new(state, canonical, fp):
                        tagged = dict(ev)
                        tagged.setdefault("source", "syslog")
                        tagged["device"] = canonical
                        tagged["fingerprint"] = fp
                        new_events.append(tagged)
                if not dry_run:
                    _spool.record(
                        state, canonical, alert_fps,
                        cursor=_newest_timestamp(parsed),
                    )

            # --- source 2: gNMI interface oper-status (link up/down) -------
            # A bounded Get + snapshot diff; the diff is the dedupe, so these
            # bypass the fingerprint ring. Degrades to a warning if gNMI is
            # unreachable — it must never sink the syslog source.
            if links and _gnmi_get is not None:
                with _silence_fd1():
                    genv = _gnmi_get(
                        path=_links.OPER_STATUS_PATH, device=canonical,
                        port=gnmi_port, user=user, password=password,
                        tls_mode=gnmi_tls_mode, timeout_s=timeout,
                    )
                summary["gnmi_status"] = genv.get("status")
                if genv.get("status") == "ok":
                    cur = _links.parse_oper_status(genv)
                    if cur:
                        old = _spool.get_links(state, canonical)
                        diffs = _links.diff_link_states(canonical, old, cur)
                        link_alerts = [
                            ev for ev in diffs
                            if _events.is_alertworthy(
                                ev, max_rank=max_rank,
                                match=match_terms, exclude=exclude_terms,
                            )
                        ]
                        summary["link_change_count"] = len(link_alerts)
                        for ev in link_alerts:
                            ev["device"] = canonical
                            ev["fingerprint"] = _events.event_fingerprint(canonical, ev)
                            new_events.append(ev)
                        if not dry_run:
                            _spool.set_links(state, canonical, cur)
                else:
                    gerrs = genv.get("errors") or ["gnmi get failed"]
                    summary["gnmi_error"] = gerrs[0]
                    warnings.append(
                        f"{canonical}: gNMI link read skipped: {gerrs[0]}"
                    )

            # --- surface + notify (both sources) --------------------------
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

            all_new.extend(new_events)
            ticked.append(summary)

        if not dry_run:
            _spool.save(state, state_path)

    if notify_errors:
        warnings.extend(notify_errors)

    # Any warning (syslog read failure, gNMI link degrade, notify error)
    # downgrades to "warning" — still exit 0, but signals a partial tick.
    status = "warning" if warnings else "ok"
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


def monitor_reset(
    devices: Optional[List[str]] = None,
    state_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Clear collector memory (cursor + dedupe ring + link snapshot).

    With no ``devices``, wipes the whole spool. Otherwise clears only the
    named devices (resolved to canonical). After a reset the next tick
    re-establishes a baseline from ``lookback`` and won't re-alert on
    already-seen history beyond that window.

    Args:
        devices: device names/aliases to clear. ``None``/empty = all.
        state_path: override the spool file path (tests).

    Returns:
        Envelope with ``cleared`` (the device list, or ``["*"]`` for all).
    """
    with _spool._LOCK:
        state = _spool.load(state_path)
        if devices:
            cleared = [_dn_devices.resolve_canonical(d) or d for d in devices]
            for d in cleared:
                _spool.reset(state, d)
        else:
            cleared = ["*"]
            _spool.reset(state, None)
        _spool.save(state, state_path)
    return make_response(
        status="ok", device=None, operation="monitor-reset", cleared=cleared,
    )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(monitor_tick)
    mcp.tool()(monitor_reset)
