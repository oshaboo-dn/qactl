"""Trace tools on NCP targets (issue #81) — no device traffic.

A non-preset ``--ncp`` call must not look in ``/core/traces/routing_engine``
(absent on NCPs): listing walks ``/core/traces/`` recursively, ``get_trace``
resolves subdir-relative names (``datapath/wb_agent.bfd``), and a failed
listing (ls/find stderr in the transcript) yields status=error.
"""

import pytest

from qactl.dnos.cli.tools import traces as traces_tool


def _ok(stdout=""):
    return {"status": "ok", "device": "cl", "host": "h", "command": "",
            "stdout": stdout, "warnings": [], "errors": [], "next_actions": []}


@pytest.fixture
def shell_calls(monkeypatch):
    """Stub run_linux_on_device; record the built pipeline + shell entry."""
    calls = []
    reply = {"response": _ok()}

    def _fake(tool, device, host, user, password, linux_command,
              timeout, next_action, *, shell_entry="run start shell"):
        calls.append({"command": linux_command, "shell_entry": shell_entry})
        return dict(reply["response"])

    monkeypatch.setattr(traces_tool, "run_linux_on_device", _fake)
    return calls, reply


# --- target resolution -------------------------------------------------------


def test_resolve_ncp_roots_at_core_traces():
    shell, base, primary, err = traces_tool._resolve_trace_target(
        None, None, "7", None,
    )
    assert err is None
    assert shell == "run start shell ncp 7"
    assert base == "/core/traces"
    assert primary is None


def test_resolve_ncc_keeps_routing_engine_default():
    for ncc in (None, "0"):
        shell, base, _primary, err = traces_tool._resolve_trace_target(
            None, ncc, None, None,
        )
        assert err is None
        assert base == "/core/traces/routing_engine"


# --- list_traces -------------------------------------------------------------


def test_list_traces_ncp_walks_subdirs(shell_calls):
    calls, _ = shell_calls
    r = traces_tool.list_traces(device="cl", ncp="7")
    assert r["status"] == "ok"
    cmd = calls[0]["command"]
    assert calls[0]["shell_entry"] == "run start shell ncp 7"
    assert cmd.startswith("find /core/traces/ -mindepth 1 -maxdepth 2 -type f")
    assert "%P" in cmd  # relative paths like datapath/wb_agent.bfd
    assert "routing_engine" not in cmd
    assert "sort -r" in cmd and "head -n 200" in cmd


def test_list_traces_ncp_keeps_filters(shell_calls):
    calls, _ = shell_calls
    traces_tool.list_traces(device="cl", ncp="7", component="wb_agent",
                            include_rotated=False)
    cmd = calls[0]["command"]
    assert "grep -v -F -- '.gz'" in cmd
    assert "grep -F -- wb_agent" in cmd


def test_list_traces_ncc_pipeline_unchanged(shell_calls):
    calls, _ = shell_calls
    traces_tool.list_traces(device="cl")
    cmd = calls[0]["command"]
    assert cmd.startswith("ls -laL")
    assert "/core/traces/routing_engine/" in cmd
    assert "find" not in cmd


def test_list_traces_preset_unchanged(shell_calls):
    calls, _ = shell_calls
    traces_tool.list_traces(target="wb_agent", device="cl", ncp="1")
    cmd = calls[0]["command"]
    assert "/core/traces/datapath/" in cmd
    assert cmd.startswith("ls -laL")


def test_list_traces_failed_listing_is_error(shell_calls):
    calls, reply = shell_calls
    reply["response"] = _ok(
        "ls: cannot access '/core/traces/routing_engine/': "
        "No such file or directory\n"
    )
    r = traces_tool.list_traces(device="cl", ncc="0")
    assert r["status"] == "error"
    assert any("cannot access" in e for e in r["errors"])
    assert r["next_actions"]


def test_list_traces_find_error_is_error(shell_calls):
    calls, reply = shell_calls
    reply["response"] = _ok(
        "find: '/core/traces/': No such file or directory\n"
    )
    r = traces_tool.list_traces(device="cl", ncp="7")
    assert r["status"] == "error"


def test_list_traces_clean_listing_stays_ok(shell_calls):
    calls, reply = shell_calls
    reply["response"] = _ok(
        "2026-07-05 14:33:12 +0300 51200 datapath/wb_agent.bfd\n"
        "2026-07-05 14:31:02 +0300 9000 node-manager/nm_traces\n"
    )
    r = traces_tool.list_traces(device="cl", ncp="7")
    assert r["status"] == "ok"
    assert r["errors"] == []


# --- get_trace ---------------------------------------------------------------


def test_get_trace_ncp_subdir_name_live(shell_calls):
    calls, _ = shell_calls
    r = traces_tool.get_trace(device="cl", ncp="7",
                              name="datapath/wb_agent.bfd", live_only=True)
    assert r["status"] == "ok"
    cmd = calls[0]["command"]
    assert calls[0]["shell_entry"] == "run start shell ncp 7"
    assert "cat /core/traces/datapath/wb_agent.bfd" in cmd
    assert "routing_engine" not in cmd


def test_get_trace_ncp_subdir_name_multi(shell_calls):
    calls, _ = shell_calls
    r = traces_tool.get_trace(device="cl", ncp="7",
                              name="datapath/wb_agent.bfd")
    assert r["status"] == "ok"
    cmd = calls[0]["command"]
    # archives are enumerated in the folded dir, by the basename.
    assert "find /core/traces/datapath -maxdepth 1" in cmd
    assert "'wb_agent.bfd-*.gz'" in cmd
    assert "/core/traces/datapath/wb_agent.bfd" in cmd


@pytest.mark.parametrize("bad", [
    "../routing_engine/bgpd_traces",
    "datapath/../../../etc/passwd",
    "datapath//wb_agent",
    "datapath/./wb_agent",
    "/etc/passwd",
    "datapath/wb;rm -rf /",
])
def test_get_trace_rejects_bad_subpaths(shell_calls, bad):
    calls, _ = shell_calls
    r = traces_tool.get_trace(device="cl", ncp="7", name=bad)
    assert r["status"] == "error"
    assert calls == []


def test_get_trace_bare_name_still_works(shell_calls):
    calls, _ = shell_calls
    r = traces_tool.get_trace(device="cl", name="rib-manager_traces",
                              live_only=True)
    assert r["status"] == "ok"
    assert "/core/traces/routing_engine/rib-manager_traces" in calls[0]["command"]
