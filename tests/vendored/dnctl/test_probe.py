"""Unit tests for `cli probe` — bare-keystroke (TAB / ?) discoverability
probes that never submit the line (issue #74). No real device — a scripted
DNOS-ish PTY fake drives send_probe / run_probes; the runner glue and tool
surface are tested with monkeypatched seams, mirroring test_raw.py.
"""

import pytest

from qactl.dnos.cli.core import runner as core_runner
from qactl.dnos.cli.core.session import Invocation, StepCapture, run_probes
from qactl.dnos.cli.core.shell import send_probe
from qactl.dnos.cli.tools import probe as probe_tool


PROMPT = "SA#"
HELP_BLOCK = "strict-mode           Configure BFD strict-mode\r\n"


class FakeProbeChannel:
    """DNOS-ish PTY fake: echoes keystrokes, answers '?' with a help block
    plus a ``PROMPT# <line>`` repaint, completes TAB from a scripted map,
    and Ctrl-U wipes the line buffer. A newline submits (repaints a bare
    prompt) — probes must never rely on it for the probe itself.
    """

    def __init__(self, tab_completions=None, help_block=HELP_BLOCK):
        self.tab_completions = tab_completions or {}
        self.help_block = help_block
        self.prompt = PROMPT
        self._line = ""
        self._buf = ("\r\nWelcome to DNOS\r\n" + PROMPT).encode()
        self.sent = []
        self.submitted = []

    def settimeout(self, _t):
        pass

    def recv_ready(self):
        return bool(self._buf)

    def recv(self, n):
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk

    def close(self):
        pass

    def _emit(self, s):
        self._buf += s.encode()

    def send(self, data):
        self.sent.append(data)
        for ch in data:
            if ch == "\n":
                self.submitted.append(self._line)
                self._line = ""
                self._emit("\r\n" + self.prompt)
            elif ch == "\x15":
                self._line = ""
            elif ch == "\t":
                suffix = self.tab_completions.get(self._line, "")
                self._line += suffix
                self._emit(suffix)
            elif ch == "?":
                self._emit(
                    "?\r\n" + self.help_block + self.prompt + " " + self._line
                )
            else:
                self._line += ch
                self._emit(ch)
        return len(data)


# --- send_probe (shell primitive) ------------------------------------------

def test_send_probe_question_mark_returns_help_never_submits():
    ch = FakeProbeChannel()
    prefix = "protocols bgp 100001 neighbor 1.1.1.1 bfd "

    output, line_buffer, hit = send_probe(ch, prefix, "?", PROMPT,
                                          overall_timeout=1.0)

    assert hit
    assert "strict-mode" in output
    # prompt repaints and the prefix echo are filtered out of the block
    assert PROMPT not in output
    # '?' never edits the buffer
    assert line_buffer == prefix
    # nothing but the post-probe Ctrl-U clear was ever submitted
    assert ch.submitted == [""]
    # the prefix went over the wire verbatim, trailing space intact, no CR
    assert ch.sent[0] == prefix
    assert ch.sent[1] == "?"
    assert ch.sent[2] == "\x15\n"


def test_send_probe_tab_completes_line_buffer():
    prefix = "protocols bgp 100001 neighbor 1.1.1.1 bfd str"
    ch = FakeProbeChannel(tab_completions={prefix: "ict-mode "})

    output, line_buffer, hit = send_probe(ch, prefix, "tab", PROMPT,
                                          overall_timeout=1.0)

    assert hit
    assert line_buffer == prefix + "ict-mode "
    assert ch.submitted == [""]
    assert "\t" in ch.sent


class AmbiguousTabChannel(FakeProbeChannel):
    """TAB on an ambiguous prefix: candidates painted + ``PROMPT# <line>``
    repaint, but the line buffer itself is unchanged."""

    def send(self, data):
        if data == "\t":
            self.sent.append(data)
            self._emit(
                "\r\nsessions   system\r\n" + self.prompt + " " + self._line
            )
            return len(data)
        return super().send(data)


def test_send_probe_tab_ambiguous_strips_prompt_repaint():
    # The buffer must come back unchanged, without the repainted prompt
    # glued on, and the candidate list lands in the output block.
    ch = AmbiguousTabChannel()
    prefix = "show s"

    output, line_buffer, hit = send_probe(ch, prefix, "tab", PROMPT,
                                          overall_timeout=1.0)
    assert hit
    assert line_buffer == prefix
    assert "sessions" in output
    assert PROMPT not in output


# --- run_probes (session driver) --------------------------------------------

class FakeTransport:
    def __init__(self, channel):
        self.client = self
        self._channel = channel
        self.host = "10.0.0.1"
        self.device = "sa"
        self.key = ("sa",)
        self.last_used = 0.0

    def invoke_shell(self, **_kw):
        return self._channel


