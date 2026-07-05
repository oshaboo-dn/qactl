"""Streaming gNMI tool: subscribe (bounded telemetry capture).

gNMI Subscribe is the push-native primitive behind "tell me when BGP
goes down" — the device streams a notification the instant a state leaf
changes, instead of the agent polling ``gnmi get`` in a loop.

A CLI invocation must terminate and emit exactly one envelope, so this
tool captures a **bounded window**: it opens a ``STREAM`` subscription,
collects updates until ``duration_s`` elapses or ``max_updates`` is hit
(whichever comes first), then returns the captured events in the
standard envelope. A long-running collector daemon (workspace-level)
loops this — or holds the stream open itself — to feed an event spool /
Slack; this tool is just the typed, gated device primitive.

DNOS / pygnmi notes (mirrors the Get surface):

- **STREAM + ON_CHANGE** is the mode you want for events; the server
  first dumps the current state of each path, sends a ``sync_response``,
  then streams only changes. Each pre-sync event is tagged
  ``pre_sync=true`` so the agent can tell the initial snapshot from real
  post-sync transitions.
- **ON_CHANGE support is per-path** — some leaves only allow ``SAMPLE``.
  If a path errors or never fires on-change, retry with ``mode=sample``.
- Keyed list paths only (``.../neighbor[neighbor-address=x]/...``), same
  as Get. ``encoding`` json or proto.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from dnctl.gnmi.core import rate_limiter
from dnctl.gnmi.core.envelope import error_envelope, make_envelope
from dnctl.gnmi.core.session import (
    DEFAULT_TIMEOUT_S,
    VALID_TLS_MODES,
    open_client,
)
from dnctl.gnmi.tools.rw import _classify_grpc_error

_VALID_ENCODINGS = ("json", "proto")
_VALID_MODES = ("on_change", "sample", "target_defined")


def _collect_events(events: List[Dict[str, Any]], resp: Dict[str, Any], sync_seen: bool) -> None:
    """Flatten one pygnmi telemetry dict into ``events`` (in place)."""
    upd = resp.get("update") or {}
    ts = upd.get("timestamp")
    for u in upd.get("update", []) or []:
        events.append({
            "timestamp": ts,
            "path": u.get("path"),
            "value": u.get("val"),
            "op": "update",
            "pre_sync": not sync_seen,
        })
    for d in upd.get("delete", []) or []:
        events.append({
            "timestamp": ts,
            "path": d,
            "value": None,
            "op": "delete",
            "pre_sync": not sync_seen,
        })


def gnmi_subscribe(
    paths: List[str],
    mode: str = "on_change",
    sample_interval_s: float = 10.0,
    duration_s: float = 30.0,
    max_updates: int = 0,
    heartbeat_s: float = 0.0,
    device: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    tls_mode: str = "insecure",
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    ca_file: Optional[str] = None,
    verify_mgmt0: bool = True,
    encoding: str = "json",
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Capture a bounded gNMI Subscribe (STREAM) window and return events.

    Opens a streaming subscription for ``paths`` and collects telemetry
    until ``duration_s`` seconds elapse or ``max_updates`` events are
    captured (whichever first), then returns one envelope.

    Args:
        paths: gNMI xpaths to subscribe. Keyed list segments need their
            key predicate (``.../neighbor[neighbor-address=10.0.0.1]``).
        mode: per-subscription mode — ``on_change`` (event-driven; the
            default and what you want for BGP/interface/alarm changes),
            ``sample`` (periodic; pair with ``sample_interval_s``), or
            ``target_defined`` (server picks).
        sample_interval_s: seconds between samples in ``sample`` mode
            (ignored otherwise).
        duration_s: wall-clock capture window. The call returns after
            this many seconds even if no event ever fires.
        max_updates: stop early once this many events are captured.
            ``0`` (default) means no cap — bounded only by time.
        heartbeat_s: optional ON_CHANGE heartbeat; the server re-sends
            the current value every N seconds even without a change.
            ``0`` disables.
        encoding: ``json`` or ``proto``.

    Returns:
        Envelope with ``result`` = ``{mode, paths, duration_s,
        event_count, sync_seen, truncated, events:[...]}``. Each event is
        ``{timestamp, path, value, op, pre_sync}``. ``sync_seen=false``
        with zero post-sync events means "subscribed fine, nothing
        changed in the window" — that's a normal ``ok``, not an error.
    """
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p.strip() for p in paths):
        return error_envelope(
            "paths must be a non-empty list of xpath strings",
            kind="subscribe", device=device, host=host, port=port, tls_mode=tls_mode,
        )
    mode_norm = (mode or "").lower()
    if mode_norm not in _VALID_MODES:
        return error_envelope(
            f"mode must be one of {_VALID_MODES}",
            kind="subscribe", device=device, host=host, port=port, tls_mode=tls_mode,
        )
    if encoding not in _VALID_ENCODINGS:
        return error_envelope(
            f"encoding must be one of {_VALID_ENCODINGS}",
            kind="subscribe", device=device, host=host, port=port, tls_mode=tls_mode,
        )
    if tls_mode not in VALID_TLS_MODES:
        return error_envelope(
            f"tls_mode must be one of {VALID_TLS_MODES}",
            kind="subscribe", device=device, host=host, port=port, tls_mode=tls_mode,
        )
    if duration_s <= 0:
        return error_envelope(
            "duration_s must be > 0",
            kind="subscribe", device=device, host=host, port=port, tls_mode=tls_mode,
        )
    if mode_norm == "sample" and sample_interval_s <= 0:
        return error_envelope(
            "sample mode requires sample_interval_s > 0",
            kind="subscribe", device=device, host=host, port=port, tls_mode=tls_mode,
        )

    request = {
        "device": device, "host": host, "port": port, "user": user,
        "tls_mode": tls_mode, "paths": list(paths), "mode": mode_norm,
        "sample_interval_s": sample_interval_s, "duration_s": duration_s,
        "max_updates": max_updates, "heartbeat_s": heartbeat_s,
        "encoding": encoding, "timeout_s": timeout_s,
    }

    subscription = []
    for p in paths:
        se: Dict[str, Any] = {"path": p, "mode": mode_norm.upper()}
        if mode_norm == "sample":
            se["sample_interval"] = int(sample_interval_s * 1e9)
        if heartbeat_s > 0:
            se["heartbeat_interval"] = int(heartbeat_s * 1e9)
        subscription.append(se)
    sub_dict = {"mode": "stream", "encoding": encoding, "subscription": subscription}

    try:
        client, resolved, _ = open_client(
            device=device, host=host, port=port,
            user=user, password=password,
            tls_mode=tls_mode,
            cert_file=cert_file, key_file=key_file, ca_file=ca_file,
            verify_mgmt0=verify_mgmt0,
        )
    except Exception as e:
        return error_envelope(
            f"resolve/setup failed: {e}",
            kind="subscribe", device=device, host=host, port=port,
            tls_mode=tls_mode, status="connect_error",
        )

    env = make_envelope(
        kind="subscribe", device=resolved.device or device,
        host=resolved.host, port=resolved.port,
        tls_mode=tls_mode, request=request,
    )
    env["warnings"].extend(resolved.warnings)
    slept = rate_limiter.gate(resolved.device, resolved.host, resolved.port)
    if slept > 0:
        env["warnings"].append(
            f"paced {slept:.2f}s before subscribing to keep DNOS rate limiter happy"
        )
    # DNOS only accepts a SAMPLE interval in [5s, 1h]; anything lower is
    # rejected with a cryptic "No valid requests in the session". Warn rather
    # than hard-fail so this stays a generic gNMI primitive for other servers.
    if mode_norm == "sample" and not (5.0 <= sample_interval_s <= 3600.0):
        env["warnings"].append(
            f"sample_interval_s={sample_interval_s} is outside DNOS's accepted "
            "5s–1h range; DNOS rejects out-of-range intervals with "
            "'No valid requests in the session'."
        )

    events: List[Dict[str, Any]] = []
    sync_seen = False
    truncated = False
    subscriber = None
    deadline = time.monotonic() + duration_s
    try:
        with client as gc:
            subscriber = gc.subscribe_stream(subscribe=sub_dict)
            # pygnmi's StreamSubscriber coalesces the first burst of updates
            # "until sync_response" (_get_updates_till_sync). If the device
            # emits a message that telemetryParser decodes to None before
            # that sync arrives — DNOS does — pygnmi runs ``"update" in
            # None`` and raises a cryptic ``TypeError: ... NoneType ... not
            # iterable``. Forcing _first_update_seen makes every get_update
            # return one decoded message at a time, which our loop already
            # handles (non-dict / None messages are skipped below), so we
            # never hit that coalescing crash.
            try:
                subscriber._first_update_seen = True  # noqa: SLF001
            except Exception:  # noqa: BLE001 - older pygnmi without the flag
                pass
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if max_updates and len(events) >= max_updates:
                    truncated = True
                    break
                # The background gRPC thread stashes any stream-side failure
                # (e.g. server closed the channel, path rejected) on
                # ``.error`` instead of raising it to us — surface it.
                stream_err = getattr(subscriber, "error", None)
                if stream_err is not None:
                    raise stream_err
                try:
                    resp = subscriber.get_update(timeout=remaining)
                except TimeoutError:
                    break
                except TypeError as te:
                    # Belt-and-suspenders: if a pygnmi build still trips the
                    # None-merge above, fall back to the stashed stream error
                    # (or a clear message) rather than leaking the TypeError.
                    raise (getattr(subscriber, "error", None) or te)
                if not isinstance(resp, dict):
                    continue
                if resp.get("sync_response"):
                    sync_seen = True
                    continue
                _collect_events(events, resp, sync_seen)
            # The server may reject/drop the subscription while get_update is
            # blocked (DNOS answers an unsupported path with INVALID_ARGUMENT
            # "No valid requests in the session"); that lands on .error after
            # we've stopped looping, so check once more before declaring ok.
            trailing_err = getattr(subscriber, "error", None)
            if trailing_err is not None and not events:
                raise trailing_err
    except Exception as e:
        msg = str(e).replace("\n", " ")
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {msg[:300]}")
        hint = _classify_grpc_error(msg)
        if hint:
            env["next_actions"].append(hint)
        return env
    finally:
        if subscriber is not None:
            try:
                subscriber.close()
            except Exception:
                pass

    if max_updates and len(events) >= max_updates:
        events = events[:max_updates]
        truncated = True

    post_sync = sum(1 for e in events if not e["pre_sync"])
    if not sync_seen:
        env["warnings"].append(
            "no sync_response within the window — the server may not "
            "support on_change for one of these paths; if events never "
            "arrive, retry with mode='sample'."
        )
    if sync_seen and post_sync == 0:
        env["warnings"].append(
            "subscribed ok; no changes during the window (nothing "
            "transitioned). This is a normal quiet result, not a failure."
        )

    env["result"] = {
        "mode": mode_norm,
        "paths": list(paths),
        "duration_s": duration_s,
        "event_count": len(events),
        "post_sync_event_count": post_sync,
        "sync_seen": sync_seen,
        "truncated": truncated,
        "events": events,
    }
    return env


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(gnmi_subscribe)
