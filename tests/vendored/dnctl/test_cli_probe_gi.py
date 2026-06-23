"""GI-mode detection for ``show system`` (no device traffic).

Covers the structural discriminators added for issue #7: a chassis sitting
in the golden-image installer environment prints ``System status: running``
just like operational DNOS, so ``show_system`` must classify ``mode`` from
the schema, not that line.
"""

import pytest

from dnctl.core.cli_probe import detect_system_mode, parse_gi_inventory, probe_via
from dnctl.cli.tools.discovery import _annotate_system_mode


GI_OUTPUT = """\
System status: running
Active NCC: CZ22500CW4

| Type | Id | Status | Hardware Model | Hardware Revision | Serial Number | ONIE version | FW MU version | BaseOS version | GI version |
|------+----+--------+----------------+-------------------+---------------+--------------+---------------+----------------+------------|
| NCC  | 0  | stable | ProLiant_x     | A1                | CZ22500CW4    | 2022.08_x    | N/A           | 2.2630318015   | 26.3.0.50_priv.feature_SW-164163_acl-based-mirroring_51 |
| NCC  | 1  | stable | ProLiant_x     | A1                | CZ22260685    | 2022.08_x    | N/A           | 2.2630318015   | 26.3.0.50_priv.feature_SW-164163_acl-based-mirroring_51 |
"""

OPERATIONAL_OUTPUT = """\
System Name: cl-chassis, System-Id: 12345678-1234-1234-1234-1234567890ab
System Type: CL-86, Family: NCR
Version: DNOS [26.3.0] build [51_priv], built by dn
System Uptime: 3 days
BGP NSR: enabled

| Type | Id | Admin | Operational | Model | Uptime | Description | Serial Number |
|------+----+-------+-------------+-------+--------+-------------+---------------|
| NCC  | 0  |       | active-up   | X86   | 3d     | dn-ncc-0    | CZ22500CW4    |
| NCC  | 1  |       | standby-up  | X86   | 3d     | dn-ncc-1    | CZ22260685    |
"""


def test_detect_gi_mode():
    assert detect_system_mode(GI_OUTPUT) == "gi"


def test_detect_operational_mode():
    assert detect_system_mode(OPERATIONAL_OUTPUT) == "operational"


def test_operational_wins_over_stray_active_ncc():
    mixed = OPERATIONAL_OUTPUT + "\nActive NCC: CZ22500CW4\n"
    assert detect_system_mode(mixed) == "operational"


def test_detect_unknown_on_empty_or_garbage():
    assert detect_system_mode("") == "unknown"
    assert detect_system_mode("% some DNOS error\n") == "unknown"


def test_running_line_alone_is_not_operational():
    # The bare status line both schemas share must never read as operational.
    assert detect_system_mode("System status: running\n") == "unknown"


def test_parse_gi_inventory_rows():
    rows = parse_gi_inventory(GI_OUTPUT)
    assert len(rows) == 2
    assert rows[0]["type"] == "NCC"
    assert rows[0]["id"] == "0"
    assert rows[0]["status"] == "stable"
    assert rows[0]["serial_number"] == "CZ22500CW4"
    assert rows[0]["baseos_version"] == "2.2630318015"
    assert rows[0]["gi_version"].startswith("26.3.0.50_priv")
    assert "fw_mu_version" in rows[0]


def test_parse_gi_inventory_empty_on_operational():
    assert parse_gi_inventory(OPERATIONAL_OUTPUT) == []


def test_annotate_marks_gi_with_warning_and_inventory():
    response = {"status": "ok", "stdout": GI_OUTPUT, "warnings": [], "next_actions": []}
    _annotate_system_mode(response)
    assert response["mode"] == "gi"
    assert response["status"] == "ok"  # the command itself succeeded
    assert len(response["gi_inventory"]) == 2
    assert any("GI mode" in w for w in response["warnings"])
    assert response["next_actions"]


def test_annotate_marks_operational_without_noise():
    response = {"status": "ok", "stdout": OPERATIONAL_OUTPUT, "warnings": [], "next_actions": []}
    _annotate_system_mode(response)
    assert response["mode"] == "operational"
    assert response["warnings"] == []
    assert "gi_inventory" not in response


# --------------------------------------------------------------------------
# probe_via: GI-mode boxes have no System Name (issue #32)
# --------------------------------------------------------------------------

def _run_show(mapping):
    """Build a run_show closure that returns canned per-command output."""
    return lambda cmd: mapping.get(cmd, "")


def test_probe_via_gi_without_allow_missing_name_raises():
    # Default contract is unchanged: no System Name is still a hard error.
    with pytest.raises(RuntimeError):
        probe_via(_run_show({"show system": GI_OUTPUT}))


def test_probe_via_gi_allow_missing_name_returns_none_name_gi_mode():
    probe = probe_via(
        _run_show({"show system": GI_OUTPUT}), allow_missing_name=True
    )
    assert probe.system_name is None
    assert probe.mode == "gi"
    # The GI inventory table still surfaces the NCC serials.
    assert probe.ncc_serials == ["CZ22500CW4", "CZ22260685"]


def test_probe_via_operational_still_parses_name_and_mode():
    probe = probe_via(
        _run_show({"show system": OPERATIONAL_OUTPUT}), allow_missing_name=True
    )
    assert probe.system_name == "cl-chassis"
    assert probe.mode == "operational"
    assert probe.expected_role == "CL"
