"""Per-device pacing to keep DNOS' gNMI rate limiter happy.

Measured behaviour on `cl`: bursts of >2 Get calls within ~1 s trip
``Rate limit exceeded!`` for several seconds. We don't get an
authoritative interval from the server, so we conservatively pace
3 s between Gets per (device, host) target.

The gate is a tiny in-process map keyed by ``(device or host, port)``;
no daemon thread, no shared state across MCP processes — fine for the
single-pid systemd-managed deployment.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Tuple


# Tunable. 3 s matches the spacing the SW-252550 capture script used.
MIN_INTERVAL_S: float = 3.0


_lock = threading.Lock()
_last_call: Dict[Tuple[str, int], float] = {}


def _key(device: str | None, host: str | None, port: int) -> Tuple[str, int]:
    target = device or host or ""
    return (target, port)


def gate(device: str | None, host: str | None, port: int) -> float:
    """Block until the per-target minimum interval has elapsed.

    Returns the number of seconds we slept (0.0 if no wait was needed).
    Updates the per-target last-call timestamp atomically with the wait
    so concurrent callers serialise correctly.
    """
    k = _key(device, host, port)
    with _lock:
        now = time.monotonic()
        last = _last_call.get(k, 0.0)
        wait = max(0.0, MIN_INTERVAL_S - (now - last))
    if wait > 0:
        time.sleep(wait)
    with _lock:
        _last_call[k] = time.monotonic()
    return wait
