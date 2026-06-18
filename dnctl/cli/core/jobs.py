"""Generic in-memory job registry for long-running background work.

Used by tools that kick off a slow device-side operation, return
immediately with a ``job_id``, drive the rest of the work in a daemon
worker thread, and let the agent poll a companion ``get_*_job`` tool
for status.

Currently consumed by:
    - ``create_techsupport``    + ``get_techsupport_job``
    - ``request_system_tar_load`` + ``get_tar_load_job``

The registry deliberately knows nothing about the tool's payload.
Every tool subclasses ``BaseJob`` with its own per-job fields and
defines its own ``_envelope`` projection; the registry only handles
storage, the device-slot lock, GC of terminal jobs after a TTL, and
the shared ``get_*_job`` lookup logic.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple


@dataclass
class BaseJob:
    """Common fields for any registry-backed background job.

    Tool-specific subclasses add their own fields (uploaded paths,
    per-step transcripts, pre-check verdicts, ...). The registry
    itself only reads ``job_id`` / ``device_key`` / ``state`` /
    ``_finished_at``.
    """

    job_id: str
    device: Optional[str]
    host: Optional[str]
    device_key: str
    resolved_host: str
    state: str
    started_utc: str
    user: str = ""
    completed_utc: Optional[str] = None
    elapsed_s: Optional[int] = None
    command: str = ""
    stdout: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    next_actions: List[str] = field(default_factory=list)
    # Slack notify config (opt-in). ``notify_channel`` is the channel
    # name / id / @user passed at kickoff; ``notify_thread_ts`` is the
    # message ts captured from the kickoff post so terminal updates
    # reply in the same thread. Both empty when notify is disabled.
    notify_channel: str = ""
    notify_thread_ts: str = ""
    _finished_at: float = 0.0


class JobRegistry:
    """Thread-safe in-memory registry for background jobs.

    Enforces one active job per device at a time. The caller is
    responsible for the conflict check itself — call
    ``active_for_device`` immediately before ``register`` and reject
    the kickoff if the existing job's state is in ``active_states``.
    Terminal jobs (``state in terminal_states``) are GCed lazily after
    ``ttl_s`` seconds to keep memory bounded across long server
    uptimes.

    The registry is shared by multiple unrelated tools — keep one
    ``JobRegistry`` instance per tool type rather than one global
    registry, so a stuck tech-support job can't block tar-load
    kickoffs and vice versa.
    """

    def __init__(
        self,
        *,
        ttl_s: int,
        terminal_states: FrozenSet[str],
        active_states: FrozenSet[str],
    ) -> None:
        self._jobs: Dict[str, BaseJob] = {}
        self._active: Dict[str, str] = {}
        self.lock = threading.Lock()
        self.ttl_s = ttl_s
        self.terminal_states = frozenset(terminal_states)
        self.active_states = frozenset(active_states)

    # ---- id mint -----------------------------------------------------

    @staticmethod
    def make_job_id(device_key: str, name: str) -> str:
        """Human-legible job id, e.g. ``sa-diag-a1b2c3``."""
        dev = re.sub(r"[^A-Za-z0-9]", "_", device_key or "")[:16] or "dev"
        nm = re.sub(r"[^A-Za-z0-9]", "_", name or "")[:16] or "job"
        return f"{dev}-{nm}-{uuid.uuid4().hex[:6]}"

    # ---- internal: caller already holds the lock --------------------

    def _gc_locked(self) -> None:
        if not self._jobs:
            return
        now = time.time()
        to_drop = [
            jid for jid, j in self._jobs.items()
            if j.state in self.terminal_states
            and j._finished_at
            and (now - j._finished_at) > self.ttl_s
        ]
        for jid in to_drop:
            self._jobs.pop(jid, None)

    # ---- public API -------------------------------------------------

    def active_for_device(self, device_key: str) -> Optional[BaseJob]:
        """Return the registered active job for ``device_key``
        (whatever its current state). Caller checks whether the state
        is in ``active_states`` to decide whether to reject a new
        kickoff."""
        with self.lock:
            self._gc_locked()
            jid = self._active.get(device_key)
            if not jid:
                return None
            return self._jobs.get(jid)

    def register(self, job: BaseJob) -> None:
        """Insert a brand-new job and stamp it as active for its
        device. Caller MUST have just verified there's no conflicting
        active job."""
        with self.lock:
            self._jobs[job.job_id] = job
            self._active[job.device_key] = job.job_id

    def finish(self, job: BaseJob, state: str) -> None:
        """Transition ``job`` to a terminal ``state`` and release its
        device slot. Idempotent re: the slot release (won't pop another
        job's slot if it's been replaced)."""
        with self.lock:
            job.state = state
            job._finished_at = time.time()
            if self._active.get(job.device_key) == job.job_id:
                self._active.pop(job.device_key, None)

    @staticmethod
    def append_bounded(items: List[str], msg: str, max_len: int) -> None:
        """Append to ``items`` while keeping ``len(items) <= max_len``;
        oldest entries are dropped first. Used by workers for the
        ``job.warnings`` list so flaky multi-hour runs can't grow it
        unboundedly."""
        items.append(msg)
        overflow = len(items) - max_len
        if overflow > 0:
            del items[:overflow]

    def lookup(
        self,
        *,
        job_id: Optional[str],
        device_key: Optional[str],
    ) -> Tuple[Optional[BaseJob], Optional[str]]:
        """Resolve a job for a ``get_*_job`` tool call.

        - If ``job_id`` is given, return that job (or an error message
          if it's been GCed or never registered). Cross-checks
          ``device_key`` if also passed.
        - Else if ``device_key`` is given, return the currently-active
          job for that device, falling back to the most recently
          finished job on the same device if none is active.
        - At least one of the two must be provided.

        Returns ``(job, error_msg)`` — exactly one is non-None."""
        if not job_id and not device_key:
            return None, "Must provide job_id or device."
        with self.lock:
            self._gc_locked()
            if job_id:
                job = self._jobs.get(job_id)
                if not job:
                    hours = max(1, self.ttl_s // 3600)
                    return None, (
                        f"No job with job_id={job_id!r} (reaped after "
                        f"{hours} h, or never registered)."
                    )
                if device_key and job.device_key != device_key:
                    return None, (
                        f"job_id={job_id!r} belongs to device "
                        f"{job.device_key!r}, not {device_key!r}."
                    )
                return job, None
            dk = device_key or ""
            jid = self._active.get(dk)
            job = self._jobs.get(jid) if jid else None
            if job is None:
                candidates = [
                    j for j in self._jobs.values() if j.device_key == dk
                ]
                if candidates:
                    job = max(candidates, key=lambda j: j._finished_at or 0.0)
            if job is None:
                return None, f"No job on record for device={device_key!r}."
            return job, None
