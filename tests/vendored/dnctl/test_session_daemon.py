"""Persistent SSH-session daemon: routing, fallback, error fidelity.

The daemon holds the TransportRegistry across invocations so back-to-back
qactl calls stop re-authing SSH (DNOS sshd rate-limits at 10 conns/min).
No real SSH anywhere: the daemon-side executors are monkeypatched, and
"direct path reached" is proven with a dummy registry whose ``get``
raises a sentinel ConnectError.
"""

import json
import socket
import threading

import pytest

from qactl.dnos.cli.core import session as sess
from qactl.dnos.cli.core import session_daemon as sd
from qactl.dnos.cli.core.edit_helpers import stop_on_rejected_statement
from qactl.dnos.cli.core.session import ConnectError, Invocation, StepCapture, UnknownDeviceError


def canned_invocation() -> Invocation:
    return Invocation(
        output="ok-output",
        hit_prompt=True,
        head_prompt_line="HOST# show x",
        tail_prompt="HOST#",
        host="SN123",
        device="dev1",
        steps=[StepCapture("show x", "HOST# show x", "ok-output", "HOST#", True)],
    )


class DirectPathReached(ConnectError):
    """Sentinel: run_* fell through to the in-process connect path."""


class SentinelRegistry:
    """Dummy TransportRegistry whose get() proves the direct path ran."""

    def get(self, **kwargs):
        raise DirectPathReached("direct")


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    """A live daemon on a tmp socket; routing enabled, autospawn off."""
    sock = str(tmp_path / "d.sock")
    monkeypatch.setenv(sd.SOCK_ENV, sock)
    monkeypatch.setenv(sd.ENABLE_ENV, "1")
    monkeypatch.setenv(sd.AUTOSPAWN_ENV, "0")
    server = sd.make_server(sock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def stub_executor(monkeypatch, result=None, exc=None):
    """Replace every daemon-side executor with a canned outcome recorder."""
    calls = []

    def fake(op, kwargs):
        calls.append((op, kwargs))
        if exc is not None:
            raise exc
        return result if result is not None else canned_invocation()

    for op in list(sd._EXECUTORS):
        monkeypatch.setitem(sd._EXECUTORS, op, fake)
    return calls


def test_invocation_round_trip():
    inv = canned_invocation()
    clone = sd.invocation_from_dict(json.loads(json.dumps(sd.invocation_to_dict(inv))))
    assert clone == inv


def test_run_once_routed(daemon, monkeypatch):
    calls = stub_executor(monkeypatch)
    inv = sess.run_once(
        SentinelRegistry(), device="dev1", host=None, user="u", password="p",
        command="show x",
    )
    assert inv == canned_invocation()
    (op, kwargs), = calls
    assert op == "run_once"
    assert kwargs["command"] == "show x"
    assert kwargs["device"] == "dev1"
    assert kwargs["mode"] == "command"


def test_error_types_survive_the_wire(daemon, monkeypatch):
    stub_executor(monkeypatch, exc=UnknownDeviceError("'x' is not in the device registry."))
    with pytest.raises(UnknownDeviceError):
        sess.run_once(SentinelRegistry(), device="x", host=None, user="u",
                      password="p", command="show x")

    stub_executor(monkeypatch, exc=ConnectError("boom", transient=True))
    with pytest.raises(ConnectError) as ei:
        sess.run_once(SentinelRegistry(), device="dev1", host=None, user="u",
                      password="p", command="show x")
    assert not isinstance(ei.value, UnknownDeviceError)
    assert ei.value.transient is True


def test_disabled_runs_direct(daemon, monkeypatch):
    monkeypatch.setenv(sd.ENABLE_ENV, "0")
    calls = stub_executor(monkeypatch)
    with pytest.raises(DirectPathReached):
        sess.run_once(SentinelRegistry(), device="dev1", host=None, user="u",
                      password="p", command="show x")
    assert calls == []


def test_daemon_unreachable_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv(sd.SOCK_ENV, str(tmp_path / "absent.sock"))
    monkeypatch.setenv(sd.ENABLE_ENV, "1")
    monkeypatch.setenv(sd.AUTOSPAWN_ENV, "0")
    with pytest.raises(DirectPathReached):
        sess.run_once(SentinelRegistry(), device="dev1", host=None, user="u",
                      password="p", command="show x")


def test_unnamed_predicate_not_routed(daemon, monkeypatch):
    calls = stub_executor(monkeypatch)
    with pytest.raises(DirectPathReached):
        sess.run_sequence(
            SentinelRegistry(), device="dev1", host=None, user="u", password="p",
            commands=["a", "b"], stop_predicate=lambda step: False,
        )
    assert calls == []


def test_named_predicate_routed_by_name(daemon, monkeypatch):
    calls = stub_executor(monkeypatch)
    sess.run_sequence(
        SentinelRegistry(), device="dev1", host=None, user="u", password="p",
        commands=["a", "b"], stop_predicate=stop_on_rejected_statement,
    )
    (_, kwargs), = calls
    assert kwargs["stop_predicate"] == "rejected_statement"
    assert kwargs["commands"] == ["a", "b"]


def test_sequence_pw_tuples_cross_the_wire(daemon, monkeypatch):
    calls = stub_executor(monkeypatch)
    sess.run_sequence_pw(
        SentinelRegistry(), device="dev1", host=None, user="u", password="p",
        commands=[("request file upload x", "secret"), ("show y", None)],
    )
    (_, kwargs), = calls
    assert kwargs["commands"] == [["request file upload x", "secret"], ["show y", None]]


def test_version_mismatch_falls_back_direct(daemon, monkeypatch):
    calls = stub_executor(monkeypatch)
    daemon.version = "not-this-version"
    with pytest.raises(DirectPathReached):
        sess.run_once(SentinelRegistry(), device="dev1", host=None, user="u",
                      password="p", command="show x")
    assert calls == []


def test_ping_status_shutdown_ops(daemon):
    ping = sd.call_daemon("ping", spawn=False)
    assert ping["ok"] and ping["version"] == daemon.version

    status = sd.call_daemon("status", spawn=False)
    assert status["ok"] and isinstance(status["transports"], list)

    down = sd.call_daemon("shutdown", spawn=False)
    assert down["ok"] and down["shutdown"]


def test_daemon_role_never_routes_to_itself(daemon, monkeypatch):
    monkeypatch.setenv(sd.ROLE_ENV, "server")
    assert sd.enabled() is False


def test_marker_file_toggle(monkeypatch, tmp_path):
    monkeypatch.delenv(sd.ENABLE_ENV, raising=False)
    monkeypatch.delenv(sd.ROLE_ENV, raising=False)
    monkeypatch.setenv("QACTL_STATE_DIR", str(tmp_path))
    assert sd.enabled() is False
    sd.set_enabled(True)
    assert sd.enabled() is True
    sd.set_enabled(False)
    assert sd.enabled() is False


def test_resolve_predicate_names():
    assert sd._resolve_predicate(None) is None
    assert sd._resolve_predicate("rejected_statement") is stop_on_rejected_statement
    assert callable(sd._resolve_predicate("detect_error"))
    with pytest.raises(ValueError):
        sd._resolve_predicate("nope")


def test_daemon_died_mid_request_maps_to_transient_connect_error(daemon, monkeypatch):
    """A response-less connection break must NOT silently rerun the command."""

    class Dead(Exception):
        pass

    def broken_read(sock):
        return b""

    monkeypatch.setattr(sd, "_read_line", broken_read)
    with pytest.raises(ConnectError) as ei:
        sess.run_once(SentinelRegistry(), device="dev1", host=None, user="u",
                      password="p", command="show x")
    assert ei.value.transient is True
    assert "mid-request" in str(ei.value)
