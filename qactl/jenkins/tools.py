"""Jenkins tool layer: envelope-returning functions for both fronts.

Shared by the CLI ([qactl jenkins ...]) and the stdio MCP server. The
cheetah parameter mapping and the trigger/poll driver live here so both
fronts behave identically. ``trigger`` / ``trigger_raw`` / ``stop`` are
gated by ``confirm`` for the MCP side; the CLI applies its ``--yes`` / TTY
gate before calling with ``confirm=True``.

Unlike the old jenkins-mcp (which spawned an async background worker and a
``get_jenkins_build_job`` registry), trigger here is synchronous: by
default it returns the queued handle immediately and the agent polls with
``jenkins_info`` / ``jenkins_list``; ``wait=True`` blocks to completion.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from qactl.core.creds import CredentialError
from qactl.core.envelope import error_envelope, ok_envelope
from qactl.jenkins.client import JenkinsClient, branch_to_job_path


def _notify(channel: str, text: str, warnings: list) -> None:
    """Best-effort Slack post about a build; never raises.

    Uses the shared :mod:`qactl.dnos.cli.core.slack_notify` transport, so
    a configured webhook (``QACTL_SLACK_WEBHOOK_URL`` — the same one the
    ``cli monitor`` collector uses) is the preferred path, falling back to
    the MCP slackbot for a named ``channel``. Delivery failures are appended
    to ``warnings`` and MUST NOT break the build wait.
    """
    try:
        from qactl.dnos.cli.core import slack_notify
    except Exception as exc:  # noqa: BLE001 — optional dep must not hard-fail
        warnings.append(f"slack notify unavailable: {type(exc).__name__}: {exc}")
        return
    res = slack_notify.post(channel, text)
    if not res.get("ok"):
        warnings.append(f"slack notify failed: {res.get('error')}")


CHEETAH_DEFAULT_PARAMS: Dict[str, Any] = {
    "HTML_ADDITIONS": "", "SHOULD_LINT": "Yes", "SHOULD_BUILD_DNOS_CONTAINERS": "Yes",
    "SHOULD_BUILD_TARBALLS": "Yes", "SHOULD_BUILD_BASEOS_CONTAINERS": "No",
    "SHOULD_RUN_SMOKE_TESTS": "Yes", "SHOULD_ALLOW_DELTA_BUILD": "No",
    "TESTS_TO_RUN": "Choose test suites to run", "TEST_NAMES": "", "SINGLE_TEST": "",
    "SINGLE_TEST_LABEL": "test-tiny", "SINGLE_TEST_CUSTOM": "", "SINGLE_TEST_PARALLEL": "1",
    "KEEP_SETUP_ON_FAILURE": "False", "SINGLE_TEST_LOOP": "1", "QA_VERSION": "False",
    "PROMOTE_RELEASE": "False", "PUSH_MODEL_FILES": "False", "ALTERNATEREG": "",
    "SLACK_CHANNEL": "", "BUILD_SPECIAL_ENV": "", "NIGHTLY": "False", "NIGHTLY_SPECIAL": "",
    "SYNC_TO_RO": "False", "SYNC_TO_AWS": "False", "SUPPORT_NCCM": "False",
}


# Cheetah archives the browser-visible download links as tiny one-line text
# artifacts (one URL each). Map a friendly key -> the archived file name so
# ``jenkins_artifacts`` can surface the baseos tar / GI / dnos refs without
# scraping the JS-rendered build page.
ARTIFACT_LINK_FILES: Dict[str, str] = {
    "baseos_tar": "gi_base_os_artifact.txt",
    "gi_tar": "gi_GI_artifact.txt",
    "dnos_tar": "gi_DNOS_artifact.txt",
    "gi_swarm_tar": "gi_GI_SWARM_artifact.txt",
    "mgmt_swarm_tar": "gi_MGMT_SWARM_artifact.txt",
    "cdnos_tar": "cdnos_artifact.txt",
}


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _parse_kv_lines(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _yn(v: bool) -> str:
    return "Yes" if v else "No"


def _tf(v: bool) -> str:
    return "True" if v else "False"


def build_cheetah_params(
    client: Optional[JenkinsClient], job_path: str, *,
    inherit_from: Optional[str] = None,
    sanitizer: bool = False, baseos: bool = False, no_lint: bool = False,
    no_dnos: bool = False, no_tarballs: bool = False, no_smoke: bool = False,
    delta_build: bool = False, single_test: str = "", single_test_label: str = "test-tiny",
    single_test_parallel: int = 1, single_test_loop: int = 1,
    keep_setup_on_failure: bool = False, nightly: bool = False, qa_version: bool = False,
    slack_channel: str = "", extra_params: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Resolve cheetah params: defaults -> inherit -> named overrides -> extra."""
    params = dict(CHEETAH_DEFAULT_PARAMS)
    warning: Optional[str] = None
    if inherit_from is not None and client is not None:
        try:
            params.update(client.get_build_parameters(job_path, inherit_from))
        except Exception as exc:  # noqa: BLE001
            warning = f"Could not inherit from build {inherit_from}: {exc}. Using defaults."
    params.update({
        "SHOULD_LINT": _yn(not no_lint),
        "SHOULD_BUILD_DNOS_CONTAINERS": _yn(not no_dnos),
        "SHOULD_BUILD_TARBALLS": _yn(not no_tarballs),
        "SHOULD_BUILD_BASEOS_CONTAINERS": _yn(baseos),
        "SHOULD_RUN_SMOKE_TESTS": _yn(not no_smoke),
        "SHOULD_ALLOW_DELTA_BUILD": _yn(delta_build),
        "TEST_NAMES": "ENABLE_SANITIZER" if sanitizer else "",
        "SINGLE_TEST": single_test,
        "SINGLE_TEST_LABEL": single_test_label,
        "SINGLE_TEST_PARALLEL": str(single_test_parallel),
        "SINGLE_TEST_LOOP": str(single_test_loop),
        "KEEP_SETUP_ON_FAILURE": _tf(keep_setup_on_failure),
        "NIGHTLY": _tf(nightly),
        "QA_VERSION": _tf(qa_version),
        "SLACK_CHANNEL": slack_channel,
    })
    if extra_params:
        params.update(extra_params)
    return params, warning


