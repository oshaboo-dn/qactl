"""All-or-nothing batch apply — abort BEFORE commit (issue #64).

A 7-statement ``qactl cli config`` batch on a live box got one statement
rejected mid-sequence (``ERROR: Unknown word: 'protocols'.`` from a
prompt-pacing race) yet still sent ``commit and-exit``, leaving a silent
partial config (4/7 applied) reported as success+error at once.

The fix: the deploy path hands ``run_sequence_pw`` a ``stop_predicate``
(:func:`stop_on_rejected_statement`) that cuts the sequence at the first
rejected statement, so the commit line is never sent. The tool then
reports ``commit.status == "aborted"`` (``status: error``), names the
rejected statement, and clears the shared candidate. No device required.
"""

from __future__ import annotations

from qactl.dnos.cli.core.edit_helpers import (
    batch_abort_errors,
    commit_was_attempted,
    stop_on_rejected_statement,
)
from qactl.dnos.cli.core.session import Invocation, StepCapture


_OK = ""
_REJECT = "ERROR: Unknown word: 'protocols'."
_COMMIT_OK = "Commit succeeded by dnroot at 02-Jul-2026 12:07:19 UTC+03:00"


def _step(cmd, out, *, hit=True):
    return StepCapture(cmd, "", out, "", hit)


# --------------------------------------------------------------------------
# stop_on_rejected_statement: the predicate
# --------------------------------------------------------------------------

def test_predicate_trips_on_rejected_statement():
    assert stop_on_rejected_statement(
        _step("protocols bgp 100001 neighbor 1.1.1.2 bfd admin-state enabled", _REJECT)
    )


def test_predicate_trips_on_scaffold_step_error():
    # A pacing race can land the rejection echo in the interleaved 'top'
    # reset's capture — that must abort too.
    assert stop_on_rejected_statement(_step("top", _REJECT))


def test_predicate_ignores_clean_steps_and_commit():
    assert not stop_on_rejected_statement(_step("configure", _OK))
    assert not stop_on_rejected_statement(_step("system name foo", _OK))
    # Commit-step errors belong to parse_commit_output, not the predicate.
    assert not stop_on_rejected_statement(
        _step("commit and-exit", "ERROR: something at commit time")
    )


# --------------------------------------------------------------------------
# commit_was_attempted / batch_abort_errors: the pure helpers
# --------------------------------------------------------------------------

def test_commit_was_attempted():
    reached = [_step("configure", _OK), _step("a b", _OK),
               _step("commit and-exit", _COMMIT_OK)]
    cut = [_step("configure", _OK), _step("a b", _REJECT)]
    assert commit_was_attempted(reached)
    assert not commit_was_attempted(cut)


def test_batch_abort_errors_names_statement_and_counts():
    steps = [
        _step("configure", _OK),
        _step("protocols bgp 100001 neighbor 1.1.1.2 remote-as 65002", _OK),
        _step("top", _OK),
        _step("protocols bgp 100001 neighbor 1.1.1.2 bfd admin-state enabled", _REJECT),
    ]
    errors = batch_abort_errors(steps, 7)
    assert any("aborted before commit" in e for e in errors)
    assert any("2 of 7" in e for e in errors)
    assert any("running config is unchanged" in e for e in errors)
    assert any(
        "rejected statement: protocols bgp 100001 neighbor 1.1.1.2 bfd "
        "admin-state enabled" in e
        for e in errors
    )
    assert any("Unknown word" in e for e in errors)


def test_batch_abort_errors_scaffold_fallback():
    # Rejection echo landed in the 'top' reset: still attributed loudly.
    steps = [
        _step("configure", _OK),
        _step("protocols bgp 100001 neighbor 1.1.1.2 remote-as 65002", _OK),
        _step("top", _REJECT),
    ]
    errors = batch_abort_errors(steps, 7)
    assert any("aborted before commit" in e for e in errors)
    assert any("error surfaced at step 'top'" in e for e in errors)
    assert any("Unknown word" in e for e in errors)


# --------------------------------------------------------------------------
# fake driver: replays run_sequence_pw's loop, honouring stop_predicate
# --------------------------------------------------------------------------

def _fake_drive(outputs, sent_log):
    """Stand-in for drive_configure_commit that emulates run_sequence_pw:
    send each step, record it in ``sent_log``, stop when the predicate
    trips. ``outputs`` maps a command to its canned capture body.
    """
    def drive(*args, **kwargs):
        predicate = kwargs.get("stop_predicate")
        executed = []
        for cmd, _pw in kwargs["steps"]:
            step = _step(cmd, outputs.get(cmd, _OK))
            executed.append(step)
            sent_log.append(cmd)
            if predicate is not None and predicate(step):
                break
        return Invocation(
            output=executed[-1].output, hit_prompt=True,
            head_prompt_line="", tail_prompt="", host="h", device="cl",
            steps=executed,
        )
    return drive


