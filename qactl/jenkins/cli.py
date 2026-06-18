"""``qactl jenkins ...`` — trigger and inspect Jenkins (cheetah) builds.

Reuses the jenkins-mcp cheetah parameter mapping. Unlike the MCP (which
kicked off an async background worker), ``trigger`` is synchronous from
the shell's point of view: by default it returns the queued handle
immediately; with ``--wait`` it polls the queue then the build to
completion and exits non-zero if the build didn't succeed.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Optional, Tuple

from qactl.core.common import confirm_or_exit, resolve_timeout
from qactl.core.creds import CredentialError
from qactl.core.envelope import error_envelope, ok_envelope
from qactl.core.output import emit
from qactl.jenkins.client import JenkinsClient, branch_to_job_path


_CHEETAH_DEFAULT_PARAMS: dict[str, Any] = {
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


def _yn(v: bool) -> str:
    return "Yes" if v else "No"


def _tf(v: bool) -> str:
    return "True" if v else "False"


def build_cheetah_params(args, client: JenkinsClient, job_path: str) -> Tuple[dict, Optional[str]]:
    """Resolve cheetah params: defaults → inherit → named overrides → extra."""
    params = dict(_CHEETAH_DEFAULT_PARAMS)
    warning: Optional[str] = None
    if args.inherit_from is not None:
        try:
            params.update(client.get_build_parameters(job_path, args.inherit_from))
        except Exception as exc:  # noqa: BLE001
            warning = f"Could not inherit from build {args.inherit_from}: {exc}. Using defaults."
    params.update({
        "SHOULD_LINT": _yn(not args.no_lint),
        "SHOULD_BUILD_DNOS_CONTAINERS": _yn(not args.no_dnos),
        "SHOULD_BUILD_TARBALLS": _yn(not args.no_tarballs),
        "SHOULD_BUILD_BASEOS_CONTAINERS": _yn(args.baseos),
        "SHOULD_RUN_SMOKE_TESTS": _yn(not args.no_smoke),
        "SHOULD_ALLOW_DELTA_BUILD": _yn(args.delta_build),
        "TEST_NAMES": "ENABLE_SANITIZER" if args.sanitizer else "",
        "SINGLE_TEST": args.single_test,
        "SINGLE_TEST_LABEL": args.single_test_label,
        "SINGLE_TEST_PARALLEL": str(args.single_test_parallel),
        "SINGLE_TEST_LOOP": str(args.single_test_loop),
        "KEEP_SETUP_ON_FAILURE": _tf(args.keep_setup_on_failure),
        "NIGHTLY": _tf(args.nightly),
        "QA_VERSION": _tf(args.qa_version),
        "SLACK_CHANNEL": args.slack_channel,
    })
    if args.extra_params:
        params.update(json.loads(args.extra_params))
    return params, warning


def _client(args, *, kind) -> Tuple[Optional[JenkinsClient], Optional[dict]]:
    try:
        return JenkinsClient.from_env(
            timeout=resolve_timeout(args, 30.0),
            user=getattr(args, "user", None),
            token=getattr(args, "token", None),
            url=getattr(args, "url", None),
        ), None
    except CredentialError as e:
        return None, error_envelope(str(e), kind=kind, status="bad_argument")


def _run(args, *, kind, fn):
    client, err = _client(args, kind=kind)
    if err is not None:
        return emit(err, as_json=args.json)
    try:
        env = fn(client)
    except Exception as e:  # noqa: BLE001
        env = error_envelope(f"{kind} failed: {e}", kind=kind)
    return emit(env, as_json=args.json)


# ---- handlers ------------------------------------------------------------

def _whoami(args):
    return _run(args, kind="jenkins_whoami",
                fn=lambda c: ok_envelope(kind="jenkins_whoami", result=c.whoami()))


def _info(args):
    job_path = branch_to_job_path(args.branch, repo=args.repo, org=args.org)
    return _run(args, kind="jenkins_info",
                fn=lambda c: ok_envelope(kind="jenkins_info", result={
                    "branch": args.branch, "repo": args.repo, "org": args.org,
                    **c.get_build(job_path, args.build_number),
                }))


def _list(args):
    job_path = branch_to_job_path(args.branch, repo=args.repo, org=args.org)
    def fn(c):
        builds = c.list_recent_builds(job_path, limit=args.limit)
        return ok_envelope(kind="jenkins_list", result={
            "branch": args.branch, "repo": args.repo, "count": len(builds), "builds": builds,
        })
    return _run(args, kind="jenkins_list", fn=fn)


def _console(args):
    job_path = branch_to_job_path(args.branch, repo=args.repo, org=args.org)
    return _run(args, kind="jenkins_console",
                fn=lambda c: ok_envelope(kind="jenkins_console", result={
                    "branch": args.branch, "build_number": args.build_number,
                    **c.get_console(job_path, args.build_number, tail_lines=args.tail),
                }))


def _do_trigger(args, c, *, kind, job_path, params, result_base, warnings, console_ref):
    """Trigger ``job_path`` and, with --wait, poll the build to completion.

    Shared by the cheetah ``trigger`` and the generic ``trigger-raw``.
    ``console_ref`` is the ``qactl jenkins console ...`` hint for failures.
    """
    trig = c.trigger_build(job_path, params)
    result: dict[str, Any] = {
        **result_base,
        "job_url": trig["job_url"], "queue_id": trig["queue_id"],
        "queue_url": trig["queue_url"], "parameters": params,
    }
    if not args.wait:
        return ok_envelope(
            kind=kind, result=result, warnings=warnings,
            next_actions=[
                f"Build queued (queue_id={trig['queue_id']}). Poll it with "
                f"`qactl jenkins info ...` or re-run with --wait."
            ],
        )
    if trig["queue_id"] is None:
        return error_envelope("Triggered but Jenkins returned no queue id to wait on.",
                              kind=kind, result=result)
    q = c.wait_for_build_number(trig["queue_id"], timeout_s=args.wait_timeout, poll_s=args.poll)
    if q.get("status") != "started":
        return error_envelope(
            f"Build did not start (queue status={q.get('status')}).",
            kind=kind, status="error" if q.get("status") == "timeout" else "aborted",
            result={**result, "queue": q},
        )
    bnum = q["build_number"]
    b = c.wait_for_build_result(job_path, bnum, timeout_s=args.wait_timeout, poll_s=args.poll)
    result.update({"build_number": bnum, "build_url": q.get("build_url"), "build": b})
    if b.get("status") == "timeout":
        return error_envelope(f"Build #{bnum} still running after {args.wait_timeout}s.",
                              kind=kind, status="error", result=result)
    if b.get("result") == "SUCCESS":
        return ok_envelope(kind=kind, result=result, warnings=warnings)
    return error_envelope(f"Build #{bnum} finished with result={b.get('result')}.",
                          kind=kind, result=result,
                          next_actions=[console_ref.format(bnum=bnum)])


def _trigger(args):
    rc = confirm_or_exit(args, kind="jenkins_trigger",
                         action=f"Trigger a {args.repo} build on branch {args.branch!r}.")
    if rc is not None:
        return rc
    job_path = branch_to_job_path(args.branch, repo=args.repo, org=args.org)

    def fn(c):
        params, warning = build_cheetah_params(args, c, job_path)
        return _do_trigger(
            args, c, kind="jenkins_trigger", job_path=job_path, params=params,
            result_base={"branch": args.branch, "repo": args.repo, "org": args.org},
            warnings=[warning] if warning else [],
            console_ref=f"qactl jenkins console {args.branch} {{bnum}} --tail 300",
        )
    return _run(args, kind="jenkins_trigger", fn=fn)


def _trigger_raw(args):
    rc = confirm_or_exit(args, kind="jenkins_trigger_raw",
                         action=f"Trigger raw Jenkins job {args.job_path!r}.")
    if rc is not None:
        return rc
    try:
        params = _parse_params(args.param, args.extra_params)
    except (ValueError, json.JSONDecodeError) as e:
        return emit(error_envelope(f"bad parameters: {e}", kind="jenkins_trigger_raw",
                                   status="bad_argument"), as_json=args.json)

    def fn(c):
        return _do_trigger(
            args, c, kind="jenkins_trigger_raw", job_path=args.job_path, params=params,
            result_base={"job_path": args.job_path}, warnings=[],
            console_ref="inspect the build URL above for failure details (build #{bnum})",
        )
    return _run(args, kind="jenkins_trigger_raw", fn=fn)


def _parse_params(pairs, extra_params_json):
    """Merge repeated ``--param K=V`` with an ``--extra-params`` JSON dict."""
    out: dict[str, Any] = {}
    for item in pairs or []:
        if "=" not in item:
            raise ValueError(f"--param {item!r} must be of the form KEY=VALUE")
        k, _, v = item.partition("=")
        out[k.strip()] = v
    if extra_params_json:
        extra = json.loads(extra_params_json)
        if not isinstance(extra, dict):
            raise ValueError("--extra-params must be a JSON object")
        out.update(extra)
    return out


def _stop(args):
    if args.queue_id is None and args.build_number is None:
        return emit(error_envelope(
            "provide --build-number (abort a running build) or --queue-id "
            "(cancel a build still in the queue).",
            kind="jenkins_stop", status="bad_argument"), as_json=args.json)
    if args.queue_id is not None:
        action = f"Cancel queued Jenkins item {args.queue_id}."
    else:
        action = f"Stop {args.repo} build #{args.build_number} on {args.branch!r}."
    rc = confirm_or_exit(args, kind="jenkins_stop", action=action)
    if rc is not None:
        return rc

    def fn(c):
        if args.queue_id is not None:
            return ok_envelope(kind="jenkins_stop", result=c.cancel_queue_item(args.queue_id))
        job_path = branch_to_job_path(args.branch, repo=args.repo, org=args.org)
        return ok_envelope(kind="jenkins_stop", result=c.stop_build(job_path, args.build_number))
    return _run(args, kind="jenkins_stop", fn=fn)


# ---- registration --------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default="cheetah", help="multibranch pipeline (default: cheetah)")
    p.add_argument("--org", default="drivenets", help="top-level Jenkins folder (default: drivenets)")
    g = p.add_argument_group("jenkins credentials (default: environment)")
    g.add_argument("--user", default=None, help="override $JENKINS_USER")
    g.add_argument("--token", default=None, help="override $JENKINS_API_TOKEN")
    g.add_argument("--url", default=None, help="override $JENKINS_URL")


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("jenkins", help="Jenkins builds (trigger / inspect / stop)")
    sub = grp.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("whoami", parents=[parent], help="sanity-check the Jenkins token")
    _add_common(p); p.set_defaults(func=_whoami)

    t = sub.add_parser("trigger", parents=[parent],
                       help="trigger a cheetah build for a branch (--yes; --wait to block)")
    _add_common(t)
    t.add_argument("branch")
    t.add_argument("--sanitizer", action="store_true", help="TEST_NAMES=ENABLE_SANITIZER")
    t.add_argument("--baseos", action="store_true", help="SHOULD_BUILD_BASEOS_CONTAINERS=Yes")
    t.add_argument("--no-lint", action="store_true")
    t.add_argument("--no-dnos", action="store_true", help="skip SHOULD_BUILD_DNOS_CONTAINERS")
    t.add_argument("--no-tarballs", action="store_true", help="skip SHOULD_BUILD_TARBALLS")
    t.add_argument("--no-smoke", action="store_true", help="skip SHOULD_RUN_SMOKE_TESTS")
    t.add_argument("--delta-build", action="store_true")
    t.add_argument("--single-test", default="")
    t.add_argument("--single-test-label", default="test-tiny")
    t.add_argument("--single-test-parallel", type=int, default=1)
    t.add_argument("--single-test-loop", type=int, default=1)
    t.add_argument("--keep-setup-on-failure", action="store_true")
    t.add_argument("--nightly", action="store_true")
    t.add_argument("--qa-version", action="store_true")
    t.add_argument("--slack-channel", default="")
    t.add_argument("--inherit-from", default=None,
                   help="build number (or 'lastBuild') to inherit parameters from")
    t.add_argument("--extra-params", default=None, help="JSON dict of raw Jenkins param overrides")
    t.add_argument("--wait", action="store_true", help="block until the build finishes")
    t.add_argument("--wait-timeout", type=float, default=4 * 3600, help="seconds to wait (with --wait)")
    t.add_argument("--poll", type=float, default=30.0, help="poll interval seconds (with --wait)")
    t.set_defaults(func=_trigger)

    tr = sub.add_parser("trigger-raw", parents=[parent],
                        help="trigger ANY parameterized job by path with raw params (--yes)")
    g = tr.add_argument_group("jenkins credentials (default: environment)")
    g.add_argument("--user", default=None, help="override $JENKINS_USER")
    g.add_argument("--token", default=None, help="override $JENKINS_API_TOKEN")
    g.add_argument("--url", default=None, help="override $JENKINS_URL")
    tr.add_argument("job_path", help="slash path (org/repo/branch) or a full Jenkins job URL")
    tr.add_argument("--param", action="append", metavar="KEY=VALUE",
                    help="raw Jenkins parameter (repeatable)")
    tr.add_argument("--extra-params", default=None, help="JSON dict of raw Jenkins params")
    tr.add_argument("--wait", action="store_true", help="block until the build finishes")
    tr.add_argument("--wait-timeout", type=float, default=4 * 3600)
    tr.add_argument("--poll", type=float, default=30.0)
    tr.set_defaults(func=_trigger_raw)

    i = sub.add_parser("info", parents=[parent], help="details on a build (params, result, causes)")
    _add_common(i)
    i.add_argument("branch"); i.add_argument("build_number", nargs="?", default="lastBuild")
    i.set_defaults(func=_info)

    c = sub.add_parser("console", parents=[parent], help="tail a build's console log")
    _add_common(c)
    c.add_argument("branch"); c.add_argument("build_number", nargs="?", default="lastBuild")
    c.add_argument("--tail", type=int, default=200)
    c.set_defaults(func=_console)

    l = sub.add_parser("list", parents=[parent], help="recent builds for a branch")
    _add_common(l)
    l.add_argument("branch"); l.add_argument("--limit", type=int, default=10)
    l.set_defaults(func=_list)

    s = sub.add_parser("stop", parents=[parent],
                       help="abort a running build (--build-number) or cancel a queued one (--queue-id) (--yes)")
    _add_common(s)
    s.add_argument("branch", nargs="?", help="branch (with --build-number)")
    s.add_argument("--build-number", type=int, default=None)
    s.add_argument("--queue-id", type=int, default=None,
                   help="cancel a build still waiting in the queue")
    s.set_defaults(func=_stop)
