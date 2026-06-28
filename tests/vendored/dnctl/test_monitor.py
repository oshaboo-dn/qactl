"""Tests for the event collector: parsing/rules, the dedupe spool, and
one ``monitor tick`` over a fake fleet.

No device traffic: ``get_system_events`` is monkeypatched to return canned
system-events text, the registry helpers are stubbed to a one-device
fleet, and Slack ``post`` is faked.
"""

import json

from typer.testing import CliRunner

from dnctl.__main__ import app
from dnctl.cli.core import event_spool as spool
from dnctl.cli.core import events as ev
from dnctl.cli.core import gnmi_links as gl
from dnctl.cli.tools import monitor as mon

runner = CliRunner()

_SAMPLE = (
    "local7.warning 2026-06-28T07:00:00.000Z HCL System - - - "
    "NCF_STATE_CHANGE_DISCONNECTED:NCF 0 state has changed to disconnected\n"
    "local7.info 2026-06-28T07:01:00.000Z HCL Routing - - - "
    "BGP_NEIGHBOR_DOWN:neighbor 10.0.0.1 went down\n"
    "local7.info 2026-06-28T07:02:00.000Z HCL System - - - "
    "SOME_BORING_EVENT:nothing worth waking anyone for\n"
)


# --- parsing ---------------------------------------------------------------

def test_parse_event_line_fields():
    line = (
        "local7.warning 2026-04-14T18:43:04.189Z OHADZS-CL System - - - "
        "NCF_STATE_CHANGE_DISCONNECTED:NCF 0 state has changed"
    )
    e = ev.parse_event_line(line)
    assert e["facility"] == "local7"
    assert e["severity"] == "warning"
    assert e["severity_rank"] == ev.SEVERITY_RANK["warning"]
    assert e["timestamp"] == "2026-04-14T18:43:04.189Z"
    assert e["host"] == "OHADZS-CL"
    assert e["subsystem"] == "System"
    assert e["event_code"] == "NCF_STATE_CHANGE_DISCONNECTED"
    assert e["message"] == "NCF 0 state has changed"


def test_parse_blank_is_none():
    assert ev.parse_event_line("   ") is None
    assert ev.parse_event_line("") is None


def test_severity_rank_order_and_unknown():
    assert ev.severity_rank("emerg") < ev.severity_rank("warning")
    assert ev.severity_rank("error") == ev.severity_rank("err")
    assert ev.severity_rank("bogus") > ev.severity_rank("debug")


def test_is_alertworthy_threshold_match_exclude():
    warn = ev.parse_event_line(
        "local7.warning 2026-06-28T07:00:00Z H S - - - X:y"
    )
    info_bgp = ev.parse_event_line(
        "local7.info 2026-06-28T07:00:00Z H S - - - BGP_NEIGHBOR_DOWN:down"
    )
    info_boring = ev.parse_event_line(
        "local7.info 2026-06-28T07:00:00Z H S - - - BORING:nothing"
    )
    rank = ev.severity_rank("warning")
    # severity threshold alone
    assert ev.is_alertworthy(warn, max_rank=rank, match=[]) is True
    # below threshold but matches a substring
    assert ev.is_alertworthy(info_bgp, max_rank=rank, match=["BGP"]) is True
    # below threshold, no match
    assert ev.is_alertworthy(info_boring, max_rank=rank, match=["BGP"]) is False
    # exclude vetoes even a matched event (substring is in the code/message)
    assert ev.is_alertworthy(
        info_bgp, max_rank=rank, match=["BGP"], exclude=["neighbor"]
    ) is False


def test_fingerprint_stable_and_distinct():
    a = ev.parse_event_line("l.info 2026-06-28T07:00:00Z H S - - - C:m")
    b = ev.parse_event_line("l.info 2026-06-28T07:00:01Z H S - - - C:m")
    assert ev.event_fingerprint("cl", a) == ev.event_fingerprint("cl", a)
    assert ev.event_fingerprint("cl", a) != ev.event_fingerprint("cl", b)
    assert ev.event_fingerprint("cl", a) != ev.event_fingerprint("sa", a)


