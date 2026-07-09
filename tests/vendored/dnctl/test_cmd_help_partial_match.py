"""cmd_help: surface the silent ANCESTOR-doc fall-back.

DNOS ``cmd help`` only resolves the canonical command string with
``<placeholder>`` tokens intact. A concrete/instantiated path (real AS/IP)
does not error — it falls back to the nearest documented ancestor doc with a
``* A partial match is found ...`` line, still at ``status: ok`` / exit 0.
That looks like real help, so cmd_help now flags it with ``partial_match`` and
a warning. These tests pin that without touching a real device.
"""

from __future__ import annotations

from qactl.dnctl.cli.tools import discovery


def _fake_run(stdout):
    return lambda *a, **k: {
        "status": "ok",
        "stdout": stdout,
        "warnings": [],
        "errors": [],
        "next_actions": [],
    }


def test_partial_match_falls_back_is_flagged(monkeypatch):
    out = (
        "* A partial match is found at 'protocols bgp'\n"
        "  AS-number  AS number, range 1-4294967295\n"
    )
    monkeypatch.setattr(discovery, "_run_on_device", _fake_run(out))

    resp = discovery.cmd_help(
        command="configure protocols bgp 100001 neighbor 1.1.1.2 "
                "bfd strict-mode hold-time",
        device="sa",
    )

    assert resp["status"] == "ok"
    assert resp["partial_match"] is True
    assert any("ancestor" in w.lower() for w in resp["warnings"])
    assert resp["next_actions"]


def test_canonical_form_not_flagged(monkeypatch):
    out = "hold-time  Hold time, range 5-300 seconds, default 30\n"
    monkeypatch.setattr(discovery, "_run_on_device", _fake_run(out))

    resp = discovery.cmd_help(
        command="configure protocols bgp <bgp> neighbor <neighbor> "
                "bfd strict-mode hold-time <hold_time>",
        device="sa",
    )

    assert resp["status"] == "ok"
    assert "partial_match" not in resp
    assert resp["warnings"] == []


def test_partial_match_not_flagged_on_error(monkeypatch):
    # An error envelope that happens to mention the marker is left as-is.
    def fake_run(*a, **k):
        return {
            "status": "error",
            "stdout": "* A partial match is found ...\n",
            "warnings": [],
            "errors": ["boom"],
            "next_actions": [],
        }

    monkeypatch.setattr(discovery, "_run_on_device", fake_run)
    resp = discovery.cmd_help(command="show bgp summary", device="sa")
    assert resp["status"] == "error"
    assert "partial_match" not in resp
