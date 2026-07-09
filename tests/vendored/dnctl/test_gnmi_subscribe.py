"""Tests for `dnctl gnmi subscribe` — bounded gNMI Subscribe capture.

No real device traffic: ``open_client`` is monkeypatched to hand back a
fake gRPC client whose ``subscribe_stream`` yields a scripted sequence of
pygnmi telemetry dicts, then ``TimeoutError`` to end the window.
"""

import json

import pytest
from typer.testing import CliRunner

from qactl.dnctl.__main__ import app
from qactl.dnctl.gnmi.core.session import Resolved
from qactl.dnctl.gnmi.tools import subscribe as sub_mod
from qactl.dnctl.gnmi.tools.subscribe import gnmi_subscribe

runner = CliRunner()


class _FakeSub:
    def __init__(self, responses):
        self._responses = list(responses)
        self.closed = False

    def get_update(self, timeout=None):
        if self._responses:
            return self._responses.pop(0)
        raise TimeoutError("no more updates")

    def close(self):
        self.closed = True


class _FakeClient:
    def __init__(self, sub):
        self._sub = sub
        self.last_subscribe = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def subscribe_stream(self, subscribe=None, target=None, extension=None):
        self.last_subscribe = subscribe
        return self._sub


@pytest.fixture
def _patch_stream(monkeypatch):
    """Install a fake client; tests pass the scripted responses."""
    holder = {}

    def _install(responses):
        sub = _FakeSub(responses)
        client = _FakeClient(sub)
        holder["client"] = client
        holder["sub"] = sub

        def _fake_open_client(**kwargs):
            return client, Resolved(host="10.0.0.1", port=50051, device="cl"), "dnroot"

        monkeypatch.setattr(sub_mod, "open_client", _fake_open_client)
        monkeypatch.setattr(sub_mod.rate_limiter, "gate", lambda *a, **k: 0.0)
        return holder

    return _install


# --- validation (no device traffic) ---------------------------------------

def test_rejects_empty_paths():
    env = gnmi_subscribe(paths=[])
    assert env["status"] == "error"
    assert any("non-empty list" in e for e in env["errors"])


def test_rejects_bad_mode():
    env = gnmi_subscribe(paths=["a/b"], mode="bogus")
    assert env["status"] == "error"
    assert any("mode must be" in e for e in env["errors"])


def test_rejects_bad_encoding():
    env = gnmi_subscribe(paths=["a/b"], encoding="xml")
    assert env["status"] == "error"
    assert any("encoding must be" in e for e in env["errors"])


def test_rejects_nonpositive_duration():
    env = gnmi_subscribe(paths=["a/b"], duration_s=0)
    assert env["status"] == "error"
    assert any("duration_s must be" in e for e in env["errors"])


def test_sample_mode_requires_interval():
    env = gnmi_subscribe(paths=["a/b"], mode="sample", sample_interval_s=0)
    assert env["status"] == "error"
    assert any("sample_interval_s" in e for e in env["errors"])


# --- capture happy path ----------------------------------------------------

def test_flattens_events_and_tags_pre_sync(_patch_stream):
    _patch_stream([
        {"update": {"timestamp": 1, "update": [{"path": "n/state", "val": "DOWN"}]}},
        {"sync_response": True},
        {"update": {"timestamp": 2,
                    "update": [{"path": "n/state", "val": "UP"}],
                    "delete": ["n/old"]}},
    ])
    env = gnmi_subscribe(paths=["n/state"], duration_s=5)
    assert env["status"] == "ok"
    r = env["result"]
    assert r["sync_seen"] is True
    assert r["event_count"] == 3
    assert r["post_sync_event_count"] == 2
    # the first (initial-state) event is tagged pre_sync, later ones not
    assert r["events"][0]["pre_sync"] is True
    assert r["events"][0]["value"] == "DOWN"
    assert [e["op"] for e in r["events"]] == ["update", "update", "delete"]
    assert r["events"][2]["op"] == "delete" and r["events"][2]["path"] == "n/old"


def test_max_updates_truncates(_patch_stream):
    _patch_stream([
        {"update": {"timestamp": 1, "update": [{"path": "a", "val": 1}]}},
        {"update": {"timestamp": 2, "update": [{"path": "b", "val": 2}]}},
        {"update": {"timestamp": 3, "update": [{"path": "c", "val": 3}]}},
    ])
    env = gnmi_subscribe(paths=["a"], duration_s=5, max_updates=1)
    r = env["result"]
    assert r["truncated"] is True
    assert r["event_count"] == 1
    assert r["events"][0]["path"] == "a"


def test_quiet_window_is_ok_with_warning(_patch_stream):
    # sync arrives, then nothing changes before the window closes.
    _patch_stream([{"sync_response": True}])
    env = gnmi_subscribe(paths=["n/state"], duration_s=5)
    assert env["status"] == "ok"
    assert env["result"]["sync_seen"] is True
    assert env["result"]["post_sync_event_count"] == 0
    assert any("no changes" in w for w in env["warnings"])


