"""CLI quick-win fixes:

- ``job_store`` path-traversal hardening + per-family namespaces.
- tech-support one-shot CLI sync worker + disk persistence (issue #17,
  same class as tar-load).
- ``device add`` CLI keys by the operator-chosen ``name`` (the chassis
  System Name is metadata only).
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
from dnctl.cli.core import job_store, ts_store
from dnctl.cli.core.dnftp import DnftpNotConfigured
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

def test_device_add_cli_takes_name_and_host():
    params = list(inspect.signature(cli_app.device_add).parameters)
    # The positional is now the operator-chosen registry name; the SSH
    # target is a separate --host, and --alias attaches nicknames.
    assert params[0] == "name"
    assert "host" in params
    assert "alias" in params


def test_manage_device_add_keys_by_name_not_system_name(monkeypatch):
    # name= is now the registry key; the chassis System Name is metadata.
    from dnctl.core.cli_probe import DeviceProbe

    monkeypatch.setattr(
        devices, "probe_device",
        lambda *a, **k: DeviceProbe(
            system_name="chassis-xyz", system_id=None, expected_role="CL",
            mgmt0="10.0.0.9", ncc_serials=[], mode="operational",
        ),
    )
    monkeypatch.setattr(devices, "_post_add_init", lambda device: (None, []))

    r = devices.manage_device(operation="add", name="foo", sn="1.2.3.4")
    assert r["status"] == "ok"
    assert r["device"] == "foo"
    assert r["derived_name_source"] == "explicit"
    assert r["entry"]["system_name"] == "chassis-xyz"


# --------------------------------------------------------------------------
# manual vendor on add: cisco / juniper skip the DNOS probe
# --------------------------------------------------------------------------

def test_device_add_cli_has_vendor_option():
    params = list(inspect.signature(cli_app.device_add).parameters)
    assert "vendor" in params


def test_manage_device_add_cisco_skips_probe(monkeypatch):
    # A non-DNOS add must never SSH-probe: blow up if it tries.
    def _boom(*a, **k):
        raise AssertionError("probe_device must not run for non-DNOS vendors")

    monkeypatch.setattr(devices, "probe_device", _boom)
    r = devices.manage_device(operation="add", sn="100.64.14.197", vendor="cisco")
    assert r["status"] == "ok"
    assert r["vendor"] == "cisco"
    assert r["device"] == "100.64.14.197"
    assert r["hosts"] == ["100.64.14.197"]
    # An IPv4 sn populates mgmt0 as the transport target.
    assert r["entry"]["mgmt0"] == "100.64.14.197"
    assert r["entry"]["vendor"] == "cisco"


def test_manage_device_add_juniper_uses_alias(monkeypatch):
    monkeypatch.setattr(
        devices, "probe_device",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no probe")),
    )
    r = devices.manage_device(
        operation="add", sn="jun204-rt01", alias="jun-rt01", vendor="juniper"
    )
    assert r["status"] == "ok"
    assert r["vendor"] == "juniper"
    assert r["device"] == "jun-rt01"
    # A hostname (not an IP) leaves mgmt0 unset for a later edit.
    assert "mgmt0" not in r["entry"]


def test_manage_device_add_vendor_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(
        devices, "probe_device",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no probe")),
    )
    r = devices.manage_device(operation="add", sn="10.0.0.9", vendor="Cisco")
    assert r["status"] == "ok"
    assert r["vendor"] == "cisco"


def test_manage_device_add_rejects_unknown_vendor():
    r = devices.manage_device(operation="add", sn="1.2.3.4", vendor="nokia")
    assert r["status"] == "error"
    assert any("not supported" in e for e in r["errors"])


def test_list_devices_reports_vendor(monkeypatch):
    monkeypatch.setattr(
        devices, "probe_device",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no probe")),
    )
    devices.manage_device(operation="add", sn="100.64.14.197", vendor="cisco")
    listed = devices.list_devices()["devices"]
    entry = next(d for d in listed if d["device"] == "100.64.14.197")
    assert entry["vendor"] == "cisco"


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


# --------------------------------------------------------------------------
# search: invalid SCOPE is a clean usage error, not a KeyError (issue #50)
# --------------------------------------------------------------------------

def test_search_invalid_scope_errors_before_device():
    # An unknown scope used to crash with KeyError + exit 0; it must now be
    # a clean error envelope with a non-zero exit and the valid choices.
    r = runner.invoke(
        app, ["cli", "search", "oper", "transceiver", "-d", "sa", "--json"]
    )
    assert r.exit_code == 1
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert any("invalid SCOPE" in e for e in payload["errors"])
    assert any("'oper'" in e for e in payload["errors"])
    assert any("show_config" in e for e in payload["errors"])


# --------------------------------------------------------------------------
# techsupport list: enumerate bundles on dnftp (issue #38)
# --------------------------------------------------------------------------

def _tsfile(filename, size=2_000_000):
    parsed = ts_store.parse_filename(filename)
    return ts_store.TSFile(
        filename=parsed.filename, device=parsed.device,
        timestamp_utc=parsed.timestamp_utc, name=parsed.name,
        size_bytes=size, path=parsed.path,
    )


def test_list_techsupports_registered_on_cli():
    names = {c.name for c in cli_app.ts_app.registered_commands}
    assert "list" in names


def test_list_techsupports_happy_path(monkeypatch):
    files = [
        _tsfile("sa__20260623-100123__diag.tar"),
        _tsfile("sa__20260622-090000__nightly.tar"),
    ]
    monkeypatch.setattr(ts_store, "list_ts", lambda device=None, limit=100: files)
    monkeypatch.setattr(ts_store, "list_orphans", lambda: [])
    r = techsupport.list_techsupports(device="sa")
    assert r["status"] == "ok"
    assert r["count"] == 2
    assert r["techsupports"][0]["filename"] == "sa__20260623-100123__diag.tar"
    assert r["techsupports"][0]["name"] == "diag"
    assert r["techsupports"][0]["size_bytes"] == 2_000_000
    assert r["ts_dir"] == ts_store.TS_DIR


def test_list_techsupports_surfaces_orphans(monkeypatch):
    monkeypatch.setattr(ts_store, "list_ts", lambda device=None, limit=100: [])
    monkeypatch.setattr(ts_store, "list_orphans", lambda: ["junk.tar"])
    r = techsupport.list_techsupports()
    assert r["status"] == "ok"
    assert r["orphans"] == ["junk.tar"]
    assert any("don't match" in w for w in r["warnings"])


def test_list_techsupports_rejects_bad_limit():
    r = techsupport.list_techsupports(limit=0)
    assert r["status"] == "error"
    assert any("limit" in e for e in r["errors"])


def test_list_techsupports_rejects_bad_device():
    r = techsupport.list_techsupports(device="bad__name")
    assert r["status"] == "error"


def test_list_techsupports_handles_unconfigured_dnftp(monkeypatch):
    def _boom(*a, **k):
        raise DnftpNotConfigured("no creds")
    monkeypatch.setattr(ts_store, "list_ts", _boom)
    r = techsupport.list_techsupports()
    assert r["status"] == "error"
    assert any("no creds" in e for e in r["errors"])
    assert any("qactl setup" in n for n in r["next_actions"])


def test_list_techsupports_handles_sftp_failure(monkeypatch):
    def _boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(ts_store, "list_ts", _boom)
    r = techsupport.list_techsupports()
    assert r["status"] == "error"
    assert any("Failed to list" in e for e in r["errors"])


def test_cli_techsupport_list_json(monkeypatch):
    monkeypatch.setattr(
        ts_store, "list_ts",
        lambda device=None, limit=100: [_tsfile("sa__20260623-100123__diag.tar")],
    )
    monkeypatch.setattr(ts_store, "list_orphans", lambda: [])
    r = runner.invoke(app, ["cli", "techsupport", "list", "-d", "sa", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.stdout)
    assert payload["status"] == "ok"
    assert payload["count"] == 1
    assert payload["techsupports"][0]["device"] == "sa"
