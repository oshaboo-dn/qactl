"""Unit tests for the `cli raw` escape hatch (run_raw tool + runner glue)
and the explicit prompt-detection knobs threaded into _init_channel. No real
device — scripted fakes throughout.
"""

import pytest

from qactl.dnctl.cli.core import runner as core_runner
from qactl.dnctl.cli.core.session import (
    Invocation,
    StepCapture,
    _init_channel,
    run_sequence,
)
from qactl.dnctl.cli.tools import raw as raw_tool


# --- _init_channel knob precedence ----------------------------------------

PROMPT = "DNAAS-LEAF-B13#"


class FakeChannel:
    """Slow box: only paints a prompt after ``nudges_needed`` bare newlines."""

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


def test_explicit_prompt_timeout_overrides_tiny_env(monkeypatch):
    # Tiny env budget would give up before the box prompts, but an explicit
    # prompt_timeout arg widens the window and detection succeeds.
    monkeypatch.setenv("DNCTL_CLI_BANNER_WAIT", "0.05")
    monkeypatch.setenv("DNCTL_CLI_PROMPT_TIMEOUT", "0.2")
    ch = FakeChannel(nudges_needed=5)

    assert _init_channel(ch, prompt_timeout=10.0) == PROMPT
    assert sum(1 for s in ch.sent if s == "\n") >= 5


def test_nonpositive_arg_falls_through_to_env(monkeypatch):
    # A bogus (<=0) explicit arg must not shrink the window below the env knob.
    monkeypatch.setenv("DNCTL_CLI_BANNER_WAIT", "0.05")
    monkeypatch.setenv("DNCTL_CLI_PROMPT_TIMEOUT", "10")
    ch = FakeChannel(nudges_needed=4)

    assert _init_channel(ch, prompt_timeout=0) == PROMPT


# --- interactive (yes/no) confirms through run_sequence (issue #82) --------

CONFIRM = "Do you want to continue? (yes/no) [no]? "


class ConfirmChannel(FakeChannel):
    """Box where 'request ...' pauses at a (yes/no) confirm until answered."""

    def __init__(self):
        super().__init__(nudges_needed=1)
        self.answers = []

    def send(self, data):
        if data == "\n":
            return super().send(data)
        self.sent.append(data)
        line = data.strip()
        if line.startswith("request"):
            self._buf += (
                line + "\r\nWarning: Package will be downloaded.\r\n" + CONFIRM
            ).encode()
        elif line in ("yes", "no", "y", "n"):
            self.answers.append(line)
            self._buf += ("\r\nstaging package\r\n" + self.prompt).encode()
        else:
            self._buf += (line + "\r\n" + self.prompt).encode()
        return len(data)


class FakeTransport:
    def __init__(self, channel):
        self.client = self
        self._channel = channel
        self.host = "10.0.0.1"
        self.device = "cl"
        self.key = ("cl",)
        self.last_used = 0.0

    def invoke_shell(self, **_kw):
        return self._channel

    def close(self):
        pass


class FakeRegistry:
    def __init__(self, channel):
        self._transport = FakeTransport(channel)

    def get(self, **_kw):
        return self._transport

    def _mark(self, _t, _d):
        pass

    def drop(self, _key, reason=None):
        pass


def test_sequence_auto_confirm_answers_and_hits_prompt(monkeypatch):
    monkeypatch.setenv("DNCTL_CLI_BANNER_WAIT", "0.05")
    ch = ConfirmChannel()

    result = run_sequence(
        FakeRegistry(ch), device="cl", host=None, user="u", password="p",
        commands=["request system target-stack load http://x/pkg.tar"],
        timeout=2.0, auto_confirm=True, confirm_answer="yes",
    )

    assert result.hit_prompt
    assert ch.answers == ["yes"]
    assert "staging package" in result.output


def test_sequence_confirm_answer_no_is_forwarded(monkeypatch):
    monkeypatch.setenv("DNCTL_CLI_BANNER_WAIT", "0.05")
    ch = ConfirmChannel()

    result = run_sequence(
        FakeRegistry(ch), device="cl", host=None, user="u", password="p",
        commands=["request system target-stack load http://x/pkg.tar"],
        timeout=2.0, auto_confirm=True, confirm_answer="no",
    )

    assert result.hit_prompt
    assert ch.answers == ["no"]


