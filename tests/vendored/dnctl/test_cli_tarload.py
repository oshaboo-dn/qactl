"""tar-load CLI front: synchronous worker, disk persistence, idempotent
re-load (issue #17).

The async job model (in-memory registry + daemon worker) only works for
the long-running MCP server. Under the one-shot CLI the process exits the
moment ``start`` returns, so:

- the worker must run synchronously (``block=True``) — covered by the
  signature check + worker tests here driving ``_tar_load_worker``
  directly with a faked ``run_sequence``;
- the terminal envelope must be persisted to disk so ``tar-load show``
  resolves from a later process;
- "file is already registered for download" must NOT abort the run, but it
  is only benign once the component is confirmed present in the
  ``show system stack`` target — a worker killed mid-download leaves the
  registration behind with nothing staged (issue #77).

No device traffic: ``run_sequence`` / ``run_once`` are monkeypatched.
"""

import inspect
import json
import os
import types

import pytest
from typer.testing import CliRunner

from dnctl.__main__ import app
from dnctl.cli.core import job_store
from dnctl.cli.tools import tarload

runner = CliRunner()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _step(command, output, hit_prompt=True):
    return types.SimpleNamespace(
        command=command,
        output=output,
        hit_prompt=hit_prompt,
        head_prompt_line=f"HOST# {command}",
        tail_prompt="HOST#",
    )


def _result(steps, host="HOST1", device="dev"):
    return types.SimpleNamespace(
        host=host,
        device=device,
        output="\n".join(s.output for s in steps),
        head_prompt_line="HOST#",
        tail_prompt="HOST#",
        steps=steps,
    )


def _job(job_id="dev-7-abcdef", device="dev"):
    return tarload.TarLoadJob(
        job_id=job_id, device=device, host=None, device_key=device,
        resolved_host="HOST1", state="loading",
        started_utc="2026-06-21T00:00:00Z", user="dnroot",
        device_mode="gi", pre_check_requested=False, effective_pre_check=False,
        step_timeout=1800, notify_channel="",
    )


_BASEOS_TAR = "http://minio/pkg/drivenets_baseos_2.2620287169.tar"
_DNOS_TAR = "http://minio/pkg/drivenets_dnos_26.2.0.610_dev.dev_v26_2_1565.tar"
_LOAD_A = f"request system target-stack load {_BASEOS_TAR}"
_LOAD_B = f"request system target-stack load {_DNOS_TAR}"
_ALREADY = "error downloading package. error: file is already registered for download"


def _stack_table(rows):
    """Render ``show system stack`` output from (label, current, target)."""
    body = "\n".join(
        f"| {label:<11} | default | default | - | {cur:<20} | {tgt:<20} |"
        for label, cur, tgt in rows
    )
    return (
        "| Component   | HW Model | HW Revision | Revert | Current | Target |\n"
        "|-------------+----------+-------------+--------+---------+--------|\n"
        + body
    )


def _fake_run_once(stack_output):
    def _run_once(reg, **kw):
        assert kw.get("command") == tarload._SHOW_STACK_CMD
        return types.SimpleNamespace(output=stack_output)
    return _run_once


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DNCTL_STATE_DIR", str(tmp_path))
    # Don't let a stale in-memory job from another test leak across.
    tarload._TARLOAD_REGISTRY._jobs.clear()
    tarload._TARLOAD_REGISTRY._active.clear()
    yield


# --------------------------------------------------------------------------
# signature / contract
# --------------------------------------------------------------------------

def test_block_param_exists_on_both_kickoffs():
    assert "block" in inspect.signature(tarload.request_system_tar_load).parameters
    assert "block" in inspect.signature(tarload.request_system_pre_check).parameters
    # default must stay async so the MCP server behaviour is unchanged
    assert inspect.signature(
        tarload.request_system_tar_load
    ).parameters["block"].default is False


def test_confirm_defaults_to_false():
    # The destructive-op gate must default to a dry-run (#28).
    assert inspect.signature(
        tarload.request_system_tar_load
    ).parameters["confirm"].default is False