def test_builds_on_change_subscription_dict(_patch_stream):
    holder = _patch_stream([{"sync_response": True}])
    gnmi_subscribe(paths=["x/y", "p/q"], mode="on_change", duration_s=5, heartbeat_s=2)
    sent = holder["client"].last_subscribe
    assert sent["mode"] == "stream"
    assert [s["path"] for s in sent["subscription"]] == ["x/y", "p/q"]
    assert all(s["mode"] == "ON_CHANGE" for s in sent["subscription"])
    # heartbeat seconds -> nanoseconds
    assert all(s["heartbeat_interval"] == 2_000_000_000 for s in sent["subscription"])


def test_subscriber_is_closed(_patch_stream):
    holder = _patch_stream([{"sync_response": True}])
    gnmi_subscribe(paths=["x"], duration_s=5)
    assert holder["sub"].closed is True


def test_grpc_error_surfaces_hint(monkeypatch):
    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def subscribe_stream(self, **kwargs):
            raise RuntimeError("Path does not exist: /drivenets-top/system/ncps/ncp")

    monkeypatch.setattr(
        sub_mod, "open_client",
        lambda **k: (_Boom(), Resolved(host="10.0.0.1", port=50051, device="cl"), "dnroot"),
    )
    monkeypatch.setattr(sub_mod.rate_limiter, "gate", lambda *a, **k: 0.0)
    env = gnmi_subscribe(paths=["system/ncps/ncp"], duration_s=5)
    assert env["status"] == "error"
    assert any("keyed list" in n for n in env["next_actions"])


class _ErrSub:
    """Fake subscriber that stashes a server-side error like pygnmi does."""

    def __init__(self, error, responses=None):
        self.error = error
        self._responses = list(responses or [])
        self.closed = False

    def get_update(self, timeout=None):
        if self._responses:
            return self._responses.pop(0)
        raise TimeoutError("no more updates")

    def close(self):
        self.closed = True


def _install_sub(monkeypatch, sub):
    client = _FakeClient(sub)
    monkeypatch.setattr(
        sub_mod, "open_client",
        lambda **k: (client, Resolved(host="10.0.0.1", port=50051, device="cl"), "dnroot"),
    )
    monkeypatch.setattr(sub_mod.rate_limiter, "gate", lambda *a, **k: 0.0)
    return client


def test_stashed_stream_error_is_surfaced_with_hint(monkeypatch):
    # DNOS answers an unsupported Subscribe with INVALID_ARGUMENT and pygnmi
    # stashes it on .error instead of raising — we must surface it, not
    # report a misleading "ok / no sync_response".
    sub = _ErrSub(
        RuntimeError("StatusCode.INVALID_ARGUMENT No valid requests in the session"),
    )
    _install_sub(monkeypatch, sub)
    env = gnmi_subscribe(paths=["drivenets-top/interfaces"], duration_s=5)
    assert env["status"] == "error"
    assert any("No valid requests" in e for e in env["errors"])
    assert any("rejected the Subscribe" in n for n in env["next_actions"])


def test_none_messages_are_skipped_not_crash(monkeypatch):
    # pygnmi can hand back None for messages it can't decode; our loop must
    # skip them (this is what dodges the upstream "update in None" TypeError),
    # while still capturing the real events.
    sub = _ErrSub(
        None,
        responses=[
            None,
            {"sync_response": True},
            {"update": {"timestamp": 7, "update": [{"path": "n/state", "val": "UP"}]}},
            None,
        ],
    )
    _install_sub(monkeypatch, sub)
    env = gnmi_subscribe(paths=["n/state"], duration_s=5)
    assert env["status"] == "ok"
    assert env["result"]["sync_seen"] is True
    assert env["result"]["event_count"] == 1
    assert env["result"]["events"][0]["value"] == "UP"


# --- CLI surface -----------------------------------------------------------

def test_cli_subscribe_json(_patch_stream):
    _patch_stream([
        {"sync_response": True},
        {"update": {"timestamp": 9, "update": [{"path": "n/state", "val": "UP"}]}},
    ])
    r = runner.invoke(
        app,
        ["gnmi", "subscribe", "n/state", "-d", "cl", "--duration", "5", "--json"],
    )
    assert r.exit_code == 0, r.stdout
    payload = json.loads(r.stdout)
    assert payload["status"] == "ok"
    assert payload["kind"] == "subscribe"
    assert payload["result"]["post_sync_event_count"] == 1


def test_cli_on_change_shorthand(_patch_stream):
    holder = _patch_stream([{"sync_response": True}])
    r = runner.invoke(
        app,
        ["gnmi", "subscribe", "x/y", "-d", "cl", "--on-change", "--duration", "5", "--json"],
    )
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["result"]["mode"] == "on_change"
    assert holder["client"].last_subscribe["subscription"][0]["mode"] == "ON_CHANGE"