def test_sequence_without_auto_confirm_times_out_at_confirm(monkeypatch):
    # The pre-#82 failure mode: line 1 sits at the confirm, the follow-up
    # 'yes' line is never sent, and the step times out without the prompt.
    monkeypatch.setenv("DNCTL_CLI_BANNER_WAIT", "0.05")
    ch = ConfirmChannel()

    result = run_sequence(
        FakeRegistry(ch), device="cl", host=None, user="u", password="p",
        commands=["request system target-stack load http://x/pkg.tar", "yes"],
        timeout=0.5,
    )

    assert not result.hit_prompt
    assert ch.answers == []
    assert len(result.steps) == 1  # 'yes' never sent
    assert "(yes/no)" in result.steps[0].output


# --- runner glue (_run_raw_on_device) -------------------------------------

@pytest.fixture
def fake_sequence(monkeypatch):
    """Replace run_sequence with a scripted Invocation; silence logging."""
    captured = {}

    def _make(steps):
        return Invocation(
            output=steps[-1].output if steps else "",
            hit_prompt=steps[-1].hit_prompt if steps else True,
            head_prompt_line="",
            tail_prompt="",
            host="10.0.0.1",
            device="b13",
            steps=steps,
        )

    state = {"factory": lambda **kw: _make([StepCapture("noop", "", "", "", True)])}

    def _fake_run_sequence(registry, **kwargs):
        captured.update(kwargs)
        return state["factory"](**kwargs)

    monkeypatch.setattr(core_runner, "run_sequence", _fake_run_sequence)
    monkeypatch.setattr(core_runner, "log_invocation", lambda *a, **k: None)
    monkeypatch.setattr(core_runner, "log_request", lambda *a, **k: None)
    return captured, state, _make


def test_runner_transcript_and_steps(fake_sequence):
    captured, state, _make = fake_sequence
    steps = [
        StepCapture("show isis neighbors", "", "Hybrid-CL  Up\n", "", True),
        StepCapture("show bgp summary", "", "2/2 established\n", "", True),
    ]
    state["factory"] = lambda **kw: _make(steps)

    r = core_runner._run_raw_on_device(
        "run_raw", "b13", None, "u", "p", ["show isis neighbors", "show bgp summary"],
        30.0, "next-action",
    )

    assert r["status"] == "ok"
    # human transcript carries every line + its output
    assert "show isis neighbors" in r["stdout"]
    assert "2/2 established" in r["stdout"]
    # structured per-line steps
    assert [s["command"] for s in r["steps"]] == [
        "show isis neighbors", "show bgp summary",
    ]
    assert all(s["hit_prompt"] for s in r["steps"])


def test_runner_stop_on_error_passes_predicate(fake_sequence):
    captured, _state, _make = fake_sequence
    core_runner._run_raw_on_device(
        "run_raw", "b13", None, "u", "p", ["configure", "bogus stmt"],
        30.0, "next-action", stop_on_error=True,
    )
    assert captured["stop_predicate"] is not None


def test_runner_continue_on_error_no_predicate(fake_sequence):
    captured, _state, _make = fake_sequence
    core_runner._run_raw_on_device(
        "run_raw", "b13", None, "u", "p", ["a", "b"],
        30.0, "next-action", stop_on_error=False,
    )
    assert captured["stop_predicate"] is None


def test_runner_knobs_passthrough(fake_sequence):
    captured, _state, _make = fake_sequence
    core_runner._run_raw_on_device(
        "run_raw", "b13", None, "u", "p", ["x"], 30.0, "next-action",
        prompt_timeout=45.0, banner_wait=3.0,
    )
    assert captured["prompt_timeout"] == 45.0
    assert captured["banner_wait"] == 3.0


def test_runner_flags_mid_sequence_error(fake_sequence):
    _captured, state, _make = fake_sequence
    steps = [
        StepCapture("configure", "", "", "", True),
        StepCapture("bogus", "", "ERROR: Unknown word: 'bogus'.\n", "", True),
    ]
    state["factory"] = lambda **kw: _make(steps)

    r = core_runner._run_raw_on_device(
        "run_raw", "b13", None, "u", "p", ["configure", "bogus"],
        30.0, "next-action",
    )
    assert r["status"] == "error"
    assert any("Unknown word" in e for e in r["errors"])


