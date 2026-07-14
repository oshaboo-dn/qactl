"""Tests for the ``qactl orc`` orchestrator (parser + phase driver + show)."""

from __future__ import annotations

import unittest
from unittest import mock

import pytest

from qactl.__main__ import build_native_parser
from qactl.orc import tools


# --- parser ---------------------------------------------------------------


class OrcParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_native_parser()

    def test_load_defaults_blocking(self):
        args = self.parser.parse_args(["orc", "load", "http://j/42/", "-d", "sa"])
        self.assertEqual(args.group, "orc")
        self.assertEqual(args.build_url, "http://j/42/")
        self.assertEqual(args.device, ["sa"])
        self.assertFalse(args.no_wait)  # blocking by default

    def test_build_defaults_detached(self):
        args = self.parser.parse_args(["orc", "build", "feature/x", "-d", "cl"])
        self.assertEqual(args.branch, "feature/x")
        self.assertFalse(args.wait)  # detached by default (wait is off)

    def test_build_multi_device_repeatable(self):
        args = self.parser.parse_args(
            ["orc", "build", "dev26.3", "-d", "cl", "-d", "sa"])
        self.assertEqual(args.device, ["cl", "sa"])

    def test_build_component_repeatable(self):
        args = self.parser.parse_args(
            ["orc", "build", "b", "-d", "cl", "-c", "dnos", "-c", "gi"])
        self.assertEqual(args.component, ["dnos", "gi"])

    def test_show_positional_or_device(self):
        a = self.parser.parse_args(["orc", "show", "sa-orc-load-abc123"])
        self.assertEqual(a.job_id, "sa-orc-load-abc123")
        b = self.parser.parse_args(["orc", "show", "-d", "sa"])
        self.assertIsNone(b.job_id)
        self.assertEqual(b.device, "sa")


# --- phase driver ---------------------------------------------------------


def _ok(**extra):
    return {"status": "ok", "errors": [], **extra}


@pytest.fixture
def _no_persist(monkeypatch):
    """Swallow job_store writes so the driver tests don't touch disk."""
    monkeypatch.setattr(tools.job_store, "save", lambda *a, **k: None)


def test_orc_load_runs_load_then_precheck(monkeypatch, _no_persist):
    calls = []

    def fake_tar_load(**kw):
        calls.append(("load", kw))
        assert kw["pre_check"] is False and kw["block"] is True and kw["confirm"] is True
        return _ok(state="done")

    def fake_pre_check(**kw):
        calls.append(("pre_check", kw))
        assert kw["block"] is True
        return _ok(state="done")

    monkeypatch.setattr(
        "qactl.dnos.cli.tools.tarload.request_system_tar_load", fake_tar_load)
    monkeypatch.setattr(
        "qactl.dnos.cli.tools.tarload.request_system_pre_check", fake_pre_check)

    env = tools.orc_load("http://j/42/", device="sa", detach=False)

    assert env["status"] == "ok"
    assert env["phase"] == "done"
    assert [c[0] for c in calls] == ["load", "pre_check"]
    # device threads through to both phases
    assert calls[0][1]["device"] == "sa"
    assert env["result"]["phases"]["load"]["state"] == "done"
    assert env["result"]["phases"]["pre_check"]["state"] == "done"


def test_orc_load_stops_at_failed_load(monkeypatch, _no_persist):
    called = {"pre_check": False}

    monkeypatch.setattr(
        "qactl.dnos.cli.tools.tarload.request_system_tar_load",
        lambda **kw: {"status": "error", "errors": ["boom"]})

    def fake_pre_check(**kw):
        called["pre_check"] = True
        return _ok()

    monkeypatch.setattr(
        "qactl.dnos.cli.tools.tarload.request_system_pre_check", fake_pre_check)

    env = tools.orc_load("http://j/42/", device="sa", detach=False)

    assert env["status"] == "error"
    assert env["phase"] == "load"
    assert called["pre_check"] is False  # never reached
    assert any("[load]" in e for e in env["errors"])