def _client(
    kind: str, *, timeout: float = 30.0, user: Optional[str] = None,
    token: Optional[str] = None, url: Optional[str] = None,
) -> Tuple[Optional[JenkinsClient], Optional[dict]]:
    try:
        return JenkinsClient.from_env(timeout=timeout, user=user, token=token, url=url), None
    except CredentialError as e:
        return None, error_envelope(str(e), kind=kind, status="bad_argument")


def _run(
    kind: str, fn: Callable[[JenkinsClient], dict], *, timeout: float = 30.0,
    user: Optional[str] = None, token: Optional[str] = None, url: Optional[str] = None,
) -> dict:
    client, err = _client(kind, timeout=timeout, user=user, token=token, url=url)
    if err is not None:
        return err
    try:
        return fn(client)
    except Exception as e:  # noqa: BLE001
        return error_envelope(f"{kind} failed: {e}", kind=kind)


def _drive_trigger(
    client: JenkinsClient, *, kind: str, job_path: str, params: Dict[str, Any],
    result_base: Dict[str, Any], warnings: list, console_ref: str,
    wait: bool, wait_timeout: float, poll: float,
    notify_slack: Optional[str] = None,
) -> dict:
    """Trigger ``job_path`` and, with wait, poll the build to completion.

    When ``notify_slack`` is not ``None`` (``""`` = webhook/default channel,
    else a channel name), post a Slack update when the build STARTS and when
    it reaches a terminal state. Notifying implies ``wait`` — the finish
    update can only be delivered by the process that polled the build.
    """
    label = result_base.get("branch") or result_base.get("job_path") or job_path
    notify = notify_slack is not None
    if notify:
        wait = True  # can't post a finish update without polling to completion
    trig = client.trigger_build(job_path, params)
    result: Dict[str, Any] = {
        **result_base,
        "job_url": trig["job_url"], "queue_id": trig["queue_id"],
        "queue_url": trig["queue_url"], "parameters": params,
    }
    if not wait:
        return ok_envelope(
            kind=kind, result=result, warnings=warnings,
            next_actions=[
                f"Build queued (queue_id={trig['queue_id']}). Poll it with "
                f"jenkins_info / jenkins_list or re-run with wait=true."
            ],
        )
    if trig["queue_id"] is None:
        return error_envelope("Triggered but Jenkins returned no queue id to wait on.",
                              kind=kind, result=result)
    q = client.wait_for_build_number(trig["queue_id"], timeout_s=wait_timeout, poll_s=poll)
    if q.get("status") != "started":
        if notify:
            _notify(notify_slack, f":warning: cheetah *{label}* did not start "
                    f"(queue status={q.get('status')}).", warnings)
        return error_envelope(
            f"Build did not start (queue status={q.get('status')}).",
            kind=kind, status="error" if q.get("status") == "timeout" else "aborted",
            result={**result, "queue": q},
        )
    bnum = q["build_number"]
    build_url = q.get("build_url") or f"{trig['job_url']}/{bnum}/"
    if notify:
        _notify(notify_slack, f":hammer_and_wrench: cheetah *{label}* build "
                f"#{bnum} started\n{build_url}", warnings)
    b = client.wait_for_build_result(job_path, bnum, timeout_s=wait_timeout, poll_s=poll)
    result.update({"build_number": bnum, "build_url": q.get("build_url"), "build": b})
    if b.get("status") == "timeout":
        if notify:
            _notify(notify_slack, f":hourglass_flowing_sand: cheetah *{label}* build "
                    f"#{bnum} still running after {wait_timeout}s\n{build_url}", warnings)
        return error_envelope(f"Build #{bnum} still running after {wait_timeout}s.",
                              kind=kind, status="error", result=result)
    if b.get("result") == "SUCCESS":
        if notify:
            _notify(notify_slack, f":white_check_mark: cheetah *{label}* build "
                    f"#{bnum} *SUCCESS*\n{build_url}", warnings)
        return ok_envelope(kind=kind, result=result, warnings=warnings)
    if notify:
        _notify(notify_slack, f":x: cheetah *{label}* build #{bnum} "
                f"*{b.get('result')}*\n{build_url}", warnings)
    return error_envelope(f"Build #{bnum} finished with result={b.get('result')}.",
                          kind=kind, result=result,
                          next_actions=[console_ref.format(bnum=bnum)])


