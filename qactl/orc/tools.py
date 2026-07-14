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


def _drive(
    job: Dict[str, Any], *,
    build_url: Optional[str],
    branch: Optional[str],
    trigger_kwargs: Dict[str, Any],
    tarload_kwargs: Dict[str, Any],
    precheck_kwargs: Dict[str, Any],
    dev_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the phases in order, persisting after each. Returns the terminal
    envelope. Runs inline (blocking mode) or inside the detached child."""
    try:
        # --- phase: build (orc build only) -----------------------------
        if branch:
            job["phase"] = "build"
            job["status"] = "running"
            _persist(job)
            from qactl.jenkins import tools as jt

            benv = jt.jenkins_trigger(branch, confirm=True, wait=True, **trigger_kwargs)
            job["phases"]["build"] = _slim_jenkins(benv)
            if benv.get("status") not in ("ok", "warning"):
                return _fail(job, "build",
                             benv.get("errors") or ["Jenkins build did not succeed."])
            build_url = (benv.get("result") or {}).get("build_url")
            job["build_url"] = build_url
            if not build_url:
                return _fail(job, "build",
                             ["Build succeeded but no build_url in the Jenkins envelope."])
            _persist(job)

        # --- phase: load -----------------------------------------------
        job["phase"] = "load"
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


def _reset_transports() -> None:
    """After a fork, abandon inherited SSH transports (their reader threads
    don't survive fork) so device phases reconnect fresh."""
    try:
        from qactl.dnos.cli.core.registry import transport_registry

        transport_registry.reset_after_fork()
    except Exception:  # noqa: BLE001 — best-effort
        pass


def _spawn_detached(job: Dict[str, Any], drive_kwargs: Dict[str, Any]) -> Optional[int]:
    """Fork a session-detached child that runs :func:`_drive` to completion.

    Returns the child pid (parent side), or ``None`` on a platform without
    ``os.fork`` (the caller then falls back to a blocking run). The child
    persists progress via :func:`_persist` so ``orc show`` from any later
    process can poll it, and detaches from the caller's session/TTY so an
    agent shell timeout killing the parent can't take the run down with it.
    """
    if not hasattr(os, "fork"):
        return None
    # Ordered handshake: the child blocks until the parent has persisted the
    # kickoff snapshot (with worker_pid) and closed the write end, so no child
    # write can race ahead of the parent's snapshot.
    r, w = os.pipe()
    pid = os.fork()
    if pid > 0:
        os.close(r)
        job["worker_pid"] = pid
        job["status"] = "running"
        job["next_actions"] = [
            f"Poll with: qactl orc show {job['job_id']}  (or: qactl orc show -d {job['device']})",
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
        _reset_transports()
        _drive(job, **drive_kwargs)
    except BaseException:  # noqa: BLE001 — nothing may escape past os._exit
        exit_code = 1
    finally:
        os._exit(exit_code)
    return 0  # unreachable


def _launch(
    *, mode: str, build_url: Optional[str], branch: Optional[str],
    device: Optional[str], host: Optional[str], user: Optional[str],
    password: Optional[str], components: Optional[List[str]],
    detach: bool, trigger_kwargs: Dict[str, Any], precheck_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    device_key = device or host or ""
    if not device_key:
        return error_envelope(
            "orc needs a target device: pass -d/--device (or --host).",
            kind=f"orc_{mode}", status="bad_argument",
        )
    dev_kwargs: Dict[str, Any] = {}
    if device:
        dev_kwargs["device"] = device
    if host:
        dev_kwargs["host"] = host
    if user:
        dev_kwargs["user"] = user
    if password:
        dev_kwargs["password"] = password
    tarload_kwargs: Dict[str, Any] = {}
    if components:
        tarload_kwargs["components"] = list(components)

    job = _new_job(mode=mode, device_key=device_key, build_url=build_url, branch=branch)
    drive_kwargs = dict(
        build_url=build_url, branch=branch, trigger_kwargs=trigger_kwargs,
        tarload_kwargs=tarload_kwargs, precheck_kwargs=precheck_kwargs,
        dev_kwargs=dev_kwargs,
    )
    if detach:
        pid = _spawn_detached(job, drive_kwargs)
        if pid is None:
            # No os.fork on this platform — fall back to a blocking run.
            return _drive(job, **drive_kwargs)
        return _orc_envelope(job)
    return _drive(job, **drive_kwargs)


# ---- public entry points -------------------------------------------------


def orc_load(
    build_url: str, *,
    device: Optional[str] = None, host: Optional[str] = None,
    user: Optional[str] = None, password: Optional[str] = None,
    components: Optional[List[str]] = None, detach: bool = False,
    pre_check_poll_interval_s: Optional[int] = None,
    pre_check_max_wait_s: Optional[int] = None,
) -> Dict[str, Any]:
    """Tar-load an existing build, then run the pre-check. Blocking by
    default (``detach=True`` forks a session-detached worker)."""
    precheck_kwargs: Dict[str, Any] = {}
    if pre_check_poll_interval_s is not None:
        precheck_kwargs["pre_check_poll_interval_s"] = pre_check_poll_interval_s
    if pre_check_max_wait_s is not None:
        precheck_kwargs["pre_check_max_wait_s"] = pre_check_max_wait_s
    return _launch(
        mode="load", build_url=build_url, branch=None,
        device=device, host=host, user=user, password=password,
        components=components, detach=detach,
        trigger_kwargs={}, precheck_kwargs=precheck_kwargs,
    )


def orc_build(
    branch: str, *,
    device: Optional[str] = None, host: Optional[str] = None,
    user: Optional[str] = None, password: Optional[str] = None,
    components: Optional[List[str]] = None, detach: bool = True,
    repo: str = "cheetah", org: str = "drivenets",
    wait_timeout: float = 4 * 3600, poll: float = 30.0,
    trigger_extra: Optional[Dict[str, Any]] = None,
    pre_check_poll_interval_s: Optional[int] = None,
    pre_check_max_wait_s: Optional[int] = None,
) -> Dict[str, Any]:
    """Trigger a cheetah build, wait for it, then tar-load + pre-check.
    Detached by default (a build can run hours); ``detach=False`` blocks."""
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
        device=device, host=host, user=user, password=password,
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
    if env.get("status") == "running" and not _pid_alive(env.get("worker_pid")):
        env["status"] = "error"
        env["state"] = "error"
        env.setdefault("errors", []).append(
            "orchestration worker process is gone — the job died mid-flight."
        )
    return env


__all__ = ["orc_load", "orc_build", "orc_show"]
