"""Cross-family job listing / inspection for ``qactl jobs``.

Every async-job family persists its envelope to :mod:`job_store` under a
namespace (``tarload-jobs`` / ``techsupport-jobs`` / ``orc-jobs`` — see
``job_store.JOB_FAMILIES``). This module walks those namespaces to answer
"what jobs are there and what state are they in?" without going near a
device — it reads only the persisted envelopes.

A job persisted as still-running whose worker process is gone is reported as
``error`` (it died mid-flight), so a stale ``running``/``loading`` row can't
mislead a poller — the same orphan rule the per-family ``show`` commands use.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from qactl.core.envelope import error_envelope, ok_envelope
from qactl.dnos.cli.core import job_store

# Envelope status/state values that mean "still working". A row in one of
# these whose worker_pid is dead is downgraded to error.
_RUNNING = {"running", "loading", "precheck", "in_progress", "queued"}


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def _subdirs_for(kind: Optional[str]) -> Optional[List[str]]:
    """Namespaces to walk for ``kind`` (a family label or a raw subdir).

    ``None`` selects every family. An unknown ``kind`` returns ``None`` (the
    caller turns that into a bad_argument)."""
    fams = job_store.JOB_FAMILIES
    if not kind:
        return list(fams)
    matches = [sd for sd, fam in fams.items() if kind in (fam, sd)]
    return matches or None


def _orphaned(env: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``env`` with a dead-worker running-state downgraded to error."""
    state = env.get("status") or env.get("state")
    pid = env.get("worker_pid")
    if state in _RUNNING and pid and not _pid_alive(pid):
        env = dict(env)
        env["status"] = "error"
        env["state"] = "error"
        env.setdefault("errors", []).append(
            "worker process is gone — the job died mid-flight."
        )
    return env


def _row(mtime: float, env: Dict[str, Any], family: str) -> Dict[str, Any]:
    """One compact list row, uniform across families."""
    status = env.get("status") or env.get("state") or "?"
    # ``detail`` = the family-specific sub-state: orc tracks a ``phase``,
    # tar-load / tech-support track a ``state``.
    detail = env.get("phase") or env.get("state") or ""
    return {
        "job_id": env.get("job_id") or "",
        "family": family,
        "device": env.get("device") or env.get("host") or "",
        "status": status,
        "detail": detail,
        "started": env.get("started_utc") or "",
        "finished": env.get("completed_utc") or "",
    }


def jobs_list(
    kind: Optional[str] = None, status: Optional[str] = None,
    device: Optional[str] = None, limit: int = 50,
) -> Dict[str, Any]:
    """List persisted jobs across families, newest-first.

    Filters (all optional): ``kind`` (family label), ``status`` (post
    orphan-downgrade), ``device``. ``limit`` caps the returned rows (0 =
    unlimited) while ``total`` reports the match count before the cap.
    """
    subdirs = _subdirs_for(kind)
    if subdirs is None:
        fams = sorted(set(job_store.JOB_FAMILIES.values()))
        return error_envelope(
            f"unknown --kind {kind!r}; choose from: {', '.join(fams)}",
            kind="jobs_list", status="bad_argument",
        )
    rows: List[Dict[str, Any]] = []
    for sd in subdirs:
        family = job_store.JOB_FAMILIES[sd]
        for mtime, env in job_store.list_jobs(sd):
            rows.append((mtime, _row(mtime, _orphaned(env), family)))
    rows.sort(key=lambda t: t[0], reverse=True)
    out = [r for _, r in rows]
    if device:
        out = [r for r in out if r["device"] == device]
    if status:
        out = [r for r in out if r["status"] == status]
    total = len(out)
    if limit and limit > 0:
        out = out[:limit]
    return ok_envelope(
        kind="jobs_list",
        result={"count": len(out), "total": total, "jobs": out},
    )


def jobs_show(
    job_id: Optional[str] = None, device: Optional[str] = None,
    kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the full persisted envelope for one job.

    Look it up by ``job_id`` (searched across families, or the ``kind``
    family) or by ``device`` (the newest job on that device). Applies the
    same dead-worker orphan downgrade as :func:`jobs_list`.
    """
    if not job_id and not device:
        return error_envelope(
            "Pass a job_id, or -d/--device to look up the latest job on it.",
            kind="jobs_show", status="bad_argument",
        )
    subdirs = _subdirs_for(kind)
    if subdirs is None:
        fams = sorted(set(job_store.JOB_FAMILIES.values()))
        return error_envelope(
            f"unknown --kind {kind!r}; choose from: {', '.join(fams)}",
            kind="jobs_show", status="bad_argument",
        )
    if job_id:
        for sd in subdirs:
            env = job_store.load(job_id, subdir=sd)
            if env is not None:
                return _orphaned(env)
        return error_envelope(
            f"No job on record with id {job_id!r}.",
            kind="jobs_show", status="error",
            next_actions=["List what's there: qactl jobs list"],
        )
    # device: newest across the selected families
    best: Optional[Dict[str, Any]] = None
    best_mtime = -1.0
    for sd in subdirs:
        for mtime, env in job_store.list_jobs(sd):
            dev = env.get("device") or env.get("host") or ""
            if dev == device and mtime > best_mtime:
                best, best_mtime = env, mtime
    if best is None:
        return error_envelope(
            f"No job on record for device {device!r}.",
            kind="jobs_show", status="error",
            next_actions=["List what's there: qactl jobs list -d " + device],
        )
    return _orphaned(best)


__all__ = ["jobs_list", "jobs_show"]
