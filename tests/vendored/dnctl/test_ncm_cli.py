"""Unit tests for the NCM nested-CLI driver + run_ncm_cli tool (no device)."""

import pytest

from dnctl.cli.core import shell as core_shell
from dnctl.cli.core.shell import (
    ends_with_ncm_prompt,
    send_ncm_cli,
)
from dnctl.cli.tools import shell as shell_tool


# --- prompt matcher -------------------------------------------------------

@pytest.mark.parametrize(
    "line",
    [
        "AAF-NCM-A0#",
        "AAF-NCM-A0#  ",
        "AAF-NCM-A0(config)#",
        "(config)#",
        "(conf-if-eth-0/5)#",
        "(conf-if-eth-0/12)#",
    ],
)
def test_ncm_prompt_matches(line):
    assert ends_with_ncm_prompt(f"some output\n{line}")


@pytest.mark.parametrize(
    "line",
    [
        "(dn40-cl-301a-ncc1)root@routing_engine:/[2026-04-20 21:55:32][inband_ns]#",
        "just some text",
        "#",
        "   ",
        "",
    ],
)
def test_ncm_prompt_rejects(line):
    assert not ends_with_ncm_prompt(f"some output\n{line}" if line else line)


# --- fake channel ---------------------------------------------------------

class FakeChannel:
    """Scripted paramiko-like channel for the NCM driver.

    ``reactions`` maps a stripped sent line to the bytes the device emits in
    response (one chunk, terminated by a prompt). ``entry`` is the response
    to the ``run start shell ncm <id>`` line.
    """

    def __init__(self, reactions, entry):
        self._reactions = reactions
        self._entry = entry
        self._buf = b""
        self.sent = []

    def send(self, data):
        self.sent.append(data)
        line = data.strip()
        if line.startswith("run start shell ncm"):
            self._buf += self._entry.encode()
        elif line in self._reactions:
            self._buf += self._reactions[line].encode()
        else:
            # default: echo nothing useful, just re-emit current ncm prompt
            self._buf += b"AAF-NCM-A0#"
        return len(data)

    def recv_ready(self):
        return bool(self._buf)

    def recv(self, n):
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk


def test_send_ncm_cli_show_no_password():
    entry = "\r\nAAF-NCM-A0#"
    reactions = {
        "show lldp neighbors": (
            "show lldp neighbors\r\n"
            "Interface  Neighbor\r\n"
            "eth 0/5    ctrl-ncp-6/0\r\n"
            "AAF-NCM-A0#"
        ),
        "end": "AAF-NCM-A0#",
        "exit": "\r\nncc1#",
    }
    ch = FakeChannel(reactions, entry)

    out, head, tail, hit = send_ncm_cli(
        ch, ["show lldp neighbors"], password="dnroot",
        dnos_prompt="ncc1#", shell_entry="run start shell ncm A0",
        overall_timeout=5.0,
    )

    assert hit is True
    assert "ctrl-ncp-6/0" in out
    assert "eth 0/5" in out
    # echoed command + trailing prompt stripped from agent output
    assert not out.strip().endswith("AAF-NCM-A0#")
    # entered the NCM shell and backed out cleanly
    assert ch.sent[0] == "run start shell ncm A0\n"
    assert "exit\n" in ch.sent


def test_send_ncm_cli_with_password_challenge():
    entry = "\r\nPassword:"
    reactions = {
        "show lldp neighbors": "show lldp neighbors\r\nok\r\nAAF-NCM-A0#",
        "end": "AAF-NCM-A0#",
        "exit": "\r\nncc1#",
    }
    ch = FakeChannel(reactions, entry)
    # After the password, the device lands at the NCM prompt; the password
    # send triggers the default reaction (re-emit the NCM prompt).

    out, _head, _tail, hit = send_ncm_cli(
        ch, ["show lldp neighbors"], password="secretpw",
        dnos_prompt="ncc1#", shell_entry="run start shell ncm A0",
        overall_timeout=5.0,
    )

    assert hit is True
    assert "ok" in out
    # the password was actually sent after the challenge
    assert "secretpw\n" in ch.sent