# ---- read tools ----------------------------------------------------------

def jenkins_whoami(
    *, timeout: float = 30.0, user: Optional[str] = None,
    token: Optional[str] = None, url: Optional[str] = None,
) -> Dict[str, Any]:
    """Sanity-check the configured Jenkins token."""
    return _run("jenkins_whoami",
                lambda c: ok_envelope(kind="jenkins_whoami", result=c.whoami()),
                timeout=timeout, user=user, token=token, url=url)


def jenkins_info(
    branch: str, build_number: str = "lastBuild", *, repo: str = "cheetah",
    org: str = "drivenets", timeout: float = 30.0, user: Optional[str] = None,
    token: Optional[str] = None, url: Optional[str] = None,
) -> Dict[str, Any]:
    """Build details (params, result, causes) for a branch build."""
    job_path = branch_to_job_path(branch, repo=repo, org=org)
    return _run("jenkins_info",
                lambda c: ok_envelope(kind="jenkins_info", result={
                    "branch": branch, "repo": repo, "org": org,
                    **c.get_build(job_path, build_number),
                }), timeout=timeout, user=user, token=token, url=url)


def jenkins_list(
    branch: str, limit: int = 10, *, repo: str = "cheetah", org: str = "drivenets",
    timeout: float = 30.0, user: Optional[str] = None,
    token: Optional[str] = None, url: Optional[str] = None,
) -> Dict[str, Any]:
    """Recent builds for a branch."""
    job_path = branch_to_job_path(branch, repo=repo, org=org)

    def fn(c: JenkinsClient) -> dict:
        builds = c.list_recent_builds(job_path, limit=limit)
        return ok_envelope(kind="jenkins_list", result={
            "branch": branch, "repo": repo, "count": len(builds), "builds": builds,
        })
    return _run("jenkins_list", fn, timeout=timeout, user=user, token=token, url=url)


def jenkins_console(
    branch: str, build_number: str = "lastBuild", tail: int = 200, *,
    repo: str = "cheetah", org: str = "drivenets", timeout: float = 30.0,
    user: Optional[str] = None, token: Optional[str] = None, url: Optional[str] = None,
) -> Dict[str, Any]:
    """Tail a build's console log."""
    job_path = branch_to_job_path(branch, repo=repo, org=org)
    return _run("jenkins_console",
                lambda c: ok_envelope(kind="jenkins_console", result={
                    "branch": branch, "build_number": build_number,
                    **c.get_console(job_path, build_number, tail_lines=tail),
                }), timeout=timeout, user=user, token=token, url=url)