# --------------------------------------------------------------------------
# confirm gate (#28): confirm=False is a dry-run that touches nothing
# --------------------------------------------------------------------------

_VALID_URL = "https://jenkins.dev.drivenets.net/job/foo/dev_v26_2/907/"


def test_confirm_false_is_dry_run_no_network(monkeypatch):
    # If the gate leaks past, it would probe the device / fetch Jenkins.
    def _boom(*a, **k):
        raise AssertionError("dry-run must not touch the network/device")

    monkeypatch.setattr(tarload, "run_once", _boom)
    monkeypatch.setattr(tarload, "run_sequence", _boom)
    monkeypatch.setattr(tarload, "_fetch_jenkins_artifact", _boom)

    env = tarload.request_system_tar_load(jenkins_url=_VALID_URL, device="dev")

    assert env["status"] == "dry_run"
    assert env["jenkins_url"] == _VALID_URL.rstrip("/")
    assert env["components_requested"] == "all"
    assert env["pre_check_requested"] is True
    assert any("confirm=true" in w for w in env["warnings"])
    # nothing was registered as an active job
    assert tarload._TARLOAD_REGISTRY.active_for_device("dev") is None


def test_confirm_false_dry_run_still_validates_inputs(monkeypatch):
    monkeypatch.setattr(tarload, "run_once",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    # bad URL must still error out, not return a dry-run
    env = tarload.request_system_tar_load(jenkins_url="http://evil/x/1", device="dev")
    assert env["status"] == "error"
    # missing device/host must still error out
    env2 = tarload.request_system_tar_load(jenkins_url=_VALID_URL)
    assert env2["status"] == "error"


# --------------------------------------------------------------------------
# job_store
# --------------------------------------------------------------------------

def test_job_store_roundtrip():
    env = {"job_id": "dev-7-aaa", "device": "dev", "state": "done", "status": "ok"}
    job_store.save(env)
    assert job_store.load("dev-7-aaa") == env
    assert job_store.load("missing") is None


def test_job_store_save_no_job_id_is_noop():
    job_store.save({"state": "done"})  # must not raise
    assert job_store.latest_for_device("dev") is None


def test_job_store_latest_for_device_picks_newest():
    import time
    job_store.save({"job_id": "dev-1-aaa", "device": "dev", "state": "error"})
    time.sleep(0.01)
    job_store.save({"job_id": "dev-2-bbb", "device": "dev", "state": "done"})
    job_store.save({"job_id": "other-9-ccc", "device": "other", "state": "done"})
    latest = job_store.latest_for_device("dev")
    assert latest["job_id"] == "dev-2-bbb"
    assert job_store.latest_for_device("other")["job_id"] == "other-9-ccc"
    assert job_store.latest_for_device("nope") is None


# --------------------------------------------------------------------------
# get_tar_load_job disk fallback (issue #17)
# --------------------------------------------------------------------------

def test_get_tar_load_job_falls_back_to_disk_by_job_id():
    env = {"job_id": "dev-7-zzz", "device": "dev", "state": "done", "status": "ok"}
    job_store.save(env)
    # registry is empty (simulating a fresh CLI process)
    got = tarload.get_tar_load_job(job_id="dev-7-zzz")
    assert got["job_id"] == "dev-7-zzz"
    assert got["status"] == "ok"


def test_get_tar_load_job_falls_back_to_disk_by_device():
    job_store.save({"job_id": "dev-7-zzz", "device": "dev", "state": "done", "status": "ok"})
    got = tarload.get_tar_load_job(device="dev")
    assert got["job_id"] == "dev-7-zzz"


def test_get_tar_load_job_disk_hit_wrong_device_is_rejected():
    job_store.save({"job_id": "dev-7-zzz", "device": "dev", "state": "done", "status": "ok"})
    got = tarload.get_tar_load_job(job_id="dev-7-zzz", device="other")
    assert got["status"] == "error"


def test_get_tar_load_job_true_miss_is_error():
    got = tarload.get_tar_load_job(job_id="never-existed")
    assert got["status"] == "error"
    assert "No tar-load job" in " ".join(got.get("errors", []))


# --------------------------------------------------------------------------
# worker: "already registered for download" — benign ONLY when the
# component is verified staged in `show system stack` (issues #17 + #77)
# --------------------------------------------------------------------------

def test_already_registered_verified_staged_is_benign(monkeypatch):
    commands = [_LOAD_A, _LOAD_B]
    steps = [_step(_LOAD_A, _ALREADY), _step(_LOAD_B, "Package loaded.")]
    monkeypatch.setattr(tarload, "run_sequence", lambda reg, **kw: _result(steps))
    # BASEOS target matches the tar version → genuinely already staged.
    monkeypatch.setattr(tarload, "run_once", _fake_run_once(_stack_table([
        ("BASEOS", "2.2620000000", "2.2620287169"),
        ("DNOS", "26.1.0.1", "26.2.0.610_dev.dev_v26_2_1565"),
    ])))

    job = _job()
    tarload._tar_load_worker(job, "pw", commands)

    assert job.state == "done"
    statuses = {s["command"]: s["status"] for s in job.steps}
    assert statuses[_LOAD_A] == "already_staged"
    assert statuses[_LOAD_B] == "ok"
    assert any("already registered" in w and "verified" in w
               for w in job.warnings)
    # persisted for a later `tar-load show`
    persisted = job_store.load(job.job_id)
    assert persisted is not None and persisted["status"] == "ok"


def test_already_registered_component_missing_fails_loudly(monkeypatch):
    # Issue #77: prior worker killed mid-download → registration left
    # behind, DNOS row absent from `show system stack`. Must NOT exit 0.
    commands = [_LOAD_B]
    steps = [_step(_LOAD_B, _ALREADY)]
    monkeypatch.setattr(tarload, "run_sequence", lambda reg, **kw: _result(steps))
    monkeypatch.setattr(tarload, "run_once", _fake_run_once(_stack_table([
        ("BASEOS", "2.2620287169", "2.2620287169"),
        ("GI", "26.2.0.1", "26.2.0.610_dev.dev_v26_2_1565"),
    ])))

    job = _job()
    tarload._tar_load_worker(job, "pw", commands)

    assert job.state == "error"
    assert job.steps[0]["status"] == "error"
    assert any("NOT staged" in e for e in job.errors)
    assert any("-c dnos" in a for a in job.next_actions)
    persisted = job_store.load(job.job_id)
    assert persisted is not None and persisted["status"] == "error"


def test_already_registered_wrong_target_version_fails(monkeypatch):
    # Registration matched, but the target stack holds a DIFFERENT dnos
    # version — the requested tar was never staged.
    commands = [_LOAD_B]
    steps = [_step(_LOAD_B, _ALREADY)]
    monkeypatch.setattr(tarload, "run_sequence", lambda reg, **kw: _result(steps))
    monkeypatch.setattr(tarload, "run_once", _fake_run_once(_stack_table([
        ("DNOS", "26.1.0.1", "26.1.0.463_dev.dev_v26_1_1344"),
    ])))

    job = _job()
    tarload._tar_load_worker(job, "pw", commands)

    assert job.state == "error"
    assert job.steps[0]["status"] == "error"
    assert any("26.1.0.463_dev.dev_v26_1_1344" in e for e in job.errors)


def test_already_registered_unverifiable_probe_fails_loudly(monkeypatch):
    # If the verification probe itself dies we must not silently assume
    # staged — fail loudly instead.
    commands = [_LOAD_B]
    steps = [_step(_LOAD_B, _ALREADY)]
    monkeypatch.setattr(tarload, "run_sequence", lambda reg, **kw: _result(steps))

    def _boom(reg, **kw):
        raise tarload.ConnectError("channel died")

    monkeypatch.setattr(tarload, "run_once", _boom)

    job = _job()
    tarload._tar_load_worker(job, "pw", commands)

    assert job.state == "error"
    assert any("could not be verified" in e for e in job.errors)


def test_no_already_registered_skips_stack_probe(monkeypatch):
    # Clean loads must not pay the extra `show system stack` round-trip.
    commands = [_LOAD_A, _LOAD_B]
    steps = [_step(_LOAD_A, "Package loaded."), _step(_LOAD_B, "Package loaded.")]
    monkeypatch.setattr(tarload, "run_sequence", lambda reg, **kw: _result(steps))
    monkeypatch.setattr(
        tarload, "run_once",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("clean run must not probe the stack")),
    )

    job = _job()
    tarload._tar_load_worker(job, "pw", commands)
    assert job.state == "done"