# --- spool -----------------------------------------------------------------

def test_spool_roundtrip_cursor_and_dedupe(tmp_path):
    p = str(tmp_path / "s.json")
    st = spool.load(p)
    assert st["devices"] == {}
    assert spool.get_cursor(st, "cl") is None
    assert spool.is_new(st, "cl", "fp1") is True

    spool.record(st, "cl", ["fp1", "fp2"], cursor="2026-06-28T07:02:00Z")
    spool.save(st, p)

    st2 = spool.load(p)
    assert spool.get_cursor(st2, "cl") == "2026-06-28T07:02:00Z"
    assert spool.is_new(st2, "cl", "fp1") is False
    assert spool.is_new(st2, "cl", "fp9") is True


def test_spool_cursor_only_moves_forward():
    st = spool.load("/nonexistent/does-not-exist.json")
    spool.record(st, "cl", [], cursor="2026-06-28T07:02:00Z")
    spool.record(st, "cl", [], cursor="2026-06-28T06:00:00Z")  # older
    assert spool.get_cursor(st, "cl") == "2026-06-28T07:02:00Z"


def test_spool_seen_is_capped():
    st = spool.load("/nonexistent/x.json")
    fps = [f"fp{i}" for i in range(spool._SEEN_CAP + 100)]
    spool.record(st, "cl", fps, cursor=None)
    assert len(st["devices"]["cl"]["seen"]) == spool._SEEN_CAP
    # newest are kept
    assert fps[-1] in st["devices"]["cl"]["seen"]


# --- monitor tick (integration with fakes) ---------------------------------

def _gnmi_env(opermap, status="ok", errors=None):
    ups = [
        {"path": f"interfaces/interface[name={k}]/state/oper-status", "val": v}
        for k, v in opermap.items()
    ]
    return {"status": status, "result": {"notification": [{"update": ups}]},
            "errors": errors or []}


def _fake_fleet(monkeypatch, stdout=_SAMPLE, status="ok", errors=None,
                gnmi_oper=None):
    monkeypatch.setattr(mon._dn_devices, "list_device_aliases", lambda: ["HCL"])
    monkeypatch.setattr(mon._dn_devices, "resolve_canonical", lambda name: name)

    def _fake_events(**kwargs):
        return {"status": status, "stdout": stdout, "errors": errors or []}

    monkeypatch.setattr(mon, "get_system_events", _fake_events)
    # gNMI link source: a stable snapshot by default (baseline, no diff).
    oper = gnmi_oper if gnmi_oper is not None else {"eth1": "UP"}
    monkeypatch.setattr(mon, "_gnmi_get", lambda **k: _gnmi_env(oper))


def test_tick_surfaces_new_alerts_then_dedupes(tmp_path, monkeypatch):
    _fake_fleet(monkeypatch)
    p = str(tmp_path / "spool.json")

    r1 = mon.monitor_tick(state_path=p)
    assert r1["status"] == "ok"
    # warning NCF event (threshold) + info BGP event (match) = 2; boring dropped
    assert r1["new_event_count"] == 2
    codes = {e["event_code"] for e in r1["new_events"]}
    assert codes == {"NCF_STATE_CHANGE_DISCONNECTED", "BGP_NEIGHBOR_DOWN"}

    # second tick over the same log: cursor + dedupe => nothing new
    r2 = mon.monitor_tick(state_path=p)
    assert r2["new_event_count"] == 0
    assert spool.get_cursor(spool.load(p), "HCL") == "2026-06-28T07:02:00.000Z"


def test_tick_dry_run_does_not_persist(tmp_path, monkeypatch):
    _fake_fleet(monkeypatch)
    p = str(tmp_path / "spool.json")

    r = mon.monitor_tick(state_path=p, dry_run=True)
    assert r["new_event_count"] == 2
    assert r["dry_run"] is True
    # nothing recorded -> next real tick still sees them as new
    assert spool.get_cursor(spool.load(p), "HCL") is None