class FakeRegistry:
    def __init__(self, channel):
        self._transport = FakeTransport(channel)

    def get(self, **_kw):
        return self._transport

    def _mark(self, _t, _d):
        pass

    def drop(self, _key, reason=None):
        pass


def test_run_probes_one_channel_config_mode(monkeypatch):
    monkeypatch.setenv("QACTL_CLI_BANNER_WAIT", "0.05")
    prefix_help = "protocols bgp 100001 neighbor 1.1.1.1 bfd "
    prefix_tab = "protocols bgp 100001 neighbor 1.1.1.1 bfd str"
    ch = FakeProbeChannel(tab_completions={prefix_tab: "ict-mode "})

    result = run_probes(
        FakeRegistry(ch), device="sa", host=None, user="u", password="p",
        probes=[(prefix_help, "?"), (prefix_tab, "tab")],
        config_mode=True, timeout=1.0,
    )

    assert result.hit_prompt
    # the configure entry is recorded as the first step, probes follow
    assert [s.command for s in result.steps] == [
        "configure", prefix_help + "?", prefix_tab + "<TAB>",
    ]
    assert "strict-mode" in result.steps[1].output
    assert result.steps[1].line_buffer == prefix_help
    assert result.steps[2].line_buffer == prefix_tab + "ict-mode "
    # config mode is entered before the probes and left afterwards; only
    # mode changes and Ctrl-U clears are ever submitted — never a probe
    assert "configure" in ch.submitted
    assert ch.submitted[-1] == "end"
    assert all(p not in ch.submitted for p in (prefix_help, prefix_tab))


def test_run_probes_oper_mode_never_enters_configure(monkeypatch):
    monkeypatch.setenv("QACTL_CLI_BANNER_WAIT", "0.05")
    ch = FakeProbeChannel()

    run_probes(
        FakeRegistry(ch), device="sa", host=None, user="u", password="p",
        probes=[("show ", "?")], timeout=1.0,
    )

    assert "configure\n" not in ch.sent
    assert "end\n" not in ch.sent


def test_run_probes_requires_probes():
    with pytest.raises(ValueError):
        run_probes(
            FakeRegistry(FakeProbeChannel()), device="sa", host=None,
            user="u", password="p", probes=[],
        )


# --- runner glue (_run_probe_on_device) --------------------------------------

@pytest.fixture
def fake_probes(monkeypatch):
    """Replace run_probes with a scripted Invocation; silence logging."""
    captured = {}

    def _make(steps):
        return Invocation(
            output=steps[-1].output if steps else "",
            hit_prompt=steps[-1].hit_prompt if steps else False,
            head_prompt_line="",
            tail_prompt="",
            host="10.0.0.1",
            device="sa",
            steps=steps,
        )

    state = {"factory": lambda: _make(
        [StepCapture("x?", "", "", "", True, "x")]
    )}

    def _fake_run_probes(registry, **kwargs):
        captured.update(kwargs)
        return state["factory"]()

    monkeypatch.setattr(core_runner, "run_probes", _fake_run_probes)
    monkeypatch.setattr(core_runner, "log_invocation", lambda *a, **k: None)
    monkeypatch.setattr(core_runner, "log_request", lambda *a, **k: None)
    return captured, state, _make


def test_runner_probe_steps_and_transcript(fake_probes):
    _captured, state, _make = fake_probes
    steps = [
        StepCapture("show bgp ?", "", "summary   BGP summary\n", "", True,
                    "show bgp "),
        StepCapture("show bgp su<TAB>", "", "", "", True, "show bgp summary "),
    ]
    state["factory"] = lambda: _make(steps)

    r = core_runner._run_probe_on_device(
        "run_probe", "sa", None, "u", "p",
        [("show bgp ", "?"), ("show bgp su", "tab")],
        30.0, "next-action",
    )

    assert r["status"] == "ok"
    assert [s["prefix"] for s in r["steps"]] == ["show bgp ", "show bgp su"]
    assert [s["key"] for s in r["steps"]] == ["?", "tab"]
    assert r["steps"][1]["line_buffer"] == "show bgp summary "
    # the tab probe's completed buffer is visible in the human transcript
    assert "[buffer] show bgp summary " in r["stdout"]
    assert "summary   BGP summary" in r["stdout"]


def test_runner_probe_knobs_passthrough(fake_probes):
    captured, _state, _make = fake_probes
    core_runner._run_probe_on_device(
        "run_probe", "sa", None, "u", "p", [("x", "?")], 30.0, "next-action",
        config_mode=True, prompt_timeout=45.0, banner_wait=3.0,
    )
    assert captured["config_mode"] is True
    assert captured["prompt_timeout"] == 45.0
    assert captured["banner_wait"] == 3.0