def test_parse_stack_targets_real_table():
    out = (
        "| Component   | HW Model   | HW Revision   | Revert   | Current | Target |\n"
        "|-------------+------------+---------------+----------+---------+--------|\n"
        "| BASEOS      | default    | default       | -        | 2.2630801007 | 2.2630801007 |\n"
        "| DNOS        | default    | default       | -        | 26.3.0.7_priv.x | 26.3.0.7_priv.y |\n"
        "| GI          | default    | default       | -        | 26.3.0.7_priv.x | 26.3.0.7_priv.y |\n"
        "\n"
        "Stack replication status:\n"
        "| Stack   | Sync status   |\n"
        "|---------+---------------|\n"
        "| current | REPLICATED    |\n"
    )
    assert tarload._parse_stack_targets(out) == {
        "BASEOS": "2.2630801007",
        "DNOS": "26.3.0.7_priv.y",
        "GI": "26.3.0.7_priv.y",
    }
    assert tarload._parse_stack_targets("") == {}


def test_real_download_error_still_aborts(monkeypatch):
    commands = [_LOAD_A, _LOAD_B]
    # device errored on A and never ran B (stop_predicate would abort)
    steps = [_step(_LOAD_A, "error downloading package. error: disk full")]
    monkeypatch.setattr(tarload, "run_sequence", lambda reg, **kw: _result(steps))

    job = _job()
    tarload._tar_load_worker(job, "pw", commands)

    assert job.state == "error"
    statuses = {s["command"]: s["status"] for s in job.steps}
    assert statuses[_LOAD_A] == "error"
    assert statuses[_LOAD_B] == "skipped"


