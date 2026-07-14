"""``qactl orc ...`` — orchestrate build → load → pre-check as one job.

Thin argparse front over :mod:`qactl.orc.tools`. Two flows plus a poller:

    orc load <build-url> -d <dev>     tar-load a build, then pre-check
    orc build <branch>   -d <dev>     jenkins build → tar-load → pre-check
    orc show [job_id] [-d <dev>]      poll a running / finished orc job

``orc load`` blocks by default (minutes); ``--no-wait`` detaches it.
``orc build`` detaches by default (a cheetah build can run hours);
``--wait`` blocks instead. Both loading flows are DESTRUCTIVE (they load a
system image) and require ``--yes``.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, Optional

from qactl.core.common import confirm_or_exit
from qactl.core.envelope import error_envelope
from qactl.core.output import emit
from qactl.orc import tools


def _cred_kwargs(args: argparse.Namespace) -> Dict[str, Any]:
    """Shared login kwargs (host/user/password), dropping None so the tool's
    defaults and per-device registry resolution win."""
    out: Dict[str, Any] = {}
    for name in ("host", "user", "password"):
        val = getattr(args, name, None)
        if val is not None:
            out[name] = val
    return out


def _targets_label(args: argparse.Namespace) -> str:
    return ", ".join((args.device or []) + ([args.host] if args.host else [])) or "?"


def _load(args):
    rc = confirm_or_exit(
        args, kind="orc_load",
        action=f"Load {args.build_url} on {_targets_label(args)} + pre-check.",
    )
    if rc is not None:
        return rc
    return emit(tools.orc_load(
        args.build_url, devices=args.device, components=args.component,
        detach=args.no_wait,
        pre_check_poll_interval_s=args.pre_check_poll,
        pre_check_max_wait_s=args.pre_check_timeout, **_cred_kwargs(args),
    ), as_json=args.json)


def _build(args):
    rc = confirm_or_exit(
        args, kind="orc_build",
        action=(f"Build cheetah {args.branch!r}, then load + pre-check on "
                f"{_targets_label(args)}."),
    )
    if rc is not None:
        return rc
    try:
        trigger_extra = json.loads(args.extra_params) if args.extra_params else None
    except json.JSONDecodeError as e:
        return emit(error_envelope(f"bad --extra-params JSON: {e}", kind="orc_build",
                                   status="bad_argument"), as_json=args.json)
    if trigger_extra is not None and not isinstance(trigger_extra, dict):
        return emit(error_envelope("--extra-params must be a JSON object", kind="orc_build",
                                   status="bad_argument"), as_json=args.json)
    extra: Dict[str, Any] = {}
    for flag in ("sanitizer", "baseos", "no_smoke", "nightly"):
        if getattr(args, flag):
            extra[flag] = True
    if args.inherit_from:
        extra["inherit_from"] = args.inherit_from
    if args.single_test:
        extra["single_test"] = args.single_test
    if trigger_extra:
        extra["extra_params"] = trigger_extra
    return emit(tools.orc_build(
        args.branch, devices=args.device, components=args.component,
        detach=not args.wait,
        repo=args.repo, org=args.org, wait_timeout=args.wait_timeout, poll=args.poll,
        trigger_extra=extra or None,
        pre_check_poll_interval_s=args.pre_check_poll,
        pre_check_max_wait_s=args.pre_check_timeout, **_cred_kwargs(args),
    ), as_json=args.json)


def _show(args):
    return emit(tools.orc_show(job_id=args.job_id, device=args.device),
                as_json=args.json)


# ---- registration --------------------------------------------------------


def _add_device(p: argparse.ArgumentParser) -> None:
    p.add_argument("-d", "--device", action="append", default=None, metavar="DEVICE",
                   help="device alias from the registry (repeatable — one build loads all)")
    p.add_argument("--host", default=None, help="override mgmt IP/host (skip alias resolution)")
    p.add_argument("--user", default=None, help="login user (default: registry / dnroot)")
    p.add_argument("--password", default=None, help="login password (default: registry / dnroot)")


def _add_precheck_tuning(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pre-check-poll", type=int, default=None, dest="pre_check_poll",
                   help="seconds between pre-check status polls (default: tool default)")
    p.add_argument("--pre-check-timeout", type=int, default=None, dest="pre_check_timeout",
                   help="hard cap in seconds on pre-check polling (default: tool default)")


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser(
        "orc", help="orchestrate build/load/pre-check flows (load / build / show)")
    sub = grp.add_subparsers(dest="cmd", required=True)

    # orc load ----------------------------------------------------------
    lo = sub.add_parser(
        "load", parents=[parent],
        help="tar-load an existing build, then pre-check (--yes; blocking, --no-wait to detach)")
    lo.add_argument("build_url", help="Jenkins build URL to load the tarballs from")
    _add_device(lo)
    lo.add_argument("--component", "-c", action="append", default=None,
                    metavar="{baseos,dnos,gi}",
                    help="restrict the load to these components (repeatable; default: all available)")
    lo.add_argument("--no-wait", action="store_true",
                    help="detach: return a job handle immediately, a background worker drives it")
    _add_precheck_tuning(lo)
    lo.set_defaults(func=_load)

    # orc build ---------------------------------------------------------
    bu = sub.add_parser(
        "build", parents=[parent],
        help="jenkins build -> tar-load -> pre-check (--yes; detached, --wait to block)")
    bu.add_argument("branch", help="git branch to build (cheetah)")
    _add_device(bu)
    bu.add_argument("--component", "-c", action="append", default=None,
                    metavar="{baseos,dnos,gi}",
                    help="restrict the load to these components (repeatable; default: all available)")
    bu.add_argument("--repo", default="cheetah", help="multibranch pipeline (default: cheetah)")
    bu.add_argument("--org", default="drivenets", help="top-level Jenkins folder (default: drivenets)")
    bu.add_argument("--sanitizer", action="store_true", help="TEST_NAMES=ENABLE_SANITIZER")
    bu.add_argument("--baseos", action="store_true", help="SHOULD_BUILD_BASEOS_CONTAINERS=Yes")
    bu.add_argument("--no-smoke", action="store_true", help="skip SHOULD_RUN_SMOKE_TESTS")
    bu.add_argument("--nightly", action="store_true")
    bu.add_argument("--inherit-from", default=None,
                    help="build number (or 'lastBuild') to inherit parameters from")
    bu.add_argument("--single-test", default="", help="build for a single test")
    bu.add_argument("--extra-params", default=None, help="JSON dict of raw Jenkins param overrides")
    bu.add_argument("--wait", action="store_true",
                    help="block in-process until the whole flow finishes (default: detached)")
    bu.add_argument("--wait-timeout", type=float, default=4 * 3600,
                    help="seconds to wait for the Jenkins build (default: 14400)")
    bu.add_argument("--poll", type=float, default=30.0,
                    help="Jenkins build poll interval in seconds (default: 30)")
    _add_precheck_tuning(bu)
    bu.set_defaults(func=_build)

    # orc show ----------------------------------------------------------
    sh = sub.add_parser("show", parents=[parent], help="poll a running / finished orc job")
    sh.add_argument("job_id", nargs="?", default=None, help="orc job id (or use -d)")
    sh.add_argument("-d", "--device", default=None, help="look up the latest orc job on this device")
    sh.set_defaults(func=_show)
