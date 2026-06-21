"""Image-upgrade-staging MCP tools — async, ``BaseJob``-backed.

Three tools:

- ``request_system_tar_load`` — fetches the per-build artifact URLs
  from a cheetah Jenkins build, probes the device with
  ``show system`` to learn whether it's deployed DNOS or Genesis Image
  (GI), and runs the on-device upload sequence:

      [set cli-no-confirm]                            # DNOS only — GI rejects 'set'
      [show system target-stack pre-check]            # snapshot prior task id
      [request system target-stack load <base_os_url>]
      request system target-stack load <dnos_url>
      request system target-stack load <gi_url>
      [request system target-stack pre-check]         # kickoff (skipped on GI)

  …then optionally polls ``show system target-stack pre-check`` until
  a fresh task reaches a terminal status. The kickoff returns FAST
  (~3-5 s) with ``status:"running"`` + ``job_id``; the slow part runs
  in a daemon thread.
- ``request_system_pre_check`` — same async-job shape as
  ``request_system_tar_load``, but skips the Jenkins fetches and the
  ``request system target-stack load`` steps. Use when the tarballs
  are already staged on the box and you just want a fresh pre-check
  verdict (e.g. after a previous ``pre_check=False`` kickoff in a
  two-build flow). Rejected on GI (pre-check is a DNOS construct).
- ``get_tar_load_job`` — in-memory lookup for a job's current envelope
  (covers both ``request_system_tar_load`` and
  ``request_system_pre_check`` — same registry).

Same async-job shape as :mod:`dnctl.cli.tools.techsupport` (independent
:class:`dnctl.cli.core.jobs.JobRegistry` instance so a stuck tar-load can't
block a tech-support kickoff and vice versa). One active tar-load per
device at a time, enforced via ``_TARLOAD_REGISTRY.active_for_device``.

Tar-load uses ``request system target-stack load <minio-url>`` — the
device pulls directly from minio. That's a different grammar from
``request file upload|download protocol sftp ...`` (handled in
:mod:`dnctl.cli.core.dnftp`); tar-load has no need for it.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from dnctl.cli.core import job_store, slack_notify
from dnctl.cli.core.envelope import error_response
from dnctl.cli.core.errors import REQUEST_TAR_LOAD_NEXT_ACTION, detect_error
from dnctl.cli.core.jobs import BaseJob, JobRegistry
from dnctl.cli.core.logging import log_invocation, log_request
from dnctl.cli.core.registry import transport_registry
from dnctl.cli.core.session import (
    DEFAULT_CMD_TIMEOUT,
    DEFAULT_PASSWORD,
    DEFAULT_USER,
    ConnectError,
    run_once,
    run_sequence,
)


# Only Jenkins URLs from the dev jenkins host are accepted. Keeps the tool
# from being repurposed as a generic outbound HTTP fetcher and pins the set
# of artifact servers the device will be told to download from.
_JENKINS_HOST = "jenkins.dev.drivenets.net"
_JENKINS_URL_RE = re.compile(
    r"^https://" + re.escape(_JENKINS_HOST) + r"/[^\s?#]+/\d+/?$"
)
# What we expect inside a gi_*.txt artifact file — a single http(s) URL,
# typically pointing at minio. Reject anything with whitespace / quotes /
# control bytes so the URL can be safely interpolated into a CLI line.
_TAR_URL_RE = re.compile(r"^https?://[A-Za-z0-9._\-:/%?&=+@~!,;]+$")
_TAR_URL_MAX_LEN = 1024

_DEFAULT_TAR_STEP_TIMEOUT = 1800  # 30 min — multi-GB tars over the dev LAN.

# `request system target-stack pre-check` is asynchronous: kickoff returns
# immediately and the verdict is only visible via
# `show system target-stack pre-check`. Pre-check is fast in practice (well
# under a minute on a healthy chassis) but can stretch on slow boxes —
# default 10 min cap, polled every 10 s.
_PRECHECK_POLL_S_MIN, _PRECHECK_POLL_S_MAX = 5, 60
_PRECHECK_WAIT_S_MIN, _PRECHECK_WAIT_S_MAX = 60, 3600
_DEFAULT_PRECHECK_POLL_S = 10
_DEFAULT_PRECHECK_WAIT_S = 600

# Terminal jobs are kept in memory for 24 h after they finish so a late
# ``get_tar_load_job`` call can still fetch results. Reaped lazily by
# the registry. Mirrors ``_TS_JOB_TTL_S`` for symmetry.
_TARLOAD_JOB_TTL_S = 24 * 3600

# Rough ETA emitted in the kickoff envelope. Each ``target-stack load``
# is typically <2 min on the dev LAN; pre-check usually finishes in
# under a minute. 600 s covers most cases including pre-check; without
# pre-check we drop to 360 s. Not a guarantee — ``step_timeout`` and
# ``pre_check_max_wait_s`` are the hard caps.
_TARLOAD_ETA_WITHOUT_PRECHECK_S = 360
_TARLOAD_ETA_WITH_PRECHECK_S = 600

# Always emit fully-spelled commands. DNOS abbreviation tables differ
# between deployed and GI modes (e.g. ``tar`` is not a valid prefix in
# every release), and the device echoes the un-abbreviated form anyway.
_PRECHECK_SHOW_CMD = "show system target-stack pre-check"
_PRECHECK_KICKOFF_CMD = "request system target-stack pre-check"
_TAR_LOAD_CMD = "request system target-stack load"
# Suppresses ``Continue? [y/n]:`` confirmation prompts on subsequent
# commands. DNOS-only — the GI (Genesis Image) shell rejects ``set``
# commands and leaves the channel hanging on whatever it printed back,
# so we MUST NOT send this on a GI box.
_NO_CONFIRM_CMD = "set cli-no-confirm"

# `show system` reliably distinguishes a deployed box (full DNOS) from
# a box still in the Genesis Image (GI — the bootstrap environment
# that runs before / between DNOS deployments). Pre-check is a DNOS
# construct, so we don't kick it off on a GI box.
#
# Deployed DNOS prints a ``Version: DNOS [...]`` line; GI does not.
# Equivalent to:
#   show system | grep -q '^Version: DNOS' && echo deployed || echo gi
_DNOS_VERSION_RE = re.compile(r"(?m)^Version:\s+DNOS\b")
_SHOW_SYSTEM_CMD = "show system"

# DNOS refuses to re-register a tarball the device is already pulling:
#   "error downloading package. error: file is already registered for
#    download"
# For staging purposes that is NOT a failure — the file is already
# (being) downloaded onto the box, which is exactly the end state a load
# step wants. detect_error() flags it (it matches the generic
# ``error downloading package`` pattern), so without this the whole
# sequence aborts and every retry re-hits the same line — leaving the
# device permanently un-loadable until the registration clears (issue
# #17's "can't upload anything" symptom). Treat it as already-staged and
# continue to the next component.
_ALREADY_REGISTERED_RE = re.compile(r"(?i)already registered for download")

# Valid values for the ``components`` arg. Order matters: it controls
# the on-device load order (``base_os`` is loaded first when present —
# DNOS expects it underneath the DNOS / GI tarballs). Aliases are
# accepted on input but normalised to the canonical names below before
# any further use.
_COMPONENT_ORDER: Tuple[str, ...] = ("base_os", "dnos", "gi")
_COMPONENT_ALIASES: Dict[str, str] = {
    "base_os": "base_os", "baseos": "base_os", "base-os": "base_os",
    "dnos": "dnos",
    "gi": "gi",
}
# Per-component Jenkins artifact filename. Each holds a single
# ``http(s)://...`` URL pointing at the actual tar (typically minio).
_COMPONENT_ARTIFACT: Dict[str, str] = {
    "base_os": "gi_base_os_artifact.txt",
    "dnos": "gi_DNOS_artifact.txt",
    "gi": "gi_GI_artifact.txt",
}


def _normalise_components(
    components: Optional[List[str]],
) -> Tuple[Optional[List[str]], Optional[str]]:
    """Validate + canonicalise the ``components`` arg.

    Returns ``(normalised_list_or_None, error_message_or_None)``:

    - ``components is None``  →  ``(None, None)`` — caller falls back
      to "all-as-available" (legacy behaviour: base_os is optional;
      DNOS / GI are required).
    - non-empty list of valid names  →  canonical, de-duplicated list
      ordered by ``_COMPONENT_ORDER``; every listed component is then
      treated as REQUIRED (404 from Jenkins becomes a hard error,
      not a warning).
    - anything else  →  ``(None, "error message")``.
    """
    if components is None:
        return None, None
    if not isinstance(components, list) or not components:
        return None, (
            "components must be a non-empty list of "
            f"{sorted(set(_COMPONENT_ALIASES.values()))} (or omit to load all)."
        )
    seen: Dict[str, None] = {}
    for c in components:
        if not isinstance(c, str):
            return None, (
                f"components entries must be strings; got {type(c).__name__}."
            )
        canon = _COMPONENT_ALIASES.get(c.strip().lower().replace("-", "_"))
        if canon is None:
            return None, (
                f"components: unknown value {c!r}. Valid: "
                f"{sorted(set(_COMPONENT_ALIASES.values()))}."
            )
        seen[canon] = None
    return [c for c in _COMPONENT_ORDER if c in seen], None


def _detect_device_mode(probe_output: str, head_prompt: str) -> str:
    """Classify a device as ``"gi"`` (Genesis Image) or ``"deployed"``
    from the output of ``show system``.

    Rule: a ``Version: DNOS [...]`` line means DNOS is installed and
    the box is deployed; its absence means the box is in GI mode.

    ``head_prompt`` is accepted for API stability but no longer
    consulted — the ``show system`` body is authoritative.
    """
    del head_prompt  # signal kept in signature for callers
    if probe_output and _DNOS_VERSION_RE.search(probe_output):
        return "deployed"
    return "gi"


@dataclass
class TarLoadJob(BaseJob):
    """One ``request_system_tar_load`` run tracked by ``_TARLOAD_REGISTRY``.

    All extra fields are tar-load-specific. Common metadata
    (``job_id`` / ``device`` / ``state`` / ``warnings`` / ...) lives on
    ``BaseJob``. Defaults are placeholders: every kickoff path sets
    these explicitly.
    """

    jenkins_url: str = ""
    base_os_url: Optional[str] = None
    dnos_url: Optional[str] = None
    gi_url: Optional[str] = None
    # Either the canonicalised list (e.g. ``["dnos","gi"]``) or
    # the literal string ``"all"`` when the caller didn't restrict.
    components_requested: Any = "all"
    device_mode: str = "deployed"            # deployed | gi
    pre_check_requested: bool = False        # the user's arg
    effective_pre_check: bool = False        # forced False on GI
    step_timeout: int = 0
    pre_check_poll_interval_s: int = 0
    pre_check_max_wait_s: int = 0
    # Per-step transcript built once the worker finishes the load
    # sequence. Each entry is ``{"command", "status", "errors", "stdout"}``.
    steps: List[Dict[str, Any]] = field(default_factory=list)
    # Pre-check sub-envelope (state / task_id / task_status / result /
    # elapsed_s / poll_count / stdout). Populated when the worker
    # transitions out of ``loading`` into ``precheck`` and finalises.
    pre_check: Dict[str, Any] = field(default_factory=dict)


_TARLOAD_REGISTRY = JobRegistry(
    ttl_s=_TARLOAD_JOB_TTL_S,
    terminal_states=frozenset({"done", "error", "timeout"}),
    active_states=frozenset({"loading", "precheck"}),
)


def _tarload_job_envelope(job: TarLoadJob) -> Dict[str, Any]:
    """Project a ``TarLoadJob`` into the tool-response envelope shape."""
    components_requested = job.components_requested
    if isinstance(components_requested, list):
        components_requested = list(components_requested)
    env: Dict[str, Any] = {
        "job_id": job.job_id,
        "state": job.state,
        "device": job.device,
        "host": job.resolved_host or (job.host or ""),
        "command": job.command,
        "started_utc": job.started_utc,
        "jenkins_url": job.jenkins_url,
        "components_requested": components_requested,
        "resolved": {
            "base_os": job.base_os_url,
            "dnos": job.dnos_url,
            "gi": job.gi_url,
        },
        "device_mode": job.device_mode,
        "steps": list(job.steps),
        "stdout": job.stdout,
        "warnings": list(job.warnings),
        "errors": list(job.errors),
        "next_actions": list(job.next_actions),
    }
    if job.pre_check:
        env["pre_check"] = dict(job.pre_check)
    if job.completed_utc:
        env["completed_utc"] = job.completed_utc
    if job.elapsed_s is not None:
        env["elapsed_s"] = job.elapsed_s
    # Map internal state → outer ``status`` for the envelope contract.
    if job.state == "done":
        env["status"] = "ok"
    elif job.state in _TARLOAD_REGISTRY.active_states:
        env["status"] = "running"
    else:
        env["status"] = job.state  # error | timeout
    return env


# Status values DNOS uses while pre-check is still running. Anything outside
# this set (most importantly ``COMPLETED``, but also any ``FAILED`` /
# ``ERROR`` / ``ABORTED`` form) is treated as terminal.
_PRECHECK_RUNNING_STATES = frozenset({
    "IN_PROGRESS", "IN-PROGRESS", "RUNNING", "PENDING", "STARTED", "QUEUED",
})

_PRECHECK_TASK_ID_RE = re.compile(
    r"^Task ID:\s*(?P<id>\d+)\s*$", re.MULTILINE,
)
_PRECHECK_TASK_STATUS_RE = re.compile(
    r"^Task status:\s*(?P<st>\S+)\s*$", re.MULTILINE,
)
_PRECHECK_RESULT_RE = re.compile(
    r"^Pre-check result:\s*(?P<r>\S+)\s*$", re.MULTILINE,
)


def _parse_precheck_show(output: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (task_id, task_status, pre_check_result) from
    ``show system target-stack pre-check`` output. Any field can be
    ``None`` when absent (empty output on a brand-new device, mid-flight
    rendering, …).
    """
    if not output:
        return None, None, None
    tid = _PRECHECK_TASK_ID_RE.search(output)
    st = _PRECHECK_TASK_STATUS_RE.search(output)
    res = _PRECHECK_RESULT_RE.search(output)
    return (
        tid.group("id") if tid else None,
        st.group("st") if st else None,
        res.group("r") if res else None,
    )