def test_runner_timeout_status(fake_sequence):
    _captured, state, _make = fake_sequence
    steps = [StepCapture("show tech-support", "", "...partial...", "", False)]
    state["factory"] = lambda **kw: _make(steps)

    r = core_runner._run_raw_on_device(
        "run_raw", "b13", None, "u", "p", ["show tech-support"],
        5.0, "next-action",
    )
    assert r["status"] == "timeout"
    # not stuck at a confirm -> the generic budget hint, not --answer-confirm
    assert not any("--answer-confirm" in a for a in r["next_actions"])
    assert any("--timeout" in a for a in r["next_actions"])


def test_runner_answer_confirm_passthrough(fake_sequence):
    captured, _state, _make = fake_sequence
    core_runner._run_raw_on_device(
        "run_raw", "cl", None, "u", "p", ["request system target-stack load x"],
        30.0, "next-action", answer_confirm="yes",
    )
    assert captured["auto_confirm"] is True
    assert captured["confirm_answer"] == "yes"


def test_runner_default_no_auto_confirm(fake_sequence):
    captured, _state, _make = fake_sequence
    core_runner._run_raw_on_device(
        "run_raw", "cl", None, "u", "p", ["show version"], 30.0, "next-action",
    )
    assert captured["auto_confirm"] is False


def test_runner_timeout_at_confirm_hints_answer_confirm(fake_sequence):
    # The issue-#82 transcript shape: the step timed out with the (yes/no)
    # confirm as the last painted line -> point at --answer-confirm.
    _captured, state, _make = fake_sequence
    steps = [StepCapture(
        "request system target-stack load http://x/pkg.tar", "",
        "Warning: Package will be downloaded and added to target stack.\n"
        "Do you want to continue? (yes/no) [no]?\n",
        "", False,
    )]
    state["factory"] = lambda **kw: _make(steps)

    r = core_runner._run_raw_on_device(
        "run_raw", "cl", None, "u", "p",
        ["request system target-stack load http://x/pkg.tar", "yes"],
        120.0, "next-action",
    )
    assert r["status"] == "timeout"
    assert any("--answer-confirm" in a for a in r["next_actions"])


# --- tool surface ---------------------------------------------------------

@pytest.fixture
def captured(monkeypatch):
    calls = {}

    def _fake(tool, device, host, user, password, lines, timeout, next_action,
              stop_on_error=True, answer_confirm=None, prompt_timeout=None,
              banner_wait=None):
        calls.update(
            tool=tool, device=device, host=host, lines=lines, timeout=timeout,
            stop_on_error=stop_on_error, answer_confirm=answer_confirm,
            prompt_timeout=prompt_timeout, banner_wait=banner_wait,
        )
        return {"status": "ok", "stdout": "out", "command": " ; ".join(lines)}

    monkeypatch.setattr(raw_tool, "_run_raw_on_device", _fake)
    return calls


def test_tool_single_line(captured):
    r = raw_tool.run_raw("show isis neighbors", device="b13")
    assert r["status"] == "ok"
    assert captured["lines"] == ["show isis neighbors"]
    assert captured["stop_on_error"] is True


def test_tool_sequence(captured):
    raw_tool.run_raw(["configure", "set x", "commit"], device="b13")
    assert captured["lines"] == ["configure", "set x", "commit"]


def test_tool_blank_lines_dropped(captured):
    raw_tool.run_raw(["  ", "show version", ""], device="b13")
    assert captured["lines"] == ["show version"]


def test_tool_empty_error(captured):
    r = raw_tool.run_raw([], device="b13")
    assert r["status"] == "error"
    assert "lines" not in captured


def test_tool_knobs_passthrough(captured):
    raw_tool.run_raw(
        "show version", device="b13",
        stop_on_error=False, prompt_timeout=30.0, banner_wait=2.0,
    )
    assert captured["stop_on_error"] is False
    assert captured["prompt_timeout"] == 30.0
    assert captured["banner_wait"] == 2.0
    assert captured["answer_confirm"] is None


def test_tool_answer_confirm_passthrough(captured):
    raw_tool.run_raw(
        "request system target-stack load http://x/pkg.tar",
        device="cl", answer_confirm="yes",
    )
    assert captured["answer_confirm"] == "yes"
