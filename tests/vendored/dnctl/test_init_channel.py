"""Unit tests for fresh-channel prompt detection (session._init_channel).

Covers the bounded nudge/backoff loop that lets slow-to-print boxes (e.g.
DNAAS-LEAF-B13) land a CLI prompt before we declare it undetectable, plus
the env-tunable timeout knobs. No real device — a scripted fake channel.
"""

import pytest

from dnctl.cli.core import session as sess
from dnctl.cli.core.session import _env_float, _init_channel


PROMPT = "DNAAS-LEAF-B13#"


class FakeChannel:
    """Scripted paramiko-like channel for _init_channel.

    Emits a non-prompt login banner on open, then only paints a real CLI
    prompt after ``nudges_needed`` bare-newline nudges. A non-newline send
    (the ``set cli-terminal-length 0`` init command) is echoed back with a
    trailing prompt so :func:`send_command` completes.
    """

    def __init__(self, nudges_needed, prompt=PROMPT):
        self.nudges_needed = nudges_needed
        self.prompt = prompt
        self._buf = b"\r\nWelcome to DNOS\r\nLast login: today\r\n"
        self._nudges = 0
        self.sent = []

    def settimeout(self, _t):
        pass

    def send(self, data):
        self.sent.append(data)
        if data == "\n":
            self._nudges += 1
            if self._nudges >= self.nudges_needed:
                self._buf += self.prompt.encode()
            else:
                self._buf += b"...still booting...\r\n"
        else:
            self._buf += (data.strip() + "\r\n" + self.prompt).encode()
        return len(data)

    def recv_ready(self):
        return bool(self._buf)

    def recv(self, n):
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk


class NeverPromptChannel(FakeChannel):
    """Never paints a prompt, no matter how many nudges it gets."""

    def send(self, data):
        self.sent.append(data)
        self._buf += b"...still booting...\r\n"
        return len(data)


def _fast_env(monkeypatch):
    # Keep per-drain windows tiny so the test loop is quick.
    monkeypatch.setenv("DNCTL_CLI_BANNER_WAIT", "0.05")


def _nudge_count(ch):
    return sum(1 for s in ch.sent if s == "\n")


def test_prompt_in_initial_banner_needs_no_nudge(monkeypatch):
    _fast_env(monkeypatch)
    ch = FakeChannel(nudges_needed=0)
    # nudges_needed=0 means the prompt is appended on open (via send of init
    # cmd) — emulate a box that prompts immediately by seeding it.
    ch._buf += PROMPT.encode()

    prompt = _init_channel(ch)

    assert prompt == PROMPT
    assert _nudge_count(ch) == 0


def test_slow_box_recovered_by_nudges(monkeypatch):
    _fast_env(monkeypatch)
    monkeypatch.setenv("DNCTL_CLI_PROMPT_TIMEOUT", "15")
    ch = FakeChannel(nudges_needed=3)

    prompt = _init_channel(ch)

    assert prompt == PROMPT
    # Took at least the scripted number of nudges before the prompt showed.
    assert _nudge_count(ch) >= 3


def test_gives_up_after_budget_exhausted(monkeypatch):
    _fast_env(monkeypatch)
    monkeypatch.setenv("DNCTL_CLI_PROMPT_TIMEOUT", "0.2")
    ch = NeverPromptChannel(nudges_needed=99)

    with pytest.raises(RuntimeError, match="Could not detect CLI prompt"):
        _init_channel(ch)


def test_longer_timeout_allows_more_nudges(monkeypatch):
    # A box that needs many nudges fails under a tiny budget but succeeds
    # once the timeout is widened — proving the knob actually extends the
    # detection window.
    _fast_env(monkeypatch)
    monkeypatch.setenv("DNCTL_CLI_PROMPT_TIMEOUT", "10")
    ch = FakeChannel(nudges_needed=5)

    assert _init_channel(ch) == PROMPT
    assert _nudge_count(ch) >= 5


def test_env_float_fallbacks(monkeypatch):
    monkeypatch.delenv("DNCTL_CLI_PROMPT_TIMEOUT", raising=False)
    assert _env_float("DNCTL_CLI_PROMPT_TIMEOUT", 15.0) == 15.0

    monkeypatch.setenv("DNCTL_CLI_PROMPT_TIMEOUT", "not-a-number")
    assert _env_float("DNCTL_CLI_PROMPT_TIMEOUT", 15.0) == 15.0

    # Non-positive values must not let detection give up faster than default.
    monkeypatch.setenv("DNCTL_CLI_PROMPT_TIMEOUT", "0")
    assert _env_float("DNCTL_CLI_PROMPT_TIMEOUT", 15.0) == 15.0
    monkeypatch.setenv("DNCTL_CLI_PROMPT_TIMEOUT", "-3")
    assert _env_float("DNCTL_CLI_PROMPT_TIMEOUT", 15.0) == 15.0

    monkeypatch.setenv("DNCTL_CLI_PROMPT_TIMEOUT", "42.5")
    assert _env_float("DNCTL_CLI_PROMPT_TIMEOUT", 15.0) == 42.5


def test_default_prompt_timeout_constant():
    assert sess.DEFAULT_PROMPT_TIMEOUT >= sess.DEFAULT_BANNER_WAIT
