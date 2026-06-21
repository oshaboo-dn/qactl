"""CLI quick-win fixes:

- ``job_store`` path-traversal hardening + per-family namespaces.
- tech-support one-shot CLI sync worker + disk persistence (issue #17,
  same class as tar-load).
- ``device add`` CLI no longer passes a ``name`` the tool rejects.
- the operational ``clear`` command is wired into the CLI.

No device traffic: the device-touching paths are exercised only up to
the validation / gate boundary, and the persistence paths drive the
job store directly.
"""

import inspect
import json

import pytest
from typer.testing import CliRunner

from dnctl.__main__ import app
from dnctl.cli import app as cli_app
from dnctl.cli.core import job_store
from dnctl.cli.tools import devices, techsupport

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DNCTL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("DNCTL_DEVICES", raising=False)
    techsupport._TS_REGISTRY._jobs.clear()
    techsupport._TS_REGISTRY._active.clear()
    yield


# --------------------------------------------------------------------------
# job_store: path traversal + namespaces
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    ["../escape", "../../etc/passwd", "a/b", "a\\b", ".", "..", "", "a" * 200],
)
def test_job_store_rejects_unsafe_job_ids(bad):
    # save() must not write outside the cache dir and load() must refuse.
    job_store.save({"job_id": bad, "state": "done"})
    assert job_store.load(bad) is None


def test_job_store_load_traversal_does_not_read_outside(tmp_path, monkeypatch):
    # Plant a JSON file one level above the cache dir; a traversing id
    # must NOT be able to read it.
    monkeypatch.setenv("DNCTL_STATE_DIR", str(tmp_path / "state"))
    job_store.save({"job_id": "dev-x-aaaaaa", "device": "dev", "state": "done"})
    outside = tmp_path / "state" / "cli" / "secret.json"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(json.dumps({"job_id": "secret"}))
    assert job_store.load("../secret") is None


def test_job_store_namespaces_are_isolated():
    job_store.save({"job_id": "dev-1-aaa", "device": "dev", "state": "done"}, "ns-a")
    job_store.save({"job_id": "dev-2-bbb", "device": "dev", "state": "done"}, "ns-b")
    assert job_store.load("dev-1-aaa", "ns-a") is not None
    assert job_store.load("dev-1-aaa", "ns-b") is None
    # latest_for_device is scoped to the namespace
    assert job_store.latest_for_device("dev", "ns-a")["job_id"] == "dev-1-aaa"
    assert job_store.latest_for_device("dev", "ns-b")["job_id"] == "dev-2-bbb"


# --------------------------------------------------------------------------
# tech-support #17: block param + disk persistence + disk fallback
# --------------------------------------------------------------------------

def test_techsupport_block_param_defaults_async():
    p = inspect.signature(techsupport.create_techsupport).parameters
    assert "block" in p
    assert p["block"].default is False


def _ts_job(job_id="dev-diag-abcdef", device="dev"):
    return techsupport.TsJob(
        job_id=job_id, device=device, host=None, device_key=device,
        resolved_host="HOST1", state="generating",
        started_utc="2026-06-21T00:00:00Z", user="dnroot",
        name="diag", local_filename="dev__x__diag.tar", ts_path="dn@dnftp:/x",
        vrf="mgmt0", poll_interval_s=30, max_wait_s=600, timeout=120,
        notify_channel="",
    )


def test_ts_finish_persists_terminal_envelope():
    job = _ts_job()
    techsupport._ts_finish(job, "done")
    persisted = job_store.load(job.job_id, techsupport._TS_JOB_STORE_SUBDIR)
    assert persisted is not None
    assert persisted["status"] == "ok"
    assert persisted["job_id"] == job.job_id


def test_get_techsupport_job_falls_back_to_disk_by_job_id():
    job = _ts_job(job_id="dev-diag-zzzzzz")
    techsupport._ts_finish(job, "done")
    # simulate a fresh CLI process: registry empty
    techsupport._TS_REGISTRY._jobs.clear()
    techsupport._TS_REGISTRY._active.clear()
    got = techsupport.get_techsupport_job(job_id="dev-diag-zzzzzz")
    assert got["job_id"] == "dev-diag-zzzzzz"
    assert got["status"] == "ok"


def test_get_techsupport_job_falls_back_to_disk_by_device():
    job = _ts_job(job_id="dev-diag-yyyyyy")
    techsupport._ts_finish(job, "done")
    techsupport._TS_REGISTRY._jobs.clear()
    techsupport._TS_REGISTRY._active.clear()
    got = techsupport.get_techsupport_job(device="dev")
    assert got["job_id"] == "dev-diag-yyyyyy"


def test_get_techsupport_job_does_not_cross_into_tarload_namespace():
    # A tar-load envelope for the device must not satisfy a ts lookup.
    job_store.save(
        {"job_id": "dev-907-abcdef", "device": "dev", "state": "done", "status": "ok"},
    )  # default (tar-load) namespace
    got = techsupport.get_techsupport_job(device="dev")
    assert got["status"] == "error"


def test_get_techsupport_job_true_miss_is_error():
    got = techsupport.get_techsupport_job(job_id="never-existed")
    assert got["status"] == "error"


# --------------------------------------------------------------------------
# device add: no more rejected name= positional
# --------------------------------------------------------------------------

def test_device_add_cli_takes_sn_not_name():
    params = list(inspect.signature(cli_app.device_add).parameters)
    assert params[0] == "sn"
    assert "name" not in params


def test_manage_device_add_still_rejects_name():
    # The tool contract the CLI must respect: name is not accepted on add.
    r = devices.manage_device(operation="add", name="foo", sn="1.2.3.4")
    assert r["status"] == "error"
    assert any("not accepted for operation='add'" in e for e in r["errors"])


# --------------------------------------------------------------------------
# clear: wired into the CLI + gated
# --------------------------------------------------------------------------

def test_clear_command_is_registered():
    names = {c.name for c in cli_app.app.registered_commands}
    assert "clear" in names


def test_clear_refuses_without_yes():
    r = runner.invoke(app, ["cli", "clear", "clear arp", "-d", "sa", "--json"])
    assert r.exit_code == 2
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert any("--yes" in n for n in payload["next_actions"])


def test_clear_invalid_command_errors_before_device():
    # --yes passes the gate; a non-'clear' verb is rejected by validation.
    r = runner.invoke(
        app, ["cli", "clear", "show foo", "-d", "sa", "--yes", "--json"]
    )
    assert r.exit_code == 1
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
