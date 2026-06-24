"""GI-mode detection for ``show system`` (no device traffic).

Covers the structural discriminators added for issue #7: a chassis sitting
in the golden-image installer environment prints ``System status: running``
just like operational DNOS, so ``show_system`` must classify ``mode`` from
the schema, not that line.
"""

import pytest

from dnctl.core.cli_probe import (
    detect_system_mode,
    mode_from_banner,
    mode_from_prompt,
    parse_gi_inventory,
    probe_via,
    prompt_hostname,
)
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


def test_probe_via_prompt_forces_gi_mode():
    # A GI prompt is authoritative even if the body parses operationally.
    probe = probe_via(
        _run_show({"show system": OPERATIONAL_OUTPUT}),
        allow_missing_name=True,
        get_prompt=lambda: "GI(24-Jun-2026-06:57:13)#",
    )
    assert probe.mode == "gi"


def test_probe_via_operational_prompt_keeps_schema_verdict():
    probe = probe_via(
        _run_show({"show system": OPERATIONAL_OUTPUT}),
        allow_missing_name=True,
        get_prompt=lambda: "dn40-cl-301a-ncc1#",
    )
    assert probe.mode == "operational"


# --------------------------------------------------------------------------
# Prompt-based GI detection (real capture from WDY1CAV500029, a single-NCP
# S9700 white-box sitting in GI). The prompt is the authoritative signal;
# note this box prints ``Active NCC: <NCP-serial>`` against an NCP row, which
# is exactly why the schema-only ``Active NCC:`` heuristic is unreliable.
# --------------------------------------------------------------------------

# Exact `show system` body the GI box returned.
GI_REAL_OUTPUT = """\
System status: running
Active NCC: WDY1CAV500029

| Type   | Id   | Status   | Hardware Model   | Hardware Revision   | Serial Number   | ONIE version   | FW MU version   | BaseOS version   | GI version   |
|--------+------+----------+------------------+---------------------+-----------------+----------------+-----------------+------------------+--------------|
| NCP    | 0    | stable   | S9700-53DX       | 3-2                 | WDY1CAV500029   | 2021.02v15     | N/A             | 2.2630801007     | 26.3.0.7_p   |
"""

# Prompt shapes seen on the wire (trailing prompt, timestamped prompt, the
# echoed head line, and the normalised form).
GI_TAIL_PROMPT = "GI# "
GI_TS_PROMPT = "GI(24-Jun-2026-06:57:13)# "
GI_HEAD_PROMPT = "GI(24-Jun-2026-06:57:13)# show system"
GI_NORMALISED_PROMPT = "GI#"
GI_BANNER = "\r\nGI CLI Loading...\r                 \rGI# "

OPERATIONAL_PROMPT = "dn40-cl-301a-ncc1(24-Jun-2026-06:57:13)# "


@pytest.mark.parametrize(
    "prompt",
    [GI_TAIL_PROMPT, GI_TS_PROMPT, GI_HEAD_PROMPT, GI_NORMALISED_PROMPT],
)
def test_prompt_hostname_extracts_gi(prompt):
    assert prompt_hostname(prompt) == "GI"


def test_prompt_hostname_extracts_operational_chassis():
    assert prompt_hostname(OPERATIONAL_PROMPT) == "dn40-cl-301a-ncc1"


def test_prompt_hostname_none_on_junk():
    assert prompt_hostname("") is None
    assert prompt_hostname(None) is None
    assert prompt_hostname("no prompt here") is None


@pytest.mark.parametrize(
    "prompt",
    [GI_TAIL_PROMPT, GI_TS_PROMPT, GI_HEAD_PROMPT, GI_NORMALISED_PROMPT],
)
def test_mode_from_prompt_gi(prompt):
    assert mode_from_prompt(prompt) == "gi"


def test_mode_from_prompt_none_for_operational():
    assert mode_from_prompt(OPERATIONAL_PROMPT) is None
    assert mode_from_prompt("") is None


def test_mode_from_banner_gi():
    assert mode_from_banner(GI_BANNER) == "gi"
    assert mode_from_banner("Last login: ...") is None
    assert mode_from_banner(None) is None


def test_detect_mode_prompt_wins_over_schema():
    # Even if the body looks operational, a GI prompt is authoritative.
    assert detect_system_mode(OPERATIONAL_OUTPUT, prompt=GI_TS_PROMPT) == "gi"


def test_detect_mode_prompt_does_not_force_gi_on_operational_box():
    # Operational prompt is inconclusive -> schema decides -> operational.
    assert (
        detect_system_mode(OPERATIONAL_OUTPUT, prompt=OPERATIONAL_PROMPT)
        == "operational"
    )


def test_detect_mode_real_gi_capture_via_schema_fallback():
    # No prompt supplied: the real GI body still classifies via schema.
    assert detect_system_mode(GI_REAL_OUTPUT) == "gi"


def test_detect_mode_real_gi_capture_with_prompt():
    assert detect_system_mode(GI_REAL_OUTPUT, prompt=GI_TS_PROMPT) == "gi"


def test_parse_gi_inventory_real_capture_single_ncp():
    rows = parse_gi_inventory(GI_REAL_OUTPUT)
    assert len(rows) == 1
    assert rows[0]["type"] == "NCP"
    assert rows[0]["serial_number"] == "WDY1CAV500029"
    assert rows[0]["gi_version"] == "26.3.0.7_p"


def test_annotate_uses_prompt_to_flag_gi():
    # Body is empty/garbage but the prompt says GI -> still flagged gi.
    response = {
        "status": "ok",
        "stdout": GI_REAL_OUTPUT,
        "prompt": GI_TS_PROMPT,
        "warnings": [],
        "next_actions": [],
    }
    _annotate_system_mode(response)
    assert response["mode"] == "gi"
    assert any("GI mode" in w for w in response["warnings"])
