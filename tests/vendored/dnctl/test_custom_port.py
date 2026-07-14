"""Per-device custom SSH port.

Several devices can share one mgmt IP but differ by port (cdnos clab nodes
fronted by per-node DNAT on the host, e.g. h263:2201/2202/2203 -> :22). The
registry stores a ``port`` on the entry; the transport opener uses it.
"""

import json

import pytest

from qactl.dnos.core import devices as dn_devices
from qactl.dnos.cli.core import session as sess


def _write_map(tmp_path, entry):
    p = tmp_path / "devices_mgmt0.json"
    p.write_text(json.dumps({"devices": {"r1": entry}}))
    return str(p)


def test_resolve_port_reads_stored_int(tmp_path):
    path = _write_map(tmp_path, {"expected_sns": ["1.2.3.4"], "port": 2201})
    assert dn_devices.resolve_port("r1", path) == 2201


@pytest.mark.parametrize("entry", [
    {"expected_sns": ["1.2.3.4"]},              # no port -> default (None)
    {"expected_sns": ["1.2.3.4"], "port": 0},   # out of range
    {"expected_sns": ["1.2.3.4"], "port": 70000},
    {"expected_sns": ["1.2.3.4"], "port": True},  # bool is not a valid port
    {"expected_sns": ["1.2.3.4"], "port": "2201"},  # string, not int
])
def test_resolve_port_none_when_absent_or_invalid(tmp_path, entry):
    assert dn_devices.resolve_port("r1", _write_map(tmp_path, entry)) is None


def _capture_port(monkeypatch):
    """Monkeypatch _try_connect_host to record the port it's called with."""
    seen = {}

    class _Client:  # minimal stand-in for a paramiko client
        pass

    def fake_connect(host, user, password, timeout, port=22):
        seen["port"] = port
        return _Client()

    monkeypatch.setattr(sess, "_try_connect_host", fake_connect)
    monkeypatch.setattr(sess, "DEVICE_HOSTS", {"r1": ["1.2.3.4"]}, raising=False)
    return seen


def test_open_transport_uses_device_stored_port(monkeypatch):
    seen = _capture_port(monkeypatch)
    monkeypatch.setattr(dn_devices, "resolve_port", lambda d, path=None: 2201)
    sess._open_transport(device="r1", host=None, user="u", password="p",
                         connect_timeout=5)
    assert seen["port"] == 2201


def test_open_transport_explicit_port_wins(monkeypatch):
    seen = _capture_port(monkeypatch)
    # Even if the entry stored something, an explicit port (probe path) wins.
    monkeypatch.setattr(dn_devices, "resolve_port", lambda d, path=None: 9999)
    sess._open_transport(device="r1", host=None, user="u", password="p",
                         connect_timeout=5, port=2202)
    assert seen["port"] == 2202


def test_open_transport_defaults_to_22(monkeypatch):
    seen = _capture_port(monkeypatch)
    monkeypatch.setattr(dn_devices, "resolve_port", lambda d, path=None: None)
    sess._open_transport(device="r1", host=None, user="u", password="p",
                         connect_timeout=5)
    assert seen["port"] == 22
