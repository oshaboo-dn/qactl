"""Connect retry in session._open_transport (issue #68).

DNOS sshd rate-limits rapid successive connections, so back-to-back qactl
calls (each a fresh process, fresh SSH session) see ~1-in-8 connect
timeouts. _open_transport now retries the candidate sweep on transient
failures only. No real device — _try_connect_host is monkeypatched.
"""

import socket

import paramiko
import pytest

from dnctl.cli.core import session as sess


class FakeClient:
    """Stand-in for a connected paramiko.SSHClient."""


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(sess.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def _script(monkeypatch, outcomes):
    """Make _try_connect_host pop one scripted outcome per call."""
    calls = []

    def fake(host, user, password, timeout):
        calls.append(host)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sess, "_try_connect_host", fake)
    return calls


def test_timeout_retries_then_succeeds(monkeypatch, no_sleep):
    client = FakeClient()
    calls = _script(
        monkeypatch,
        [socket.timeout("timed out"), socket.timeout("timed out"), client],
    )
    t = sess._open_transport(
        device=None, host="10.0.0.1", user="u", password="p", connect_timeout=1
    )
    assert t.client is client
    assert len(calls) == 3
    assert no_sleep == [2.0, 5.0]


def test_all_timeouts_exhaust_attempts(monkeypatch, no_sleep):
    calls = _script(monkeypatch, [socket.timeout("timed out")] * 3)
    with pytest.raises(sess.ConnectError, match="timed out"):
        sess._open_transport(
            device=None, host="10.0.0.1", user="u", password="p", connect_timeout=1
        )
    assert len(calls) == 3
    assert no_sleep == [2.0, 5.0]


def test_auth_rejected_fails_fast(monkeypatch, no_sleep):
    calls = _script(monkeypatch, [paramiko.AuthenticationException("Authentication failed.")])
    with pytest.raises(sess.ConnectError):
        sess._open_transport(
            device=None, host="10.0.0.1", user="u", password="p", connect_timeout=1
        )
    assert len(calls) == 1
    assert no_sleep == []


def test_banner_error_is_retried(monkeypatch, no_sleep):
    client = FakeClient()
    calls = _script(
        monkeypatch,
        [paramiko.SSHException("Error reading SSH protocol banner"), client],
    )
    t = sess._open_transport(
        device=None, host="10.0.0.1", user="u", password="p", connect_timeout=1
    )
    assert t.client is client
    assert len(calls) == 2


def test_retries_env_fail_fast(monkeypatch, no_sleep):
    monkeypatch.setenv("QACTL_CONNECT_RETRIES", "1")
    calls = _script(monkeypatch, [socket.timeout("timed out")])
    with pytest.raises(sess.ConnectError):
        sess._open_transport(
            device=None, host="10.0.0.1", user="u", password="p", connect_timeout=1
        )
    assert len(calls) == 1
    assert no_sleep == []


def test_backoff_env_last_value_repeats(monkeypatch, no_sleep):
    monkeypatch.setenv("QACTL_CONNECT_RETRIES", "4")
    monkeypatch.setenv("QACTL_CONNECT_BACKOFF", "0.1")
    _script(monkeypatch, [socket.timeout("timed out")] * 4)
    with pytest.raises(sess.ConnectError):
        sess._open_transport(
            device=None, host="10.0.0.1", user="u", password="p", connect_timeout=1
        )
    assert no_sleep == [0.1, 0.1, 0.1]


def test_bad_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("QACTL_CONNECT_RETRIES", "0")
    monkeypatch.setenv("QACTL_CONNECT_BACKOFF", "fast,slow")
    assert sess._connect_attempts() == sess.DEFAULT_CONNECT_ATTEMPTS
    assert sess._connect_backoff() == sess.DEFAULT_CONNECT_BACKOFF


def test_dual_candidate_sweep_retried_when_one_is_transient(monkeypatch, no_sleep):
    """Auth-reject on NCC-A + timeout on NCC-B → the sweep retries both."""
    monkeypatch.setitem(sess.DEVICE_HOSTS, "dev-under-test", ["ncc-a", "ncc-b"])
    client = FakeClient()
    calls = _script(
        monkeypatch,
        [
            paramiko.AuthenticationException("Authentication failed."),
            socket.timeout("timed out"),
            paramiko.AuthenticationException("Authentication failed."),
            client,
        ],
    )
    t = sess._open_transport(
        device="dev-under-test", host=None, user="u", password="p", connect_timeout=1
    )
    assert t.client is client
    assert calls == ["ncc-a", "ncc-b", "ncc-a", "ncc-b"]


def test_unknown_device_never_retries(no_sleep):
    with pytest.raises(sess.UnknownDeviceError):
        sess._open_transport(
            device="no-such-device-xyz", host=None, user="u", password="p", connect_timeout=1
        )
    assert no_sleep == []


@pytest.mark.parametrize(
    "exc,transient",
    [
        (socket.timeout("timed out"), True),
        (TimeoutError("timed out"), True),
        (EOFError(), True),
        (ConnectionResetError(), True),
        (paramiko.SSHException("Error reading SSH protocol banner"), True),
        (paramiko.AuthenticationException("Authentication timed out."), True),
        (paramiko.AuthenticationException("Authentication failed."), False),
        (socket.gaierror("Name or service not known"), False),
        (paramiko.SSHException("No existing session"), False),
        (ValueError("nope"), False),
    ],
)
def test_transient_classification(exc, transient):
    assert sess._is_transient_connect_error(exc) is transient