def _fetch_jenkins_artifact(
    base: str, name: str, fetch_timeout: int,
) -> Tuple[Optional[str], Optional[str]]:
    """GET ``<base>/artifact/<name>`` and return (url, error_message).

    Treats HTTP 404 as ``(None, None)`` so optional artifacts (currently
    just ``gi_base_os_artifact.txt``) don't fail the whole operation. Any
    other failure — non-200, transport error, malformed body — comes back
    as ``(None, "...")`` so the caller surfaces it.
    """
    url = f"{base.rstrip('/')}/artifact/{name}"
    try:
        with urllib.request.urlopen(url, timeout=fetch_timeout) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, None
        return None, f"GET {name}: HTTP {exc.code} {exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"GET {name}: {exc}"

    if status != 200:
        return None, f"GET {name}: HTTP {status}"

    body = body.strip()
    if not body:
        return None, None

    # Some Jenkins jobs put a comment / blank lines around the URL — pick
    # the first non-empty token that looks like a URL.
    candidate: Optional[str] = None
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        candidate = s
        break
    if not candidate:
        return None, None

    if len(candidate) > _TAR_URL_MAX_LEN:
        return None, f"{name}: tar URL is longer than {_TAR_URL_MAX_LEN} chars"
    if not _TAR_URL_RE.match(candidate):
        return None, (
            f"{name}: tar URL has invalid characters or shape "
            f"(must be plain http(s)://...)"
        )
    return candidate, None


