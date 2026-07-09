"""techsupport issue #67: ``create --include`` pass-through + ``upload``.

- ``create_techsupport(include=...)`` forwards DNOS info-types
  (basic / core-dumps / journal-files) onto the kickoff command and
  rejects unknown tokens before any device traffic.
- ``upload_techsupport`` adopts an EXISTING on-device tech-support file
  into the dnftp store with the managed password fed at the interactive
  prompt — never on the device command line.

No device traffic: ``run_sequence_pw`` is monkeypatched everywhere it
would be reached; validation failures are asserted to short-circuit
before it.
"""

import inspect
from types import SimpleNamespace

import pytest

from qactl.dnos.cli import app as cli_app
from qactl.dnos.cli.tools import techsupport

pytestmark = []


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("QACTL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("QACTL_DEVICES", raising=False)
    techsupport._TS_REGISTRY._jobs.clear()
    techsupport._TS_REGISTRY._active.clear()
    yield


def _invocation(output, hit_prompt=True):
    return SimpleNamespace(
        output=output, hit_prompt=hit_prompt, head_prompt_line="dev#",
        tail_prompt="dev#", host="HOST1", device="sa", steps=[],
    )


def _no_device_traffic(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not touch the device")
    monkeypatch.setattr(techsupport, "run_sequence_pw", _boom)


# --------------------------------------------------------------------------
# create --include: token normalisation
# --------------------------------------------------------------------------

def test_normalise_include_accepts_repeats_and_comma_lists():
    toks, err = techsupport._normalise_include(
        ["core-dumps,journal-files", "core-dumps", "basic"]
    )
    assert err is None
    assert toks == ["core-dumps", "journal-files", "basic"]


def test_normalise_include_empty_is_noop():
    assert techsupport._normalise_include(None) == ([], None)
    assert techsupport._normalise_include([]) == ([], None)


def test_normalise_include_rejects_unknown_token():
    toks, err = techsupport._normalise_include(["cores"])
    assert toks == []
    assert "invalid include info-type" in err
    assert "core-dumps" in err  # the valid choices are listed


def test_create_rejects_bad_include_before_device(monkeypatch):
    _no_device_traffic(monkeypatch)
    r = techsupport.create_techsupport(
        name="diag", device="sa", include=["everything"]
    )
    assert r["status"] == "error"
    assert any("invalid include info-type" in e for e in r["errors"])


def test_create_kickoff_command_carries_include(monkeypatch):
    captured = {}

    def fake_run(registry, **kw):
        captured.update(kw)
        # No tech-support ack in the output -> create returns an error
        # envelope BEFORE registering a job / spawning the worker, so the
        # test stays synchronous. The kickoff command was still captured.
        return _invocation("collection did not start")

    monkeypatch.setattr(techsupport, "run_sequence_pw", fake_run)
    r = techsupport.create_techsupport(
        name="diag", device="sa", include=["core-dumps", "journal-files"]
    )
    assert r["status"] == "error"
    cmds = [c for c, _pw in captured["commands"]]
    assert (
        "request system tech-support diag include core-dumps journal-files"
        in cmds
    )


def test_create_kickoff_command_without_include_is_unchanged(monkeypatch):
    captured = {}

    def fake_run(registry, **kw):
        captured.update(kw)
        return _invocation("collection did not start")

    monkeypatch.setattr(techsupport, "run_sequence_pw", fake_run)
    techsupport.create_techsupport(name="diag", device="sa")
    cmds = [c for c, _pw in captured["commands"]]
    assert "request system tech-support diag" in cmds


def test_cli_create_has_include_option():
    assert "include" in inspect.signature(cli_app.techsupport_create).parameters


# --------------------------------------------------------------------------
# upload: adopt an existing on-device tech-support file
# --------------------------------------------------------------------------

def test_upload_registered_on_cli():
    names = {c.name for c in cli_app.ts_app.registered_commands}
    assert "upload" in names


def test_upload_rejects_unsafe_file_arg(monkeypatch):
    _no_device_traffic(monkeypatch)
    for bad in ("ts_x.tar; rm -rf /", "a b.tar", "/etc/passwd", "../x.tar", ""):
        r = techsupport.upload_techsupport(file=bad, device="sa")
        assert r["status"] == "error", bad
        assert any("bare device-side filename" in e for e in r["errors"])


def test_upload_derives_name_from_dnos_filename(monkeypatch):
    monkeypatch.setattr(techsupport, "DNFTP_PASSWORD", "s3cret")
    captured = {}

    def fake_run(registry, **kw):
        captured.update(kw)
        return _invocation("Uploading file\nDone")

    monkeypatch.setattr(techsupport, "run_sequence_pw", fake_run)
    monkeypatch.setattr(
        techsupport.ts_store, "stat_ts",
        lambda fn: SimpleNamespace(size_bytes=5_000_000),
    )
    r = techsupport.upload_techsupport(
        file="ts_bgpd-cores_12_30_00_02-07-2026.tar", device="sa"
    )
    assert r["status"] == "ok"
    assert r["name"] == "bgpd-cores"
    assert r["device_filename"] == "ts_bgpd-cores_12_30_00_02-07-2026.tar"
    assert r["local_filename"].startswith("sa__")
    assert r["local_filename"].endswith("__bgpd-cores.tar")
    assert r["size_bytes"] == 5_000_000
    # The managed password is fed at the interactive prompt, never on the
    # device command line (that's the whole point — no accounting leak).
    cmd, sub_pw = captured["commands"][0]
    assert cmd.startswith(
        "request file upload tech-support "
        "ts_bgpd-cores_12_30_00_02-07-2026.tar "
    )
    assert "s3cret" not in cmd
    assert sub_pw == "s3cret"
    assert "s3cret" not in r["command"]


def test_upload_explicit_name_wins(monkeypatch):
    monkeypatch.setattr(techsupport, "DNFTP_PASSWORD", "s3cret")
    monkeypatch.setattr(
        techsupport, "run_sequence_pw",
        lambda registry, **kw: _invocation("Done"),
    )
    monkeypatch.setattr(
        techsupport.ts_store, "stat_ts",
        lambda fn: SimpleNamespace(size_bytes=5_000_000),
    )
    r = techsupport.upload_techsupport(
        file="ts_bgpd-cores_12_30_00_02-07-2026.tar", device="sa",
        name="sw279187-cores",
    )
    assert r["status"] == "ok"
    assert r["name"] == "sw279187-cores"
    assert r["local_filename"].endswith("__sw279187-cores.tar")


def test_upload_requires_name_when_underivable(monkeypatch):
    _no_device_traffic(monkeypatch)
    r = techsupport.upload_techsupport(file="weird.tar", device="sa")
    assert r["status"] == "error"
    assert any("Pass an explicit" in e for e in r["errors"])


def test_upload_without_dnftp_password_is_clean_error(monkeypatch):
    _no_device_traffic(monkeypatch)
    monkeypatch.setattr(techsupport, "DNFTP_PASSWORD", None)
    r = techsupport.upload_techsupport(
        file="ts_diag_10_00_00_01-07-2026.tar", device="sa"
    )
    assert r["status"] == "error"
    assert any("qactl setup" in n for n in r["next_actions"])


def test_upload_surfaces_device_error(monkeypatch):
    monkeypatch.setattr(techsupport, "DNFTP_PASSWORD", "s3cret")
    monkeypatch.setattr(
        techsupport, "run_sequence_pw",
        lambda registry, **kw: _invocation("% Error: file not found"),
    )
    r = techsupport.upload_techsupport(
        file="ts_diag_10_00_00_01-07-2026.tar", device="sa"
    )
    assert r["status"] == "error"
    assert any("file not found" in e for e in r["errors"])
    assert any("show system tech-support status" in n for n in r["next_actions"])


def test_upload_missing_on_dnftp_is_error(monkeypatch):
    monkeypatch.setattr(techsupport, "DNFTP_PASSWORD", "s3cret")
    monkeypatch.setattr(
        techsupport, "run_sequence_pw",
        lambda registry, **kw: _invocation("Done"),
    )
    monkeypatch.setattr(techsupport.ts_store, "stat_ts", lambda fn: None)
    r = techsupport.upload_techsupport(
        file="ts_diag_10_00_00_01-07-2026.tar", device="sa"
    )
    assert r["status"] == "error"
    assert any("not present" in e for e in r["errors"])


def test_upload_truncated_file_is_error(monkeypatch):
    monkeypatch.setattr(techsupport, "DNFTP_PASSWORD", "s3cret")
    monkeypatch.setattr(
        techsupport, "run_sequence_pw",
        lambda registry, **kw: _invocation("Done"),
    )
    monkeypatch.setattr(
        techsupport.ts_store, "stat_ts",
        lambda fn: SimpleNamespace(size_bytes=512),
    )
    r = techsupport.upload_techsupport(
        file="ts_diag_10_00_00_01-07-2026.tar", device="sa"
    )
    assert r["status"] == "error"
    assert any("suspiciously small" in e for e in r["errors"])


def test_upload_timeout_keeps_ts_path_hint(monkeypatch):
    monkeypatch.setattr(techsupport, "DNFTP_PASSWORD", "s3cret")
    monkeypatch.setattr(
        techsupport, "run_sequence_pw",
        lambda registry, **kw: _invocation("still copying...", hit_prompt=False),
    )
    r = techsupport.upload_techsupport(
        file="ts_diag_10_00_00_01-07-2026.tar", device="sa"
    )
    assert r["status"] == "timeout"
    assert any("may still be running" in e for e in r["errors"])
