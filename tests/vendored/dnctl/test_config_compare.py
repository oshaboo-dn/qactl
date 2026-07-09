"""``config --compare`` — candidate-vs-running diff preview (#58).

Stages statements, runs ``show config compare``, then ``rollback 0`` — the
running config is never touched, so no ``--yes`` gate. Pins the diff
extraction, the non-destructive command shape, and the rejected-statement
guard without touching a real device.
"""

from __future__ import annotations

from qactl.dnos.cli.core.session import Invocation, StepCapture
from qactl.dnos.cli.tools import edit


_DIFF = "+ protocols bgp neighbor 1.1.1.1 peer-as 65001"


def _inv(steps, *, hit_prompt=True):
    return Invocation(
        output="\n".join(s.output for s in steps),
        hit_prompt=hit_prompt,
        head_prompt_line="", tail_prompt="", host="h", device="cl",
        steps=steps,
    )


def _compare_steps(diff, *, stmt="protocols bgp neighbor 1.1.1.1 peer-as 65001"):
    return [
        StepCapture("configure", "", "", "", True),
        StepCapture(stmt, "", "", "", True),
        StepCapture("show config compare", "", diff, "", True),
        StepCapture("rollback 0", "", "", "", True),
    ]


def test_compare_returns_diff(monkeypatch):
    steps = _compare_steps(_DIFF)
    monkeypatch.setattr(edit, "drive_configure_commit", lambda *a, **k: _inv(steps))
    resp = edit.edit_config_compare(
        ["protocols bgp neighbor 1.1.1.1 peer-as 65001"], device="cl",
    )
    assert resp["status"] == "ok"
    assert resp["compare"] == _DIFF
    # The diff is also the printed/--log body.
    assert resp["stdout"] == _DIFF


def test_compare_command_stages_and_rolls_back(monkeypatch):
    captured = {}

    def fake_drive(*a, **k):
        captured["command"] = k["command"]
        captured["steps"] = [c for c, _ in k["steps"]]
        return _inv(_compare_steps(_DIFF))

    monkeypatch.setattr(edit, "drive_configure_commit", fake_drive)
    edit.edit_config_compare(
        ["protocols bgp neighbor 1.1.1.1 peer-as 65001"], device="cl",
    )
    # configure -> stmt -> top -> show config compare -> rollback 0; never
    # commits. The 'top' context reset guards against enter-level statements
    # poisoning the rest of the batch (issue #63).
    assert captured["steps"] == [
        "configure",
        "protocols bgp neighbor 1.1.1.1 peer-as 65001",
        "top",
        "show config compare",
        "rollback 0",
    ]
    assert "commit" not in captured["command"]


def test_compare_empty_diff_is_ok(monkeypatch):
    steps = _compare_steps("")
    monkeypatch.setattr(edit, "drive_configure_commit", lambda *a, **k: _inv(steps))
    resp = edit.edit_config_compare(["protocols bgp router-id 1.1.1.1"], device="cl")
    assert resp["status"] == "ok"
    assert resp["compare"] == ""


def test_compare_rejected_statement_is_error(monkeypatch):
    steps = [
        StepCapture("configure", "", "", "", True),
        StepCapture("protocols bgp bogus", "", "ERROR: Unknown word: 'bogus'.", "", True),
        StepCapture("show config compare", "", "", "", True),
        StepCapture("rollback 0", "", "", "", True),
    ]
    monkeypatch.setattr(edit, "drive_configure_commit", lambda *a, **k: _inv(steps))
    resp = edit.edit_config_compare(["protocols bgp bogus"], device="cl")
    assert resp["status"] == "error"
    assert any("rejected statement: protocols bgp bogus" in e for e in resp["errors"])


def test_compare_timeout_surfaces(monkeypatch):
    monkeypatch.setattr(
        edit, "drive_configure_commit",
        lambda *a, **k: _inv(_compare_steps(_DIFF), hit_prompt=False),
    )
    resp = edit.edit_config_compare(["protocols bgp router-id 1.1.1.1"], device="cl")
    assert resp["status"] == "timeout"


def test_compare_rejects_empty_statements():
    resp = edit.edit_config_compare([], device="cl")
    assert resp["status"] == "error"
