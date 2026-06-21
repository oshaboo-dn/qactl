"""Bucket E — the idle reaper must not close a busy transport.

A single long step (e.g. a 2-hour ``target-stack load``) never touches
``last_used`` mid-read, so the 30-minute idle reaper used to close the
pooled SSH transport under it and truncate the download. The ``in_use``
flag fixes that: busy transports are skipped regardless of idle age.
"""

from __future__ import annotations

import time

from dnctl.cli.core.session import Transport, TransportRegistry


class _FakeTransport:
    def __init__(self, alive: bool):
        self._alive = alive

    def is_active(self) -> bool:
        return self._alive


class _FakeClient:
    def __init__(self, alive: bool = True):
        self._t = _FakeTransport(alive)

    def get_transport(self):
        return self._t

    def close(self):
        pass


def _mk(reg, key, last_used, *, in_use=0, alive=True):
    t = Transport(
        key=key, device=key[0], host=key[0], user=key[1],
        client=_FakeClient(alive), last_used=last_used, in_use=in_use,
    )
    reg._transports[key] = t
    return t


def test_select_stale_skips_busy_and_fresh():
    reg = TransportRegistry(idle_max=10)
    now = time.time()
    _mk(reg, ("idle", "u"), now - 100)                       # stale
    _mk(reg, ("busy", "u"), now - 100, in_use=1)             # busy -> keep
    _mk(reg, ("fresh", "u"), now)                            # fresh -> keep
    _mk(reg, ("dead-busy", "u"), now, in_use=2, alive=False) # busy -> keep
    _mk(reg, ("dead", "u"), now, alive=False)                # dead -> stale

    stale = set(reg._select_stale(now))
    assert ("idle", "u") in stale
    assert ("dead", "u") in stale
    assert ("busy", "u") not in stale
    assert ("fresh", "u") not in stale
    assert ("dead-busy", "u") not in stale


def test_mark_increments_and_clamps():
    reg = TransportRegistry(idle_max=10)
    t = _mk(reg, ("a", "u"), time.time())
    reg._mark(t, 1)
    assert t.in_use == 1
    reg._mark(t, 1)
    assert t.in_use == 2
    reg._mark(t, -1)
    assert t.in_use == 1
    reg._mark(t, -1)
    reg._mark(t, -1)
    assert t.in_use == 0  # never goes negative
