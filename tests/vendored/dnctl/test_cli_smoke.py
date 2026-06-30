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


def test_shell_write_refuses_without_yes():
    # A mutating shell command stays gated: non-interactive (CliRunner has no
    # TTY) → must refuse, exit 2. (#56: read-only commands run ungated; only
    # writes keep the --yes gate.)
    r = runner.invoke(app, ["cli", "shell", "rm -rf /tmp/x", "-d", "sa", "--json"])
    assert r.exit_code == 2
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert any("--yes" in n for n in payload["next_actions"])


def test_raw_refuses_without_yes():
    # raw runs arbitrary CLI lines (can configure/commit) → gated by --yes.
    r = runner.invoke(app, ["cli", "raw", "show version", "-d", "sa", "--json"])
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


def test_ncm_cli_refuses_without_yes():
    # non-interactive (CliRunner has no TTY) → must refuse, exit 2.
    r = runner.invoke(
        app,
        ["cli", "ncm-cli", "show lldp neighbors", "--ncm", "A0", "-d", "sa", "--json"],
    )
    assert r.exit_code == 2
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert any("--yes" in n for n in payload["next_actions"])


def test_ncm_cli_invalid_ncm_errors_before_device():
    # --yes passes the gate; bad --ncm is rejected by validation, no SSH.
    r = runner.invoke(
        app,
        ["cli", "ncm-cli", "show lldp neighbors", "--ncm", "bad id",
         "-d", "sa", "--yes", "--json"],
    )
    assert r.exit_code == 1
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert any("ncm" in e for e in payload["errors"])


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


def test_device_rename_in_place(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(
        json.dumps(
            {"devices": {"Hybrid_Omer": {
                "mgmt0": "10.0.0.9", "expected_role": "CL",
                "expected_sns": ["SN-A", "SN-B"], "system_id": "uuid-1",
            }}}
        )
    )
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))

    r = runner.invoke(
        app, ["cli", "device", "rename", "Hybrid_Omer", "Hybrid-CL",
              "--yes", "--json"]
    )
    assert r.exit_code == 0, r.stdout
    payload = json.loads(r.stdout)
    assert payload["status"] == "ok"
    assert payload["renamed"] is True
    assert payload["new_name"] == "Hybrid-CL"
    # entry preserved (creds / sns / role / system_id), no re-probe
    assert payload["entry"]["expected_sns"] == ["SN-A", "SN-B"]
    assert payload["entry"]["system_id"] == "uuid-1"

    # the new name is canonical; the old name still resolves as an alias
    r = runner.invoke(app, ["cli", "device", "list", "--json"])
    listing = json.loads(r.stdout)
    names = {d["device"] for d in listing["devices"]}
    assert "Hybrid-CL" in names
    assert "Hybrid_Omer" not in names
    cl = next(d for d in listing["devices"] if d["device"] == "Hybrid-CL")
    assert cl["aliases"] == ["Hybrid_Omer"]


def test_device_rename_drop_old_alias(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(
        json.dumps({"devices": {"old": {"mgmt0": "10.0.0.9", "expected_sns": ["SN"]}}})
    )
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))
    r = runner.invoke(
        app, ["cli", "device", "rename", "old", "new", "--drop-old-alias",
              "--yes", "--json"]
    )
    assert r.exit_code == 0, r.stdout
    payload = json.loads(r.stdout)
    assert payload["aliases"] == []


def test_device_rename_refuses_without_yes(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(json.dumps({"devices": {"a": {"mgmt0": "10.0.0.1"}}}))
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))
    r = runner.invoke(app, ["cli", "device", "rename", "a", "b", "--json"])
    assert r.exit_code == 2
    assert any("--yes" in n for n in json.loads(r.stdout)["next_actions"])


def test_device_rename_collision_errors(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(
        json.dumps({"devices": {
            "a": {"mgmt0": "10.0.0.1"}, "b": {"mgmt0": "10.0.0.2"},
        }})
    )
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))
    r = runner.invoke(app, ["cli", "device", "rename", "a", "b", "--yes", "--json"])
    assert r.exit_code == 1
    assert json.loads(r.stdout)["status"] == "error"