def request_system_tar_load(
    jenkins_url: str,
    pre_check: bool = True,
    components: Optional[List[str]] = None,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    step_timeout: int = _DEFAULT_TAR_STEP_TIMEOUT,
    fetch_timeout: int = 30,
    pre_check_poll_interval_s: int = _DEFAULT_PRECHECK_POLL_S,
    pre_check_max_wait_s: int = _DEFAULT_PRECHECK_WAIT_S,
    notify_slack: str = "@oshaboo",
    block: bool = False,
) -> Dict[str, Any]:
    """Stage upgrade tarballs on a device from a cheetah Jenkins build;
    returns IMMEDIATELY (unless ``block=True``).

    ASYNC / NON-BLOCKING. Kickoff is synchronous (Jenkins-artifact
    fetches + ``show system`` device-mode probe, ~3-5 s); the slow
    on-device sequence (``set cli-no-confirm`` + per-image
    ``request system target-stack load`` + optional pre-check kickoff
    + pre-check polling) runs in a background daemon thread. The tool
    itself returns a small envelope with ``status:"running"``,
    ``state:"loading"``, and a ``job_id``.

    Each ``target-stack load`` is a multi-GB SFTP-from-minio that
    holds the SSH channel for up to ``step_timeout`` seconds (default
    1800). With three loads + a pre-check poll loop, a synchronous
    call would hold the FastMCP HTTP request for tens of minutes —
    Cursor's MCP client gives up well before that. The async pattern
    keeps the HTTP path snappy.

    Follow-up flow for the agent:
        1. Call this tool → get
           ``{job_id, state:"loading", device_mode, resolved, ...}``.
           Tell the user "tar-load in progress, ETA ~6-10 min, will
           land on the device". Don't block; the user can do other
           things.
        2. When the user asks for the result, call
           ``get_tar_load_job(job_id=...)`` or
           ``get_tar_load_job(device=...)`` once. That call is always
           fast (in-memory lookup; doesn't touch the device).
        3. When ``state:"done"`` the envelope has the full final shape
           (``steps``, ``pre_check``, ``elapsed_s``, ...). On
           ``state:"error"`` / ``"timeout"``, ``errors`` explains why.

    Guards:
        - One active tar-load per device. A second
          ``request_system_tar_load`` for a device that already has
          ``state in {loading, precheck}`` is rejected INSTANTLY with
          the existing ``job_id``; it doesn't queue and it doesn't
          wait.
        - The load + kickoff phase shares ONE ephemeral SSH channel
          (so the initial ``set cli-no-confirm`` and a load step's
          session state can't leak into other tools); pre-check
          polling runs on fresh channels reusing the same cached
          transport.

    Device-mode probe
    -----------------
    Before registering the job, the kickoff runs ``show system`` on
    its own channel to classify the box as ``"deployed"`` (full DNOS)
    or ``"gi"`` (Genesis Image — the bootstrap environment that runs
    before / between DNOS deployments). A ``Version: DNOS [...]``
    line in the output means DNOS is installed (deployed); its
    absence means the box is in GI mode. On a GI box pre-check
    doesn't exist as an operation, so
    ``request system target-stack pre-check`` + the
    ``show system target-stack pre-check`` poll loop are silently
    skipped (a warning is added and ``pre_check.state="skipped_gi"``
    in the final envelope). The image loads still run.

    Image resolution
    ----------------
    For a build URL like
    ``https://jenkins.dev.drivenets.net/job/.../dev_v26_2/907/``
    the MCP fetches:

    - ``<build>/artifact/gi_base_os_artifact.txt`` — **optional** by
      default. Older builds don't publish base-OS; HTTP 404 or empty
      body = skipped (a warning is added to the envelope).
    - ``<build>/artifact/gi_DNOS_artifact.txt`` — **required** by
      default.
    - ``<build>/artifact/gi_GI_artifact.txt`` — **required** by
      default.

    Each text file holds a single ``http(s)://...`` URL (typically a
    minio path) that is interpolated verbatim into the CLI line. URLs
    are validated against ``[A-Za-z0-9._\\-:/%?&=+@~!,;]+`` and capped
    at 1024 chars before being sent to the device.

    Pass ``components=[...]`` to fetch + load only a SUBSET (e.g.
    ``components=["dnos","gi"]`` to skip base-OS, or
    ``components=["base_os"]`` to refresh just base-OS from a
    different build). Anything explicitly listed becomes a hard
    requirement: a 404 / empty artifact for a listed component is an
    error, not a warning. Components NOT listed are silently skipped
    — their artifact isn't fetched, no ``target-stack load`` is
    issued, and the on-device staging area for that component is
    left untouched (whatever was previously loaded stays). See
    "Mixing components from multiple builds" below.

    Mixing components from multiple builds
    --------------------------------------
    A common upgrade-validation flow is to combine the DNOS + GI from
    one cheetah build with the base-OS from a different build (e.g.
    matrix-test a new base-OS layer against a known-good DNOS / GI).
    Two back-to-back kickoffs give you that:

        # Step 1 — load DNOS + GI from build A. Skip pre-check
        # because base-OS isn't there yet; pre-check would either
        # complain or validate against whatever stale base-OS was
        # last on the box.
        request_system_tar_load(
            device="cl",
            jenkins_url="https://jenkins.dev.drivenets.net/.../901/",
            components=["dnos", "gi"],
            pre_check=False,
        )
        # ...wait for state="done" via get_tar_load_job(device="cl").

        # Step 2 — load base-OS from build B and validate the
        # combined set with pre-check.
        request_system_tar_load(
            device="cl",
            jenkins_url="https://jenkins.dev.drivenets.net/.../907/",
            components=["base_os"],
            pre_check=True,
        )

    DNOS preserves the previously-staged components between calls,
    so step 2's pre-check sees the combined set (build-A DNOS / GI +
    build-B base-OS). The "one active tar-load per device" guard
    means step 2 only starts after step 1 has reached a terminal
    state — call ``get_tar_load_job(device=...)`` between the two.

    On-device sequence (single channel)
    -----------------------------------
    1. ``set cli-no-confirm``                              (DNOS only — skipped on GI; the GI shell rejects ``set`` commands)
    2. ``show system target-stack pre-check``              (snapshot prior task id; DNOS only, only when ``pre_check=True``)
    3. ``request system target-stack load <base_os_url>``  (skipped if absent / not requested via ``components``)
    4. ``request system target-stack load <dnos_url>``     (skipped if not requested via ``components``)
    5. ``request system target-stack load <gi_url>``       (skipped if not requested via ``components``)
    6. ``request system target-stack pre-check``           (kickoff; skipped if pre_check=False or GI mode)

    On GI, each load step pauses with a ``(yes/no)?`` confirmation
    prompt; the worker auto-answers ``yes`` inline (DNOS doesn't, since
    ``set cli-no-confirm`` already suppressed the prompt).

    After step 6 (when ``pre_check=True``), the tool polls
    ``show system target-stack pre-check`` every
    ``pre_check_poll_interval_s`` seconds (default 10) up to
    ``pre_check_max_wait_s`` (default 600 = 10 min). Completion = the
    parsed Task ID differs from the snapshot AND ``Task status`` is no
    longer in the running set (``IN_PROGRESS`` / ``RUNNING`` /
    ``PENDING`` / ``STARTED`` / ``QUEUED``).

    Abort-on-error: if any of steps 1–6 report a CLI error (``% ...``,
    ``Invalid input``, ``Error:`` …) the sequence stops immediately —
    subsequent ``target-stack load``s, the kickoff, and the poll loop
    are NOT run. Already-loaded images stay staged on the device;
    nothing is rolled back.

    This tool stages the install only; it does NOT call
    ``request system target-stack install`` or reboot the device. Drive
    the actual upgrade with ``request_system_restart`` (or the install
    command, when added).

    Args:
        jenkins_url: A cheetah Jenkins build URL on
            ``https://jenkins.dev.drivenets.net/...``. Must end in the
            build number (e.g. ``.../dev_v26_2/907`` or with trailing
            slash).
        pre_check: Run ``request system target-stack pre-check`` after
            all loads and wait for it to finish. Default ``True``. Set
            ``False`` to skip the kickoff + polling entirely (still
            uploads the images). Common reason to disable: the first
            of two back-to-back kickoffs that mix components from
            different builds — see "Mixing components from multiple
            builds" above.
        components: Optional list selecting which images to fetch +
            load. Accepted values: ``"base_os"`` (alias ``"baseos"``),
            ``"dnos"``, ``"gi"``. Default ``None`` reproduces the
            historical behaviour: load all available images
            (``base_os`` is optional, ``dnos`` + ``gi`` required). Any
            value listed here becomes hard-required — a 404 / empty
            artifact for that component is an error. Components NOT
            listed are silently skipped (no fetch, no on-device load;
            previously-staged copy on the device is left untouched).
            Examples: ``["dnos","gi"]``, ``["base_os"]``,
            ``["dnos"]``.
        device: Device alias (cl, sa, kira, ...).
        host: Raw hostname/IP (alternative to ``device``).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        step_timeout: Per-command timeout in seconds for the upload
            phase. Each ``target-stack load`` downloads a multi-GB tar
            over the dev LAN; default 1800.
        fetch_timeout: HTTP timeout in seconds for the Jenkins artifact
            GETs (per file). Default 30.
        pre_check_poll_interval_s: Seconds between
            ``show system target-stack pre-check`` polls. Default 10,
            clamped to [5, 60].
        pre_check_max_wait_s: Hard cap on total polling time, in
            seconds. Default 600, clamped to [60, 3600].
        block: Run the whole load (+ optional pre-check) SYNCHRONOUSLY
            and return the terminal envelope instead of the
            ``state:"loading"`` kickoff. Default ``False`` (async — the
            shape the long-running MCP server wants). The one-shot CLI
            front (``qactl cli tar-load start``) passes ``True``: its
            process *is* the worker, so it must run to completion in-line
            (otherwise the worker thread dies with the process, aborting
            the on-device load mid-download — issue #17). When ``True``
            the terminal envelope is also persisted to disk so a later
            ``tar-load show <job_id>`` resolves it.

    Returns:
        Kickoff envelope:

        - ``job_id``: opaque token; pass it to ``get_tar_load_job``.
        - ``state``: ``"loading"`` (the worker has just started).
        - ``status``: ``"running"`` while the worker is in flight.
        - ``device_mode``: ``"deployed"`` or ``"gi"``.
        - ``jenkins_url``: the (normalised) build URL.
        - ``components_requested``: the (canonicalised) ``components``
          arg, or ``"all"`` when the caller didn't restrict.
        - ``resolved``: ``{"base_os": <url|None>, "dnos": <url|None>,
          "gi": <url|None>}``. Each entry is ``null`` when that
          component wasn't requested or wasn't published by the
          build (only allowed for ``base_os`` in default mode).
        - ``command``: the on-device sequence the worker will run
          (``"; "``-joined).
        - ``eta_s``: rough ETA hint (~6 min without pre-check,
          ~10 min with).
        - ``next_actions``: a single line telling the agent to call
          ``get_tar_load_job(job_id=...)``.

        Final envelope (after ``get_tar_load_job`` once the worker
        finishes) adds:

        - ``steps``: per-step list of ``{"command", "status",
          "errors", "stdout"}`` in execution order, with aborted-skip
          steps surfaced as ``status="skipped"``.
        - ``pre_check`` (only when ``pre_check=True``):
          ``{"state", "task_id", "task_status", "result",
          "elapsed_s", "poll_count", "stdout"}``. ``state`` is
          ``"passed"`` / ``"failed"`` / ``"timeout"`` / ``"skipped"``
          / ``"skipped_gi"`` / ``"error"``.
        - ``completed_utc`` / ``elapsed_s``.
        - ``status``: ``"ok"`` iff every step ran cleanly AND, when
          pre-check was requested AND the box is deployed,
          ``Pre-check result == Succeeded``. Otherwise ``"error"`` /
          ``"timeout"``.
    """
    tool = "request_system_tar_load"
    request = {
        "device": device, "host": host, "user": user,
        "jenkins_url": jenkins_url, "pre_check": pre_check,
        "components": components,
        "step_timeout": step_timeout, "fetch_timeout": fetch_timeout,
        "pre_check_poll_interval_s": pre_check_poll_interval_s,
        "pre_check_max_wait_s": pre_check_max_wait_s,
    }

    if not isinstance(jenkins_url, str) or not jenkins_url.strip():
        return error_response(
            "jenkins_url must be a non-empty string.",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    base = jenkins_url.strip().rstrip("/")
    if not _JENKINS_URL_RE.match(base + "/"):
        return error_response(
            f"jenkins_url must point at a build under https://{_JENKINS_HOST}/ "
            "(e.g. https://jenkins.dev.drivenets.net/job/.../<job>/<build_no>/).",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if not isinstance(step_timeout, int) or isinstance(step_timeout, bool):
        return error_response(
            "step_timeout must be an integer (seconds).",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if not (60 <= step_timeout <= 7200):
        return error_response(
            "step_timeout must be in [60, 7200] seconds.",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if not isinstance(fetch_timeout, int) or isinstance(fetch_timeout, bool):
        return error_response(
            "fetch_timeout must be an integer (seconds).",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if not (1 <= fetch_timeout <= 300):
        return error_response(
            "fetch_timeout must be in [1, 300] seconds.",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if (
        not isinstance(pre_check_poll_interval_s, int)
        or isinstance(pre_check_poll_interval_s, bool)
    ):
        return error_response(
            "pre_check_poll_interval_s must be an integer (seconds).",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if not (
        _PRECHECK_POLL_S_MIN <= pre_check_poll_interval_s <= _PRECHECK_POLL_S_MAX
    ):
        return error_response(
            f"pre_check_poll_interval_s must be in "
            f"[{_PRECHECK_POLL_S_MIN}, {_PRECHECK_POLL_S_MAX}] seconds.",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if (
        not isinstance(pre_check_max_wait_s, int)
        or isinstance(pre_check_max_wait_s, bool)
    ):
        return error_response(
            "pre_check_max_wait_s must be an integer (seconds).",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if not (_PRECHECK_WAIT_S_MIN <= pre_check_max_wait_s <= _PRECHECK_WAIT_S_MAX):
        return error_response(
            f"pre_check_max_wait_s must be in "
            f"[{_PRECHECK_WAIT_S_MIN}, {_PRECHECK_WAIT_S_MAX}] seconds.",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if not device and not host:
        return error_response(
            "device or host is required.",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )

    requested_components, comp_err = _normalise_components(components)
    if comp_err is not None:
        return error_response(
            comp_err, device=device, host=host,
            next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )

    # Two modes:
    #   - default (``components=None``): historical behaviour. Fetch
    #     all three artifacts; ``base_os`` is optional (404 / empty =
    #     skip + warn), DNOS / GI are required.
    #   - explicit list: fetch ONLY the listed components, and every
    #     listed one is required (404 / empty = hard error). The
    #     caller asked for it on purpose; if it's not there, we
    #     don't silently downgrade the request.
    if requested_components is None:
        fetch_required = {"base_os": False, "dnos": True, "gi": True}
        components_label = "all"
    else:
        fetch_required = {c: True for c in requested_components}
        components_label = list(requested_components)

    resolved_urls: Dict[str, Optional[str]] = {
        "base_os": None, "dnos": None, "gi": None,
    }
    warnings: List[str] = []
    for c in _COMPONENT_ORDER:
        if c not in fetch_required:
            continue
        url, err = _fetch_jenkins_artifact(
            base, _COMPONENT_ARTIFACT[c], fetch_timeout,
        )
        if err:
            return error_response(
                err, device=device, host=host,
                next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
            )
        if not url:
            if fetch_required[c]:
                return error_response(
                    f"{_COMPONENT_ARTIFACT[c]} not found or empty "
                    f"({c} image is required for the requested "
                    f"components={components_label!r}).",
                    device=device, host=host,
                    next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
                )
            warnings.append(
                f"{_COMPONENT_ARTIFACT[c]} not published by this build "
                f"— skipping {c} load."
            )
            continue
        resolved_urls[c] = url

    base_os_url = resolved_urls["base_os"]
    dnos_url = resolved_urls["dnos"]
    gi_url = resolved_urls["gi"]

    if requested_components is not None:
        warnings.append(
            "components filter active: "
            f"{requested_components!r} — other components are NOT "
            "fetched and NOT loaded; whatever was previously staged "
            "for them on the device is left untouched."
        )

    # Probe `show system` first to learn whether the device is fully
    # deployed or still in GI mode. Pre-check exists only on deployed
    # DNOS — kicking it off on a GI box would just throw a CLI error.
    # The probe runs on its own channel; the cached transport is reused
    # by the load sequence below.
    device_mode = "deployed"
    try:
        probe = run_once(
            transport_registry,
            device=device, host=host, user=user, password=password,
            command=_SHOW_SYSTEM_CMD,
            timeout=DEFAULT_CMD_TIMEOUT,
        )
    except ConnectError as exc:
        return error_response(
            str(exc), device=device, host=host,
            next_action="Verify device is reachable and credentials are correct.",
        )
    except Exception as exc:  # noqa: BLE001 — we want to surface anything from paramiko
        return error_response(
            f"{_SHOW_SYSTEM_CMD} probe failed: {exc}",
            device=device, host=host,
            next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if probe.hit_prompt:
        device_mode = _detect_device_mode(probe.output, probe.head_prompt_line)
        log_invocation(
            probe.device or device, probe.host,
            _SHOW_SYSTEM_CMD, probe.output,
            probe.head_prompt_line, probe.tail_prompt,
            steps=probe.steps,
        )
    else:
        # Probe timed out — safer to bail than to barrel ahead blind.
        return error_response(
            f"{_SHOW_SYSTEM_CMD!r} probe timed out after "
            f"{DEFAULT_CMD_TIMEOUT}s; cannot determine device mode.",
            device=device, host=probe.host or host,
            next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )

    effective_pre_check = pre_check
    if device_mode == "gi":
        if pre_check:
            warnings.append(
                "Device is in GI mode (pre-deployment) — pre-check kickoff "
                "and polling are skipped (pre-check exists only on a "
                "deployed DNOS box)."
            )
        effective_pre_check = False
        warnings.append(
            "Device is in GI mode — 'set cli-no-confirm' is skipped (the "
            "GI shell does not support 'set' configuration commands). "
            "Each 'request system target-stack load' will pause with a "
            "'(yes/no)?' prompt; the worker answers 'yes' inline."
        )

    # Build the on-device command sequence the worker will run. We
    # build it here (in the kickoff) so the kickoff envelope can show
    # the agent exactly which commands will be issued; the worker just
    # runs the prepared list.
    #
    # 'set cli-no-confirm' is DNOS-only — the GI (Genesis Image) shell
    # does not support 'set' commands, so on a GI box we skip it. GI's
    # 'request system target-stack load' does not prompt for
    # confirmation, so dropping the suppressor is safe.
    #
    # Each ``target-stack load`` is only emitted when the corresponding
    # URL was resolved. With ``components=...`` the caller can leave
    # whole components out — the previously-staged copy on the device
    # stays in place untouched.
    commands: List[str] = []
    if device_mode == "deployed":
        commands.append(_NO_CONFIRM_CMD)
    # When we're going to kick off a fresh pre-check, snapshot the prior
    # task id FIRST so the post-kickoff poll loop knows which task is
    # "ours" vs. a leftover from an earlier upgrade.
    if effective_pre_check:
        commands.append(_PRECHECK_SHOW_CMD)
    for c in _COMPONENT_ORDER:
        url = resolved_urls.get(c)
        if url:
            commands.append(f"{_TAR_LOAD_CMD} {url}")
    if effective_pre_check:
        commands.append(_PRECHECK_KICKOFF_CMD)

    if not any(resolved_urls[c] for c in _COMPONENT_ORDER):
        return error_response(
            "components selection resolved to zero images to load; "
            "nothing to do.",
            device=device, host=host,
            next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )

    # --- claim the device slot ------------------------------------------
    device_key = device or host or ""
    active = _TARLOAD_REGISTRY.active_for_device(device_key)
    if active is not None and active.state in _TARLOAD_REGISTRY.active_states:
        return error_response(
            f"A tar-load is already running on {device_key!r} "
            f"(job_id={active.job_id}, state={active.state}, "
            f"started_utc={active.started_utc}). Call "
            f"get_tar_load_job(job_id={active.job_id!r}) to check on "
            "it, or wait for it to finish before starting a new one.",
            device=device, host=host,
            next_action=f"get_tar_load_job(job_id={active.job_id!r})",
        )

    # --- register the job + spawn the worker ----------------------------
    started = datetime.now(timezone.utc)
    # Use the build number (last path segment of the Jenkins URL) as
    # the human-friendly job-id suffix so e.g. ``cl-907-a1b2c3``.
    build_label = base.rstrip("/").rsplit("/", 1)[-1] or "build"
    job_id = _TARLOAD_REGISTRY.make_job_id(device_key, build_label)
    job = TarLoadJob(
        job_id=job_id,
        device=device,
        host=host,
        device_key=device_key,
        resolved_host=probe.host or "",
        state="loading",
        started_utc=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        user=user,
        command=" ; ".join(commands),
        warnings=list(warnings),
        jenkins_url=base,
        base_os_url=base_os_url,
        dnos_url=dnos_url,
        gi_url=gi_url,
        components_requested=components_label,
        device_mode=device_mode,
        pre_check_requested=pre_check,
        effective_pre_check=effective_pre_check,
        step_timeout=step_timeout,
        pre_check_poll_interval_s=pre_check_poll_interval_s,
        pre_check_max_wait_s=pre_check_max_wait_s,
        notify_channel=notify_slack,
    )
    _TARLOAD_REGISTRY.register(job)
    _tarload_notify_kickoff(job)

    # Persist the kickoff request shape for the audit log; the worker
    # logs the final envelope from inside the thread.
    request["job_id"] = job_id

    worker = threading.Thread(
        target=_tar_load_worker,
        name=f"tarload-{job_id}",
        args=(job, password, list(commands)),
        daemon=True,
    )
    worker.start()

    # block=True (the CLI front): run the worker to completion in this
    # process and return the terminal envelope. The CLI process IS the
    # worker — a daemon thread would die when the command returns,
    # aborting the on-device load mid-download (issue #17).
    #
    # block=False (the MCP server): settle window only. Real loads take
    # minutes, so the worker is still alive after 2 s and we fall through
    # to the async kickoff envelope. Fast-fails (device-busy refusal,
    # connect_error, bad URL rejected by DNOS) terminate in ~1-3 s; if
    # so, build the envelope AFTER the worker has finished so the kickoff
    # itself carries the terminal verdict instead of making the agent do
    # a second call.
    worker.join() if block else worker.join(timeout=2.0)

    envelope = _tarload_job_envelope(job)
    if job.state in {"done", "error", "timeout"}:
        log_request(tool, request, envelope)
        return envelope

    eta_s = (
        _TARLOAD_ETA_WITH_PRECHECK_S if effective_pre_check
        else _TARLOAD_ETA_WITHOUT_PRECHECK_S
    )
    envelope["eta_s"] = eta_s
    envelope["next_actions"] = [
        f"Tar-load running in background on {device_key} ({device_mode}). "
        f"Typical ETA ~{eta_s // 60} min (hard caps: step_timeout="
        f"{step_timeout}s/load, pre_check_max_wait_s="
        f"{pre_check_max_wait_s}s). Call "
        f"get_tar_load_job(job_id={job_id!r}) around then to check status."
    ]
    log_request(tool, request, envelope)
    return envelope


def _tarload_fmt_elapsed(seconds: Optional[int]) -> str:
    if not seconds or seconds < 0:
        return "?"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _tarload_notify_kickoff(job: TarLoadJob) -> None:
    """Post the parent kickoff Slack message; stash thread_ts on the job."""
    if not job.notify_channel:
        return
    if job.jenkins_url:
        build_label = job.jenkins_url.rstrip("/").rsplit("/", 1)[-1] or "?"
        cr = job.components_requested
        if isinstance(cr, list):
            components_label = "+".join(cr) if cr else "all"
        else:
            components_label = str(cr or "all")
        text = (
            f":construction: *tar-load loading* — `{job.device_key}` "
            f"({job.device_mode}) from build `{build_label}` "
            f"components=`{components_label}` "
            f"(job_id=`{job.job_id}`)\n"
            f"_pre_check={'on' if job.effective_pre_check else 'off'}_"
        )
    else:
        # No Jenkins URL ⇒ pre-check-only kickoff (no uploads, no build).
        text = (
            f":mag: *pre-check kickoff* — `{job.device_key}` "
            f"({job.device_mode}) (job_id=`{job.job_id}`)"
        )
    r = slack_notify.post(job.notify_channel, text)
    if r.get("ts"):
        job.notify_thread_ts = r["ts"]
    if r.get("error"):
        job.warnings.append(f"slack notify (kickoff): {r['error']}")


def _tarload_notify_terminal(job: TarLoadJob, final_state: str) -> None:
    """Post the terminal Slack message in the kickoff thread."""
    if not job.notify_channel:
        return
    icon = {
        "done": ":white_check_mark:",
        "error": ":x:",
        "timeout": ":hourglass:",
    }.get(final_state, ":question:")
    elapsed = _tarload_fmt_elapsed(job.elapsed_s)
    pre = job.pre_check
    pre_state = pre.get("state") if isinstance(pre, dict) else None
    label = "tar-load" if job.jenkins_url else "pre-check"
    if final_state == "done":
        if pre_state and pre_state not in {"skipped", "skipped_gi"}:
            text = (
                f"{icon} *{label} done* — `{job.device_key}` in {elapsed} "
                f"— pre-check `{pre_state}`"
            )
        else:
            text = (
                f"{icon} *{label} done* — `{job.device_key}` in {elapsed} "
                f"(pre-check skipped)"
            )
    else:
        last_err = job.errors[-1] if job.errors else final_state.upper()
        text = (
            f"{icon} *{label} {final_state}* — `{job.device_key}` "
            f"after {elapsed}\n_err: {last_err}_"
        )
    r = slack_notify.post(
        job.notify_channel, text,
        thread_ts=job.notify_thread_ts or None,
    )
    if r.get("error"):
        job.warnings.append(f"slack notify (terminal): {r['error']}")


def _tarload_finish(job: TarLoadJob, final_state: str) -> None:
    """Wrap ``_TARLOAD_REGISTRY.finish`` + disk persist + Slack notify.

    The disk persist is what lets ``get_tar_load_job`` resolve a job
    from a *different* process (the one-shot CLI front): the in-memory
    registry dies with the process, so the terminal envelope is cached
    under the state dir for later ``tar-load show`` calls (issue #17).
    """
    _TARLOAD_REGISTRY.finish(job, final_state)
    job_store.save(_tarload_job_envelope(job))
    _tarload_notify_terminal(job, final_state)


def _tar_load_worker(
    job: TarLoadJob,
    password: str,
    commands: List[str],
) -> None:
    """Background driver: run the on-device load sequence + pre-check
    poll for one ``request_system_tar_load`` job.

    Runs in a daemon thread spawned by the kickoff AFTER the
    Jenkins-artifact fetches and ``show system`` device-mode probe
    have succeeded. Every mutation of ``job.*`` is an individual
    attribute write, which is safe against concurrent reads from
    ``get_tar_load_job`` (Python attribute writes are atomic; readers
    may observe a mid-flight state, which is fine — that's the whole
    point of a progress tool).
    """
    started = time.time()
    tool = "request_system_tar_load"
    request_envelope = {
        "job_id": job.job_id, "device": job.device, "host": job.host,
        "jenkins_url": job.jenkins_url,
    }

    # The first command (when present) is just session setup
    # (``set cli-no-confirm``); we don't show it as a "step" to the
    # agent. Same for an error in it, which would be very surprising —
    # handled via the standard fall-through below. ``set cli-no-confirm``
    # is only sent on deployed DNOS — GI mode skips it (the GI shell
    # does not support ``set`` commands). The pre-check snapshot show
    # command is also cosmetically a "step" we surface, but its output
    # is allowed to be empty (no prior task) — only treat truly broken
    # DNOS outputs as an abort signal.
    def _stop_on_dnos_error(step) -> bool:
        if step.command == _NO_CONFIRM_CMD:
            return False
        is_err, _ = detect_error(step.output)
        # "file is already registered for download" is benign — the
        # file is already staged on the device; keep going.
        if is_err and _ALREADY_REGISTERED_RE.search(step.output or ""):
            return False
        return is_err

    try:
        result = run_sequence(
            transport_registry,
            device=job.device, host=job.host,
            user=job.user, password=password,
            commands=commands,
            timeout=job.step_timeout,
            stop_predicate=_stop_on_dnos_error,
            # GI shell rejects ``set cli-no-confirm`` so confirmation
            # prompts (``(yes/no)?``) on each ``target-stack load`` must
            # be answered inline. Deployed DNOS keeps the existing
            # ``set cli-no-confirm`` preface and doesn't need this.
            auto_confirm=(job.device_mode == "gi"),
        )
    except ConnectError as exc:
        job.errors.append(str(exc))
        job.next_actions.append(
            "Verify device is reachable and credentials are correct."
        )
        job.elapsed_s = int(time.time() - started)
        job.completed_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _tarload_finish(job, "error")
        log_request(tool, request_envelope, _tarload_job_envelope(job))
        return
    except Exception as exc:  # noqa: BLE001 — surface anything from paramiko / sequence
        job.errors.append(str(exc))
        job.elapsed_s = int(time.time() - started)
        job.completed_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _tarload_finish(job, "error")
        log_request(tool, request_envelope, _tarload_job_envelope(job))
        return

    if result.host:
        job.resolved_host = result.host
    job.stdout = result.output
    log_invocation(
        result.device or job.device,
        result.host,
        commands[-1] if commands else "",
        result.output,
        result.head_prompt_line,
        result.tail_prompt,
        steps=result.steps,
    )

    # Build per-step view in execution order. Mark commands we never
    # sent (because of an earlier abort) as "skipped" so the caller
    # sees the full plan vs. what actually ran.
    #
    # ``set cli-no-confirm`` and the snapshot ``show system target-stack
    # pre-check`` are infrastructure — they live in the transcript log
    # but are not surfaced as agent-facing steps (noise for an upgrade
    # report). The kickoff ``request system target-stack pre-check``
    # IS surfaced.
    sent = {s.command: s for s in result.steps}
    snapshot_step = sent.get(_PRECHECK_SHOW_CMD)
    snapshot_task_id, _, _ = (
        _parse_precheck_show(snapshot_step.output) if snapshot_step else (None, None, None)
    )

    kickoff_ran_clean = False
    overall_status = "ok"
    overall_errors: List[str] = []
    step_envelopes: List[Dict[str, Any]] = []
    for cmd in commands:
        if cmd in (_NO_CONFIRM_CMD, _PRECHECK_SHOW_CMD):
            continue
        s = sent.get(cmd)
        if s is None:
            step_envelopes.append({
                "command": cmd, "status": "skipped",
                "errors": [], "stdout": "",
            })
            continue
        if not s.hit_prompt:
            overall_status = "timeout"
            err_line = (
                f"Timed out waiting for CLI prompt after {job.step_timeout}s."
            )
            overall_errors.append(f"{cmd}: {err_line}")
            step_envelopes.append({
                "command": cmd, "status": "timeout",
                "errors": [err_line], "stdout": s.output,
            })
            continue
        is_err, err_lines = detect_error(s.output)
        if is_err and _ALREADY_REGISTERED_RE.search(s.output or ""):
            # Benign: the device already has this tarball registered for
            # download (e.g. from a prior load). Treat as already staged
            # and keep the overall run healthy.
            job.warnings.append(
                f"{cmd}: file already registered for download on the "
                "device — treated as already staged (no re-load needed)."
            )
            step_envelopes.append({
                "command": cmd, "status": "already_staged",
                "errors": [], "stdout": s.output,
            })
            continue
        if is_err:
            overall_status = "error" if overall_status == "ok" else overall_status
            tail = err_lines[-5:]
            overall_errors.extend(f"{cmd}: {ln}" for ln in tail)
            step_envelopes.append({
                "command": cmd, "status": "error",
                "errors": tail, "stdout": s.output,
            })
            continue
        if cmd == _PRECHECK_KICKOFF_CMD:
            kickoff_ran_clean = True
        step_envelopes.append({
            "command": cmd, "status": "ok",
            "errors": [], "stdout": s.output,
        })

    job.steps = step_envelopes

    # Pre-check post-processing. Three relevant inputs:
    #   - ``pre_check_requested`` (did the user ask for it at all?)
    #   - ``device_mode`` (we won't run pre-check on GI even if asked)
    #   - ``kickoff_ran_clean`` (did the kickoff actually land cleanly?)
    if job.pre_check_requested and job.device_mode == "gi":
        job.pre_check = {
            "state": "skipped_gi",
            "task_id": None, "task_status": None, "result": None,
            "elapsed_s": 0, "poll_count": 0, "stdout": "",
        }
    elif job.pre_check_requested and not kickoff_ran_clean:
        if any(e["command"] == _PRECHECK_KICKOFF_CMD and e["status"] == "skipped"
               for e in step_envelopes):
            job.pre_check = {
                "state": "skipped",
                "task_id": None, "task_status": None, "result": None,
                "elapsed_s": 0, "poll_count": 0, "stdout": "",
            }
        # If kickoff is in step_envelopes but state != ok, the earlier
        # loop already pushed overall_status to error/timeout.
    elif job.pre_check_requested and kickoff_ran_clean:
        # Transition state for the agent's progress view: we're done
        # uploading and now waiting on pre-check.
        job.state = "precheck"
        pre_env = _poll_tar_pre_check(
            tool=tool, device=job.device, host=job.host,
            user=job.user, password=password,
            snapshot_task_id=snapshot_task_id,
            poll_interval_s=job.pre_check_poll_interval_s,
            max_wait_s=job.pre_check_max_wait_s,
            cmd_timeout=DEFAULT_CMD_TIMEOUT,
        )
        job.pre_check = pre_env
        if pre_env["state"] == "passed":
            pass
        elif pre_env["state"] == "failed":
            overall_status = "error" if overall_status == "ok" else overall_status
            overall_errors.append(
                f"pre-check failed (Pre-check result={pre_env.get('result')!r}, "
                f"Task status={pre_env.get('task_status')!r}, "
                f"Task ID={pre_env.get('task_id')!r})."
            )
        elif pre_env["state"] == "timeout":
            overall_status = "timeout" if overall_status == "ok" else overall_status
            overall_errors.append(
                f"pre-check did not complete within {job.pre_check_max_wait_s}s "
                f"(polled {pre_env['poll_count']} time(s)). Run "
                f"'{_PRECHECK_SHOW_CMD}' on the device to inspect current state."
            )
        elif pre_env["state"] == "error":
            overall_status = "error" if overall_status == "ok" else overall_status
            overall_errors.extend(pre_env.get("errors") or [])

    if overall_errors:
        job.errors.extend(overall_errors)
        job.next_actions.append(REQUEST_TAR_LOAD_NEXT_ACTION)

    job.elapsed_s = int(time.time() - started)
    job.completed_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    final_state = (
        "done" if overall_status == "ok"
        else "timeout" if overall_status == "timeout"
        else "error"
    )
    _tarload_finish(job, final_state)
    log_request(tool, request_envelope, _tarload_job_envelope(job))


def get_tar_load_job(
    job_id: Optional[str] = None,
    device: Optional[str] = None,
) -> Dict[str, Any]:
    """Look up a tar-load job started by ``request_system_tar_load``.

    Returns the current envelope (same shape as the kickoff envelope,
    plus ``steps`` / ``pre_check`` / ``completed_utc`` / ``elapsed_s``
    once the worker finishes). Safe to call at any cadence — only
    touches the in-memory registry, no device traffic.

    Lookup rules:
        - If ``job_id`` is given, return that exact job (or an error
          envelope if it was reaped / never existed).
        - Else if ``device`` is given, return the currently-active
          tar-load for that device, falling back to the most recent
          terminal one.
        - At least one of the two must be provided.

    ``state`` values:
        ``loading``  — kickoff succeeded; worker is running the
            ``set cli-no-confirm`` + ``target-stack load`` sequence
            (and, if requested, the pre-check kickoff).
        ``precheck`` — loads finished, pre-check kickoff hit the
            device cleanly, polling
            ``show system target-stack pre-check`` until verdict.
        ``done``     — terminal: every step ran cleanly AND, when
            pre-check was requested on a deployed box,
            ``Pre-check result == Succeeded``.
        ``error``    — terminal: a step hit a CLI error, or pre-check
            ended with anything other than ``Succeeded`` / a
            ``connect_error`` happened during the worker run, or the
            pre-check poll itself errored.
        ``timeout``  — terminal: a step or the pre-check poll exceeded
            its hard cap.

    Terminal jobs (done/error/timeout) stay in memory for 24 h after
    they finish, then are lazily reaped.
    """
    job, err = _TARLOAD_REGISTRY.lookup(job_id=job_id, device_key=device)
    if err is not None:
        # In-memory miss. Under the one-shot CLI front the job ran in a
        # now-exited process, so fall back to the on-disk cache the
        # synchronous (``block=True``) path persisted (issue #17).
        persisted = (
            job_store.load(job_id) if job_id
            else job_store.latest_for_device(device or "")
        )
        if persisted is not None:
            pdev = persisted.get("device") or persisted.get("host") or ""
            # When both job_id and device were given, only honour the
            # cached hit if it actually belongs to that device.
            if not (job_id and device) or pdev == device:
                log_request(
                    "get_tar_load_job",
                    {"job_id": job_id, "device": device},
                    persisted,
                )
                return persisted
        if not job_id and not device:
            return error_response(
                err,
                next_action=(
                    "Pass job_id returned by request_system_tar_load, or "
                    "device to look up the active/latest tar-load on it."
                ),
            )
        if job_id and "No job with" in err:
            return error_response(
                err.replace("No job with", "No tar-load job with"),
                next_action=(
                    "Check the MCP request log for the original "
                    "request_system_tar_load envelope, or start a new one."
                ),
            )
        if "No job on record" in err:
            return error_response(
                err.replace("No job on record", "No tar-load job on record"),
                device=device,
                next_action=(
                    "Start one with request_system_tar_load("
                    "device=..., jenkins_url=...)."
                ),
            )
        return error_response(err, device=device)

    assert job is not None  # narrow type for the linter; lookup guarantees this
    envelope = _tarload_job_envelope(job)
    log_request(
        "get_tar_load_job",
        {"job_id": job_id, "device": device},
        envelope,
    )
    return envelope


def request_system_pre_check(
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    pre_check_poll_interval_s: int = _DEFAULT_PRECHECK_POLL_S,
    pre_check_max_wait_s: int = _DEFAULT_PRECHECK_WAIT_S,
    notify_slack: str = "@oshaboo",
    block: bool = False,
) -> Dict[str, Any]:
    """Kick off ``request system target-stack pre-check`` on a device
    that already has tarballs staged, and wait for the verdict; returns
    IMMEDIATELY (unless ``block=True``).

    ASYNC / NON-BLOCKING. Same async-job shape as
    ``request_system_tar_load`` — kickoff returns ~3-5 s with
    ``state:"loading"`` + ``job_id``; the snapshot + kickoff + poll
    loop runs in a daemon thread. Look up progress with
    ``get_tar_load_job(job_id=...)`` (same registry, same envelope).

    When to use this vs. ``request_system_tar_load``
    ------------------------------------------------
    - ``request_system_tar_load`` always runs pre-check after the
      uploads (unless ``pre_check=False``). Use this tool when the
      tarballs are ALREADY on the device and you just want a fresh
      verdict — e.g. after a previous ``pre_check=False`` kickoff (the
      first leg of a "DNOS+GI from build A, base-OS from build B"
      flow), or to re-validate after the device state changed.
    - This tool does NOT fetch from Jenkins, does NOT call
      ``request system target-stack load``, and does NOT take a
      ``components`` arg. Whatever is currently staged on the box is
      what gets validated.

    On-device sequence (single channel)
    -----------------------------------
    1. ``set cli-no-confirm``                       (suppresses any prompts)
    2. ``show system target-stack pre-check``       (snapshot prior task id)
    3. ``request system target-stack pre-check``    (kickoff)

    Then polls ``show system target-stack pre-check`` every
    ``pre_check_poll_interval_s`` seconds (default 10) up to
    ``pre_check_max_wait_s`` (default 600 s = 10 min). Completion =
    the parsed Task ID differs from the snapshot AND ``Task status``
    is no longer in the running set (``IN_PROGRESS`` / ``RUNNING`` /
    ``PENDING`` / ``STARTED`` / ``QUEUED``).

    Refuses to run on a Genesis Image (GI) box — pre-check is a DNOS
    construct and the GI shell doesn't implement it. The kickoff
    probes ``show system`` to classify the box (``Version: DNOS [...]``
    line ⇒ deployed) and returns an error envelope if the box is in
    GI mode.

    Per-device guard: one active job at a time. A second
    ``request_system_pre_check`` (or ``request_system_tar_load``) for
    a device that already has a tar-load / pre-check in flight is
    rejected instantly with the existing ``job_id``.

    Args:
        device: Device alias (cl, sa, kira, ...).
        host: Raw hostname/IP (alternative to ``device``).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        pre_check_poll_interval_s: Seconds between
            ``show system target-stack pre-check`` polls. Default 10,
            clamped to [5, 60].
        pre_check_max_wait_s: Hard cap on total polling time, in
            seconds. Default 600, clamped to [60, 3600].
        notify_slack: Slack channel/user for kickoff + terminal
            notifications, threaded under the kickoff message. Default
            ``"@oshaboo"``; pass ``""`` to disable.
        block: Run the pre-check sequence + poll SYNCHRONOUSLY and return
            the terminal envelope. Default ``False`` (async — the MCP
            server shape). The one-shot CLI front passes ``True`` so the
            worker runs to completion in-process and the result is
            persisted for a later ``tar-load show`` (issue #17).

    Returns:
        Same envelope shape as ``request_system_tar_load``. Notable
        fields:

        - ``state``: ``loading`` (running the 3-command sequence) →
          ``precheck`` (polling) → ``done`` / ``error`` / ``timeout``.
        - ``status``: ``"running"`` in flight, ``"ok"`` on a passed
          pre-check, else ``"error"`` / ``"timeout"``.
        - ``device_mode``: always ``"deployed"`` here (GI is rejected
          at kickoff).
        - ``jenkins_url``: empty string (no build involved).
        - ``components_requested``: ``"pre_check_only"`` — a sentinel
          so the envelope makes the kickoff mode obvious.
        - ``resolved``: ``{"base_os": null, "dnos": null, "gi": null}``.
        - ``steps``: one entry — the
          ``request system target-stack pre-check`` kickoff.
        - ``pre_check``: ``{"state", "task_id", "task_status",
          "result", "elapsed_s", "poll_count", "stdout"}``. ``state``
          ∈ ``passed`` / ``failed`` / ``timeout`` / ``error``.
    """
    tool = "request_system_pre_check"
    request = {
        "device": device, "host": host, "user": user,
        "pre_check_poll_interval_s": pre_check_poll_interval_s,
        "pre_check_max_wait_s": pre_check_max_wait_s,
    }

    if (
        not isinstance(pre_check_poll_interval_s, int)
        or isinstance(pre_check_poll_interval_s, bool)
    ):
        return error_response(
            "pre_check_poll_interval_s must be an integer (seconds).",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if not (
        _PRECHECK_POLL_S_MIN <= pre_check_poll_interval_s <= _PRECHECK_POLL_S_MAX
    ):
        return error_response(
            f"pre_check_poll_interval_s must be in "
            f"[{_PRECHECK_POLL_S_MIN}, {_PRECHECK_POLL_S_MAX}] seconds.",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if (
        not isinstance(pre_check_max_wait_s, int)
        or isinstance(pre_check_max_wait_s, bool)
    ):
        return error_response(
            "pre_check_max_wait_s must be an integer (seconds).",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if not (_PRECHECK_WAIT_S_MIN <= pre_check_max_wait_s <= _PRECHECK_WAIT_S_MAX):
        return error_response(
            f"pre_check_max_wait_s must be in "
            f"[{_PRECHECK_WAIT_S_MIN}, {_PRECHECK_WAIT_S_MAX}] seconds.",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if not device and not host:
        return error_response(
            "device or host is required.",
            device=device, host=host, next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )

    # Probe `show system` to confirm the device is deployed DNOS, not GI.
    # Pre-check doesn't exist on GI, so we reject up-front rather than
    # firing off a sequence that's guaranteed to error.
    try:
        probe = run_once(
            transport_registry,
            device=device, host=host, user=user, password=password,
            command=_SHOW_SYSTEM_CMD,
            timeout=DEFAULT_CMD_TIMEOUT,
        )
    except ConnectError as exc:
        return error_response(
            str(exc), device=device, host=host,
            next_action="Verify device is reachable and credentials are correct.",
        )
    except Exception as exc:  # noqa: BLE001 — surface anything from paramiko
        return error_response(
            f"{_SHOW_SYSTEM_CMD} probe failed: {exc}",
            device=device, host=host,
            next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    if not probe.hit_prompt:
        return error_response(
            f"{_SHOW_SYSTEM_CMD!r} probe timed out after "
            f"{DEFAULT_CMD_TIMEOUT}s; cannot determine device mode.",
            device=device, host=probe.host or host,
            next_action=REQUEST_TAR_LOAD_NEXT_ACTION,
        )
    device_mode = _detect_device_mode(probe.output, probe.head_prompt_line)
    log_invocation(
        probe.device or device, probe.host,
        _SHOW_SYSTEM_CMD, probe.output,
        probe.head_prompt_line, probe.tail_prompt,
        steps=probe.steps,
    )
    if device_mode == "gi":
        return error_response(
            "Device is in GI mode (pre-deployment); pre-check is a DNOS "
            "construct and doesn't exist in the Genesis Image shell.",
            device=device, host=probe.host or host,
            next_action=(
                "Wait for the box to be deployed, or use "
                "request_system_tar_load if you need to stage images first."
            ),
        )

    # Same 3-command sequence that the load tool's tail would run.
    commands: List[str] = [
        _NO_CONFIRM_CMD,
        _PRECHECK_SHOW_CMD,
        _PRECHECK_KICKOFF_CMD,
    ]

    # Reuse the tar-load device slot — load + pre-check share the same
    # device-level mutex (you can't run both at once on one box).
    device_key = device or host or ""
    active = _TARLOAD_REGISTRY.active_for_device(device_key)
    if active is not None and active.state in _TARLOAD_REGISTRY.active_states:
        return error_response(
            f"A tar-load/pre-check is already running on {device_key!r} "
            f"(job_id={active.job_id}, state={active.state}, "
            f"started_utc={active.started_utc}). Call "
            f"get_tar_load_job(job_id={active.job_id!r}) to check on "
            "it, or wait for it to finish before starting a new one.",
            device=device, host=host,
            next_action=f"get_tar_load_job(job_id={active.job_id!r})",
        )

    started = datetime.now(timezone.utc)
    job_id = _TARLOAD_REGISTRY.make_job_id(device_key, "precheck")
    job = TarLoadJob(
        job_id=job_id,
        device=device,
        host=host,
        device_key=device_key,
        resolved_host=probe.host or "",
        state="loading",
        started_utc=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        user=user,
        command=" ; ".join(commands),
        warnings=[],
        jenkins_url="",
        base_os_url=None,
        dnos_url=None,
        gi_url=None,
        components_requested="pre_check_only",
        device_mode=device_mode,
        pre_check_requested=True,
        effective_pre_check=True,
        # Pre-check sequence commands each finish in <5 s in practice;
        # _DEFAULT_TAR_STEP_TIMEOUT (1800) is overkill but harmless and
        # avoids one more knob on the public surface.
        step_timeout=_DEFAULT_TAR_STEP_TIMEOUT,
        pre_check_poll_interval_s=pre_check_poll_interval_s,
        pre_check_max_wait_s=pre_check_max_wait_s,
        notify_channel=notify_slack,
    )
    _TARLOAD_REGISTRY.register(job)
    _tarload_notify_kickoff(job)

    request["job_id"] = job_id

    worker = threading.Thread(
        target=_tar_load_worker,
        name=f"precheck-{job_id}",
        args=(job, password, list(commands)),
        daemon=True,
    )
    worker.start()
    # block=True (CLI front): run to completion in-process; block=False
    # (MCP server): settle window then return the async kickoff envelope.
    worker.join() if block else worker.join(timeout=2.0)

    envelope = _tarload_job_envelope(job)
    if job.state in {"done", "error", "timeout"}:
        log_request(tool, request, envelope)
        return envelope

    envelope["eta_s"] = pre_check_max_wait_s
    envelope["next_actions"] = [
        f"Pre-check running in background on {device_key}. Polled every "
        f"{pre_check_poll_interval_s}s, hard cap {pre_check_max_wait_s}s. "
        f"Call get_tar_load_job(job_id={job_id!r}) to check status."
    ]
    log_request(tool, request, envelope)
    return envelope


def _poll_tar_pre_check(
    *,
    tool: str,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    snapshot_task_id: Optional[str],
    poll_interval_s: int,
    max_wait_s: int,
    cmd_timeout: int,
) -> Dict[str, Any]:
    """Poll ``show system target-stack pre-check`` until a fresh task
    reaches a terminal status, or ``max_wait_s`` elapses.

    "Fresh" = the parsed Task ID is non-empty AND differs from
    ``snapshot_task_id``. This guards against the previous run's
    completed record being misread as our verdict — important since the
    show command always returns the most recent task, even one from
    days ago.

    Returns the ``pre_check`` sub-envelope: ``state`` ∈
    ``passed`` / ``failed`` / ``timeout`` / ``error``, plus the parsed
    fields from the last poll output.
    """
    started = time.time()
    deadline = started + max_wait_s
    last_output = ""
    last_task_id: Optional[str] = None
    last_status: Optional[str] = None
    last_result: Optional[str] = None
    poll_count = 0
    poll_errors: List[str] = []

    while time.time() < deadline:
        time.sleep(poll_interval_s)
        poll_count += 1
        try:
            probe = run_once(
                transport_registry,
                device=device, host=host,
                user=user, password=password,
                command=_PRECHECK_SHOW_CMD,
                timeout=cmd_timeout,
            )
        except ConnectError as exc:
            poll_errors.append(f"poll #{poll_count}: {exc}")
            return {
                "state": "error",
                "task_id": last_task_id,
                "task_status": last_status,
                "result": last_result,
                "elapsed_s": int(time.time() - started),
                "poll_count": poll_count,
                "stdout": last_output,
                "errors": poll_errors,
            }
        except Exception as exc:
            poll_errors.append(f"poll #{poll_count}: {exc}")
            return {
                "state": "error",
                "task_id": last_task_id,
                "task_status": last_status,
                "result": last_result,
                "elapsed_s": int(time.time() - started),
                "poll_count": poll_count,
                "stdout": last_output,
                "errors": poll_errors,
            }

        last_output = probe.output
        if not probe.hit_prompt:
            continue
        # Log every poll into the per-device transcript so the audit log
        # shows exactly what we observed before declaring the verdict.
        log_invocation(
            probe.device or device,
            probe.host,
            _PRECHECK_SHOW_CMD,
            probe.output,
            probe.head_prompt_line,
            probe.tail_prompt,
            steps=probe.steps,
        )

        tid, status, result = _parse_precheck_show(probe.output)
        last_task_id, last_status, last_result = tid, status, result
        if not tid or tid == snapshot_task_id:
            # Either DNOS hasn't started rendering the new record yet,
            # or it's still showing the previous one — keep polling.
            continue
        if status and status.upper() in _PRECHECK_RUNNING_STATES:
            continue
        # Terminal state reached. Map verdict.
        elapsed = int(time.time() - started)
        if (status or "").upper() == "COMPLETED" and (result or "").lower() == "succeeded":
            state = "passed"
        else:
            state = "failed"
        return {
            "state": state,
            "task_id": tid,
            "task_status": status,
            "result": result,
            "elapsed_s": elapsed,
            "poll_count": poll_count,
            "stdout": probe.output,
        }

    return {
        "state": "timeout",
        "task_id": last_task_id,
        "task_status": last_status,
        "result": last_result,
        "elapsed_s": int(time.time() - started),
        "poll_count": poll_count,
        "stdout": last_output,
    }


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(request_system_tar_load)
    mcp.tool()(request_system_pre_check)
    mcp.tool()(get_tar_load_job)
