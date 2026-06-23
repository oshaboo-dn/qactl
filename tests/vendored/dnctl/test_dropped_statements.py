"""Silent statement drops in a multi-statement commit (issue #47).

DNOS commits whatever parsed and reports ``Commit succeeded`` even when
individual statements were rejected mid-batch — typically a top-level
``interfaces ...`` / ``network-services ...`` create parsed inside a stale
context left by a preceding ``no ...`` delete, yielding
``ERROR: Unknown word: 'interfaces'.``. Those per-statement errors live in
the rejected statement's own step output, invisible to
``parse_commit_output`` (which only reads the commit step), so the old code
returned ``status: ok`` while the running config was partial.

These tests pin the corrected behaviour: edit_config / edit_config_check
fail (non-zero) and name each rejected statement. No device required.
"""

from __future__ import annotations

from dnctl.cli.core.edit_helpers import detect_rejected_statements
from dnctl.cli.core.session import Invocation, StepCapture


# Two qinq legs after a `no ...` delete: leg A parses, leg B is rejected
# because the delete left a stale parse context (the issue's repro shape).
_OK = ""
_REJECT = "ERROR: Unknown word: 'interfaces'."
_COMMIT_OK = "Commit succeeded by dnroot at 21-Apr-2026 09:55:32 UTC"


def _step(cmd, out, *, hit=True):
    return StepCapture(cmd, "", out, "", hit)


def _inv(steps, *, last_output, hit_prompt=True):
    return Invocation(
        output=last_output, hit_prompt=hit_prompt,
        head_prompt_line="", tail_prompt="", host="h", device="cl",
        steps=steps,
    )


# --------------------------------------------------------------------------
# detect_rejected_statements: the pure helper
# --------------------------------------------------------------------------

def test_detect_skips_scaffolding_and_clean_statements():
    steps = [
        _step("configure", _OK),
        _step("no interfaces ge100-0/0/31.276", _OK),
        _step("interfaces ge100-0/0/31.277 l2-service enabled", _OK),
        _step("commit and-exit", _COMMIT_OK),
    ]
    assert detect_rejected_statements(steps) == []


def test_detect_flags_rejected_statement():
    steps = [
        _step("configure", _OK),
        _step("no interfaces ge100-0/0/31.276", _OK),
        _step("interfaces ge100-0/0/31.277 l2-service enabled", _OK),
        _step("interfaces ge100-0/0/31.278 l2-service enabled", _REJECT),
        _step("commit and-exit", _COMMIT_OK),
    ]
    rejected = detect_rejected_statements(steps)
    assert len(rejected) == 1
    stmt, lines = rejected[0]
    assert stmt == "interfaces ge100-0/0/31.278 l2-service enabled"
    assert any("Unknown word" in ln for ln in lines)


def test_detect_ignores_error_in_commit_step():
    # An error-looking line in the commit step must not be mistaken for a
    # rejected statement (commit parsing owns that signal).
    steps = [
        _step("configure", _OK),
        _step("system name foo", _OK),
        _step("commit and-exit", _COMMIT_OK + "\nERROR: noise"),
    ]
    assert detect_rejected_statements(steps) == []


# --------------------------------------------------------------------------
# edit_config: a silently-dropped statement is now a hard error
# --------------------------------------------------------------------------

def test_edit_config_dropped_statement_is_error(monkeypatch):
    from dnctl.cli.tools import edit

    steps = [
        _step("configure", _OK),
        _step("no interfaces bundle-60000.277", _OK),
        _step("interfaces ge100-0/0/31.277 l2-service enabled", _OK),
        _step("interfaces ge100-0/0/31.278 l2-service enabled", _REJECT),
        _step("commit and-exit", _COMMIT_OK),
    ]
    inv = _inv(steps, last_output=_COMMIT_OK)
    monkeypatch.setattr(edit, "drive_configure_commit", lambda *a, **k: inv)
    # Must NOT run candidate cleanup — the commit already applied (partially).
    monkeypatch.setattr(
        edit, "abort_shared_candidate",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("cleanup must not run on a partial-apply error")
        ),
    )

    resp = edit.edit_config(
        [
            "no interfaces bundle-60000.277",
            "interfaces ge100-0/0/31.277 l2-service enabled",
            "interfaces ge100-0/0/31.278 l2-service enabled",
        ],
        device="cl",
    )
    assert resp["status"] == "error"
    # commit verdict itself stays "ok" — the failure is the dropped statement.
    assert resp["commit"]["status"] == "ok"
    assert any("silently dropped" in e for e in resp["errors"])
    assert any(
        "rejected statement: interfaces ge100-0/0/31.278" in e
        for e in resp["errors"]
    )


def test_edit_config_all_clean_stays_ok(monkeypatch):
    from dnctl.cli.tools import edit

    steps = [
        _step("configure", _OK),
        _step("system name foo", _OK),
        _step("commit and-exit", _COMMIT_OK),
    ]
    inv = _inv(steps, last_output=_COMMIT_OK)
    monkeypatch.setattr(edit, "drive_configure_commit", lambda *a, **k: inv)

    resp = edit.edit_config(["system name foo"], device="cl")
    assert resp["status"] == "ok"
    assert resp["commit"]["status"] == "ok"
    assert not resp["errors"]


# --------------------------------------------------------------------------
# edit_config_check: the dry-run surfaces the same rejection
# --------------------------------------------------------------------------

def test_edit_config_check_dropped_statement_is_error(monkeypatch):
    from dnctl.cli.tools import edit

    check_ok = "Commit check passed successfully"
    steps = [
        _step("configure", _OK),
        _step("no interfaces bundle-60000.277", _OK),
        _step("interfaces ge100-0/0/31.278 l2-service enabled", _REJECT),
        _step("commit check no-warning", check_ok),
        _step("rollback 0", _OK),
    ]
    # edit_config_check uses capture_all=True: output is the concatenation.
    inv = _inv(steps, last_output="\n".join(s.output for s in steps))
    monkeypatch.setattr(edit, "drive_configure_commit", lambda *a, **k: inv)

    resp = edit.edit_config_check(
        [
            "no interfaces bundle-60000.277",
            "interfaces ge100-0/0/31.278 l2-service enabled",
        ],
        device="cl",
    )
    assert resp["status"] == "error"
    assert resp["commit"]["status"] == "check_ok"
    assert any("silently dropped" in e for e in resp["errors"])