def test_device_refresh_warns_on_system_name_drift(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(
        json.dumps({"devices": {"old": {
            "mgmt0": "10.0.0.1", "expected_role": "CL", "expected_sns": ["SN-1"],
        }}})
    )
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))

    from dnctl.cli.tools import devices as devtool
    from dnctl.core.cli_probe import DeviceProbe

    monkeypatch.setattr(
        devtool, "probe_device",
        lambda *a, **k: DeviceProbe(
            system_name="new-name", system_id="uuid-1",
            expected_role="CL", mgmt0="10.0.0.1", ncc_serials=[],
        ),
    )

    r = runner.invoke(app, ["cli", "device", "refresh", "old", "--yes", "--json"])
    assert r.exit_code == 0, r.stdout
    payload = json.loads(r.stdout)
    # refresh does NOT silently change the key...
    assert payload["device"] == "old"
    # ...but it flags the drift and points at rename
    assert any(
        "new-name" in w and "rename" in w for w in payload["warnings"]
    )


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


# --------------------------------------------------------------------------
# device add: GI-mode chassis (no System Name) — issue #32
# --------------------------------------------------------------------------

def _gi_probe(**overrides):
    """A DeviceProbe as returned for a box with no System Name (GI mode)."""
    from dnctl.core.cli_probe import DeviceProbe

    base = dict(
        system_name=None, system_id=None, expected_role=None,
        mgmt0=None, ncc_serials=[], mode="gi",
    )
    base.update(overrides)
    return DeviceProbe(**base)


def _stub_add_probe(monkeypatch, probe):
    """Patch the SSH probe + best-effort post-add init out of the add path."""
    from dnctl.cli.tools import devices as devtool

    monkeypatch.setattr(devtool, "probe_device", lambda *a, **k: probe)
    monkeypatch.setattr(devtool, "_post_add_init", lambda device: (None, []))


def test_device_add_defaults_name_to_ssh_host(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(json.dumps({"devices": {}}))
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))
    _stub_add_probe(monkeypatch, _gi_probe(ncc_serials=["CZ22500CW4"]))

    # No explicit --host: the registry key defaults to the SSH host (NAME).
    r = runner.invoke(
        app, ["cli", "device", "add", "WDY1A17P0001A-P3", "--yes", "--json"]
    )
    assert r.exit_code == 0, r.stdout
    payload = json.loads(r.stdout)
    assert payload["added"] is True
    assert payload["derived_name"] == "WDY1A17P0001A-P3"
    assert payload["derived_name_source"] == "ssh-host"
    # The probed host plus the discovered NCC serial are both enrolled.
    assert "WDY1A17P0001A-P3" in payload["hosts"]
    assert "CZ22500CW4" in payload["hosts"]


def test_device_add_explicit_name_with_host(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(json.dumps({"devices": {}}))
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))
    _stub_add_probe(monkeypatch, _gi_probe())

    # NAME is the operator-chosen key; --host is the SSH target.
    r = runner.invoke(
        app,
        ["cli", "device", "add", "lab-spine-1",
         "--host", "WDY1A17P0001A-P3", "--yes", "--json"],
    )
    assert r.exit_code == 0, r.stdout
    payload = json.loads(r.stdout)
    assert payload["added"] is True
    assert payload["derived_name"] == "lab-spine-1"
    assert payload["derived_name_source"] == "explicit"
    assert payload["device"] == "lab-spine-1"
    assert "WDY1A17P0001A-P3" in payload["hosts"]


def test_device_add_unknown_mode_still_registers(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(json.dumps({"devices": {}}))
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))
    # No System Name at all: with the operator-chosen key this is fine now.
    _stub_add_probe(monkeypatch, _gi_probe(mode="unknown"))

    r = runner.invoke(
        app, ["cli", "device", "add", "1.2.3.4", "--yes", "--json"]
    )
    assert r.exit_code == 0, r.stdout
    payload = json.loads(r.stdout)
    assert payload["status"] == "ok"
    assert payload["added"] is True
    assert payload["device"] == "1.2.3.4"
    assert payload["derived_name_source"] == "ssh-host"


def test_device_add_ignores_chassis_system_name(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(json.dumps({"devices": {}}))
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))
    _stub_add_probe(
        monkeypatch,
        _gi_probe(
            system_name="cl-chassis", system_id="uuid-1",
            expected_role="CL", mgmt0="10.0.0.9",
            ncc_serials=["CZ1", "CZ2"], mode="operational",
        ),
    )

    r = runner.invoke(
        app, ["cli", "device", "add", "CZ1", "--yes", "--json"]
    )
    assert r.exit_code == 0, r.stdout
    payload = json.loads(r.stdout)
    # The chassis System Name is NOT used as the key — the SSH host is.
    assert payload["derived_name"] == "CZ1"
    assert payload["derived_name_source"] == "ssh-host"
    # ...but it is captured as metadata and flagged.
    assert payload["entry"]["system_name"] == "cl-chassis"
    assert any("System Name 'cl-chassis'" in w for w in payload["warnings"])


