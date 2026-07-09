"""On-disk persistence for tar-load / pre-check job envelopes.

The async job model (in-memory :class:`~qactl.cli.core.jobs.JobRegistry`
+ a daemon worker thread) is built for the long-running MCP *server*
process: a kickoff returns a ``job_id``, the worker keeps running inside
the same process, and ``get_tar_load_job`` reads the result back from
memory.

Under the one-shot CLI front (``qactl cli tar-load ...``) that model
breaks: the process exits the instant the command returns, so the
in-memory registry is gone and a later ``qactl cli tar-load show <id>``
finds nothing (issue #17). To make ``show`` resolvable across separate
CLI invocations, the CLI runs the worker synchronously (see ``block`` in
:mod:`qactl.cli.tools.tarload`) and persists the job envelope here as a
small JSON file keyed by ``job_id``. Since issue #76 the tar-load worker
persists *live* envelopes too (kickoff, per step, precheck transition),
each carrying a ``worker_pid``, so a detached ``--no-wait`` load can be
polled — and told apart from a dead one — by any later process.

This is a pure local-filesystem cache of the envelope dict — it never
touches the device and stores no secrets (the envelope itself carries
none). Files older than :data:`_TTL_S` are reaped lazily.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from qactl.dnos.core import paths as _paths

# Minted ids look like ``<dev>-<name>-<6 hex>`` (see
# ``JobRegistry.make_job_id``): word chars, hyphens, dots only. Anything
# else — path separators, ``..``, NUL — is rejected before it can be
# joined into a filesystem path, so a hostile ``show <id>`` can't escape
# the cache dir and read arbitrary ``.json`` files.
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _is_safe_job_id(job_id: str) -> bool:
    return bool(job_id) and job_id not in (".", "..") and bool(_SAFE_JOB_ID.match(job_id))

# Keep finished jobs on disk for the same window the in-memory registry
# keeps them (24 h), so the two fronts behave consistently.
_TTL_S = 24 * 3600

# Default namespace (sub-directory under the CLI state dir). Each async
# job family gets its own so a ``latest_for_device`` lookup for one
# (e.g. tech-support) never returns another's envelope (e.g. tar-load).
_SUBDIR = "tarload-jobs"


def _dir(subdir: str = _SUBDIR) -> str:
    return os.path.join(str(_paths.state_dir("cli")), subdir)


def _path(job_id: str, subdir: str = _SUBDIR) -> str:
    return os.path.join(_dir(subdir), f"{job_id}.json")


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


def save(envelope: Dict[str, Any], subdir: str = _SUBDIR) -> None:
    """Persist a terminal job ``envelope`` keyed by its ``job_id``.

    No-op when the envelope has no ``job_id``. Writes atomically (temp +
    rename) so a concurrent :func:`load` never sees a half-written file.
    Failures are swallowed — persistence is a convenience, not a
    correctness requirement (the kickoff still returns the live result).
    ``subdir`` selects the namespace (one per async job family).
    """
    job_id = envelope.get("job_id")
    if not job_id or not _is_safe_job_id(str(job_id)):
        return
    directory = _dir(subdir)
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = os.path.join(directory, f".{job_id}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(envelope, f, ensure_ascii=False)
        os.replace(tmp, _path(job_id, subdir))
        _reap(directory)
    except OSError:
        pass


def load(job_id: str, subdir: str = _SUBDIR) -> Optional[Dict[str, Any]]:
    """Return the persisted envelope for ``job_id``, or ``None``."""
    if not _is_safe_job_id(job_id):
        return None
    try:
        with open(_path(job_id, subdir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def latest_for_device(device_key: str, subdir: str = _SUBDIR) -> Optional[Dict[str, Any]]:
    """Return the most recent persisted envelope for ``device_key``.

    "Most recent" is by the file's mtime (which tracks the last
    :func:`save`, i.e. when the job reached its terminal state).
    """
    if not device_key:
        return None
    directory = _dir(subdir)
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
