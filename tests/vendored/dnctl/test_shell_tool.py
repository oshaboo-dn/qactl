"""Unit tests for the run_shell tool (no device traffic)."""

import pytest

from qactl.dnctl.cli.tools import shell as shell_tool


@pytest.fixture
def captured(monkeypatch):
    """Stub run_linux_on_device; capture the linux_command + shell_entry."""
    calls = {}

    def _fake(tool, device, host, user, password, linux_command,
              timeout, next_action, *, shell_entry="run start shell"):
        calls.update(
            tool=tool, device=device, host=host, user=user,
            linux_command=linux_command, timeout=timeout,
            shell_entry=shell_entry,
        )
        return {"status": "ok", "stdout": "out", "command": linux_command}

    monkeypatch.setattr(shell_tool, "run_linux_on_device", _fake)
    return calls


def test_single_command(captured):
    r = shell_tool.run_shell("ls -la /var/log", device="sa")
    assert r["status"] == "ok"
    assert captured["linux_command"] == "ls -la /var/log"
    assert captured["shell_entry"] == "run start shell"


def test_sequence_joined_with_and(captured):
    shell_tool.run_shell(["cd /tmp", "ls", "df -h"], device="sa")
    assert captured["linux_command"] == "cd /tmp && ls && df -h"


def test_sequence_continue_on_error_uses_semicolon(captured):
    shell_tool.run_shell(["false", "echo done"], device="sa", continue_on_error=True)
    assert captured["linux_command"] == "false ; echo done"


def test_blank_commands_are_dropped(captured):
    shell_tool.run_shell(["  ", "uptime", ""], device="sa")
    assert captured["linux_command"] == "uptime"


def test_targets_ncp(captured):
    shell_tool.run_shell("uptime", device="sa", ncp="0")
    assert captured["shell_entry"] == "run start shell ncp 0"


def test_targets_container(captured):
    shell_tool.run_shell("uptime", device="sa", ncc="1", container="netconf")
    assert captured["shell_entry"] == "run start shell ncc 1 container netconf"


def test_targets_ncm(captured):
    shell_tool.run_shell("uptime", device="sa", ncm="A0")
    assert captured["shell_entry"] == "run start shell ncm A0"


def test_targets_ncm_b0(captured):
    shell_tool.run_shell("uptime", device="sa", ncm="B0")
    assert captured["shell_entry"] == "run start shell ncm B0"


def test_invalid_ncm_error(captured):
    r = shell_tool.run_shell("ls", device="sa", ncm="bad id")
    assert r["status"] == "error"
    assert "linux_command" not in captured


def test_ncm_ncc_mutually_exclusive(captured):
    r = shell_tool.run_shell("ls", device="sa", ncm="A0", ncc="0")
    assert r["status"] == "error"
    assert "linux_command" not in captured


def test_ncm_ncp_mutually_exclusive(captured):
    r = shell_tool.run_shell("ls", device="sa", ncm="A0", ncp="0")
    assert r["status"] == "error"
    assert "linux_command" not in captured


def test_ncm_container_rejected(captured):
    r = shell_tool.run_shell("ls", device="sa", ncm="A0", container="netconf")
    assert r["status"] == "error"
    assert "linux_command" not in captured


def test_empty_commands_error(captured):
    r = shell_tool.run_shell([], device="sa")
    assert r["status"] == "error"
    assert "linux_command" not in captured  # never reached the device


def test_invalid_ncc_error(captured):
    r = shell_tool.run_shell("ls", device="sa", ncc="9")
    assert r["status"] == "error"
    assert "linux_command" not in captured


def test_ncc_ncp_mutually_exclusive(captured):
    r = shell_tool.run_shell("ls", device="sa", ncc="0", ncp="0")
    assert r["status"] == "error"
    assert "linux_command" not in captured