def test_device_add_attaches_secondary_alias(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(json.dumps({"devices": {}}))
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))
    _stub_add_probe(
        monkeypatch,
        _gi_probe(system_name="hcl", mgmt0="100.64.10.252", expected_role="CL"),
    )

    r = runner.invoke(
        app,
        ["cli", "device", "add", "HCL", "--host", "100.64.10.252",
         "--alias", "cl", "--yes", "--json"],
    )
    assert r.exit_code == 0, r.stdout
    payload = json.loads(r.stdout)
    assert payload["device"] == "HCL"
    assert "cl" in payload["aliases"]


# --------------------------------------------------------------------------
# device name-check: report drift, optionally sync to the chassis name
# --------------------------------------------------------------------------

def _stub_probe(monkeypatch, system_name):
    from dnctl.cli.tools import devices as devtool
    from dnctl.core.cli_probe import DeviceProbe

    monkeypatch.setattr(
        devtool, "probe_device",
        lambda *a, **k: DeviceProbe(
            system_name=system_name, system_id="uuid-1",
            expected_role="CL", mgmt0="10.0.0.9", ncc_serials=[],
            mode="operational",
        ),
    )


def test_device_name_check_in_sync(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(json.dumps(
        {"devices": {"HCL": {"mgmt0": "10.0.0.9", "expected_sns": ["CZ1"]}}}
    ))
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))
    _stub_probe(monkeypatch, "HCL")

    r = runner.invoke(app, ["cli", "device", "name-check", "HCL", "--json"])
    assert r.exit_code == 0, r.stdout
    p = json.loads(r.stdout)
    assert p["in_sync"] is True
    assert p["synced"] is False
    assert p["chassis_system_name"] == "HCL"


def test_device_name_check_drift_reports_warning(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(json.dumps(
        {"devices": {"HCL": {"mgmt0": "10.0.0.9", "expected_sns": ["CZ1"]}}}
    ))
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))
    _stub_probe(monkeypatch, "Hybrid-CL")

    r = runner.invoke(app, ["cli", "device", "name-check", "HCL", "--json"])
    assert r.exit_code == 0, r.stdout  # warning still exits 0
    p = json.loads(r.stdout)
    assert p["status"] == "warning"
    assert p["in_sync"] is False
    assert p["synced"] is False
    assert p["registry_name"] == "HCL"
    assert p["chassis_system_name"] == "Hybrid-CL"
    assert any("--sync" in w for w in p["warnings"])


def test_device_name_check_sync_renames_and_keeps_aliases(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(json.dumps(
        {"devices": {"HCL": {
            "mgmt0": "10.0.0.9", "expected_sns": ["CZ1"], "aliases": ["cl"],
        }}}
    ))
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))
    _stub_probe(monkeypatch, "Hybrid-CL")

    r = runner.invoke(
        app, ["cli", "device", "name-check", "HCL", "--sync", "--yes", "--json"]
    )
    assert r.exit_code == 0, r.stdout
    p = json.loads(r.stdout)
    assert p["synced"] is True
    assert p["device"] == "Hybrid-CL"
    # old name kept as alias, and the existing 'cl' nickname survives
    assert "HCL" in p["aliases"]
    assert "cl" in p["aliases"]

    # registry now keyed by the chassis name; 'cl' still resolves to it
    listing = json.loads(runner.invoke(app, ["cli", "device", "list", "--json"]).stdout)
    names = {d["device"] for d in listing["devices"]}
    assert "Hybrid-CL" in names and "HCL" not in names


def test_device_name_check_sync_refuses_without_yes(tmp_path, monkeypatch):
    devmap = tmp_path / "devices.json"
    devmap.write_text(json.dumps(
        {"devices": {"HCL": {"mgmt0": "10.0.0.9", "expected_sns": ["CZ1"]}}}
    ))
    monkeypatch.setenv("DNCTL_DEVICES", str(devmap))
    _stub_probe(monkeypatch, "Hybrid-CL")

    r = runner.invoke(app, ["cli", "device", "name-check", "HCL", "--sync", "--json"])
    assert r.exit_code == 2
    assert any("--yes" in n for n in json.loads(r.stdout)["next_actions"])
