"""``qactl jenkins ...`` — trigger and inspect Jenkins (cheetah) builds.

Thin argparse front over :mod:`qactl.jenkins.tools` (the same envelope
layer the stdio MCP server exposes). ``build_cheetah_params`` and
``_parse_params`` are kept here (delegating to ``tools``) so existing
imports/tests keep working.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, Optional, Tuple

from qactl.core.common import confirm_or_exit, resolve_timeout
from qactl.core.envelope import error_envelope
from qactl.core.output import emit
from qactl.jenkins import tools
from qactl.jenkins.client import JenkinsClient


def build_cheetah_params(args, client: Optional[JenkinsClient], job_path: str) -> Tuple[dict, Optional[str]]:
    """Resolve cheetah params from parsed args (delegates to the tool layer)."""
    return tools.build_cheetah_params(
        client, job_path,
        inherit_from=args.inherit_from, sanitizer=args.sanitizer, baseos=args.baseos,
        no_lint=args.no_lint, no_dnos=args.no_dnos, no_tarballs=args.no_tarballs,
        no_smoke=args.no_smoke, delta_build=args.delta_build, single_test=args.single_test,
        single_test_label=args.single_test_label, single_test_parallel=args.single_test_parallel,
        single_test_loop=args.single_test_loop, keep_setup_on_failure=args.keep_setup_on_failure,
        nightly=args.nightly, qa_version=args.qa_version, slack_channel=args.slack_channel,
        extra_params=json.loads(args.extra_params) if args.extra_params else None,
    )


def _parse_params(pairs, extra_params_json) -> Dict[str, Any]:
    """Merge repeated ``--param K=V`` with an ``--extra-params`` JSON dict."""
    out: Dict[str, Any] = {}
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


def _creds(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "timeout": resolve_timeout(args, 30.0),
        "user": getattr(args, "user", None),
        "token": getattr(args, "token", None),
        "url": getattr(args, "url", None),
    }


# ---- handlers ------------------------------------------------------------

def _whoami(args):
    return emit(tools.jenkins_whoami(**_creds(args)), as_json=args.json)


def _info(args):
    return emit(tools.jenkins_info(args.branch, args.build_number, repo=args.repo,
                                   org=args.org, **_creds(args)), as_json=args.json)


def _list(args):
    return emit(tools.jenkins_list(args.branch, limit=args.limit, repo=args.repo,
                                   org=args.org, **_creds(args)), as_json=args.json)


def _console(args):
    return emit(tools.jenkins_console(args.branch, args.build_number, tail=args.tail,
                                      repo=args.repo, org=args.org, **_creds(args)),
                as_json=args.json)


def _artifacts(args):
    return emit(tools.jenkins_artifacts(args.branch, args.build_number,
                                        all_artifacts=args.all, repo=args.repo,
                                        org=args.org, **_creds(args)), as_json=args.json)


def _spawn_detached_watch(args, env: dict) -> Optional[int]:
    """Fire a detached ``qactl jenkins watch --queue-id …`` for a queued build.

    Returns the child PID (or ``None`` if we couldn't spawn). The child
    survives this process, polls the build, and posts Slack start+finish.
    """
    import os
    import subprocess
    import sys

    qid = env.get("result", {}).get("queue_id")
    if qid is None:
        return None
    argv = [sys.executable, "-m", "qactl", "jenkins", "watch", args.branch,
            "--queue-id", str(qid), "--notify-slack", args.notify_slack,
            "--repo", args.repo, "--org", args.org,
            "--poll", str(args.poll), "--wait-timeout", str(args.wait_timeout)]
    try:
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        return True
    except Exception:  # noqa: BLE001 — detach is best-effort
        return None


def _trigger(args):
    rc = confirm_or_exit(args, kind="jenkins_trigger",
                         action=f"Trigger a {args.repo} build on branch {args.branch!r}.")
    if rc is not None:
        return rc
    # notify + no --wait → trigger, then hand off to a DETACHED watcher so the
    # command returns immediately while Slack still gets start+finish. With
    # --wait we notify inline (blocking), the classic behavior.
    detach = args.notify_slack is not None and not args.wait
    env = tools.jenkins_trigger(
        args.branch, confirm=True, repo=args.repo, org=args.org,
        sanitizer=args.sanitizer, baseos=args.baseos, no_lint=args.no_lint,
        no_dnos=args.no_dnos, no_tarballs=args.no_tarballs, no_smoke=args.no_smoke,
        delta_build=args.delta_build, single_test=args.single_test,
        single_test_label=args.single_test_label, single_test_parallel=args.single_test_parallel,
        single_test_loop=args.single_test_loop, keep_setup_on_failure=args.keep_setup_on_failure,
        nightly=args.nightly, qa_version=args.qa_version, slack_channel=args.slack_channel,
        inherit_from=args.inherit_from,
        extra_params=json.loads(args.extra_params) if args.extra_params else None,
        wait=args.wait, wait_timeout=args.wait_timeout, poll=args.poll,
        notify_slack=None if detach else args.notify_slack, **_creds(args),
    )
    if detach and env.get("status") in ("ok", "warning"):
        spawned = _spawn_detached_watch(args, env)
        note = ("Watching in background; Slack will get build start + finish."
                if spawned else "WARNING: could not spawn background watcher — no Slack updates.")
        env.setdefault("next_actions", []).insert(0, note)
        if not spawned:
            env.setdefault("warnings", []).append(note)
    return emit(env, as_json=args.json)


def _watch(args):
    return emit(tools.jenkins_watch(
        args.branch, build_number=args.build_number, queue_id=args.queue_id,
        repo=args.repo, org=args.org, notify_slack=args.notify_slack,
        wait_timeout=args.wait_timeout, poll=args.poll, **_creds(args),
    ), as_json=args.json)


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
    return emit(tools.jenkins_trigger_raw(
        args.job_path, params, confirm=True, wait=args.wait,
        wait_timeout=args.wait_timeout, poll=args.poll,
        notify_slack=args.notify_slack, **_creds(args),
    ), as_json=args.json)


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
    return emit(tools.jenkins_stop(
        args.branch, build_number=args.build_number, queue_id=args.queue_id,
        confirm=True, repo=args.repo, org=args.org, **_creds(args),
    ), as_json=args.json)


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
    t.add_argument("--notify-slack", nargs="?", const="", default=None, metavar="CHANNEL",
                   help="post a Slack update on build start + finish (implies --wait). "
                        "Bare flag uses the configured webhook ($QACTL_SLACK_WEBHOOK_URL); "
                        "give a CHANNEL for the MCP slackbot fallback.")
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
    tr.add_argument("--notify-slack", nargs="?", const="", default=None, metavar="CHANNEL",
                    help="post a Slack update on build start + finish (implies --wait). "
                         "Bare flag uses the configured webhook ($QACTL_SLACK_WEBHOOK_URL).")
    tr.set_defaults(func=_trigger_raw)

    w = sub.add_parser("watch", parents=[parent],
                       help="watch an already-triggered build to completion (--notify-slack to post)")
    _add_common(w)
    w.add_argument("branch")
    grp_w = w.add_mutually_exclusive_group()
    grp_w.add_argument("--build-number", type=int, default=None,
                       help="attach to this running build number")
    grp_w.add_argument("--queue-id", type=int, default=None,
                       help="attach to a still-queued item (resolves to a build number)")
    w.add_argument("--notify-slack", nargs="?", const="", default=None, metavar="CHANNEL",
                   help="post a Slack update on start (queued only) + finish. "
                        "Bare flag uses the configured webhook ($QACTL_SLACK_WEBHOOK_URL).")
    w.add_argument("--wait-timeout", type=float, default=4 * 3600)
    w.add_argument("--poll", type=float, default=30.0)
    w.set_defaults(func=_watch)

    i = sub.add_parser("info", parents=[parent], help="details on a build (params, result, causes)")
    _add_common(i)
    i.add_argument("branch"); i.add_argument("build_number", nargs="?", default="lastBuild")
    i.set_defaults(func=_info)

    c = sub.add_parser("console", parents=[parent], help="tail a build's console log")
    _add_common(c)
    c.add_argument("branch"); c.add_argument("build_number", nargs="?", default="lastBuild")
    c.add_argument("--tail", type=int, default=200)
    c.set_defaults(func=_console)

    a = sub.add_parser("artifacts", parents=[parent],
                       help="a build's published download links (baseos / GI / dnos / cdnos)")
    _add_common(a)
    a.add_argument("branch"); a.add_argument("build_number", nargs="?", default="lastBuild")
    a.add_argument("--all", action="store_true",
                   help="also include the full archived-artifact listing")
    a.set_defaults(func=_artifacts)

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
