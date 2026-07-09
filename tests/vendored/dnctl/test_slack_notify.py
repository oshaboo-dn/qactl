"""Tests for the Slack notify transport selection.

The webhook path (``QACTL_SLACK_WEBHOOK_URL``) must be preferred over the MCP
slackbot path and must never raise — a failing notify must not break the
caller (the event collector stashes the error instead).
"""

from qactl.dnos.cli.core import slack_notify


def test_webhook_preferred_and_posts(monkeypatch):
    """With a webhook set, ``post`` uses it (not the MCP path) and reports ok."""
    captured = {}

    class _Resp:
        status = 200

        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        captured["timeout"] = timeout
        return _Resp()

    # Fail loudly if the MCP slackbot path is taken instead of the webhook.
    monkeypatch.setattr(slack_notify, "SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    monkeypatch.setattr(
        slack_notify, "_run_blocking",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("used MCP path")),
    )
    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", _fake_urlopen)

    r = slack_notify.post("#whatever", "hello world")
    assert r == {"ok": True, "ts": None, "error": None}
    assert captured["url"] == "https://hooks.slack.com/services/T/B/x"
    assert b"hello world" in captured["body"]


def test_webhook_workflow_trigger_json_body_is_ok(monkeypatch):
    """A Workflow Builder trigger replies 200 with a JSON ack (not ``ok``);
    we must treat any 2xx as success, matching diva."""
    class _Resp:
        status = 200

        def read(self):
            return b'{"ok":true,"workflow":"started"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(slack_notify, "SLACK_WEBHOOK_URL", "https://hooks.slack.com/triggers/T/x/y")
    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert slack_notify.post("#x", "hi") == {"ok": True, "ts": None, "error": None}


def test_webhook_non_200_is_error(monkeypatch):
    class _Resp:
        status = 500

        def read(self):
            return b"no_service"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(slack_notify, "SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", lambda *a, **k: _Resp())

    r = slack_notify.post("#whatever", "boom")
    assert r["ok"] is False
    assert "500" in r["error"]


def test_webhook_exception_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(slack_notify, "SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", _boom)

    r = slack_notify.post("#whatever", "boom")
    assert r["ok"] is False
    assert "network down" in r["error"]


def test_no_webhook_no_channel_is_error(monkeypatch):
    """Without a webhook and without a channel, post reports the missing channel."""
    monkeypatch.setattr(slack_notify, "SLACK_WEBHOOK_URL", "")
    r = slack_notify.post("", "x")
    assert r["ok"] is False
    assert r["error"] == "no channel set"
