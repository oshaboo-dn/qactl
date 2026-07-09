"""Unit tests for the aggregated `cli interfaces` view (issue #42).

Parsers are exercised against fixtures captured from a live DNOS box
(Hybrid-CL); the tool itself is exercised with a fake ``run_sequence`` so
no device is touched.
"""

import pytest

from qactl.dnctl.cli.core.session import Invocation, StepCapture
from qactl.dnctl.cli.tools import interfaces as ifmod
from qactl.dnctl.cli.tools.interfaces import (
    build_interface_view,
    interfaces,
    parse_interfaces_description,
    parse_interfaces_table,
    parse_isis_interfaces,
    parse_lldp_table,
    parse_ospf_interfaces,
)


# --- fixtures (trimmed real output) ---------------------------------------

INTERFACES_OUT = """
Legend: i - inner vlan, b - interface disabled due to breakout


| Interface                  |  Admin   | Operational     | IPv4 Address           | IPv6 Address    | VLAN          | MTU  | Network-Service                             | Bundle-Id  |
+----------------------------+----------+-----------------+------------------------+-----------------+---------------+------+---------------------------------------------+------------+
| ge10-3/0/0                 | disabled | down            |                        |                 |               | 1514 | VRF (default)                               |            |
| ge400-7/0/8.6              | enabled  | up              | 123.4.5.4/24           |                 | 6             | 1518 | VRF (default)                               |            |
| ge400-7/0/9.50             | enabled  | up              | 123.4.50.4/24          |                 | 50            | 1518 | VRF (default)                               |            |
| lo0                        | enabled  | up              | 123.4.4.4/32           |                 |               | 1514 | VRF (default)                               |            |
"""

DESCRIPTION_OUT = """
Legend: i - inner vlan


| Interface                  |  Admin   | Operational     | Description                          |
+----------------------------+----------+-----------------+--------------------------------------+
| ge10-3/0/0                 | disabled | down            |                                      |
| ge400-7/0/8                | enabled  | up              | uplink to leaf B10                   |
| lo0                        | enabled  | up              | router-id loopback                   |
"""

LLDP_OUT = """
| Interface    | Neighbor System Name   | Neighbor interface   | Neighbor TTL   |
|--------------+------------------------+----------------------+----------------|
| ge100-3/0/65 | DNAAS-LEAF-B10         | ge100-0/0/18         | 120            |
| ge400-7/0/8  | DNAAS-LEAF-B10         | ge100-0/0/32         | 120            |
| ge400-7/0/9  |                        |                      |                |
"""

ISIS_OUT = """
Instance core:
  Instance Level: L2
  Interface: ge400-7/0/8.6, State: Up, Active
    Type: point-to-point, Level: L2
    Level-2 Information:
      IPv4 Unicast topology: Enabled
        Metric: 10
      Active neighbors: 1
    IP Prefix(es):
      123.4.5.4/24

  Interface: lo0, State: Up, Passive
    Type: loopback, Level: L2
    Level-2 Information:
      Metric: 0
      Active neighbors: 0
"""

OSPF_OUT = "\nOSPF Routing Process not enabled\n"


# --- parsers ---------------------------------------------------------------

def test_parse_interfaces_table():
    table = parse_interfaces_table(INTERFACES_OUT)
    assert list(table) == ["ge10-3/0/0", "ge400-7/0/8.6", "ge400-7/0/9.50", "lo0"]
    sub = table["ge400-7/0/8.6"]
    assert sub["admin"] == "enabled"
    assert sub["operational"] == "up"
    assert sub["ipv4"] == "123.4.5.4/24"
    assert sub["vlan"] == "6"
    assert sub["mtu"] == "1518"
    assert sub["network_service"] == "VRF (default)"
    # Empty cells are dropped, not stored as "".
    assert "ipv6" not in sub
    assert "bundle_id" not in sub
    # A bare port with no addressing still appears with admin/oper.
    assert table["ge10-3/0/0"]["admin"] == "disabled"
    assert "ipv4" not in table["ge10-3/0/0"]


def test_parse_interfaces_description():
    desc = parse_interfaces_description(DESCRIPTION_OUT)
    assert desc == {
        "ge400-7/0/8": "uplink to leaf B10",
        "lo0": "router-id loopback",
    }


def test_parse_lldp_table():
    lldp = parse_lldp_table(LLDP_OUT)
    # Rows with no neighbor are skipped.
    assert set(lldp) == {"ge100-3/0/65", "ge400-7/0/8"}
    assert lldp["ge400-7/0/8"] == {
        "neighbor": "DNAAS-LEAF-B10",
        "neighbor_interface": "ge100-0/0/32",
        "ttl": "120",
    }


def test_parse_isis_interfaces():
    isis = parse_isis_interfaces(ISIS_OUT)
    assert set(isis) == {"ge400-7/0/8.6", "lo0"}
    sub = isis["ge400-7/0/8.6"]
    assert sub["protocol"] == "isis"
    assert sub["instance"] == "core"
    assert sub["level"] == "L2"
    assert sub["state"] == "Up"
    assert sub["passive"] is False
    assert sub["metric"] == 10
    assert sub["neighbors"] == 1
    assert isis["lo0"]["passive"] is True
    assert isis["lo0"]["neighbors"] == 0


