"""Enter-level statements poisoning the rest of a batch (issue #63).

A configure-mode statement that ends at a bare enter-level node (e.g.
``protocols bgp 100001 neighbor-group SG-STRICT address-family
ipv4-unicast``) drops the CLI session into that sub-mode
(``(cfg-bgp-group-afi)#``). Every following absolute-path statement in the
same channel then misparses (``ERROR: Unknown word: 'protocols'.``) and is
silently dropped, while ``commit and-exit`` still reports success for the
partial candidate.

The fix interleaves a ``top`` context reset after every statement — ``top``
returns to the config root and is a silent no-op when already there. These
tests pin the interleave in every statement-pushing path and that the
``top`` scaffolding is never itself validated as a user statement. No
device required.
"""

from __future__ import annotations

from qactl.dnos.cli.core.edit_helpers import (
    CONTEXT_RESET,
    build_edit_config_commands,
    detect_rejected_statements,
)
from qactl.dnos.cli.core.session import Invocation, StepCapture


_STMTS = [
    "protocols bgp 100001 neighbor-group SG-STRICT bfd strict-mode admin-state enabled",
    "protocols bgp 100001 neighbor-group SG-STRICT address-family ipv4-unicast",
    "protocols bgp 100001 neighbor-group SG-STRICT address-family ipv4-multicast",
    "protocols bgp 100001 neighbor-group SG-STRICT neighbor 123.5.5.5",
]


def _step(cmd, out, *, hit=True):
    return StepCapture(cmd, "", out, "", hit)


def _inv(steps, *, last_output, hit_prompt=True):
    return Invocation(
        output=last_output, hit_prompt=hit_prompt,
        head_prompt_line="", tail_prompt="", host="h", device="cl",
        steps=steps,
    )


# --------------------------------------------------------------------------
# build_edit_config_commands: every statement is followed by a reset
# --------------------------------------------------------------------------

def test_deploy_steps_interleave_top_after_every_statement():
    steps, commit_line, joined = build_edit_config_commands(
        list(_STMTS), None, deploy=True,
    )
    cmds = [cmd for cmd, _ in steps]
    expected = ["configure"]
    for s in _STMTS:
        expected += [s, CONTEXT_RESET]
    expected += ["commit and-exit"]
    assert cmds == expected
    assert commit_line == "commit and-exit"
    assert joined == " ; ".join(expected)


def test_check_steps_interleave_top_after_every_statement():
    steps, _commit_line, _joined = build_edit_config_commands(
        list(_STMTS), None, deploy=False,
    )
    cmds = [cmd for cmd, _ in steps]
    # Last statement is also followed by a reset, before commit check.
    assert cmds[-3:] == [CONTEXT_RESET, "commit check no-warning", "rollback 0"]
    for i, cmd in enumerate(cmds):
        if cmd in _STMTS:
            assert cmds[i + 1] == CONTEXT_RESET


# --------------------------------------------------------------------------
# detect_rejected_statements: 'top' is scaffolding, never a user statement
# --------------------------------------------------------------------------

def test_detect_skips_top_scaffolding():
    # Even if a 'top' step somehow carried error-looking output, it must not
    # be reported as a rejected user statement.
    steps = [
        _step("configure", ""),
        _step(_STMTS[0], ""),
        _step(CONTEXT_RESET, "ERROR: noise"),
        _step("commit and-exit", "Commit succeeded by dnroot at ..."),
    ]
    assert detect_rejected_statements(steps) == []


# --------------------------------------------------------------------------
# edit_config / edit_config_compare: the wire sequence carries the resets
# --------------------------------------------------------------------------

def _capture_steps(monkeypatch, module):
    captured = {}

    def fake_drive(*_a, **kw):
        captured["steps"] = list(kw["steps"])
        cmds = [cmd for cmd, _ in captured["steps"]]
        return _inv(
            [_step(c, "") for c in cmds],
            last_output="Commit succeeded by dnroot at ...",
        )

    monkeypatch.setattr(module, "drive_configure_commit", fake_drive)
    return captured


def test_edit_config_sends_top_between_statements(monkeypatch):
    from qactl.dnos.cli.tools import edit

    captured = _capture_steps(monkeypatch, edit)
    resp = edit.edit_config(list(_STMTS), device="cl")
    assert resp["status"] == "ok"
    cmds = [cmd for cmd, _ in captured["steps"]]
    # The enter-level statement is immediately followed by a reset, so the
    # next absolute-path statement parses from the config root.
    i = cmds.index(_STMTS[1])
    assert cmds[i + 1] == CONTEXT_RESET
    assert cmds[i + 2] == _STMTS[2]


def test_edit_config_compare_sends_top_between_statements(monkeypatch):
    from qactl.dnos.cli.tools import edit

    captured = _capture_steps(monkeypatch, edit)
    resp = edit.edit_config_compare(list(_STMTS), device="cl")
    assert resp["status"] == "ok"
    cmds = [cmd for cmd, _ in captured["steps"]]
    i = cmds.index(_STMTS[1])
    assert cmds[i + 1] == CONTEXT_RESET
    # Diff + candidate drop still close the sequence.
    assert cmds[-2:] == ["show config compare", "rollback 0"]