def test_orc_build_runs_build_then_load_then_precheck(monkeypatch, _no_persist):
    seq = []

    def fake_trigger(branch, **kw):
        seq.append("build")
        assert kw["confirm"] is True and kw["wait"] is True
        return {"status": "ok", "errors": [],
                "result": {"branch": branch, "build_number": 42,
                           "build_url": "http://j/feature/42/",
                           "build": {"result": "SUCCESS"}}}

    def fake_tar_load(**kw):
        seq.append("load")
        assert kw["jenkins_url"] == "http://j/feature/42/"
        return _ok(state="done")

    monkeypatch.setattr("qactl.jenkins.tools.jenkins_trigger", fake_trigger)
    monkeypatch.setattr(
        "qactl.dnos.cli.tools.tarload.request_system_tar_load", fake_tar_load)
    monkeypatch.setattr(
        "qactl.dnos.cli.tools.tarload.request_system_pre_check",
        lambda **kw: (seq.append("pre_check"), _ok())[1])

    env = tools.orc_build("feature/x", device="cl", detach=False)

    assert env["status"] == "ok"
    assert seq == ["build", "load", "pre_check"]
    assert env["result"]["build_url"] == "http://j/feature/42/"
    assert env["result"]["phases"]["build"]["build_number"] == 42


def test_orc_build_stops_at_failed_build(monkeypatch, _no_persist):
    monkeypatch.setattr(
        "qactl.jenkins.tools.jenkins_trigger",
        lambda branch, **kw: {"status": "error", "errors": ["FAILURE"],
                              "result": {"build_number": 7}})
    # load must never be called if the build failed
    monkeypatch.setattr(
        "qactl.dnos.cli.tools.tarload.request_system_tar_load",
        lambda **kw: pytest.fail("load ran despite a failed build"))

    env = tools.orc_build("feature/x", device="cl", detach=False)

    assert env["status"] == "error"
    assert env["phase"] == "build"


def test_orc_build_detached_returns_handle(monkeypatch, _no_persist):
    """detach=True (the orc build default) forks; here we stub the fork out
    and assert we get a pollable kickoff handle, not a terminal envelope."""
    def fake_spawn(specs, **kw):
        for s in specs:
            s["job"]["worker_pid"] = 99999
            s["job"]["status"] = "running"
        return 99999

    monkeypatch.setattr(tools, "_spawn_detached", fake_spawn)
    # If the driver ran inline, these would be hit — they must not be.
    monkeypatch.setattr(
        "qactl.jenkins.tools.jenkins_trigger",
        lambda *a, **k: pytest.fail("driver ran inline under detach"))

    env = tools.orc_build("feature/x", device="cl")  # default detach=True

    # A successful detached launch reports ok (exit 0); the per-job snapshot
    # keeps its running state for pollers.
    assert env["status"] == "ok"
    assert env["worker_pid"] == 99999
    assert env["state"] == "running"
    assert env["result"]["mode"] == "build"


def test_orc_build_fans_out_to_multiple_devices(monkeypatch, _no_persist):
    """One build, two devices: build triggers ONCE, then load+pre-check runs
    for each device, and the roll-up lists both."""
    builds = []
    loads = []

    def fake_trigger(branch, **kw):
        builds.append(branch)
        return {"status": "ok", "errors": [],
                "result": {"branch": branch, "build_number": 7,
                           "build_url": "http://j/dev26.3/7/",
                           "build": {"result": "SUCCESS"}}}

    def fake_tar_load(**kw):
        loads.append(kw.get("device"))
        assert kw["jenkins_url"] == "http://j/dev26.3/7/"
        return _ok(state="done")

    monkeypatch.setattr("qactl.jenkins.tools.jenkins_trigger", fake_trigger)
    monkeypatch.setattr(
        "qactl.dnos.cli.tools.tarload.request_system_tar_load", fake_tar_load)
    monkeypatch.setattr(
        "qactl.dnos.cli.tools.tarload.request_system_pre_check", lambda **kw: _ok())

    env = tools.orc_build("dev26.3", devices=["cl", "sa"], detach=False)

    assert builds == ["dev26.3"]              # exactly one build
    assert loads == ["cl", "sa"]              # loaded on both, in order
    assert env["status"] == "ok"
    assert env["result"]["devices"] == ["cl", "sa"]
    assert len(env["result"]["jobs"]) == 2
    assert all(j["status"] == "ok" for j in env["result"]["jobs"])


