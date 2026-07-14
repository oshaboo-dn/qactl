"""Orchestration engine for ``qactl orc`` — chain build → load → pre-check.

The heavy lifting already exists as three self-contained, envelope-returning
building blocks:

- ``jenkins_trigger(..., wait=True)``          — trigger a cheetah build and
  poll it to a terminal state; a SUCCESS carries ``result.build_url``.
- ``request_system_tar_load(jenkins_url=..., pre_check=False, block=True)``
  — download the build's per-component tarballs and load them on the device.
- ``request_system_pre_check(block=True)``     — run the pre-upgrade
  system pre-check and wait for the verdict.

This module wires them into ONE job with an explicit phase sequence and
persists a combined envelope to :mod:`qactl.dnos.cli.core.job_store` (under
its own ``orc-jobs`` namespace) after every phase transition. That makes a
run pollable across separate processes with ``orc show`` — the same model
the tar-load worker uses.

Two flows, one driver (:func:`_drive`):

- ``orc load``   runs the load + pre-check phases (blocking by default; the
  load+pre-check is minutes).
- ``orc build``  prepends the Jenkins build phase (detached by default; a
  cheetah build can run hours, so we fork a session-detached worker and
  return a job handle immediately).

Nothing here talks to a device directly — device work stays inside the
tar-load / pre-check tools, so their SSH handling, per-device guard,
GI-detection and evidence journaling all carry over unchanged.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from qactl.core.envelope import error_envelope, make_envelope
from qactl.dnos.cli.core import job_store

# Own namespace under the CLI state dir, so an ``orc`` job never collides with
# a tar-load / pre-check / tech-support envelope in ``latest_for_device``.
_ORC_SUBDIR = "orc-jobs"

# The ordered phase names an orc job moves through. ``build`` is only present
# for ``orc build``; ``orc load`` starts at ``load``.
_PHASES_LOAD = ("load", "pre_check")
_PHASES_BUILD = ("build",) + _PHASES_LOAD


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pid_alive(pid: Optional[int]) -> bool:
    """True if ``pid`` names a live process we can see.

    A ``PermissionError`` means the process exists but isn't ours (still
    alive); ``ProcessLookupError`` means it's gone.
    """
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError,):
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def _mint_job_id(device_key: str, name: str) -> str:
    from qactl.dnos.cli.core.jobs import JobRegistry

    return JobRegistry.make_job_id(device_key, name)


def _new_job(*, mode: str, device_key: str, build_url: Optional[str],
             branch: Optional[str]) -> Dict[str, Any]:
    phases = _PHASES_BUILD if mode == "build" else _PHASES_LOAD
    return {
        "kind": f"orc_{mode}",
        "mode": mode,                 # "load" | "build"
        "job_id": _mint_job_id(device_key, f"orc-{mode}"),
        "device": device_key,
        "worker_pid": None,
        "status": "running",          # ok | running | error
        "phase": phases[0],           # current / terminal phase
        "phase_plan": list(phases),
        "build_url": build_url,
        "branch": branch,
        "phases": {},                 # per-phase envelope (slimmed for build)
        "errors": [],
        "next_actions": [],
        "started_utc": _now(),
        "updated_utc": _now(),
    }


def _orc_envelope(job: Dict[str, Any]) -> Dict[str, Any]:
    """Build the response/persistence envelope for a job.

    Pollable identity fields (``job_id`` / ``device`` / ``worker_pid`` /
    ``phase``) are kept at the TOP level so :mod:`job_store` can key and
    device-match on them (mirrors the tar-load envelope), while ``result``
    carries the same summary for a normal ``--json`` reader.
    """
    env = make_envelope(kind=job["kind"])
    env["status"] = job["status"]
    summary = {
        "job_id": job["job_id"],
        "mode": job["mode"],
        "device": job["device"],
        "worker_pid": job.get("worker_pid"),
        "phase": job["phase"],
        "phase_plan": job["phase_plan"],
        "branch": job.get("branch"),
        "build_url": job.get("build_url"),
        "phases": job["phases"],
        "started_utc": job["started_utc"],
        "updated_utc": job["updated_utc"],
    }
    # Top-level pollable fields for job_store (save keys on job_id; device
    # match uses top-level device/host).
    env.update({
        "job_id": job["job_id"],
        "device": job["device"],
        "worker_pid": job.get("worker_pid"),
        "state": job["status"],
        "phase": job["phase"],
    })
    env["result"] = summary
    env["errors"] = list(job.get("errors", []))
    env["next_actions"] = list(job.get("next_actions", []))
    return env


def _persist(job: Dict[str, Any]) -> Dict[str, Any]:
    job["updated_utc"] = _now()
    env = _orc_envelope(job)
    job_store.save(env, subdir=_ORC_SUBDIR)
    return env


def _fail(job: Dict[str, Any], phase: str, errors: List[str]) -> Dict[str, Any]:
    job["phase"] = phase
    job["status"] = "error"
    job["errors"] = [f"[{phase}] {e}" for e in (errors or ["failed"])]
    job["next_actions"] = [
        f"Inspect the failing phase: qactl orc show {job['job_id']} --json "
        f"| jq '.result.phases.{phase}'",
    ]
    return _persist(job)


def _slim_jenkins(env: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the useful bits of a jenkins-trigger envelope (drop the bulky
    raw build blob) for the persisted orc record."""
    res = env.get("result") or {}
    build = res.get("build") or {}
    return {
        "status": env.get("status"),
        "branch": res.get("branch"),
        "build_number": res.get("build_number"),
        "build_url": res.get("build_url"),
        "result": build.get("result"),
        "errors": env.get("errors") or [],
    }


