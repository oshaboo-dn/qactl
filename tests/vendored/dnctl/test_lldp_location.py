"""LLDP-based device location capture (issue #40, no device traffic).

`device add` auto-discovers where a chassis physically lives — its rack
and DNAAS fabric leaf — from `show lldp neighbors`, with a `--rack`
manual override and a `--no-discover` opt-out, then persists
`rack` / `mgmt_switch` / `fabric_leaf` on the registry entry and surfaces
them in `device list`.
"""

from __future__ import annotations

import json

import pytest

from qactl.dnctl.core import devices as dn_devices
from qactl.dnctl.core.cli_probe import (
    DeviceProbe,
    LldpLocation,
    derive_location,
    parse_lldp_neighbors,
    probe_via,
    rack_from_name,
)
from qactl.dnctl.cli.tools.devices import _location_fields


# A plausible DNOS ``show lldp neighbors`` capture, matching the example
# in the issue: Kira's mgmt0 neighbors IL-SW-B13, and its data ports
# neighbor DNAAS-LEAF-B13. Columns are pipe-delimited like the other
# DNOS show tables.
LLDP_OUTPUT = """\
| Local Interface | Neighbor System Name | Neighbor Port ID | TTL |
|-----------------+----------------------+------------------+-----|
| mgmt0           | IL-SW-B13            | Eth1/13          | 120 |
| ge100-0/0/2     | DNAAS-LEAF-B13       | ge100-0/0/16     | 120 |
| ge100-0/0/3     | DNAAS-LEAF-B13       | ge100-0/0/17     | 120 |
"""


# --- rack_from_name --------------------------------------------------------

def test_rack_from_mgmt_switch_name():
    assert rack_from_name("IL-SW-B13") == "B13"


def test_rack_from_leaf_name():
    assert rack_from_name("DNAAS-LEAF-B13") == "B13"


def test_rack_from_name_none_when_no_token():
    assert rack_from_name("DNAAS-LEAF") is None
    assert rack_from_name("") is None
    assert rack_from_name(None) is None


# --- parse_lldp_neighbors --------------------------------------------------

def test_parse_lldp_neighbors_rows():
    rows = parse_lldp_neighbors(LLDP_OUTPUT)
    assert len(rows) == 3
    assert rows[0] == {
        "local_interface": "mgmt0",
        "neighbor": "IL-SW-B13",
        "remote_port": "Eth1/13",
    }
    assert rows[1]["local_interface"] == "ge100-0/0/2"
    assert rows[1]["neighbor"] == "DNAAS-LEAF-B13"
    assert rows[1]["remote_port"] == "ge100-0/0/16"


def test_parse_lldp_neighbors_empty_on_garbage():
    assert parse_lldp_neighbors("") == []
    assert parse_lldp_neighbors("% no lldp\n") == []


# --- derive_location -------------------------------------------------------

def test_derive_location_full():
    loc = derive_location(LLDP_OUTPUT)
    assert loc.rack == "B13"
    assert loc.mgmt_switch == "IL-SW-B13"
    assert [e["leaf"] for e in loc.fabric_leaf] == ["DNAAS-LEAF-B13", "DNAAS-LEAF-B13"]
    assert loc.fabric_leaf[0]["local_port"] == "ge100-0/0/2"
    assert loc.fabric_leaf[0]["remote_port"] == "ge100-0/0/16"
    assert loc.warnings == []


def test_derive_location_falls_back_to_leaf_rack_without_mgmt():
    only_data = """\
| Local Interface | Neighbor System Name | Neighbor Port ID |
| ge100-0/0/2     | DNAAS-LEAF-B13       | ge100-0/0/16     |
"""
    loc = derive_location(only_data)
    assert loc.mgmt_switch is None
    assert loc.rack == "B13"


def test_derive_location_warns_on_mgmt_leaf_disagreement():
    mixed = """\
| Local Interface | Neighbor System Name | Neighbor Port ID |
| mgmt0           | IL-SW-B13            | Eth1/13          |
| ge100-0/0/2     | DNAAS-LEAF-C07       | ge100-0/0/16     |
"""
    loc = derive_location(mixed)
    assert loc.rack == "B13"  # mgmt switch wins
    assert any("disagrees" in w for w in loc.warnings)


# --- probe_via with discover_location --------------------------------------