def test_parse_ospf_not_enabled():
    assert parse_ospf_interfaces(OSPF_OUT) == {}
    assert parse_ospf_interfaces("") == {}


def test_ospf_reuses_isis_shape_when_enabled():
    # Same block layout, protocol tag rewritten.
    ospf = parse_ospf_interfaces(ISIS_OUT)
    assert ospf["ge400-7/0/8.6"]["protocol"] == "ospf"


# --- join ------------------------------------------------------------------

def test_build_interface_view_joins_and_inherits():
    view = build_interface_view(
        INTERFACES_OUT, DESCRIPTION_OUT, LLDP_OUT, ISIS_OUT, OSPF_OUT
    )
    sub = view["ge400-7/0/8.6"]
    # State joined.
    assert sub["state"]["ipv4"] == "123.4.5.4/24"
    # Description inherited from parent physical port.
    assert sub["description"] == "uplink to leaf B10"
    assert sub["description_inherited_from"] == "ge400-7/0/8"
    # LLDP inherited from parent physical port.
    assert sub["lldp"]["neighbor"] == "DNAAS-LEAF-B10"
    assert sub["lldp_inherited_from"] == "ge400-7/0/8"
    # IGP from ISIS.
    assert sub["igp"]["protocol"] == "isis"
    assert sub["igp"]["neighbors"] == 1

    # lo0 has its own description, no inheritance, passive IGP.
    lo0 = view["lo0"]
    assert lo0["description"] == "router-id loopback"
    assert "description_inherited_from" not in lo0
    assert lo0["igp"]["passive"] is True

    # A bare port with nothing learned: no lldp, no igp.
    bare = view["ge10-3/0/0"]
    assert bare["lldp"] is None
    assert bare["igp"] is None
    assert bare["description"] == ""


def test_build_interface_view_igp_prefers_isis_over_ospf():
    # If both protocols report an interface, ISIS wins.
    view = build_interface_view(
        INTERFACES_OUT, "", "", ISIS_OUT, ISIS_OUT  # ospf parses same shape
    )
    assert view["ge400-7/0/8.6"]["igp"]["protocol"] == "isis"


# --- tool ------------------------------------------------------------------

def _fake_invocation():
    steps = [
        StepCapture(ifmod.SHOW_INTERFACES, "", INTERFACES_OUT, "", True),
        StepCapture(ifmod.SHOW_DESCRIPTION, "", DESCRIPTION_OUT, "", True),
        StepCapture(ifmod.SHOW_LLDP, "", LLDP_OUT, "", True),
        StepCapture(ifmod.SHOW_ISIS, "", ISIS_OUT, "", True),
        StepCapture(ifmod.SHOW_OSPF, "", OSPF_OUT, "", True),
    ]
    return Invocation(
        output=OSPF_OUT, hit_prompt=True, head_prompt_line="",
        tail_prompt="", host="1.2.3.4", device="cl", steps=steps,
    )


@pytest.fixture
def _stub(monkeypatch):
    monkeypatch.setattr(ifmod, "log_invocation", lambda *a, **k: None)
    monkeypatch.setattr(ifmod, "log_request", lambda *a, **k: None)


def test_interfaces_tool_ok(monkeypatch, _stub):
    monkeypatch.setattr(ifmod, "run_sequence", lambda *a, **k: _fake_invocation())
    resp = interfaces(device="cl")
    assert resp["status"] == "ok"
    assert resp["device"] == "cl"
    assert resp["interface_count"] == 4
    assert "ge400-7/0/8.6" in resp["interfaces"]
    assert resp["interfaces"]["lo0"]["igp"]["passive"] is True
    # Human-readable body present and mentions the LLDP neighbor.
    assert "DNAAS-LEAF-B10" in resp["stdout"]


def test_interfaces_tool_single_filter(monkeypatch, _stub):
    monkeypatch.setattr(ifmod, "run_sequence", lambda *a, **k: _fake_invocation())
    resp = interfaces(device="cl", interface="lo0")
    assert resp["status"] == "ok"
    assert list(resp["interfaces"]) == ["lo0"]
    assert resp["interface_count"] == 1


def test_interfaces_tool_unknown_interface(monkeypatch, _stub):
    monkeypatch.setattr(ifmod, "run_sequence", lambda *a, **k: _fake_invocation())
    resp = interfaces(device="cl", interface="ge99-9/9/9")
    assert resp["status"] == "error"
    assert "not found" in resp["errors"][0]


def test_interfaces_tool_connect_error(monkeypatch, _stub):
    def _boom(*a, **k):
        raise ifmod.ConnectError("no route to host")

    monkeypatch.setattr(ifmod, "run_sequence", _boom)
    resp = interfaces(device="cl")
    assert resp["status"] == "connect_error"
    assert "no route to host" in resp["errors"][0]


def test_interfaces_tool_empty_interface_table(monkeypatch, _stub):
    steps = [StepCapture(ifmod.SHOW_INTERFACES, "", "", "", False)]
    inv = Invocation(
        output="", hit_prompt=False, head_prompt_line="",
        tail_prompt="", host="1.2.3.4", device="cl", steps=steps,
    )
    monkeypatch.setattr(ifmod, "run_sequence", lambda *a, **k: inv)
    resp = interfaces(device="cl")
    assert resp["status"] == "timeout"
