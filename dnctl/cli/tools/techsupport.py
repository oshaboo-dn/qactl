"""Tech-support MCP tools — async, ``BaseJob``-backed.

Two tools:

- ``create_techsupport`` — kicks off DNOS ``request system tech-support``,
  spawns a daemon worker that polls ``show system tech-support status``
  until DNOS prints ``Tech-support file generated at ...`` for *our*
  run, then runs ``request file upload tech-support ... protocol sftp
  vrf ...`` to land the tarball on ``dnftp``. Returns FAST (~2 s) with
  ``status:"running"`` + ``job_id``.
- ``get_techsupport_job`` — in-memory lookup for that job's current
  envelope.

The "long-running, returns immediately, agent polls later" shape is the
same one tar-load uses; the registry implementation
(:class:`dnctl.cli.core.jobs.JobRegistry`) is shared. One active ts per device
at a time, enforced via ``_TS_REGISTRY.active_for_device`` (separate
registry instance from tar-load so a stuck ts can't block a tar-load
kickoff and vice versa).

Status-probe resilience: each poll cycle's ``show system tech-support
status`` opens a fresh SSH channel; DNOS occasionally fails to push a
CLI prompt onto that channel within the banner window while it's busy
collecting a tech-support. We retry the probe up to
:data:`_TS_PROBE_RETRY_MAX` times within one cycle (force-dropping the
cached SSH transport between attempts) and tolerate up to
:data:`_TS_PROBE_MAX_CONSECUTIVE_FAILURES` fully-failed cycles before
declaring the job errored.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from dnctl.cli.core import slack_notify, ts_store
from dnctl.cli.core.dnftp import DNFTP_PASSWORD, DNFTP_VRF, build_upload_command
from dnctl.cli.core.envelope import error_response, make_response
from dnctl.cli.core.errors import CREATE_TS_NEXT_ACTION, detect_error
from dnctl.cli.core.jobs import BaseJob, JobRegistry
from dnctl.cli.core.logging import log_invocation, log_request
from dnctl.cli.core.redact import scrub_password, scrub_steps
from dnctl.cli.core.registry import transport_registry
from dnctl.cli.core.session import (
    DEFAULT_PASSWORD,
    DEFAULT_USER,
    ConnectError,
    run_once,
    run_sequence_pw,
)
from dnctl.cli.core.validation import _int_in


# Minimum plausible size for a DNOS tech-support tar. Real tech-supports are
# hundreds of MB to several GB; anything below this means the transfer
# silently truncated or never actually happened.
_TS_MIN_BYTES = 1_000_000

# Completion signal printed by ``show system tech-support status`` once the
# async tech-support generation finishes.
_TS_DONE_RE = re.compile(
    r"^Tech-support file generated at\s+(?P<ts>.+?)\s*$", re.MULTILINE,
)
_TS_FILENAME_RE = re.compile(r"^File name\s+(?P<fn>\S+)\s*$", re.MULTILINE)
_TS_LOCATION_RE = re.compile(r"^File location\s+(?P<loc>\S+)\s*$", re.MULTILINE)

_TS_POLL_MIN_S, _TS_POLL_MAX_S = 10, 300
_TS_WAIT_MIN_S, _TS_WAIT_MAX_S = 120, 7200

# Tech-support generation on a big chassis can easily take ~10 min; default
# 30 min cap, clamped to [2 min, 2 h].
_TS_DEFAULT_POLL_S = 30
_TS_DEFAULT_MAX_WAIT_S = 30 * 60

# Keep terminal (done/error/timeout) jobs in memory this long so a late
# ``get_techsupport_job`` can still fetch the result. Reaped lazily.
_TS_JOB_TTL_S = 24 * 3600

# Status-probe resilience knobs. The probe opens a fresh ephemeral
# channel each cycle and has to drain the SSH banner + detect a CLI
# prompt; DNOS occasionally fails to push a prompt onto a freshly-
# opened channel while busy collecting a tech-support. Treat that as
# transient: retry within the cycle, tolerate a small run of fully-
# failed cycles, only escalate to terminal ``error`` after a longer
# run of failures.
_TS_PROBE_RETRY_MAX = 3
_TS_PROBE_RETRY_BACKOFF_S = 5
_TS_PROBE_MAX_CONSECUTIVE_FAILURES = 5
# Soft cap on ``job.warnings`` so a long, very flaky generation doesn't
# balloon memory. We keep the most recent entries (drop oldest on overflow).
_TS_WARNINGS_MAX = 50


@dataclass
class TsJob(BaseJob):
    """One tech-support run tracked by ``_TS_REGISTRY``.

    All extra fields are TS-specific. Common metadata
    (``job_id`` / ``device`` / ``state`` / ``warnings`` / ...) lives
    on ``BaseJob``. Defaults are placeholders: every kickoff path
    sets these explicitly.
    """

    name: str = ""
    local_filename: str = ""
    ts_path: str = ""        # scp-style URL: dn@dnftp:/ftpdisk/dn/oshaboo/ts/<file>
    vrf: str = ""
    poll_interval_s: int = 0
    max_wait_s: int = 0
    timeout: int = 0
    poll_count: int = 0
    device_filename: Optional[str] = None
    device_location: Optional[str] = None
    size_bytes: Optional[int] = None
    # Number of consecutive poll cycles whose status-probe failed end-to-end
    # (every immediate retry within the cycle threw). Reset to 0 the moment
    # a probe succeeds. The worker escalates to a terminal ``error`` state
    # once this exceeds ``_TS_PROBE_MAX_CONSECUTIVE_FAILURES``.
    consecutive_probe_failures: int = 0


_TS_REGISTRY = JobRegistry(
    ttl_s=_TS_JOB_TTL_S,
    terminal_states=frozenset({"done", "error", "timeout"}),
    active_states=frozenset({"generating", "uploading"}),
)


def _ts_job_envelope(job: TsJob) -> Dict[str, Any]:
    """Project a ``TsJob`` into the tool-response envelope shape."""
    env: Dict[str, Any] = {
        "job_id": job.job_id,
        "state": job.state,
        "device": job.device,
        "host": job.resolved_host or (job.host or ""),
        "name": job.name,
        "command": job.command,
        "started_utc": job.started_utc,
        "local_filename": job.local_filename,
        "ts_path": job.ts_path,
        "poll_count": job.poll_count,
        "stdout": job.stdout,
        "warnings": list(job.warnings),
        "errors": list(job.errors),
        "next_actions": list(job.next_actions),
    }
    if job.completed_utc:
        env["completed_utc"] = job.completed_utc
    if job.elapsed_s is not None:
        env["elapsed_s"] = job.elapsed_s
    if job.device_filename:
        env["device_filename"] = job.device_filename
    if job.device_location:
        env["device_location"] = job.device_location
    if job.size_bytes is not None:
        env["size_bytes"] = job.size_bytes
    # Map internal state → outer ``status`` for the envelope contract.
    if job.state == "done":
        env["status"] = "ok"
    elif job.state in _TS_REGISTRY.active_states:
        env["status"] = "running"
    else:
        env["status"] = job.state  # error | timeout
    return env


def _ts_drop_transport(job: TsJob) -> None:
    """Drop the cached SSH transport for ``job``'s device.

    Used when a status-probe channel keeps failing on what looks like
    a half-dead transport — re-auth from scratch on the next attempt
    is the cheapest way to recover, and matches what a human would do
    by reaching for a fresh ssh session.
    """
    try:
        key = (job.device or job.host or "", job.user)
        transport_registry.drop(key, reason="ts-probe-retry")
    except Exception:  # noqa: BLE001 - drop is best-effort
        pass


def _ts_run_status_probe(
    job: TsJob,
    password: str,
    status_cmd: str,
):
    """Run the tech-support status probe with immediate retries.

    ``run_once`` opens a fresh ephemeral channel, drains the SSH banner,
    detects a CLI prompt, and only then sends the command. Any of those
    steps can flake — most often DNOS fails to push a prompt onto a
    freshly-opened channel within :data:`DEFAULT_BANNER_WAIT` while it
    is busy collecting a tech-support, raising
    ``RuntimeError("Could not detect CLI prompt on fresh channel")``.

    Treat that as transient: the device is alive, generation is still
    running, only the probe channel itself flaked. We try up to
    :data:`_TS_PROBE_RETRY_MAX` times within one poll cycle, sleeping
    :data:`_TS_PROBE_RETRY_BACKOFF_S` between attempts and force-dropping
    the cached SSH transport between attempts so the next try re-auths
    from scratch.

    Returns the :class:`Invocation` on success, or ``None`` if every
    immediate retry failed. In the latter case ``job.warnings`` has
    been appended with one diagnostic line per attempt; the worker
    then increments ``job.consecutive_probe_failures`` and only
    escalates to a terminal ``error`` after
    :data:`_TS_PROBE_MAX_CONSECUTIVE_FAILURES` cycles fail end-to-end.
    """
    for attempt in range(1, _TS_PROBE_RETRY_MAX + 1):
        try:
            return run_once(
                transport_registry,
                device=job.device, host=job.host,
                user=job.user, password=password,
                command=status_cmd, timeout=job.timeout,
            )
        except ConnectError as exc:
            kind, err_msg = "connect", str(exc)
        except Exception as exc:  # noqa: BLE001 - probe must never crash worker
            # Most importantly catches ``RuntimeError("Could not detect
            # CLI prompt on fresh channel")`` from session._init_channel.
            kind, err_msg = "probe", str(exc)
        suffix = (
            "retrying with a fresh transport"
            if attempt < _TS_PROBE_RETRY_MAX
            else "giving up this cycle, will retry on the next poll"
        )
        _TS_REGISTRY.append_bounded(
            job.warnings,
            f"status-probe #{job.poll_count}.{attempt}/"
            f"{_TS_PROBE_RETRY_MAX} ({kind}): {err_msg} — {suffix}",
            _TS_WARNINGS_MAX,
        )
        _ts_drop_transport(job)
        if attempt < _TS_PROBE_RETRY_MAX:
            time.sleep(_TS_PROBE_RETRY_BACKOFF_S)
    return None


def _ts_fmt_elapsed(seconds: Optional[int]) -> str:
    if not seconds or seconds < 0:
        return "?"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _ts_notify_kickoff(job: TsJob) -> None:
    """Post the parent kickoff Slack message; stash thread_ts on the job."""
    if not job.notify_channel:
        return
    text = (
        f":construction: *ts generating* — `{job.device_key}` "
        f"(name=`{job.name}`, job_id=`{job.job_id}`)\n"
        f"_ETA ~10 min, will land at `{job.ts_path}`_"
    )
    r = slack_notify.post(job.notify_channel, text)
    if r.get("ts"):
        job.notify_thread_ts = r["ts"]
    if r.get("error"):
        _TS_REGISTRY.append_bounded(
            job.warnings, f"slack notify (kickoff): {r['error']}",
            _TS_WARNINGS_MAX,
        )


def _ts_notify_terminal(job: TsJob, final_state: str) -> None:
    """Post the terminal Slack message in the kickoff thread."""
    if not job.notify_channel:
        return
    icon = {
        "done": ":white_check_mark:",
        "error": ":x:",
        "timeout": ":hourglass:",
    }.get(final_state, ":question:")
    elapsed = _ts_fmt_elapsed(job.elapsed_s)
    if final_state == "done":
        size_mb = (
            f"{(job.size_bytes or 0) / (1024*1024):.0f} MB"
            if job.size_bytes else "?"
        )
        text = (
            f"{icon} *ts done* — `{job.device_key}` in {elapsed} — "
            f"{size_mb} at `{job.ts_path}`"
        )
    else:
        last_err = job.errors[-1] if job.errors else final_state.upper()
        text = (
            f"{icon} *ts {final_state}* — `{job.device_key}` after {elapsed}\n"
            f"_err: {last_err}_"
        )
    r = slack_notify.post(
        job.notify_channel, text,
        thread_ts=job.notify_thread_ts or None,
    )
    if r.get("error"):
        _TS_REGISTRY.append_bounded(
            job.warnings, f"slack notify (terminal): {r['error']}",
            _TS_WARNINGS_MAX,
        )


def _ts_finish(job: TsJob, final_state: str) -> None:
    """Wrap ``_TS_REGISTRY.finish`` + Slack terminal notify.

    Use this everywhere the worker reaches a terminal state so we
    never miss a Slack post.
    """
    _TS_REGISTRY.finish(job, final_state)
    _ts_notify_terminal(job, final_state)


def _ts_poll_and_upload_worker(
    job: TsJob,
    password: str,
    name_marker: str,
    status_cmd: str,
    started_dt: datetime,
) -> None:
    """Background driver: poll ``show system tech-support status`` → upload → stat.

    Runs in a daemon thread spawned by ``create_techsupport`` AFTER the
    kickoff has succeeded. Every mutation of ``job.*`` is an individual
    attribute write, which is safe against concurrent reads from
    ``get_techsupport_job`` (Python attribute writes are atomic; readers
    may observe a mid-flight state, which is fine — that's the whole
    point of a progress tool).
    """
    deadline = started_dt + timedelta(seconds=job.max_wait_s)
    last_status_out = ""
    device_src: Optional[str] = None
    device_loc: Optional[str] = None
    completed_dt: Optional[datetime] = None

    try:
        while datetime.now(timezone.utc) < deadline:
            time.sleep(job.poll_interval_s)
            job.poll_count += 1

            # Probe with immediate retries. Returns None iff every retry
            # within this cycle failed; that's transient by default — we
            # only escalate to a terminal error after a run of consecutive
            # fully-failed cycles (see _TS_PROBE_MAX_CONSECUTIVE_FAILURES).
            probe = _ts_run_status_probe(job, password, status_cmd)
            if probe is None:
                job.consecutive_probe_failures += 1
                if (
                    job.consecutive_probe_failures
                    >= _TS_PROBE_MAX_CONSECUTIVE_FAILURES
                ):
                    job.errors.append(
                        f"Status probe failed for "
                        f"{job.consecutive_probe_failures} consecutive "
                        f"poll cycles ({_TS_PROBE_RETRY_MAX} immediate "
                        f"retries each). Giving up — see warnings for "
                        f"per-attempt detail. Tech-support generation "
                        f"may still complete on the device; verify with "
                        f"`{status_cmd}` and the .tar landing at "
                        f"{job.ts_path!r}."
                    )
                    job.next_actions.append(CREATE_TS_NEXT_ACTION)
                    _ts_finish(job, "error")
                    log_request("create_techsupport", {
                        "job_id": job.job_id, "device": job.device,
                        "name": job.name,
                    }, _ts_job_envelope(job))
                    return
                # Transient: the device is presumably still generating;
                # come back next cycle.
                continue

            # Probe came back — generation is still being talked to.
            job.consecutive_probe_failures = 0
            last_status_out = probe.output
            if not probe.hit_prompt:
                # Channel opened but command timed out before a trailing
                # prompt — also transient. Don't terminate.
                _TS_REGISTRY.append_bounded(
                    job.warnings,
                    f"status-probe #{job.poll_count}: opened channel but "
                    f"timed out waiting for prompt after {job.timeout}s; "
                    f"will retry on next poll.",
                    _TS_WARNINGS_MAX,
                )
                continue

            # The probe succeeded. Two things to look for in the output:
            #
            # 1. An explicit DNOS error (``% Error``, ``Error:``, etc.).
            #    That's the *device* telling us generation failed; treat
            #    it as terminal. This is the second case in the user's
            #    request: distinguish "probe could not connect" from
            #    "device says generation failed" — only the second errors.
            # 2. The "Tech-support file generated at ..." completion
            #    marker for *our* run (matched on ``name_marker``).
            is_err, err_lines = detect_error(probe.output)
            if is_err:
                job.stdout = probe.output
                job.command = status_cmd
                job.errors.extend(err_lines[-5:])
                job.errors.append(
                    "Device reported an error in `show system "
                    "tech-support status`; tech-support generation "
                    "appears to have failed on the device."
                )
                job.next_actions.append(CREATE_TS_NEXT_ACTION)
                _ts_finish(job, "error")
                log_request("create_techsupport", {
                    "job_id": job.job_id, "device": job.device,
                    "name": job.name,
                }, _ts_job_envelope(job))
                return

            done_m = _TS_DONE_RE.search(probe.output)
            fn_m = _TS_FILENAME_RE.search(probe.output)
            loc_m = _TS_LOCATION_RE.search(probe.output)
            if done_m and fn_m and name_marker in fn_m.group("fn"):
                device_src = fn_m.group("fn")
                device_loc = loc_m.group("loc") if loc_m else None
                completed_dt = datetime.now(timezone.utc)
                break

        if device_src is None or completed_dt is None:
            job.stdout = last_status_out or job.stdout
            job.command = status_cmd
            job.errors.append(
                f"Tech-support generation did not complete within "
                f"{job.max_wait_s}s (polled {job.poll_count} time(s)). Run "
                f"{status_cmd!r} manually to check current state."
            )
            job.next_actions.append(CREATE_TS_NEXT_ACTION)
            _ts_finish(job, "timeout")
            log_request("create_techsupport", {
                "job_id": job.job_id, "device": job.device,
                "name": job.name,
            }, _ts_job_envelope(job))
            return

        job.device_filename = device_src
        job.device_location = device_loc
        job.completed_utc = completed_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        job.elapsed_s = int((completed_dt - started_dt).total_seconds())

        # --- upload --------------------------------------------------------
        job.state = "uploading"
        upload_cmd = build_upload_command(
            kind="tech-support",
            local_name=device_src,
            remote_path=ts_store.remote_path(job.local_filename),
            vrf=job.vrf,
        )
        job.command = upload_cmd
        try:
            up = run_sequence_pw(
                transport_registry,
                device=job.device, host=job.host,
                user=job.user, password=password,
                commands=[(upload_cmd, DNFTP_PASSWORD)],
                timeout=job.timeout,
            )
        except ConnectError as exc:
            job.errors.append(str(exc))
            job.next_actions.append(
                "Verify the device is reachable and credentials are correct.",
            )
            _ts_finish(job, "error")
            log_request("create_techsupport", {
                "job_id": job.job_id, "device": job.device,
                "name": job.name,
            }, _ts_job_envelope(job))
            return
        except Exception as exc:
            job.errors.append(str(exc))
            _ts_finish(job, "error")
            log_request("create_techsupport", {
                "job_id": job.job_id, "device": job.device,
                "name": job.name,
            }, _ts_job_envelope(job))
            return

        if up.host:
            job.resolved_host = up.host
        scrubbed = scrub_password(up.output, DNFTP_PASSWORD)
        job.stdout = scrubbed
        log_invocation(
            up.device or job.device, up.host,
            upload_cmd, scrubbed,
            up.head_prompt_line, up.tail_prompt,
            steps=scrub_steps(up.steps, DNFTP_PASSWORD),
        )

        if not up.hit_prompt:
            job.errors.append(
                f"Timed out waiting for CLI prompt after {job.timeout}s "
                f"on upload."
            )
            job.next_actions.append(CREATE_TS_NEXT_ACTION)
            _ts_finish(job, "timeout")
            log_request("create_techsupport", {
                "job_id": job.job_id, "device": job.device,
                "name": job.name,
            }, _ts_job_envelope(job))
            return

        is_err, err_lines = detect_error(scrubbed)
        if is_err:
            job.errors.extend(err_lines[-5:])
            job.next_actions.append(CREATE_TS_NEXT_ACTION)
            _ts_finish(job, "error")
            log_request("create_techsupport", {
                "job_id": job.job_id, "device": job.device,
                "name": job.name,
            }, _ts_job_envelope(job))
            return

        # --- stat the local file -------------------------------------------
        stat = ts_store.stat_ts(job.local_filename)
        if stat is None:
            job.errors.append(
                f"Upload completed without error but {job.local_filename!r} "
                f"is not present on {ts_store.TS_HOST}:{ts_store.TS_DIR} "
                "(MCP-side SFTP stat returned no file) — check sshd "
                "landing directory and that the MCP can SFTP into "
                f"{ts_store.TS_HOST} with the {ts_store.TS_USER} account."
            )
            job.next_actions.append(CREATE_TS_NEXT_ACTION)
            _ts_finish(job, "error")
            log_request("create_techsupport", {
                "job_id": job.job_id, "device": job.device,
                "name": job.name,
            }, _ts_job_envelope(job))
            return
        if stat.size_bytes < _TS_MIN_BYTES:
            job.errors.append(
                f"Uploaded file {job.local_filename!r} is suspiciously small "
                f"({stat.size_bytes} bytes). Treating as a failed transfer."
            )
            job.next_actions.append(CREATE_TS_NEXT_ACTION)
            _ts_finish(job, "error")
            log_request("create_techsupport", {
                "job_id": job.job_id, "device": job.device,
                "name": job.name,
            }, _ts_job_envelope(job))
            return

        # ``ts_path`` was populated at kickoff with the scp-style URL and
        # stays stable; we just record the on-disk size here.
        job.size_bytes = stat.size_bytes
        _ts_finish(job, "done")
        log_request("create_techsupport", {
            "job_id": job.job_id, "device": job.device,
            "name": job.name,
        }, _ts_job_envelope(job))
    except Exception as exc:  # noqa: BLE001 - defensive: never leak a thread crash
        job.errors.append(f"worker crashed: {exc!r}")
        _ts_finish(job, "error")
        log_request("create_techsupport", {
            "job_id": job.job_id, "device": job.device,
            "name": job.name,
        }, _ts_job_envelope(job))


def create_techsupport(
    name: str,
    device: Optional[str] = None,
    host: Optional[str] = None,
    vrf: str = DNFTP_VRF,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    poll_interval_s: int = _TS_DEFAULT_POLL_S,
    max_wait_s: int = _TS_DEFAULT_MAX_WAIT_S,
    timeout: int = 120,
    notify_slack: str = "@oshaboo",
) -> Dict[str, Any]:
    """Start a tech-support tarball generation on a device; returns IMMEDIATELY.

    Short name: ``ts`` (i.e. "take a ts on <device>" / "grab a ts"
    / "ts it" all map to this tool).

    ASYNC / NON-BLOCKING. Kickoff is synchronous (auth + ``request system
    tech-support``, ~2 s), the poll-generation + upload + stat phases run
    in a background daemon thread. The tool itself returns a small envelope
    in ~2 s with ``status:"running"``, ``state:"generating"``, and a
    ``job_id``. The final ``.tar`` lands at ``ts_path``, given as an
    scp-style URL ``dn@dnftp:/ftpdisk/dn/oshaboo/ts/<file>``;
    this is computed before the kickoff and guaranteed stable.

    Envelope carries ``eta_s: 600`` (10 min) as a rough guide — it
    covers most devices we've seen but isn't a guarantee (big chassis
    can run longer). ``max_wait_s`` is the hard cap. Tell the user
    "about 10 minutes" and poll ``get_techsupport_job`` around then.

    Follow-up flow for the agent:
        1. Call this tool → get ``{job_id, state:"generating",
           ts_path}``. Tell the user "ts in progress, about 10 min,
           will land at ``ts_path``." Don't block the chat; the user
           can do other things.
        2. When the user asks for the result, call
           ``get_techsupport_job(job_id=...)`` or
           ``get_techsupport_job(device=...)`` once. That call is
           always fast (in-memory lookup; it doesn't touch the device).
        3. When ``state:"done"`` the envelope has the full final shape
           (``size_bytes``, ``elapsed_s``, ``device_filename``, etc.).
           On ``state:"error"`` / ``"timeout"``, ``errors`` explains why.

    Guards:
        - One active ts per device. A second ``create_techsupport`` for
          a device that already has ``state in {generating, uploading}``
          is rejected INSTANTLY with the existing ``job_id``; it doesn't
          queue and it doesn't wait.
        - No config lock is taken. Ts doesn't touch running config so
          it doesn't serialise against backup / restore / edit_config.

    Kickoff command on the device is
    ``set cli-no-confirm ; request system tech-support <name>`` on one
    ephemeral channel; the ``cli-no-confirm`` preface suppresses the
    "Previous techsupport file exist, are you sure you want to delete
    (yes/no)?" prompt DNOS raises when ``/techsupport/`` isn't empty.
    DNOS acknowledges with ``<TS> System is generating a Tech-support
    file`` (and sometimes ``Tech-support collection started`` — we
    accept either).

    The destination ``.tar`` is always named
    ``<device>__<UTC-YYYYMMDD-HHMMSS>__<name>.tar`` under
    ``/ftpdisk/dn/oshaboo/ts/`` on ``dnftp`` (a separate SFTP host —
    NOT the MCP host). The background worker polls ``show system
    tech-support status`` every ``poll_interval_s`` (default 30 s)
    until DNOS prints ``Tech-support file generated at ...`` with a
    ``File name`` whose DNOS-local-time stem contains the sanitised
    ``name`` (guards against stale prior files), then runs
    ``request file upload tech-support <device-src>
    dn@dnftp:/.../<local> protocol sftp vrf <vrf>``. After upload the
    MCP opens its own SFTP session to ``dnftp`` to stat the landed
    file and transitions the job to ``done`` (min 1 MB enforced).

    Probe resilience: each poll cycle's status probe opens a fresh
    SSH channel; DNOS occasionally fails to push a CLI prompt onto
    that channel within the banner window while it's busy collecting
    a tech-support. The worker retries the probe up to 3 times within
    one cycle (small backoff between, and the cached SSH transport is
    force-dropped between attempts so the next try re-auths from
    scratch), and tolerates up to 5 fully-failed cycles before
    declaring the job errored. Per-attempt detail lands in the
    envelope's ``warnings`` list. A successful probe whose output
    contains a DNOS error line (``% ...`` / ``Error: ...``) is the
    only "device says generation failed" path that terminates the
    job early.

    Jobs are kept in memory for 24 h after completion so a late
    ``get_techsupport_job`` can still fetch results; a server restart
    drops them. The ``.tar`` on disk and ``mcp-logs/*.jsonl`` are the
    durable record.

    Args:
        name: Tech-support name, fed verbatim to DNOS. Sanitised to
            ``[A-Za-z0-9._-]{1,40}``; must be non-empty after sanitisation.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        vrf: DNOS VRF used to reach the backup host. Default ``mgmt0``.
        user: SSH username on the device (default dnroot).
        password: SSH password on the device (default dnroot).
        poll_interval_s: Seconds between ``show system tech-support
            status`` probes. Clamped to [10, 300]. Default 30.
        max_wait_s: Hard cap on generation wall-time. Clamped to
            [120, 7200]. Default 1800 (30 min).
        timeout: Per-command SSH timeout seconds (kickoff, each status
            probe, upload — each independently).
    """
    # --- validate args ---------------------------------------------------
    device_key = device or host or ""
    err = ts_store.validate_device(device_key)
    if err:
        return error_response(
            err, device=device, host=host, next_action=CREATE_TS_NEXT_ACTION,
        )

    err = ts_store.validate_name(name)
    if err:
        return error_response(
            err, device=device, host=host, next_action=CREATE_TS_NEXT_ACTION,
        )
    clean_name = ts_store.sanitise_name(name)

    err = _int_in("poll_interval_s", poll_interval_s, _TS_POLL_MIN_S, _TS_POLL_MAX_S)
    if err:
        return error_response(
            err, device=device, host=host, next_action=CREATE_TS_NEXT_ACTION,
        )
    err = _int_in("max_wait_s", max_wait_s, _TS_WAIT_MIN_S, _TS_WAIT_MAX_S)
    if err:
        return error_response(
            err, device=device, host=host, next_action=CREATE_TS_NEXT_ACTION,
        )

    try:
        local_name = ts_store.make_filename(device_key, clean_name)
    except ValueError as exc:
        return error_response(
            str(exc), device=device, host=host,
            next_action=CREATE_TS_NEXT_ACTION,
        )

    kickoff_cmd = f"request system tech-support {clean_name}"
    kickoff_commands: List[Tuple[str, Optional[str]]] = [
        ("set cli-no-confirm", None),
        (kickoff_cmd, None),
    ]
    status_cmd = "show system tech-support status"
    remote = ts_store.remote_url(local_name)
    # Substring that must appear in the DNOS-generated filename to prove
    # the ``status`` output refers to *our* run (DNOS bakes the name as
    # ``ts_<name>_HH_MM_SS_DD-MM-YYYY.tar`` so surrounding underscores
    # make the match unambiguous).
    name_marker = f"_{clean_name}_"

    # --- claim the device slot ------------------------------------------
    active = _TS_REGISTRY.active_for_device(device_key)
    if active is not None and active.state in _TS_REGISTRY.active_states:
        return error_response(
            f"A tech-support is already running on {device_key!r} "
            f"(job_id={active.job_id}, state={active.state}, "
            f"started_utc={active.started_utc}). Call "
            f"get_techsupport_job(job_id={active.job_id}) to check "
            "on it, or wait for it to finish before starting a new one.",
            device=device, host=host,
            next_action=(
                f"get_techsupport_job(job_id={active.job_id!r})"
            ),
        )

    request = {
        "device": device, "host": host, "user": user,
        "name": clean_name, "vrf": vrf, "local_filename": local_name,
        "poll_interval_s": poll_interval_s, "max_wait_s": max_wait_s,
    }

    # --- step 1: kick off (SYNC, ~2 s) ----------------------------------
    response = make_response(
        device=device, host=host, command=kickoff_cmd,
        name=clean_name, local_filename=local_name,
    )
    try:
        result = run_sequence_pw(
            transport_registry,
            device=device, host=host, user=user, password=password,
            commands=kickoff_commands,
            timeout=timeout,
        )
    except ConnectError as exc:
        response.update(
            status="connect_error",
            errors=[str(exc)],
            next_actions=[
                "Verify the device is reachable and credentials are correct.",
            ],
        )
        log_request("create_techsupport", request, response)
        return response
    except Exception as exc:
        response.update(status="error", errors=[str(exc)])
        log_request("create_techsupport", request, response)
        return response

    kickoff_out = result.output
    response["host"] = result.host
    response["device"] = result.device or device
    response["stdout"] = kickoff_out
    log_invocation(
        result.device or device, result.host,
        kickoff_cmd, kickoff_out,
        result.head_prompt_line, result.tail_prompt,
    )

    if not result.hit_prompt:
        response["status"] = "timeout"
        response["errors"].append(
            f"Timed out waiting for CLI prompt after {timeout}s "
            f"on kickoff ({kickoff_cmd!r})."
        )
        response["next_actions"].append(CREATE_TS_NEXT_ACTION)
        log_request("create_techsupport", request, response)
        return response

    is_err, err_lines = detect_error(kickoff_out)
    if is_err:
        response["status"] = "error"
        response["errors"].extend(err_lines[-5:])
        response["next_actions"].append(CREATE_TS_NEXT_ACTION)
        log_request("create_techsupport", request, response)
        return response

    kickoff_ack = (
        "System is generating a Tech-support file" in kickoff_out
        or "Tech-support collection started" in kickoff_out
    )
    if not kickoff_ack:
        response["status"] = "error"
        tail = [ln.strip() for ln in kickoff_out.splitlines() if ln.strip()]
        response["errors"].append(
            "Kickoff output did not contain a tech-support acknowledgement "
            "('System is generating a Tech-support file' or 'Tech-support "
            "collection started') — the device may have refused the "
            "request (e.g. another generation already in progress)."
        )
        response["errors"].extend(tail[-5:])
        response["next_actions"].append(CREATE_TS_NEXT_ACTION)
        log_request("create_techsupport", request, response)
        return response

    # --- step 2: register the job + spawn the worker --------------------
    started = datetime.now(timezone.utc)
    job_id = _TS_REGISTRY.make_job_id(device_key, clean_name)
    job = TsJob(
        job_id=job_id,
        device=device,
        host=host,
        device_key=device_key,
        resolved_host=result.host or "",
        state="generating",
        started_utc=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        user=user,
        command=kickoff_cmd,
        stdout=kickoff_out,
        name=clean_name,
        local_filename=local_name,
        ts_path=remote,
        vrf=vrf,
        poll_interval_s=poll_interval_s,
        max_wait_s=max_wait_s,
        timeout=timeout,
        notify_channel=notify_slack,
    )
    _TS_REGISTRY.register(job)
    _ts_notify_kickoff(job)

    worker = threading.Thread(
        target=_ts_poll_and_upload_worker,
        name=f"ts-{job_id}",
        args=(job, password, name_marker, status_cmd, started),
        daemon=True,
    )
    worker.start()

    envelope = _ts_job_envelope(job)
    # Rough ETA: 10 min covers most devices we've seen. Not a tight
    # guarantee — big chassis can run longer; ``max_wait_s`` is the
    # hard cap. Callers should poll ``get_techsupport_job`` around
    # this mark rather than treat it as a promise.
    envelope["eta_s"] = 600
    envelope["next_actions"] = [
        f"Tech-support generation running in background on {device_key}. "
        f"Typical ETA ~10 min (hard cap max_wait_s={max_wait_s}s). Call "
        f"get_techsupport_job(job_id={job_id!r}) around then to check "
        f"status. Final tar will land at {job.ts_path!r}.",
    ]
    log_request("create_techsupport", request, envelope)
    return envelope


def get_techsupport_job(
    job_id: Optional[str] = None,
    device: Optional[str] = None,
) -> Dict[str, Any]:
    """Look up a tech-support job started by ``create_techsupport``.

    Returns the current envelope (same shape as ``create_techsupport``'s
    final success envelope, plus ``job_id`` and ``state``). Safe to
    call at any cadence — it only touches the in-memory registry, it
    doesn't talk to the device.

    Lookup rules:
        - If ``job_id`` is given, return that exact job (or an error
          envelope if it was reaped / never existed).
        - Else if ``device`` is given, return the currently-active job
          for that device, or the most recent terminal job if none is
          active.
        - At least one of ``job_id`` / ``device`` must be provided.

    ``state`` values:
        ``generating`` — kickoff succeeded, poll loop waiting for DNOS
            to finish collecting. Transient probe failures (channel
            init, prompt-detect timeouts) are tolerated for several
            cycles and surfaced as ``warnings``, NOT as ``error`` —
            the device often keeps generating fine even when the probe
            channel flakes.
        ``uploading``  — DNOS reports done, background worker is SFTPing
            the .tar to the MCP host.
        ``done``       — .tar on disk at ``ts_path``, stat checked
            (>= 1 MB). Envelope has ``size_bytes``, ``elapsed_s``, etc.
        ``timeout``    — generation or upload exceeded ``max_wait_s``
            (generation) or per-command ``timeout`` (upload).
        ``error``      — terminal failure: either the device explicitly
            reported a CLI error in the status output, the upload
            failed, or the status probe failed end-to-end for many
            consecutive cycles. See ``errors`` (terminal cause) and
            ``warnings`` (per-cycle probe diagnostics).

    Terminal jobs (done/error/timeout) stay in memory for 24 h after
    they finish, then are lazily reaped.
    """
    job, err = _TS_REGISTRY.lookup(job_id=job_id, device_key=device)
    if err is not None:
        if not job_id and not device:
            return error_response(
                err,
                next_action=(
                    "Pass job_id returned by create_techsupport, or device "
                    "to look up the active/latest ts job on that device."
                ),
            )
        if job_id and "No job with" in err:
            return error_response(
                err.replace("No job with", "No tech-support job with"),
                next_action=(
                    "Check the MCP request log for the original "
                    "create_techsupport envelope, or start a new one."
                ),
            )
        if "No job on record" in err:
            return error_response(
                err.replace("No job on record", "No tech-support job on record"),
                device=device,
                next_action=(
                    "Start one with create_techsupport(device=..., name=...)."
                ),
            )
        return error_response(err, device=device)

    assert job is not None  # narrow type for the linter; lookup guarantees this
    envelope = _ts_job_envelope(job)
    log_request(
        "get_techsupport_job",
        {"job_id": job_id, "device": device},
        envelope,
    )
    return envelope


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(create_techsupport)
    mcp.tool()(get_techsupport_job)