def test_stop_predicate_treats_already_registered_as_non_stop():
    # The benign line must not trip the sequence's abort predicate either.
    assert tarload._ALREADY_REGISTERED_RE.search(_ALREADY)
    assert not tarload._ALREADY_REGISTERED_RE.search("error downloading package. error: disk full")


# --------------------------------------------------------------------------
# CLI `-c/--component` plumbing (#30): the option must forward through to
# request_system_tar_load(components=...). The library already implements
# selection; the gap was pure CLI exposure.
# --------------------------------------------------------------------------

def _capture_tar_load(monkeypatch):
    """Replace the app's request_system_tar_load with a capturing stub.

    Returns the dict the stub fills in with the kwargs it was called
    with. The explicit params (not just **kw) matter: O.call filters
    kwargs against the target's signature, so the stub must name the
    params it wants to observe.
    """
    captured: dict = {}

    def _stub(jenkins_url=None, pre_check=True, components=None,
              confirm=False, block=False, detach=False, device=None,
              host=None, user=None, password=None, **kw):
        captured.update(
            jenkins_url=jenkins_url, pre_check=pre_check,
            components=components, confirm=confirm, block=block,
            detach=detach, device=device,
        )
        return {"status": "ok", "state": "done", "job_id": "dev-907-aaa",
                "device": device}

    from dnctl.cli import app as cli_app
    monkeypatch.setattr(cli_app, "request_system_tar_load", _stub)
    return captured