def jenkins_artifacts(
    branch: str, build_number: str = "lastBuild", *, all_artifacts: bool = False,
    repo: str = "cheetah", org: str = "drivenets", timeout: float = 30.0,
    user: Optional[str] = None, token: Optional[str] = None, url: Optional[str] = None,
) -> Dict[str, Any]:
    """A build's published artifact download links (baseos / GI / dnos / cdnos).

    Reads the small text artifacts cheetah archives (``gi_*_artifact.txt``,
    ``cdnos_*``, ``metadata.images``) so the baseos tar URL, image tarballs,
    and registry image refs can be fed straight into ``tar-load`` / an ONIE
    ``wget`` without scraping the JS-rendered build page. ``all_artifacts``
    additionally returns the full archived-artifact listing.
    """
    job_path = branch_to_job_path(branch, repo=repo, org=org)

    def fn(c: JenkinsClient) -> dict:
        meta = c.get_build_artifacts(job_path, build_number)
        build_url = meta["url"]
        artifacts = meta["artifacts"]
        by_name = {a.get("fileName"): a.get("relativePath") for a in artifacts}
        warnings: list = []

        downloads: Dict[str, str] = {}
        for key, fname in ARTIFACT_LINK_FILES.items():
            rel = by_name.get(fname)
            if not rel:
                continue
            try:
                val = _first_line(c.get_artifact_text(build_url, rel))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"could not read {fname}: {exc}")
                continue
            if val:
                downloads[key] = val

        images: Dict[str, Any] = {}
        cdnos_rel = by_name.get("cdnos_images.txt")
        if cdnos_rel:
            try:
                kv = _parse_kv_lines(c.get_artifact_text(build_url, cdnos_rel))
                if kv.get("CDNOS_IMAGE"):
                    images["cdnos"] = kv["CDNOS_IMAGE"]
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"could not read cdnos_images.txt: {exc}")
        meta_rel = by_name.get("metadata.images")
        if meta_rel:
            try:
                refs = [ln.strip() for ln in
                        c.get_artifact_text(build_url, meta_rel).splitlines() if ln.strip()]
                if refs:
                    images["registry"] = refs
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"could not read metadata.images: {exc}")

        result: Dict[str, Any] = {
            "branch": branch, "repo": repo, "org": org,
            "build_number": meta["number"], "build_url": build_url,
            "result": meta["result"], "building": meta["building"],
            "artifact_base_url": f"{build_url.rstrip('/')}/artifact/",
            "artifact_count": len(artifacts),
            "downloads": downloads, "images": images,
        }
        if all_artifacts:
            result["artifacts"] = artifacts

        next_actions: list = []
        if not downloads and not images:
            if meta.get("building"):
                warnings.append("Build is still running; artifact links may not be archived yet.")
            else:
                warnings.append(
                    "No published artifact links found. The build may not archive "
                    "baseos/GI/dnos artifacts; re-run with all_artifacts=true to see "
                    "the full archived listing.")
        return ok_envelope(kind="jenkins_artifacts", result=result,
                           warnings=warnings, next_actions=next_actions)

    return _run("jenkins_artifacts", fn, timeout=timeout, user=user, token=token, url=url)


# ---- write / destructive tools -------------------------------------------

def jenkins_trigger(
    branch: str, *, confirm: bool = False, repo: str = "cheetah", org: str = "drivenets",
    sanitizer: bool = False, baseos: bool = False, no_lint: bool = False,
    no_dnos: bool = False, no_tarballs: bool = False, no_smoke: bool = False,
    delta_build: bool = False, single_test: str = "", single_test_label: str = "test-tiny",
    single_test_parallel: int = 1, single_test_loop: int = 1,
    keep_setup_on_failure: bool = False, nightly: bool = False, qa_version: bool = False,
    slack_channel: str = "", inherit_from: Optional[str] = None,
    extra_params: Optional[Dict[str, Any]] = None, wait: bool = False,
    wait_timeout: float = 4 * 3600, poll: float = 30.0, timeout: float = 30.0,
    notify_slack: Optional[str] = None,
    user: Optional[str] = None, token: Optional[str] = None, url: Optional[str] = None,
) -> Dict[str, Any]:
    """Trigger a cheetah build for a branch (destructive; needs confirm=true)."""
    if not confirm:
        return error_envelope(
            f"Refusing to trigger a {repo} build on {branch!r} without confirm=true.",
            kind="jenkins_trigger", status="confirmation_required",
            next_actions=["Re-call with confirm=true to proceed."],
        )
    job_path = branch_to_job_path(branch, repo=repo, org=org)

    def fn(c: JenkinsClient) -> dict:
        params, warning = build_cheetah_params(
            c, job_path, inherit_from=inherit_from, sanitizer=sanitizer, baseos=baseos,
            no_lint=no_lint, no_dnos=no_dnos, no_tarballs=no_tarballs, no_smoke=no_smoke,
            delta_build=delta_build, single_test=single_test, single_test_label=single_test_label,
            single_test_parallel=single_test_parallel, single_test_loop=single_test_loop,
            keep_setup_on_failure=keep_setup_on_failure, nightly=nightly, qa_version=qa_version,
            slack_channel=slack_channel, extra_params=extra_params,
        )
        return _drive_trigger(
            c, kind="jenkins_trigger", job_path=job_path, params=params,
            result_base={"branch": branch, "repo": repo, "org": org},
            warnings=[warning] if warning else [],
            console_ref=f"qactl jenkins console {branch} {{bnum}} --tail 300",
            wait=wait, wait_timeout=wait_timeout, poll=poll, notify_slack=notify_slack,
        )
    return _run("jenkins_trigger", fn, timeout=timeout, user=user, token=token, url=url)


