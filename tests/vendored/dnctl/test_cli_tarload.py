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
- "file is already registered for download" must NOT abort the run (it is
  the device telling us the tarball is already staged).

No device traffic: ``run_sequence`` is monkeypatched.
"""

import inspect
import types

import pytest

from dnctl.cli.core import job_store
from dnctl.cli.tools import tarload


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


_LOAD_A = "request system target-stack load http://minio/baseos.tar"
_LOAD_B = "request system target-stack load http://minio/dnos.tar"
_ALREADY = "error downloading package. error: file is already registered for download"


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
# worker: idempotent "already registered for download"
# --------------------------------------------------------------------------

def test_already_registered_is_benign_and_run_completes(monkeypatch):
    commands = [_LOAD_A, _LOAD_B]
    steps = [_step(_LOAD_A, _ALREADY), _step(_LOAD_B, "Package loaded.")]
    monkeypatch.setattr(tarload, "run_sequence", lambda reg, **kw: _result(steps))

    job = _job()
    tarload._tar_load_worker(job, "pw", commands)

    assert job.state == "done"
    statuses = {s["command"]: s["status"] for s in job.steps}
    assert statuses[_LOAD_A] == "already_staged"
    assert statuses[_LOAD_B] == "ok"
    assert any("already registered" in w for w in job.warnings)
    # persisted for a later `tar-load show`
    persisted = job_store.load(job.job_id)
    assert persisted is not None and persisted["status"] == "ok"


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
