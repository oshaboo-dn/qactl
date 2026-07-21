"""Unit tests for the `cli run` operational passthrough — the
``_validate_run_command`` validator, the ``run_command`` tool, and the
`qactl cli run` CLI wiring. No real device: the validator is pure and the
tool's device hop is stubbed.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from qactl.dnos.__main__ import app
from qactl.dnos.cli.core.validation import _validate_run_command
from qactl.dnos.cli.tools import run as run_tool


runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("QACTL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("QACTL_DEVICES", raising=False)


# --- validator -------------------------------------------------------------

def test_validate_run_accepts_traceroute():
    full, err = _validate_run_command("  run   traceroute   10.0.0.1  ")
    assert err is None
    assert full == "run traceroute 10.0.0.1"


def test_validate_run_accepts_traceroute_mpls_isis():
    full, err = _validate_run_command("run traceroute mpls isis 10.0.0.9/32")
    assert err is None
    assert full == "run traceroute mpls isis 10.0.0.9/32"


def test_validate_run_empty_rejected():
    _, err = _validate_run_command("   ")
    assert err and "non-empty" in err


def test_validate_run_requires_run_prefix():
    _, err = _validate_run_command("traceroute 10.0.0.1")
    assert err and "must start with 'run'" in err


def test_validate_run_requires_subcommand():
    _, err = _validate_run_command("run")
    assert err and "subcommand after 'run'" in err


def test_validate_run_rejects_start_shell():
    _, err = _validate_run_command("run start shell")
    assert err and "qactl cli shell" in err


def test_validate_run_rejects_request():
    _, err = _validate_run_command("run request system reboot")
    assert err and "qactl cli raw" in err


# --- tool ------------------------------------------------------------------

def test_run_command_bad_input_never_hits_device(monkeypatch):
    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("device must not be contacted on a bad command")

    monkeypatch.setattr(run_tool, "_run_on_device", _boom)
    resp = run_tool.run_command("run start shell", host="1.2.3.4")
    assert resp["status"] == "error"
    assert any("qactl cli shell" in e for e in resp["errors"])


def test_run_command_passes_normalized_command(monkeypatch):
    seen = {}

    def _fake(tool, device, host, user, password, command, timeout, na):
        seen["command"] = command
        return {"status": "ok", "stdout": "traceroute to 10.0.0.1", "errors": []}

    monkeypatch.setattr(run_tool, "_run_on_device", _fake)
    resp = run_tool.run_command("run  traceroute   10.0.0.1", host="1.2.3.4")
    assert resp["status"] == "ok"
    assert seen["command"] == "run traceroute 10.0.0.1"


# --- CLI wiring ------------------------------------------------------------

def test_cli_run_rejects_request_without_touching_device():
    r = runner.invoke(
        app, ["cli", "run", "run request system reboot", "--host", "1.2.3.4",
              "--json"]
    )
    assert r.exit_code != 0
    env = json.loads(r.stdout)
    assert env["status"] == "error"
    assert any("qactl cli raw" in e for e in env["errors"])