def test_tick_notify_posts_and_counts(tmp_path, monkeypatch):
    _fake_fleet(monkeypatch)
    posts = []
    monkeypatch.setattr(
        mon.slack_notify, "post",
        lambda channel, text, **k: posts.append((channel, text)) or {"ok": True, "ts": "1"},
    )
    r = mon.monitor_tick(state_path=str(tmp_path / "s.json"), notify_slack="#net")
    assert r["notified"] == 2
    assert len(posts) == 2 and all(c == "#net" for c, _ in posts)


def test_tick_notify_failure_is_warning_not_crash(tmp_path, monkeypatch):
    _fake_fleet(monkeypatch)
    monkeypatch.setattr(
        mon.slack_notify, "post",
        lambda channel, text, **k: {"ok": False, "ts": None, "error": "boom"},
    )
    r = mon.monitor_tick(state_path=str(tmp_path / "s.json"), notify_slack="#net")
    assert r["status"] == "warning"
    assert r["notified"] == 0
    assert any("boom" in e for e in r["notify_errors"])


def test_tick_read_error_is_warning(tmp_path, monkeypatch):
    _fake_fleet(monkeypatch, status="error", errors=["ssh failed"])
    r = mon.monitor_tick(state_path=str(tmp_path / "s.json"))
    assert r["status"] == "warning"
    assert r["new_event_count"] == 0
    assert any("ssh failed" in w for w in r["warnings"])
    # a failed read must not advance the cursor
    assert spool.get_cursor(spool.load(str(tmp_path / "s.json")), "HCL") is None


def test_tick_bad_severity_errors(tmp_path, monkeypatch):
    _fake_fleet(monkeypatch)
    r = mon.monitor_tick(state_path=str(tmp_path / "s.json"), severity="loud")
    assert r["status"] == "error"
    assert any("severity must be" in e for e in r["errors"])


def test_tick_no_default_rules_only_threshold(tmp_path, monkeypatch):
    _fake_fleet(monkeypatch)
    # without default rules and at default warning threshold, the info BGP
    # line no longer qualifies (only the warning NCF event does)
    r = mon.monitor_tick(state_path=str(tmp_path / "s.json"), use_default_rules=False)
    assert r["new_event_count"] == 1
    assert r["new_events"][0]["event_code"] == "NCF_STATE_CHANGE_DISCONNECTED"


# --- gNMI link source ------------------------------------------------------

def test_parse_oper_status():
    env = _gnmi_env({"ge100-0/0/0": "DOWN", "ge100-0/0/1": "UP"})
    got = gl.parse_oper_status(env)
    assert got == {"ge100-0/0/0": "DOWN", "ge100-0/0/1": "UP"}


def test_diff_link_states_no_baseline_is_silent():
    assert gl.diff_link_states("cl", None, {"a": "UP"}) == []
    assert gl.diff_link_states("cl", {}, {"a": "UP"}) == []


def test_diff_link_states_down_and_up_transitions():
    old = {"a": "UP", "b": "UP"}
    new = {"a": "DOWN", "b": "UP"}  # only 'a' changed
    evs = gl.diff_link_states("cl", old, new)
    assert len(evs) == 1
    e = evs[0]
    assert e["event_code"] == "OPER_STATUS_DOWN"
    assert e["severity"] == "warning"
    assert "a oper-status UP -> DOWN" in e["message"]
    # a recovery uses OPER_STATUS_UP at notice
    up = gl.diff_link_states("cl", {"a": "DOWN"}, {"a": "UP"})
    assert up[0]["event_code"] == "OPER_STATUS_UP"
    assert up[0]["severity"] == "notice"


def test_spool_links_roundtrip(tmp_path):
    p = str(tmp_path / "s.json")
    st = spool.load(p)
    assert spool.get_links(st, "cl") is None
    spool.set_links(st, "cl", {"a": "UP", "b": "DOWN"})
    spool.save(st, p)
    assert spool.get_links(spool.load(p), "cl") == {"a": "UP", "b": "DOWN"}


