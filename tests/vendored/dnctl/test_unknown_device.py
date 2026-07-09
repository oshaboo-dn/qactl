"""Registry-miss vs unreachable device hinting.

When a device alias/SN is simply not in the registry, registry-backed
commands must say so and hint --host / `device add` — NOT blame
reachability/credentials, which sent users chasing phantom connectivity
problems. See the issue: "device-not-in-registry returns misleading
'Unknown device / Verify device is reachable'".
"""

import pytest

from qactl.dnos.cli.core import session as sess
from qactl.dnos.cli.core.session import (
    ConnectError,
    UnknownDeviceError,
    connect_error_next_actions,
)
from qactl.dnos.cli.tools import interfaces as ifmod


# --- the resolver raises the distinct error on a registry miss ------------

def test_open_transport_unknown_device_raises_unknown(monkeypatch):
    monkeypatch.setattr(sess, "DEVICE_HOSTS", {})
    with pytest.raises(UnknownDeviceError) as ei:
        sess._open_transport(
            device="WDY1CAV500029", host=None,
            user="u", password="p", connect_timeout=1,
        )
    msg = str(ei.value)
    assert "WDY1CAV500029" in msg
    assert "not in the device registry" in msg
    # still a ConnectError, so existing handlers keep catching it
    assert isinstance(ei.value, ConnectError)


# --- next_actions distinguish the two cases -------------------------------

def test_next_actions_registry_miss_hints_host_and_add():
    actions = connect_error_next_actions(
        UnknownDeviceError("'X' is not in the device registry.")
    )
    assert len(actions) == 1
    hint = actions[0]
    assert "--host" in hint
    assert "device add" in hint
    assert "manage_device" in hint
    # must NOT blame reachability/credentials
    assert "reachable" not in hint.lower()


def test_next_actions_real_connect_failure_keeps_generic_hint():
    actions = connect_error_next_actions(ConnectError("no route to host"))
    assert actions == [
        "Verify device is reachable and credentials are correct."
    ]


# --- end-to-end through the interfaces tool -------------------------------

def _stub(monkeypatch):
    monkeypatch.setattr(ifmod, "log_invocation", lambda *a, **k: None)
    monkeypatch.setattr(ifmod, "log_request", lambda *a, **k: None)


def test_interfaces_registry_miss_hints_registry(monkeypatch):
    _stub(monkeypatch)

    def _miss(*a, **k):
        raise UnknownDeviceError("'WDY1CAV500029' is not in the device registry.")

    monkeypatch.setattr(ifmod, "run_sequence", _miss)
    resp = ifmod.interfaces(device="WDY1CAV500029")
    assert resp["status"] == "connect_error"
    assert "not in the device registry" in resp["errors"][0]
    assert "--host" in resp["next_actions"][0]
    assert "reachable" not in resp["next_actions"][0].lower()


def test_interfaces_unreachable_keeps_generic_hint(monkeypatch):
    _stub(monkeypatch)

    def _boom(*a, **k):
        raise ConnectError("no route to host")

    monkeypatch.setattr(ifmod, "run_sequence", _boom)
    resp = ifmod.interfaces(device="cl")
    assert resp["status"] == "connect_error"
    assert resp["next_actions"] == [
        "Verify device is reachable and credentials are correct."
    ]
