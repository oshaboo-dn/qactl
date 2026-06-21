"""On-disk persistence for tar-load / pre-check job envelopes.

The async job model (in-memory :class:`~dnctl.cli.core.jobs.JobRegistry`
+ a daemon worker thread) is built for the long-running MCP *server*
process: a kickoff returns a ``job_id``, the worker keeps running inside
the same process, and ``get_tar_load_job`` reads the result back from
memory.

Under the one-shot CLI front (``qactl cli tar-load ...``) that model
breaks: the process exits the instant the command returns, so the
in-memory registry is gone and a later ``qactl cli tar-load show <id>``
finds nothing (issue #17). To make ``show`` resolvable across separate
CLI invocations, the CLI runs the worker synchronously (see ``block`` in
:mod:`dnctl.cli.tools.tarload`) and persists the *terminal* job envelope
here as a small JSON file keyed by ``job_id``.

This is a pure local-filesystem cache of the envelope dict — it never
touches the device and stores no secrets (the envelope itself carries
none). Files older than :data:`_TTL_S` are reaped lazily.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from dnctl.core import paths as _paths

# Keep finished jobs on disk for the same window the in-memory registry
# keeps them (24 h), so the two fronts behave consistently.
_TTL_S = 24 * 3600

_SUBDIR = "tarload-jobs"


def _dir() -> str:
    return os.path.join(str(_paths.state_dir("cli")), _SUBDIR)


def _path(job_id: str) -> str:
    return os.path.join(_dir(), f"{job_id}.json")


def _device_key(env: Dict[str, Any]) -> str:
    return str(env.get("device") or env.get("host") or "")


def _reap(directory: str) -> None:
    """Drop persisted envelopes older than the TTL. Best-effort."""
    now = time.time()
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        p = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(p) > _TTL_S:
                os.remove(p)
        except OSError:
            pass


def save(envelope: Dict[str, Any]) -> None:
    """Persist a terminal job ``envelope`` keyed by its ``job_id``.

    No-op when the envelope has no ``job_id``. Writes atomically (temp +
    rename) so a concurrent :func:`load` never sees a half-written file.
    Failures are swallowed — persistence is a convenience, not a
    correctness requirement (the kickoff still returns the live result).
    """
    job_id = envelope.get("job_id")
    if not job_id:
        return
    directory = _dir()
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = os.path.join(directory, f".{job_id}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(envelope, f, ensure_ascii=False)
        os.replace(tmp, _path(job_id))
        _reap(directory)
    except OSError:
        pass


def load(job_id: str) -> Optional[Dict[str, Any]]:
    """Return the persisted envelope for ``job_id``, or ``None``."""
    if not job_id:
        return None
    try:
        with open(_path(job_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def latest_for_device(device_key: str) -> Optional[Dict[str, Any]]:
    """Return the most recent persisted envelope for ``device_key``.

    "Most recent" is by the file's mtime (which tracks the last
    :func:`save`, i.e. when the job reached its terminal state).
    """
    if not device_key:
        return None
    directory = _dir()
    best: Optional[Dict[str, Any]] = None
    best_mtime = -1.0
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for name in names:
        if not name.endswith(".json"):
            continue
        p = os.path.join(directory, name)
        try:
            mtime = os.path.getmtime(p)
            with open(p, encoding="utf-8") as f:
                env = json.load(f)
        except (OSError, ValueError):
            continue
        if _device_key(env) != device_key:
            continue
        if mtime > best_mtime:
            best, best_mtime = env, mtime
    return best


__all__: List[str] = ["save", "load", "latest_for_device"]