_BATCH = [
    "protocols bgp 100001 neighbor 1.1.1.2 remote-as 65002",
    "protocols bgp 100001 neighbor 1.1.1.2 local-as 65001",
    "protocols bgp 100001 neighbor 1.1.1.2 bfd admin-state enabled",
    "protocols bgp 100001 neighbor 1.1.1.2 bfd bfd-type single-hop",
]
_REJECTED_STMT = _BATCH[2]


# --------------------------------------------------------------------------
# edit_config: rejection mid-batch never reaches the commit line
# --------------------------------------------------------------------------

def test_edit_config_aborts_before_commit(monkeypatch):
    from qactl.dnos.cli.tools import edit

    sent = []
    cleanups = []
    monkeypatch.setattr(
        edit, "drive_configure_commit",
        _fake_drive({_REJECTED_STMT: _REJECT}, sent),
    )
    monkeypatch.setattr(
        edit, "abort_shared_candidate",
        lambda *a, **k: cleanups.append(a) or None,
    )

    resp = edit.edit_config(list(_BATCH), device="cl")

    # The commit line was never sent — that's the whole point.
    assert not any(c.startswith("commit") for c in sent)
    # Statements after the rejected one were not sent either.
    assert _BATCH[3] not in sent
    assert resp["status"] == "error"
    assert resp["commit"]["status"] == "aborted"
    assert any("aborted before commit" in e for e in resp["errors"])
    assert any(f"rejected statement: {_REJECTED_STMT}" in e for e in resp["errors"])
    # The pre-rejection statements were staged: candidate cleanup must run.
    assert len(cleanups) == 1
    assert any("candidate-abort cleanup" in w for w in resp["warnings"])


def test_edit_config_clean_batch_still_commits(monkeypatch):
    from qactl.dnos.cli.tools import edit

    sent = []
    monkeypatch.setattr(
        edit, "drive_configure_commit",
        _fake_drive({"commit and-exit": _COMMIT_OK}, sent),
    )

    resp = edit.edit_config(list(_BATCH), device="cl")
    assert "commit and-exit" in sent
    assert resp["status"] == "ok"
    assert resp["commit"]["status"] == "ok"
    assert not resp["errors"]


def test_edit_config_abort_honours_abort_on_failure_false(monkeypatch):
    from qactl.dnos.cli.tools import edit

    monkeypatch.setattr(
        edit, "drive_configure_commit",
        _fake_drive({_REJECTED_STMT: _REJECT}, []),
    )
    monkeypatch.setattr(
        edit, "abort_shared_candidate",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("cleanup must not run with abort_on_failure=False")
        ),
    )

    resp = edit.edit_config(list(_BATCH), device="cl", abort_on_failure=False)
    assert resp["status"] == "error"
    assert resp["commit"]["status"] == "aborted"


# --------------------------------------------------------------------------
# template/scale deploy path: same all-or-nothing guarantee
# --------------------------------------------------------------------------

def test_deploy_rendered_statements_aborts_before_commit(monkeypatch):
    from qactl.dnos.cli.tools import templates

    sent = []
    cleanups = []
    monkeypatch.setattr(
        templates, "drive_configure_commit",
        _fake_drive({_REJECTED_STMT: _REJECT}, sent),
    )
    monkeypatch.setattr(
        templates, "abort_shared_candidate",
        lambda *a, **k: cleanups.append(a) or None,
    )

    resp = templates._deploy_rendered_statements(
        tool_name="scale_deploy", statements=list(_BATCH),
        template_name="t", device="cl", host=None,
        user="u", password="p", timeout=5,
        log=None, deploy=True, abort_on_failure=True,
    )

    assert not any(c.startswith("commit") for c in sent)
    assert resp["status"] == "error"
    assert resp["commit"]["status"] == "aborted"
    assert any("aborted before commit" in e for e in resp["errors"])
    assert len(cleanups) == 1


def test_deploy_rendered_statements_dry_run_reports_all_rejections(monkeypatch):
    # deploy=False must NOT stop early: the dry-run ends in 'rollback 0'
    # and never applies, so it keeps going to surface every rejection.
    from qactl.dnos.cli.tools import templates

    sent = []
    check_ok = "Commit check passed successfully"
    monkeypatch.setattr(
        templates, "drive_configure_commit",
        _fake_drive(
            {_REJECTED_STMT: _REJECT, "commit check no-warning": check_ok},
            sent,
        ),
    )

    templates._deploy_rendered_statements(
        tool_name="scale_deploy", statements=list(_BATCH),
        template_name="t", device="cl", host=None,
        user="u", password="p", timeout=5,
        log=None, deploy=False, abort_on_failure=True,
    )
    # Whole sequence ran, including the trailing candidate cleanup.
    assert "commit check no-warning" in sent
    assert "rollback 0" in sent