def _do_build(branch: str, trigger_kwargs: Dict[str, Any]):
    """Trigger the (single, shared) Jenkins build and wait for it.

    Returns ``(build_url, build_slim, errors)`` — ``build_url`` is ``None``
    and ``errors`` non-empty on any failure."""
    from qactl.jenkins import tools as jt

    benv = jt.jenkins_trigger(branch, confirm=True, wait=True, **trigger_kwargs)
    slim = _slim_jenkins(benv)
    if benv.get("status") not in ("ok", "warning"):
        return None, slim, (benv.get("errors") or ["Jenkins build did not succeed."])
    build_url = (benv.get("result") or {}).get("build_url")
    if not build_url:
        return None, slim, ["Build succeeded but no build_url in the Jenkins envelope."]
    return build_url, slim, []


def _drive_device(
    job: Dict[str, Any], build_url: str, *,
    tarload_kwargs: Dict[str, Any], precheck_kwargs: Dict[str, Any],
    dev_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the load + pre-check phases for ONE device's job, persisting after
    each. The build phase (if any) is done once by the caller and stamped into
    ``job['phases']['build']`` before this runs."""
    try:
        # --- phase: load -----------------------------------------------
        job["phase"] = "load"
        job["status"] = "running"
        _persist(job)
        from qactl.dnos.cli.tools.tarload import request_system_tar_load

        lenv = request_system_tar_load(
            jenkins_url=build_url, pre_check=False, confirm=True, block=True,
            **tarload_kwargs, **dev_kwargs,
        )
        job["phases"]["load"] = lenv
        if lenv.get("status") != "ok":
            return _fail(job, "load", lenv.get("errors") or ["tar-load did not complete."])
        _persist(job)

        # --- phase: pre_check ------------------------------------------
        job["phase"] = "pre_check"
        _persist(job)
        from qactl.dnos.cli.tools.tarload import request_system_pre_check

        penv = request_system_pre_check(block=True, **precheck_kwargs, **dev_kwargs)
        job["phases"]["pre_check"] = penv
        if penv.get("status") != "ok":
            return _fail(job, "pre_check", penv.get("errors") or ["pre-check did not pass."])

        # --- done ------------------------------------------------------
        job["phase"] = "done"
        job["status"] = "ok"
        return _persist(job)
    except BaseException as e:  # noqa: BLE001 — never let a phase raise past the driver
        return _fail(job, job.get("phase") or "orc", [f"{type(e).__name__}: {str(e)[:240]}"])


def _drive(specs: List[Dict[str, Any]], *, branch: Optional[str],
           build_url: Optional[str], trigger_kwargs: Dict[str, Any]) -> None:
    """Drive one or more device jobs: build ONCE (shared), then load +
    pre-check per device, serially. Each ``spec`` is ``{job, tarload_kwargs,
    precheck_kwargs, dev_kwargs}``. Persists progress per job; returns
    nothing (callers read the mutated job dicts / persisted envelopes)."""
    jobs = [s["job"] for s in specs]
    shared_url = build_url

    if branch:
        # A single build feeds every device. Mark all jobs 'build' first.
        for j in jobs:
            j["phase"] = "build"
            j["status"] = "running"
            _persist(j)
        shared_url, build_slim, errors = _do_build(branch, trigger_kwargs)
        for j in jobs:
            j["phases"]["build"] = build_slim
        if not shared_url:
            for j in jobs:
                _fail(j, "build", errors)
            return
        for j in jobs:
            j["build_url"] = shared_url
            _persist(j)

    # Load + pre-check each device in turn (serial: per-device tar-load is
    # already strictly serial; different devices are independent but we keep
    # one worker simple by sequencing them).
    for s in specs:
        _drive_device(
            s["job"], shared_url,
            tarload_kwargs=s["tarload_kwargs"], precheck_kwargs=s["precheck_kwargs"],
            dev_kwargs=s["dev_kwargs"],
        )


def _reset_transports() -> None:
    """After a fork, abandon inherited SSH transports (their reader threads
    don't survive fork) so device phases reconnect fresh."""
    try:
        from qactl.dnos.cli.core.registry import transport_registry

        transport_registry.reset_after_fork()
    except Exception:  # noqa: BLE001 — best-effort
        pass


def _spawn_detached(specs: List[Dict[str, Any]], *, branch: Optional[str],
                    build_url: Optional[str], trigger_kwargs: Dict[str, Any]) -> Optional[int]:
    """Fork a session-detached child that runs :func:`_drive` over every job.

    Returns the child pid (parent side), or ``None`` on a platform without
    ``os.fork`` (the caller then falls back to a blocking run). The child
    persists progress so ``orc show`` from any later process can poll it, and
    detaches from the caller's session/TTY so an agent shell timeout killing
    the parent can't take the run down with it."""
    if not hasattr(os, "fork"):
        return None
    # Ordered handshake: the child blocks until the parent has persisted every
    # kickoff snapshot (with worker_pid) and closed the write end, so no child
    # write can race ahead of the parent's snapshots.
    r, w = os.pipe()
    pid = os.fork()
    if pid > 0:
        os.close(r)
        for spec in specs:
            job = spec["job"]
            job["worker_pid"] = pid
            job["status"] = "running"
            job["next_actions"] = [
                f"Poll with: qactl orc show {job['job_id']}  "
                f"(or: qactl orc show -d {job['device']})",
            ]
            _persist(job)
        os.close(w)  # EOF releases the child
        return pid
    # --- child: never returns ------------------------------------------
    os.close(w)
    try:
        os.read(r, 1)  # blocks until the parent closes w (EOF)
    except OSError:
        pass
    finally:
        try:
            os.close(r)
        except OSError:
            pass
    exit_code = 0
    try:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)
        # This process IS the worker — record our own pid on every job so the
        # envelopes the child persists carry the LIVE pid (the parent set it on
        # its own copies before the fork handshake; the child's copies still
        # held None), letting a later poll tell a live run from a dead one.
        for spec in specs:
            spec["job"]["worker_pid"] = os.getpid()
        _reset_transports()
        _drive(specs, branch=branch, build_url=build_url, trigger_kwargs=trigger_kwargs)
    except BaseException:  # noqa: BLE001 — nothing may escape past os._exit
        exit_code = 1
    finally:
        os._exit(exit_code)
    return 0  # unreachable


def _summary_envelope(mode: str, branch: Optional[str], build_url: Optional[str],
                      jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A roll-up envelope for a multi-device orc run (one row per device)."""
    statuses = [j["status"] for j in jobs]
    if any(s == "running" for s in statuses):
        status = "running"
    elif all(s == "ok" for s in statuses):
        status = "ok"
    else:
        status = "error"
    env = make_envelope(kind=f"orc_{mode}")
    env["status"] = status
    env["result"] = {
        "mode": mode,
        "branch": branch,
        "build_url": build_url,
        "devices": [j["device"] for j in jobs],
        "jobs": [
            {"job_id": j["job_id"], "device": j["device"],
             "phase": j["phase"], "status": j["status"]}
            for j in jobs
        ],
    }
    env["next_actions"] = [
        "Poll a device: qactl orc show -d <dev>   |   all: qactl jobs list --kind orc",
    ]
    return env


def _targets(devices: Optional[List[str]], host: Optional[str],
             user: Optional[str], password: Optional[str]) -> List[Dict[str, Any]]:
    """Normalise the requested devices into a list of dev_kwargs dicts (one per
    target). ``--user`` / ``--password`` apply to every target."""
    base: Dict[str, Any] = {}
    if user:
        base["user"] = user
    if password:
        base["password"] = password
    out: List[Dict[str, Any]] = []
    seen = set()
    for d in devices or []:
        if d in seen:
            continue
        seen.add(d)
        out.append({**base, "device": d})
    if host and host not in seen:
        out.append({**base, "host": host})
    return out


def _launch(
    *, mode: str, build_url: Optional[str], branch: Optional[str],
    devices: Optional[List[str]], host: Optional[str], user: Optional[str],
    password: Optional[str], components: Optional[List[str]],
    detach: bool, trigger_kwargs: Dict[str, Any], precheck_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    targets = _targets(devices, host, user, password)
    if not targets:
        return error_envelope(
            "orc needs at least one target device: pass -d/--device (repeatable) or --host.",
            kind=f"orc_{mode}", status="bad_argument",
        )
    tarload_kwargs: Dict[str, Any] = {}
    if components:
        tarload_kwargs["components"] = list(components)

    specs: List[Dict[str, Any]] = []
    for dev_kwargs in targets:
        device_key = dev_kwargs.get("device") or dev_kwargs.get("host") or ""
        specs.append({
            "job": _new_job(mode=mode, device_key=device_key,
                            build_url=build_url, branch=branch),
            "dev_kwargs": dev_kwargs,
            "tarload_kwargs": tarload_kwargs,
            "precheck_kwargs": precheck_kwargs,
        })
    jobs = [s["job"] for s in specs]

    if detach:
        pid = _spawn_detached(specs, branch=branch, build_url=build_url,
                              trigger_kwargs=trigger_kwargs)
        if pid is not None:
            # Detached launch SUCCEEDED — the response reports the launch
            # (status ok / exit 0), while each per-job snapshot keeps its real
            # ``running`` state for pollers. Otherwise a good kickoff would
            # exit non-zero just because the job hasn't finished.
            res = _result(mode, branch, build_url, jobs)
            res["status"] = "ok"
            return res
        # No os.fork on this platform — fall back to a blocking run.
        _drive(specs, branch=branch, build_url=build_url, trigger_kwargs=trigger_kwargs)
    else:
        _drive(specs, branch=branch, build_url=build_url, trigger_kwargs=trigger_kwargs)

    return _result(mode, branch, build_url, jobs)


def _result(mode: str, branch: Optional[str], build_url: Optional[str],
            jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Single device → the flat single-job envelope; multiple → a roll-up."""
    if len(jobs) == 1:
        return _orc_envelope(jobs[0])
    return _summary_envelope(mode, branch, build_url, jobs)


# ---- public entry points -------------------------------------------------


def _device_list(device: Optional[str], devices: Optional[List[str]]) -> List[str]:
    out = list(devices or [])
    if device and device not in out:
        out.insert(0, device)
    return out


def orc_load(
    build_url: str, *,
    device: Optional[str] = None, devices: Optional[List[str]] = None,
    host: Optional[str] = None, user: Optional[str] = None,
    password: Optional[str] = None, components: Optional[List[str]] = None,
    detach: bool = False,
    pre_check_poll_interval_s: Optional[int] = None,
    pre_check_max_wait_s: Optional[int] = None,
) -> Dict[str, Any]:
    """Tar-load an existing build on one or more devices, then pre-check each.
    Blocking by default (``detach=True`` forks a session-detached worker)."""
    precheck_kwargs: Dict[str, Any] = {}
    if pre_check_poll_interval_s is not None:
        precheck_kwargs["pre_check_poll_interval_s"] = pre_check_poll_interval_s
    if pre_check_max_wait_s is not None:
        precheck_kwargs["pre_check_max_wait_s"] = pre_check_max_wait_s
    return _launch(
        mode="load", build_url=build_url, branch=None,
        devices=_device_list(device, devices), host=host, user=user, password=password,
        components=components, detach=detach,
        trigger_kwargs={}, precheck_kwargs=precheck_kwargs,
    )


def orc_build(
    branch: str, *,
    device: Optional[str] = None, devices: Optional[List[str]] = None,
    host: Optional[str] = None, user: Optional[str] = None,
    password: Optional[str] = None, components: Optional[List[str]] = None,
    detach: bool = True, repo: str = "cheetah", org: str = "drivenets",
    wait_timeout: float = 4 * 3600, poll: float = 30.0,
    trigger_extra: Optional[Dict[str, Any]] = None,
    pre_check_poll_interval_s: Optional[int] = None,
    pre_check_max_wait_s: Optional[int] = None,
) -> Dict[str, Any]:
    """Trigger ONE cheetah build, wait for it, then tar-load + pre-check on
    every requested device. Detached by default (a build can run hours);
    ``detach=False`` blocks."""
    trigger_kwargs: Dict[str, Any] = {
        "repo": repo, "org": org, "wait_timeout": wait_timeout, "poll": poll,
    }
    if trigger_extra:
        trigger_kwargs.update(trigger_extra)
    precheck_kwargs: Dict[str, Any] = {}
    if pre_check_poll_interval_s is not None:
        precheck_kwargs["pre_check_poll_interval_s"] = pre_check_poll_interval_s
    if pre_check_max_wait_s is not None:
        precheck_kwargs["pre_check_max_wait_s"] = pre_check_max_wait_s
    return _launch(
        mode="build", build_url=None, branch=branch,
        devices=_device_list(device, devices), host=host, user=user, password=password,
        components=components, detach=detach,
        trigger_kwargs=trigger_kwargs, precheck_kwargs=precheck_kwargs,
    )


def orc_show(
    job_id: Optional[str] = None, device: Optional[str] = None,
) -> Dict[str, Any]:
    """Poll an orc job by ``job_id`` or the latest for ``device``.

    A job persisted as ``running`` whose worker process is gone is reported
    as ``error`` (it died mid-flight) rather than ``running`` forever.
    """
    if not job_id and not device:
        return error_envelope(
            "Pass a job_id, or -d/--device to look up the latest orc job on it.",
            kind="orc_show", status="bad_argument",
        )
    env = (
        job_store.load(job_id, subdir=_ORC_SUBDIR) if job_id
        else job_store.latest_for_device(device or "", subdir=_ORC_SUBDIR)
    )
    if env is None:
        return error_envelope(
            "No orc job on record for that "
            + ("job_id." if job_id else "device."),
            kind="orc_show", status="error",
            next_actions=["Start one with: qactl orc load <build-url> -d <dev>  "
                          "or  qactl orc build <branch> -d <dev>"],
        )
    # Only downgrade when we have a concrete pid that is provably dead. A
    # missing/None pid means "can't tell" (e.g. an in-flight envelope written
    # before the worker recorded its pid) — leave it as running rather than
    # falsely flagging a live run as dead.
    pid = env.get("worker_pid")
    if env.get("status") == "running" and pid and not _pid_alive(pid):
        env["status"] = "error"
        env["state"] = "error"
        env.setdefault("errors", []).append(
            "orchestration worker process is gone — the job died mid-flight."
        )
    return env


__all__ = ["orc_load", "orc_build", "orc_show"]
