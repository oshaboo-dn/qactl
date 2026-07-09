"""Multi-show batching — several full `show` commands on ONE channel.

`qactl cli show "show a b" "show c d"` (and the show-config analog) must
route to `show_many` / `show_config_many`, which validate every command
up front and run the batch via `_run_raw_on_device` with
``stop_on_error=False``. Word-form and single-command calls must keep
the legacy join-the-words single-channel path. No real device — the
runner boundary (`run_once` / `run_sequence`) is faked throughout.
"""

import json

import pytest
from typer.testing import CliRunner

from qactl.dnos.__main__ import app
from qactl.dnos.cli.app import _batch_show_args
from qactl.dnos.cli.core import runner as core_runner
from qactl.dnos.cli.core.session import Invocation, StepCapture
from qactl.dnos.cli.tools import discovery

runner = CliRunner()


# --- _batch_show_args form detection ---------------------------------------

@pytest.mark.parametrize(
    "argv,expected",
    [
        # ≥2 args, each a full multi-word show command → batch.
        (["show bgp summary", "show route summary"],
         ["show bgp summary", "show route summary"]),
        (["show config protocols bgp", "show config interfaces"],
         ["show config protocols bgp", "show config interfaces"]),
        # Word form (legacy) → not a batch.
        (["show", "bgp", "summary"], None),
        # Single quoted command → not a batch.
        (["show bgp summary"], None),
        # Mixed: one arg is not a full show command → not a batch (joined).
        (["show bgp", "neighbor 1.2.3.4"], None),
        # Bare 'show' can't be a full command.
        (["show", "show bgp summary"], None),
        ([], None),
    ],
)
def test_batch_show_args_detection(argv, expected):
    assert _batch_show_args(argv) == expected


# --- fakes at the runner boundary ------------------------------------------

@pytest.fixture
def fake_boundary(monkeypatch):
    """Capture run_once / run_sequence calls; silence logging."""
    calls = {"once": [], "sequence": []}

    def _fake_run_once(registry, **kwargs):
        calls["once"].append(kwargs)
        return Invocation(
            output="ok\n", hit_prompt=True, head_prompt_line="",
            tail_prompt="", host="10.0.0.1", device=kwargs.get("device"),
            steps=[StepCapture(kwargs["command"], "", "ok\n", "", True)],
        )

    def _fake_run_sequence(registry, **kwargs):
        calls["sequence"].append(kwargs)
        steps = [
            StepCapture(cmd, "", f"out of {cmd}\n", "", True)
            for cmd in kwargs["commands"]
        ]
        return Invocation(
            output=steps[-1].output, hit_prompt=True, head_prompt_line="",
            tail_prompt="", host="10.0.0.1", device=kwargs.get("device"),
            steps=steps,
        )

    monkeypatch.setattr(core_runner, "run_once", _fake_run_once)
    monkeypatch.setattr(core_runner, "run_sequence", _fake_run_sequence)
    monkeypatch.setattr(core_runner, "log_invocation", lambda *a, **k: None)
    monkeypatch.setattr(core_runner, "log_request", lambda *a, **k: None)
    return calls


# --- CLI routing ------------------------------------------------------------

def test_cli_show_batch_runs_one_sequence(fake_boundary):
    r = runner.invoke(app, [
        "cli", "show", "--host", "10.0.0.1",
        "show bgp summary", "show route summary", "--json",
    ])
    assert r.exit_code == 0, r.output
    assert len(fake_boundary["sequence"]) == 1
    assert fake_boundary["sequence"][0]["commands"] == [
        "show bgp summary", "show route summary",
    ]
    # independent reads: no stop-on-first-error predicate
    assert fake_boundary["sequence"][0]["stop_predicate"] is None
    payload = json.loads(r.output)
    assert [s["command"] for s in payload["steps"]] == [
        "show bgp summary", "show route summary",
    ]
    assert "out of show bgp summary" in payload["stdout"]
    assert "out of show route summary" in payload["stdout"]


def test_cli_show_word_form_stays_single(fake_boundary):
    r = runner.invoke(app, [
        "cli", "show", "--host", "10.0.0.1", "show", "bgp", "summary", "--json",
    ])
    assert r.exit_code == 0, r.output
    assert fake_boundary["sequence"] == []
    assert len(fake_boundary["once"]) == 1
    assert fake_boundary["once"][0]["command"] == "show bgp summary"


def test_cli_show_single_quoted_stays_single(fake_boundary):
    r = runner.invoke(app, [
        "cli", "show", "--host", "10.0.0.1", "show bgp summary", "--json",
    ])
    assert r.exit_code == 0, r.output
    assert fake_boundary["sequence"] == []
    assert fake_boundary["once"][0]["command"] == "show bgp summary"


def test_cli_show_config_batch_runs_one_sequence(fake_boundary):
    r = runner.invoke(app, [
        "cli", "show-config", "--host", "10.0.0.1",
        "show config protocols bgp", "show config interfaces", "--json",
    ])
    assert r.exit_code == 0, r.output
    assert fake_boundary["sequence"][0]["commands"] == [
        "show config protocols bgp", "show config interfaces",
    ]


# --- tool-level validation ---------------------------------------------------

def test_show_many_rejects_config_command_before_device(fake_boundary):
    resp = discovery.show_many(
        ["show bgp summary", "show config protocols bgp"], host="10.0.0.1",
    )
    assert resp["status"] == "error"
    assert fake_boundary["sequence"] == []  # never touched the channel
    assert "show-config" in resp["errors"][0]


def test_show_config_many_rejects_operational_command(fake_boundary):
    resp = discovery.show_config_many(
        ["show config interfaces", "show bgp summary"], host="10.0.0.1",
    )
    assert resp["status"] == "error"
    assert fake_boundary["sequence"] == []


def test_show_many_rejects_empty_list(fake_boundary):
    resp = discovery.show_many([], host="10.0.0.1")
    assert resp["status"] == "error"
    assert fake_boundary["sequence"] == []


def test_show_many_surfaces_mid_batch_device_error(monkeypatch):
    # A failing command mid-batch must not be masked by a clean last step —
    # and the later command still runs (stop_on_error=False).
    def _fake_run_sequence(registry, **kwargs):
        steps = [
            StepCapture("show bgp summary", "", "2/2 established\n", "", True),
            StepCapture("show bogus thing", "", "% Unknown command\n", "", True),
            StepCapture("show route summary", "", "42 routes\n", "", True),
        ]
        return Invocation(
            output=steps[-1].output, hit_prompt=True, head_prompt_line="",
            tail_prompt="", host="10.0.0.1", device=None, steps=steps,
        )

    monkeypatch.setattr(core_runner, "run_sequence", _fake_run_sequence)
    monkeypatch.setattr(core_runner, "log_invocation", lambda *a, **k: None)
    monkeypatch.setattr(core_runner, "log_request", lambda *a, **k: None)

    resp = discovery.show_many(
        ["show bgp summary", "show bogus thing", "show route summary"],
        host="10.0.0.1",
    )
    assert resp["status"] == "error"
    assert any("Unknown command" in e for e in resp["errors"])
    assert len(resp["steps"]) == 3
    assert "42 routes" in resp["stdout"]