def test_component_option_forwards_single(monkeypatch):
    captured = _capture_tar_load(monkeypatch)
    r = runner.invoke(
        app,
        ["cli", "tar-load", "start", _VALID_URL, "-d", "dev",
         "-c", "dnos", "--yes", "--json"],
    )
    assert r.exit_code == 0, r.stdout
    assert captured["components"] == ["dnos"]
    assert captured["confirm"] is True and captured["block"] is True


def test_component_option_forwards_multiple_in_order(monkeypatch):
    captured = _capture_tar_load(monkeypatch)
    r = runner.invoke(
        app,
        ["cli", "tar-load", "start", _VALID_URL, "-d", "dev",
         "-c", "baseos", "-c", "dnos", "-c", "gi", "--yes", "--json"],
    )
    assert r.exit_code == 0, r.stdout
    assert captured["components"] == ["baseos", "dnos", "gi"]


def test_no_component_loads_all(monkeypatch):
    captured = _capture_tar_load(monkeypatch)
    r = runner.invoke(
        app,
        ["cli", "tar-load", "start", _VALID_URL, "-d", "dev",
         "--yes", "--json"],
    )
    assert r.exit_code == 0, r.stdout
    # No -c given → components omitted → library's load-all default.
    assert captured["components"] is None


def test_invalid_component_clean_error_no_device(monkeypatch):
    # End-to-end through the real tool: an unknown -c must be rejected by
    # validation before any device is touched.
    monkeypatch.setattr(
        tarload, "run_once",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not touch the device on a bad component")),
    )
    monkeypatch.setattr(
        tarload, "_fetch_jenkins_artifact",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not fetch Jenkins on a bad component")),
    )
    r = runner.invoke(
        app,
        ["cli", "tar-load", "start", _VALID_URL, "-d", "dev",
         "-c", "bogus", "--yes", "--json"],
    )
    assert r.exit_code == 1
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert any("bogus" in e for e in payload["errors"])


def test_component_help_lists_values():
    r = runner.invoke(app, ["cli", "tar-load", "start", "--help"])
    assert r.exit_code == 0
    assert "--component" in r.stdout
    assert "-c" in r.stdout


# --------------------------------------------------------------------------
# --no-wait: detached worker, live persisted state, cross-process device
# guard, orphan detection (issue #76)
# --------------------------------------------------------------------------

_GI_TAR = "http://minio/pkg/drivenets_gi_26.2.0.610_dev.dev_v26_2_1565.tar"


def _mock_kickoff_network(monkeypatch):
    """Mock the Jenkins fetches + `show system` probe so a confirm=True
    kickoff reaches the guard / registration without any network."""
    urls = {"gi_DNOS_artifact.txt": _DNOS_TAR, "gi_GI_artifact.txt": _GI_TAR}

    def _fetch(base, name, fetch_timeout):
        return urls.get(name), None

    monkeypatch.setattr(tarload, "_fetch_jenkins_artifact", _fetch)

    def _probe(reg, **kw):
        assert kw.get("command") == tarload._SHOW_SYSTEM_CMD
        return types.SimpleNamespace(
            output="Version: DNOS [26.2.0.610]", hit_prompt=True,
            head_prompt_line="HOST#", tail_prompt="HOST#",
            host="HOST1", device="dev", steps=[],
        )

    monkeypatch.setattr(tarload, "run_once", _probe)


def _clean_sequence(commands_seen=None):
    """A run_sequence stand-in that answers every command cleanly and
    drives the stop_predicate exactly like the real one."""
    def _fake(reg, **kw):
        steps = [_step(c, "Package loaded.") for c in kw["commands"]]
        sp = kw.get("stop_predicate")
        for s in steps:
            if sp is not None and sp(s):
                break
        if commands_seen is not None:
            commands_seen.extend(kw["commands"])
        return _result(steps)
    return _fake


def test_detach_param_exists_default_false():
    params = inspect.signature(tarload.request_system_tar_load).parameters
    assert "detach" in params
    assert params["detach"].default is False