def test_send_ncm_cli_config_sequence():
    entry = "\r\nAAF-NCM-A0#"
    reactions = {
        "configure": "configure\r\nAAF-NCM-A0(config)#",
        "interface eth 0/5": "interface eth 0/5\r\nAAF-NCM-A0(conf-if-eth-0/5)#",
        "shutdown": "shutdown\r\nAAF-NCM-A0(conf-if-eth-0/5)#",
        "end": "AAF-NCM-A0#",
        "exit": "\r\nncc1#",
    }
    ch = FakeChannel(reactions, entry)

    out, _head, _tail, hit = send_ncm_cli(
        ch, ["configure", "interface eth 0/5", "shutdown"],
        password="dnroot", dnos_prompt="ncc1#",
        shell_entry="run start shell ncm A0", overall_timeout=5.0,
    )

    assert hit is True
    assert "configure\n" in ch.sent
    assert "interface eth 0/5\n" in ch.sent
    assert "shutdown\n" in ch.sent
    # backed out of config mode before exiting
    assert "end\n" in ch.sent


def test_send_ncm_cli_entry_timeout_backs_out():
    # entry never reaches a prompt or password → driver gives up, hit False.
    ch = FakeChannel({"exit": "\r\nncc1#", "end": "AAF-NCM-A0#"},
                     entry="\r\nstill booting...")
    out, _head, _tail, hit = send_ncm_cli(
        ch, ["show lldp neighbors"], password="dnroot",
        dnos_prompt="ncc1#", shell_entry="run start shell ncm A0",
        overall_timeout=0.3,
    )
    assert hit is False
    assert out == ""


# --- tool surface ---------------------------------------------------------

@pytest.fixture
def captured(monkeypatch):
    calls = {}

    def _fake(tool, device, host, user, password, ncm_commands,
              shell_entry, timeout, next_action):
        calls.update(
            tool=tool, device=device, host=host,
            ncm_commands=ncm_commands, shell_entry=shell_entry,
            timeout=timeout,
        )
        return {"status": "ok", "stdout": "out", "command": " ; ".join(ncm_commands)}

    monkeypatch.setattr(shell_tool, "_run_ncm_on_device", _fake)
    return calls


def test_tool_single_command(captured):
    r = shell_tool.run_ncm_cli("show lldp neighbors", ncm="A0", device="cl")
    assert r["status"] == "ok"
    assert captured["ncm_commands"] == ["show lldp neighbors"]
    assert captured["shell_entry"] == "run start shell ncm A0"


def test_tool_sequence(captured):
    shell_tool.run_ncm_cli(
        ["configure", "interface eth 0/5", "shutdown"], ncm="B0", device="cl"
    )
    assert captured["ncm_commands"] == ["configure", "interface eth 0/5", "shutdown"]
    assert captured["shell_entry"] == "run start shell ncm B0"


def test_tool_blank_commands_dropped(captured):
    shell_tool.run_ncm_cli(["  ", "show lldp neighbors", ""], ncm="A0", device="cl")
    assert captured["ncm_commands"] == ["show lldp neighbors"]


def test_tool_empty_commands_error(captured):
    r = shell_tool.run_ncm_cli([], ncm="A0", device="cl")
    assert r["status"] == "error"
    assert "ncm_commands" not in captured


def test_tool_missing_ncm_error(captured):
    r = shell_tool.run_ncm_cli("show lldp neighbors", ncm="", device="cl")
    assert r["status"] == "error"
    assert "ncm_commands" not in captured


def test_tool_invalid_ncm_error(captured):
    r = shell_tool.run_ncm_cli("show lldp neighbors", ncm="bad id", device="cl")
    assert r["status"] == "error"
    assert "ncm_commands" not in captured