def jenkins_trigger_raw(
    job_path: str, params: Optional[Dict[str, Any]] = None, *, confirm: bool = False,
    wait: bool = False, wait_timeout: float = 4 * 3600, poll: float = 30.0,
    timeout: float = 30.0, notify_slack: Optional[str] = None, user: Optional[str] = None,
    token: Optional[str] = None, url: Optional[str] = None,
) -> Dict[str, Any]:
    """Trigger ANY parameterized job by path with raw params (destructive; needs confirm=true)."""
    if not confirm:
        return error_envelope(
            f"Refusing to trigger raw Jenkins job {job_path!r} without confirm=true.",
            kind="jenkins_trigger_raw", status="confirmation_required",
            next_actions=["Re-call with confirm=true to proceed."],
        )
    params = dict(params or {})

    def fn(c: JenkinsClient) -> dict:
        return _drive_trigger(
            c, kind="jenkins_trigger_raw", job_path=job_path, params=params,
            result_base={"job_path": job_path}, warnings=[],
            console_ref="inspect the build URL above for failure details (build #{bnum})",
            wait=wait, wait_timeout=wait_timeout, poll=poll, notify_slack=notify_slack,
        )
    return _run("jenkins_trigger_raw", fn, timeout=timeout, user=user, token=token, url=url)


def jenkins_stop(
    branch: Optional[str] = None, *, build_number: Optional[int] = None,
    queue_id: Optional[int] = None, confirm: bool = False, repo: str = "cheetah",
    org: str = "drivenets", timeout: float = 30.0, user: Optional[str] = None,
    token: Optional[str] = None, url: Optional[str] = None,
) -> Dict[str, Any]:
    """Abort a running build (build_number) or cancel a queued one (queue_id)."""
    if queue_id is None and build_number is None:
        return error_envelope(
            "provide build_number (abort a running build) or queue_id (cancel a queued one).",
            kind="jenkins_stop", status="bad_argument")
    if not confirm:
        action = (f"Cancel queued Jenkins item {queue_id}." if queue_id is not None
                  else f"Stop {repo} build #{build_number} on {branch!r}.")
        return error_envelope(
            f"Refusing destructive operation without confirm=true: {action}",
            kind="jenkins_stop", status="confirmation_required",
            next_actions=["Re-call with confirm=true to proceed."],
        )

    def fn(c: JenkinsClient) -> dict:
        if queue_id is not None:
            return ok_envelope(kind="jenkins_stop", result=c.cancel_queue_item(queue_id))
        job_path = branch_to_job_path(branch, repo=repo, org=org)
        return ok_envelope(kind="jenkins_stop", result=c.stop_build(job_path, build_number))
    return _run("jenkins_stop", fn, timeout=timeout, user=user, token=token, url=url)


def register(mcp) -> None:
    """Wire the Jenkins tools onto a FastMCP (or compatible) instance."""
    mcp.tool()(jenkins_whoami)
    mcp.tool()(jenkins_info)
    mcp.tool()(jenkins_list)
    mcp.tool()(jenkins_console)
    mcp.tool()(jenkins_artifacts)
    mcp.tool()(jenkins_trigger)
    mcp.tool()(jenkins_trigger_raw)
    mcp.tool()(jenkins_stop)