def test_runner_probe_timeout_mid_probes(fake_probes):
    _captured, state, _make = fake_probes
    state["factory"] = lambda: _make(
        [StepCapture("x?", "", "...partial...", "", False, "x")]
    )
    r = core_runner._run_probe_on_device(
        "run_probe", "sa", None, "u", "p", [("x", "?"), ("y", "?")],
        5.0, "next-action",
    )
    assert r["status"] == "timeout"
    assert any("probe 1 of 2" in e for e in r["errors"])


def test_runner_probe_timeout_entering_configure(fake_probes):
    _captured, state, _make = fake_probes
    state["factory"] = lambda: _make(
        [StepCapture("configure", "", "", "", False)]
    )
    r = core_runner._run_probe_on_device(
        "run_probe", "sa", None, "u", "p", [("x", "?")],
        5.0, "next-action", config_mode=True,
    )
    assert r["status"] == "timeout"
    assert any("configure" in e for e in r["errors"])


def test_runner_probe_config_mode_surfaces_failed_configure(fake_probes):
    # A GI-mode box answers 'configure' with an error but still presents a
    # prompt (issue #74 live-run lesson) — the envelope must not say ok.
    _captured, state, _make = fake_probes
    state["factory"] = lambda: _make([
        StepCapture("configure", "",
                    "-----^\nERROR: Unknown word.\n", "", True),
        StepCapture("protocols bgp ?", "", "", "", True, "protocols bgp "),
    ])
    r = core_runner._run_probe_on_device(
        "run_probe", "sa", None, "u", "p", [("protocols bgp ", "?")],
        30.0, "next-action", config_mode=True,
    )
    assert r["status"] == "error"
    assert any("Unknown word" in e for e in r["errors"])
    # the probe steps still map 1:1 to the requested probes
    assert [s["prefix"] for s in r["steps"]] == ["protocols bgp "]
    # the configure exchange is visible in the transcript
    assert r["stdout"].startswith("configure\n")


def test_runner_probe_surfaces_dnos_error(fake_probes):
    _captured, state, _make = fake_probes
    state["factory"] = lambda: _make([
        StepCapture("bogus ?", "", "ERROR: Unrecognized command\n", "", True,
                    "bogus "),
    ])
    r = core_runner._run_probe_on_device(
        "run_probe", "sa", None, "u", "p", [("bogus ", "?")],
        30.0, "next-action",
    )
    assert r["status"] == "error"
    assert any("Unrecognized" in e for e in r["errors"])


# --- tool surface (run_probe) -------------------------------------------------

@pytest.fixture
def captured(monkeypatch):
    calls = {}

    def _fake(tool, device, host, user, password, probes, timeout, next_action,
              config_mode=False, prompt_timeout=None, banner_wait=None):
        calls.update(
            tool=tool, device=device, probes=probes, timeout=timeout,
            config_mode=config_mode, prompt_timeout=prompt_timeout,
            banner_wait=banner_wait,
        )
        return {"status": "ok", "stdout": "", "steps": []}

    monkeypatch.setattr(probe_tool, "_run_probe_on_device", _fake)
    return calls


def test_tool_single_prefix_default_key(captured):
    r = probe_tool.run_probe("show bgp ", device="sa")
    assert r["status"] == "ok"
    assert captured["probes"] == [("show bgp ", "?")]


def test_tool_trailing_space_preserved(captured):
    probe_tool.run_probe(["protocols bgp 100001 neighbor 1.1.1.1 bfd "],
                         key="?", device="sa")
    assert captured["probes"][0][0].endswith("bfd ")


def test_tool_key_aliases(captured):
    probe_tool.run_probe("x", key="TAB", device="sa")
    assert captured["probes"] == [("x", "tab")]
    probe_tool.run_probe("x", key="\t", device="sa")
    assert captured["probes"] == [("x", "tab")]
    probe_tool.run_probe("x", key="help", device="sa")
    assert captured["probes"] == [("x", "?")]


def test_tool_bad_key_errors(captured):
    r = probe_tool.run_probe("x", key="enter", device="sa")
    assert r["status"] == "error"
    assert "probes" not in captured


def test_tool_empty_prefixes_error(captured):
    r = probe_tool.run_probe([], device="sa")
    assert r["status"] == "error"
    assert "probes" not in captured


def test_tool_root_probe_allowed(captured):
    # an empty-string prefix probes the tree root — that's valid
    probe_tool.run_probe([""], device="sa")
    assert captured["probes"] == [("", "?")]


def test_tool_config_mode_passthrough(captured):
    probe_tool.run_probe("x", config_mode=True, device="sa",
                         prompt_timeout=30.0, banner_wait=2.0)
    assert captured["config_mode"] is True
    assert captured["prompt_timeout"] == 30.0
    assert captured["banner_wait"] == 2.0