def test_pid_alive_basics():
    import subprocess
    assert tarload._pid_alive(os.getpid()) is True
    assert tarload._pid_alive(None) is False
    assert tarload._pid_alive(-1) is False
    assert tarload._pid_alive(0) is False
    assert tarload._pid_alive(True) is False
    p = subprocess.Popen(["true"])
    p.wait()  # reaped → the pid no longer exists
    assert tarload._pid_alive(p.pid) is False


def test_detach_kickoff_returns_immediately(monkeypatch):
    _mock_kickoff_network(monkeypatch)
    monkeypatch.setattr(tarload.os, "fork", lambda: 4242)
    monkeypatch.setattr(
        tarload, "run_sequence",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("parent must not run the load sequence")),
    )

    env = tarload.request_system_tar_load(
        jenkins_url=_VALID_URL, device="dev", confirm=True,
        detach=True, pre_check=False, notify_slack="",
    )

    # Successful kickoff exits 0 under the CLI contract; the job itself
    # is still loading and carries the detached worker's pid.
    assert env["status"] == "ok"
    assert env["state"] == "loading"
    assert env["worker_pid"] == 4242
    assert env["eta_s"] > 0
    assert any("tar-load show" in a for a in env["next_actions"])
    # The parent persisted the kickoff snapshot so `show` resolves the
    # job_id even before the child's first save.
    persisted = job_store.load(env["job_id"])
    assert persisted is not None
    assert persisted["state"] == "loading"
    assert persisted["worker_pid"] == 4242


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_detach_real_fork_runs_load_to_done(monkeypatch):
    _mock_kickoff_network(monkeypatch)
    monkeypatch.setattr(tarload, "run_sequence", _clean_sequence())

    env = tarload.request_system_tar_load(
        jenkins_url=_VALID_URL, device="dev", confirm=True,
        detach=True, pre_check=False, notify_slack="",
    )
    assert env["state"] == "loading"
    pid = env["worker_pid"]
    assert isinstance(pid, int) and pid > 0

    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0

    # Simulate a fresh process for `show`: the in-memory registry is
    # stale in the kickoff process (the child owned the job).
    tarload._TARLOAD_REGISTRY._jobs.clear()
    tarload._TARLOAD_REGISTRY._active.clear()
    got = tarload.get_tar_load_job(job_id=env["job_id"])
    assert got["state"] == "done"
    assert got["status"] == "ok"
    statuses = {s["command"]: s["status"] for s in got["steps"]}
    assert statuses[f"request system target-stack load {_DNOS_TAR}"] == "ok"
    assert statuses[f"request system target-stack load {_GI_TAR}"] == "ok"


def test_worker_persists_live_progress(monkeypatch):
    # A separate process polling `tar-load show` mid-flight must see the
    # in-flight state and the per-component steps completed so far.
    commands = [_LOAD_A, _LOAD_B]
    observed = []

    def _fake_sequence(reg, **kw):
        sp = kw["stop_predicate"]
        for cmd in kw["commands"]:
            sp(_step(cmd, "Package loaded."))
            persisted = job_store.load("dev-7-abcdef")
            observed.append(
                (persisted["state"], len(persisted["steps"]),
                 persisted["worker_pid"]),
            )
        return _result([_step(c, "Package loaded.") for c in kw["commands"]])

    monkeypatch.setattr(tarload, "run_sequence", _fake_sequence)
    job = _job()
    tarload._tar_load_worker(job, "pw", commands)

    me = os.getpid()
    assert observed == [("loading", 1, me), ("loading", 2, me)]
    final = job_store.load(job.job_id)
    assert final["state"] == "done"
    assert len(final["steps"]) == 2


