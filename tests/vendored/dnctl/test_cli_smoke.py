"""End-to-end CLI smoke tests via Typer's runner (no device traffic)."""

import json

import pytest
from typer.testing import CliRunner

from dnctl.__main__ import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DNCTL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("DNCTL_DEVICES", raising=False)


def test_version():
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0
    assert "dnctl" in r.stdout


def test_group_help_lists_commands():
    for group in ("cli", "nc", "gnmi", "rc"):
        r = runner.invoke(app, [group, "--help"])
        assert r.exit_code == 0


def test_devices_json_seeded():
    r = runner.invoke(app, ["gnmi", "devices", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.stdout)
    assert payload["status"] == "ok"


def test_destructive_refuses_without_yes():
    # non-interactive (CliRunner has no TTY) → must refuse, exit 2.
    r = runner.invoke(app, ["cli", "restart", "system", "-d", "sa", "--json"])
    assert r.exit_code == 2
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert any("--yes" in n for n in payload["next_actions"])


def test_shell_refuses_without_yes():
    # non-interactive (CliRunner has no TTY) → must refuse, exit 2.
    r = runner.invoke(app, ["cli", "shell", "uptime", "-d", "sa", "--json"])
    assert r.exit_code == 2
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert any("--yes" in n for n in payload["next_actions"])


def test_shell_invalid_ncc_errors_before_device():
    # --yes passes the gate; bad --ncc is rejected by validation, no SSH.
    r = runner.invoke(
        app, ["cli", "shell", "uptime", "-d", "sa", "--ncc", "9", "--yes", "--json"]
    )
    assert r.exit_code == 1
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert any("ncc" in e for e in payload["errors"])


def test_missing_payload_clean_error():
    r = runner.invoke(app, ["nc", "edit", "-d", "sa", "--yes", "--json"], input="")
    assert r.exit_code == 1
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"


def test_device_alias_roundtrip(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(
        json.dumps({"devices": {"sa": {"mgmt0": "10.0.0.1", "expected_sns": ["SN-SA"]}}})
    )
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))

    # attach a nickname
    r = runner.invoke(app, ["cli", "device", "alias", "sa", "spine-a", "--yes", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.stdout)
    assert payload["status"] == "ok"
    assert payload["added"] is True
    assert payload["aliases"] == ["spine-a"]

    # it shows up on the device listing
    r = runner.invoke(app, ["cli", "device", "list", "--json"])
    assert r.exit_code == 0
    listing = json.loads(r.stdout)
    sa = next(d for d in listing["devices"] if d["device"] == "sa")
    assert sa["aliases"] == ["spine-a"]

    # the nickname resolves to the same box (gnmi devices reads the same map)
    r = runner.invoke(app, ["cli", "device", "unalias", "spine-a", "--yes", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.stdout)["removed"] is True


def test_device_alias_rejects_shadowing_device(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(
        json.dumps(
            {
                "devices": {
                    "sa": {"mgmt0": "10.0.0.1", "expected_sns": ["SN-SA"]},
                    "cl": {"mgmt0": "10.0.0.2", "expected_sns": ["SN-CL"]},
                }
            }
        )
    )
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))

    r = runner.invoke(app, ["cli", "device", "alias", "sa", "cl", "--yes", "--json"])
    assert r.exit_code == 1
    assert json.loads(r.stdout)["status"] == "error"