def test_tick_detects_link_down_across_ticks(tmp_path, monkeypatch):
    p = str(tmp_path / "s.json")
    # tick 1: baseline eth1=UP, no boring syslog alerts beyond the sample
    _fake_fleet(monkeypatch, stdout="", gnmi_oper={"eth1": "UP"})
    r1 = mon.monitor_tick(state_path=p)
    assert all(e.get("event_code") != "OPER_STATUS_DOWN" for e in r1["new_events"])

    # tick 2: eth1 goes DOWN -> a link-down event appears, tagged source gnmi
    monkeypatch.setattr(mon, "_gnmi_get", lambda **k: _gnmi_env({"eth1": "DOWN"}))
    r2 = mon.monitor_tick(state_path=p)
    downs = [e for e in r2["new_events"] if e.get("event_code") == "OPER_STATUS_DOWN"]
    assert len(downs) == 1
    assert downs[0]["source"] == "gnmi-oper"
    assert downs[0]["device"] == "HCL"

    # tick 3: still DOWN (no change) -> not re-alerted (snapshot is the dedupe)
    r3 = mon.monitor_tick(state_path=p)
    assert all(e.get("event_code") != "OPER_STATUS_DOWN" for e in r3["new_events"])


def test_tick_no_links_skips_gnmi(tmp_path, monkeypatch):
    _fake_fleet(monkeypatch, stdout="")
    called = {"n": 0}

    def _boom(**k):
        called["n"] += 1
        return _gnmi_env({"eth1": "UP"})

    monkeypatch.setattr(mon, "_gnmi_get", _boom)
    mon.monitor_tick(state_path=str(tmp_path / "s.json"), links=False)
    assert called["n"] == 0


def test_tick_gnmi_failure_degrades_to_warning(tmp_path, monkeypatch):
    _fake_fleet(monkeypatch, stdout="")
    monkeypatch.setattr(
        mon, "_gnmi_get",
        lambda **k: {"status": "connect_error", "errors": ["gnmi down"]},
    )
    r = mon.monitor_tick(state_path=str(tmp_path / "s.json"))
    assert r["status"] == "warning"
    assert any("gNMI link read skipped" in w for w in r["warnings"])


def test_reset_clears_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mon._dn_devices, "resolve_canonical", lambda name: name)
    p = str(tmp_path / "s.json")
    st = spool.load(p)
    spool.record(st, "HCL", ["fp1"], cursor="2026-06-28T07:00:00Z")
    spool.set_links(st, "HCL", {"a": "UP"})
    spool.save(st, p)

    r = mon.monitor_reset(devices=["HCL"], state_path=p)
    assert r["status"] == "ok"
    assert "HCL" in r["cleared"]
    after = spool.load(p)
    assert spool.get_cursor(after, "HCL") is None
    assert spool.get_links(after, "HCL") is None


# --- CLI surface -----------------------------------------------------------

def test_cli_monitor_tick_json(tmp_path, monkeypatch):
    _fake_fleet(monkeypatch)
    monkeypatch.setattr(spool, "_path", lambda: str(tmp_path / "cli-spool.json"))
    r = runner.invoke(app, ["cli", "monitor", "tick", "--json"])
    assert r.exit_code == 0, r.stdout
    payload = json.loads(r.stdout)
    assert payload["operation"] == "monitor-tick"
    assert payload["new_event_count"] == 2


def test_cli_monitor_tick_notify_refuses_without_yes(tmp_path, monkeypatch):
    _fake_fleet(monkeypatch)
    monkeypatch.setattr(spool, "_path", lambda: str(tmp_path / "cli-spool.json"))
    r = runner.invoke(app, ["cli", "monitor", "tick", "--notify", "#net", "--json"])
    assert r.exit_code == 2
    assert any("--yes" in n for n in json.loads(r.stdout)["next_actions"])