def test_orc_build_multi_one_device_fails_others_proceed(monkeypatch, _no_persist):
    def fake_tar_load(**kw):
        if kw.get("device") == "cl":
            return {"status": "error", "errors": ["cl load boom"]}
        return _ok(state="done")

    monkeypatch.setattr(
        "qactl.jenkins.tools.jenkins_trigger",
        lambda branch, **kw: {"status": "ok", "errors": [],
                              "result": {"build_url": "http://j/dev26.3/7/",
                                         "build": {"result": "SUCCESS"}}})
    monkeypatch.setattr(
        "qactl.dnos.cli.tools.tarload.request_system_tar_load", fake_tar_load)
    monkeypatch.setattr(
        "qactl.dnos.cli.tools.tarload.request_system_pre_check", lambda **kw: _ok())

    env = tools.orc_build("dev26.3", devices=["cl", "sa"], detach=False)

    by_dev = {j["device"]: j for j in env["result"]["jobs"]}
    assert by_dev["cl"]["status"] == "error" and by_dev["cl"]["phase"] == "load"
    assert by_dev["sa"]["status"] == "ok"
    assert env["status"] == "error"  # roll-up: any device error


def test_orc_build_multi_failed_build_fails_all(monkeypatch, _no_persist):
    monkeypatch.setattr(
        "qactl.jenkins.tools.jenkins_trigger",
        lambda branch, **kw: {"status": "error", "errors": ["FAILURE"], "result": {}})
    monkeypatch.setattr(
        "qactl.dnos.cli.tools.tarload.request_system_tar_load",
        lambda **kw: pytest.fail("load ran despite a failed build"))

    env = tools.orc_build("dev26.3", devices=["cl", "sa"], detach=False)

    assert env["status"] == "error"
    assert all(j["phase"] == "build" for j in env["result"]["jobs"])


# --- show / orphan detection ---------------------------------------------


def test_orc_show_marks_dead_worker_as_error(monkeypatch, tmp_path):
    monkeypatch.setenv("QACTL_STATE_DIR", str(tmp_path))
    # Persist a 'running' job whose worker pid is dead.
    job = tools._new_job(mode="load", device_key="sa", build_url="http://j/1/", branch=None)
    job["status"] = "running"
    job["worker_pid"] = 2  # almost certainly not us / dead-or-not-ours
    tools._persist(job)

    monkeypatch.setattr(tools, "_pid_alive", lambda pid: False)
    env = tools.orc_show(job_id=job["job_id"])

    assert env["status"] == "error"
    assert any("died mid-flight" in e for e in env["errors"])


def test_orc_show_running_without_pid_is_not_downgraded(monkeypatch, tmp_path):
    """A running envelope with no concrete worker_pid (in-flight, pid not yet
    recorded) must NOT be falsely flagged dead."""
    monkeypatch.setenv("QACTL_STATE_DIR", str(tmp_path))
    job = tools._new_job(mode="load", device_key="sa", build_url="http://j/1/", branch=None)
    job["status"] = "running"
    job["worker_pid"] = None
    tools._persist(job)

    monkeypatch.setattr(tools, "_pid_alive", lambda pid: False)
    env = tools.orc_show(job_id=job["job_id"])

    assert env["status"] == "running"


def test_orc_show_needs_a_selector():
    env = tools.orc_show()
    assert env["status"] == "bad_argument"