def test_kickoff_refuses_when_other_process_load_alive(monkeypatch):
    _mock_kickoff_network(monkeypatch)
    monkeypatch.setattr(
        tarload, "run_sequence",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("guard must fire before the load sequence")),
    )
    monkeypatch.setattr(tarload, "_pid_alive", lambda pid: True)
    job_store.save({
        "job_id": "dev-1565-live", "device": "dev", "state": "loading",
        "worker_pid": 99999, "started_utc": "2026-07-05T12:00:00Z",
    })

    env = tarload.request_system_tar_load(
        jenkins_url=_VALID_URL, device="dev", confirm=True,
        pre_check=False, notify_slack="",
    )

    assert env["status"] == "error"
    joined = " ".join(env["errors"])
    assert "another process" in joined and "dev-1565-live" in joined
    # the live job was not disturbed
    assert job_store.load("dev-1565-live")["state"] == "loading"


def test_kickoff_flips_stale_load_and_proceeds(monkeypatch):
    # A persisted "loading" job whose worker died must not block new
    # kickoffs forever — it is flipped to error and the load proceeds.
    _mock_kickoff_network(monkeypatch)
    monkeypatch.setattr(tarload, "run_sequence", _clean_sequence())
    monkeypatch.setattr(tarload, "_pid_alive", lambda pid: False)
    job_store.save({
        "job_id": "dev-1500-dead", "device": "dev", "state": "loading",
        "worker_pid": 99999, "started_utc": "2026-07-05T11:00:00Z",
    })

    env = tarload.request_system_tar_load(
        jenkins_url=_VALID_URL, device="dev", confirm=True,
        pre_check=False, notify_slack="", block=True,
    )

    assert env["state"] == "done"
    stale = job_store.load("dev-1500-dead")
    assert stale["state"] == "error"
    assert any("no longer running" in e for e in stale["errors"])


def test_show_flags_orphaned_job(monkeypatch):
    monkeypatch.setattr(tarload, "_pid_alive", lambda pid: False)
    job_store.save({
        "job_id": "dev-1565-orphan", "device": "dev", "state": "loading",
        "status": "running", "worker_pid": 99999,
    })

    got = tarload.get_tar_load_job(job_id="dev-1565-orphan")

    assert got["state"] == "error"
    assert got["status"] == "error"
    assert any("no longer running" in e for e in got["errors"])
    # the flip is persisted so the device guard won't refuse new loads
    assert job_store.load("dev-1565-orphan")["state"] == "error"


def test_pre_check_kickoff_honours_cross_process_guard(monkeypatch):
    def _probe(reg, **kw):
        return types.SimpleNamespace(
            output="Version: DNOS [26.2.0.610]", hit_prompt=True,
            head_prompt_line="HOST#", tail_prompt="HOST#",
            host="HOST1", device="dev", steps=[],
        )

    monkeypatch.setattr(tarload, "run_once", _probe)
    monkeypatch.setattr(tarload, "_pid_alive", lambda pid: True)
    job_store.save({
        "job_id": "dev-1565-live", "device": "dev", "state": "loading",
        "worker_pid": 99999,
    })

    env = tarload.request_system_pre_check(device="dev", notify_slack="")
    assert env["status"] == "error"
    assert "another process" in " ".join(env["errors"])


def test_no_wait_flag_forwards_detach(monkeypatch):
    captured = _capture_tar_load(monkeypatch)
    r = runner.invoke(
        app,
        ["cli", "tar-load", "start", _VALID_URL, "-d", "dev",
         "--no-wait", "--yes", "--json"],
    )
    assert r.exit_code == 0, r.stdout
    assert captured["detach"] is True
    assert captured["block"] is False


def test_default_stays_blocking(monkeypatch):
    captured = _capture_tar_load(monkeypatch)
    r = runner.invoke(
        app,
        ["cli", "tar-load", "start", _VALID_URL, "-d", "dev",
         "--yes", "--json"],
    )
    assert r.exit_code == 0, r.stdout
    assert captured["detach"] is False
    assert captured["block"] is True


def test_no_wait_in_help():
    r = runner.invoke(app, ["cli", "tar-load", "start", "--help"])
    assert r.exit_code == 0
    assert "--no-wait" in r.stdout