_SYS = "System Name: kira, System-Id: 12345678-1234-1234-1234-1234567890ab\nSystem Type: SA-40\n"


def _run_show(mapping):
    return lambda cmd: mapping.get(cmd, "")


def test_probe_via_discovers_location_when_asked():
    probe = probe_via(
        _run_show({"show system": _SYS, "show lldp neighbors": LLDP_OUTPUT}),
        discover_location=True,
    )
    assert probe.location is not None
    assert probe.location.rack == "B13"
    assert probe.location.mgmt_switch == "IL-SW-B13"


def test_probe_via_skips_location_by_default():
    probe = probe_via(_run_show({"show system": _SYS, "show lldp neighbors": LLDP_OUTPUT}))
    assert probe.location is None


def test_probe_via_location_best_effort_on_lldp_error():
    def run(cmd):
        if cmd == "show system":
            return _SYS
        raise RuntimeError("lldp boom")

    probe = probe_via(run, discover_location=True)
    assert probe.location is None  # never fails the probe


# --- _location_fields (override + warning logic) ---------------------------

def _probe_with_location():
    return DeviceProbe(
        system_name="kira",
        location=LldpLocation(
            rack="B13", mgmt_switch="IL-SW-B13",
            fabric_leaf=[{"leaf": "DNAAS-LEAF-B13", "local_port": "ge100-0/0/2", "remote_port": "ge100-0/0/16"}],
        ),
    )


def test_location_fields_uses_discovered_rack():
    warnings = []
    fields = _location_fields(_probe_with_location(), None, True, warnings)
    assert fields["rack"] == "B13"
    assert fields["mgmt_switch"] == "IL-SW-B13"
    assert fields["fabric_leaf"]
    assert warnings == []


def test_location_fields_override_wins_and_warns_on_mismatch():
    warnings = []
    fields = _location_fields(_probe_with_location(), "C07", True, warnings)
    assert fields["rack"] == "C07"
    assert any("override" in w for w in warnings)


def test_location_fields_override_without_discovery():
    warnings = []
    fields = _location_fields(DeviceProbe(system_name="x"), "B13", False, warnings)
    assert fields == {"rack": "B13"}
    assert warnings == []


def test_location_fields_warns_when_discovery_finds_nothing():
    warnings = []
    fields = _location_fields(DeviceProbe(system_name="x"), None, True, warnings)
    assert "rack" not in fields
    assert any("could not auto-discover" in w for w in warnings)


# --- list_devices surfaces rack / leaf -------------------------------------

@pytest.fixture
def device_map_env(tmp_path, monkeypatch):
    p = tmp_path / "devices_mgmt0.json"
    p.write_text(
        json.dumps(
            {
                "devices": {
                    "kira": {
                        "mgmt0": "10.0.0.1",
                        "expected_sns": ["SN-KIRA"],
                        "rack": "B13",
                        "mgmt_switch": "IL-SW-B13",
                        "fabric_leaf": [
                            {"leaf": "DNAAS-LEAF-B13", "local_port": "ge100-0/0/2", "remote_port": "ge100-0/0/16"},
                            {"leaf": "DNAAS-LEAF-B13", "local_port": "ge100-0/0/3", "remote_port": "ge100-0/0/17"},
                        ],
                    },
                    "cl": {"mgmt0": "10.0.0.2", "expected_sns": ["SN-CL"]},
                }
            }
        )
    )
    monkeypatch.setenv("DNCTL_DEVICES", str(p))
    return str(p)


def test_list_devices_surfaces_location(device_map_env):
    from qactl.dnctl.cli.core import session
    from qactl.dnctl.cli.tools.devices import list_devices

    session.reload_device_hosts()
    resp = list_devices()
    by_name = {d["device"]: d for d in resp["devices"]}

    assert by_name["kira"]["rack"] == "B13"
    assert by_name["kira"]["mgmt_switch"] == "IL-SW-B13"
    assert by_name["kira"]["leaf"] == ["DNAAS-LEAF-B13"]  # deduped

    # A device added before location capture has empty location fields.
    assert by_name["cl"]["rack"] is None
    assert by_name["cl"]["leaf"] == []


def test_unknown_device_resolution_unaffected(device_map_env):
    assert dn_devices.get_device_entry("kira", device_map_env)["rack"] == "B13"
