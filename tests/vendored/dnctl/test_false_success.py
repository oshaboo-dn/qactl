"""Bucket A — "errors must surface" sweep.

Every tool below used to report ``status: ok`` (exit 0) on a real
failure: a 100%-loss ping, a missing ``/.gitcommit``, a missing log
file, a destructive commit that failed/timed out, or a restore whose
SFTP download/load died before commit. These tests pin the corrected
behaviour without touching a real device.
"""

from __future__ import annotations

import pytest

from dnctl.cli.core.session import Invocation, StepCapture
from dnctl.core import output


# --------------------------------------------------------------------------
# exit-code contract: warning is a zero exit
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,code",
    [
        ("ok", 0),
        ("warning", 0),
        ("error", 1),
        ("connect_error", 2),
        ("timeout", 3),
        ("weird", 1),
    ],
)
def test_exit_code_for_status(status, code):
    assert output.exit_code_for({"status": status}) == code


# --------------------------------------------------------------------------
# ping: 100% packet loss -> error
# --------------------------------------------------------------------------

def test_ping_total_loss_helper():
    from dnctl.cli.tools import ping

    assert ping._ping_total_loss("5 packets transmitted, 0 received, 100% packet loss")
    assert not ping._ping_total_loss("5 packets transmitted, 5 received, 0% packet loss")
    assert not ping._ping_total_loss("no summary here")


def test_ping_100pct_loss_becomes_error(monkeypatch):
    from dnctl.cli.tools import ping

    def fake_run(*a, **k):
        return {
            "status": "ok",
            "stdout": "5 packets transmitted, 0 received, 100% packet loss\n",
            "errors": [],
            "next_actions": [],
        }

    monkeypatch.setattr(ping, "_run_on_device", fake_run)
    resp = ping.run_ping_ipv4("10.0.0.1", device="cl")
    assert resp["status"] == "error"
    assert any("100% packet loss" in e for e in resp["errors"])


def test_ping_success_stays_ok(monkeypatch):
    from dnctl.cli.tools import ping

    monkeypatch.setattr(
        ping, "_run_on_device",
        lambda *a, **k: {
            "status": "ok",
            "stdout": "5 packets transmitted, 5 received, 0% packet loss\n",
            "errors": [], "next_actions": [],
        },
    )
    assert ping.run_ping_ipv4("10.0.0.1", device="cl")["status"] == "ok"


# --------------------------------------------------------------------------
# gitcommit: unreadable /.gitcommit -> error
# --------------------------------------------------------------------------

def test_gitcommit_missing_file_is_error(monkeypatch):
    from dnctl.cli.tools import gitcommit

    monkeypatch.setattr(
        gitcommit, "run_linux_on_device",
        lambda *a, **k: {
            "status": "ok",
            "stdout": "cat: /.gitcommit: No such file or directory",
            "errors": [], "next_actions": [],
        },
    )
    resp = gitcommit.get_gitcommit(device="cl")
    assert resp["status"] == "error"
    assert "commit_sha" not in resp


def test_gitcommit_parses_sha(monkeypatch):
    from dnctl.cli.tools import gitcommit

    monkeypatch.setattr(
        gitcommit, "run_linux_on_device",
        lambda *a, **k: {
            "status": "ok",
            "stdout": "b669275319207358e3a196c1dd0c7a5f4b67116b-PR-86107",
            "errors": [], "next_actions": [],
        },
    )
    resp = gitcommit.get_gitcommit(device="cl")
    assert resp["status"] == "ok"
    assert resp["commit_sha"] == "b669275319207358e3a196c1dd0c7a5f4b67116b"
    assert resp["pr_number"] == 86107


# --------------------------------------------------------------------------
# log_read: no candidate path -> error
# --------------------------------------------------------------------------

def test_log_read_preamble_emits_marker():
    from dnctl.cli.tools import log_read

    cmd, err = log_read._build_log_read(
        ("/a/x.log", "/b/x.log"), None, None, None, None, None, False,
    )
    assert err is None
    assert log_read._LOG_NOT_FOUND_MARKER in cmd


def test_log_read_not_found_is_error(monkeypatch):
    from dnctl.cli.tools import log_read

    monkeypatch.setattr(
        log_read, "run_linux_on_device",
        lambda *a, **k: {
            "status": "ok",
            "stdout": "ERR: log file not found; tried: /a, /b",
            "errors": [], "next_actions": [], "warnings": [],
        },
    )
    resp = log_read.get_accounting(device="cl")
    assert resp["status"] == "error"


# --------------------------------------------------------------------------
# edit: load_override / rollback must prove the commit landed
# --------------------------------------------------------------------------

def _inv(output_str, *, hit_prompt=True):
    return Invocation(
        output=output_str, hit_prompt=hit_prompt,
        head_prompt_line="", tail_prompt="", host="h", device="cl",
        steps=[],
    )


def test_load_override_failed_commit_is_error(monkeypatch):
    from dnctl.cli.tools import edit

    monkeypatch.setattr(
        edit, "drive_configure_commit",
        lambda *a, **k: _inv("% Error: commit failed: validation error"),
    )
    resp = edit.load_override_factory_default(device="cl")
    assert resp["status"] == "error"


def test_load_override_timeout_surfaces(monkeypatch):
    from dnctl.cli.tools import edit

    monkeypatch.setattr(
        edit, "drive_configure_commit",
        lambda *a, **k: _inv("", hit_prompt=False),
    )
    resp = edit.load_override_factory_default(device="cl")
    assert resp["status"] == "timeout"


def test_rollback_success_is_ok(monkeypatch):
    from dnctl.cli.tools import edit

    monkeypatch.setattr(
        edit, "drive_configure_commit",
        lambda *a, **k: _inv("Commit succeeded by dnroot at 21-Apr-2026 09:55:32 UTC"),
    )
    resp = edit.rollback_config(rollback_id=1, device="cl")
    assert resp["status"] == "ok"
    assert resp["commit"]["status"] == "ok"


def test_rollback_failed_commit_is_error(monkeypatch):
    from dnctl.cli.tools import edit

    monkeypatch.setattr(
        edit, "drive_configure_commit",
        lambda *a, **k: _inv("Commit failed: rollback id not found"),
    )
    resp = edit.rollback_config(rollback_id=9, device="cl")
    assert resp["status"] == "error"


# --------------------------------------------------------------------------
# restore: a failed download/load step is a hard error, even if commit "ok"
# --------------------------------------------------------------------------

def _fake_backupfile(device):
    from dnctl.cli.core.backup_store import BackupFile

    return BackupFile(
        filename=f"{device}_20260101-000000.cli",
        device=device,
        timestamp_utc="2026-01-01T00:00:00Z",
        description=None,
        bucket=None,
        size_bytes=1234,
        path=f"/tmp/{device}.cli",
    )


def _restore_env(monkeypatch, steps):
    from dnctl.cli.tools import backup

    monkeypatch.setattr(backup.backup_store, "validate_device", lambda d: None)
    monkeypatch.setattr(backup.backup_store, "validate_bucket", lambda b: None)
    monkeypatch.setattr(
        backup.backup_store, "stat_backup",
        lambda *a, **k: _fake_backupfile("cl"),
    )
    monkeypatch.setattr(backup, "require_password", lambda: "sftp-pw")

    inv = Invocation(
        output="\n".join(s.output for s in steps),
        hit_prompt=all(s.hit_prompt for s in steps),
        head_prompt_line="", tail_prompt="", host="h", device="cl",
        steps=steps,
    )
    monkeypatch.setattr(backup, "drive_configure_commit", lambda *a, **k: inv)
    return backup


def test_restore_failed_download_is_error(monkeypatch):
    steps = [
        StepCapture("set cli-no-confirm", "", "", "", True),
        StepCapture(
            "request file download config cl_x.cli protocol sftp ...",
            "", "% Error: Connection refused", "", True,
        ),
        StepCapture("configure", "", "", "", True),
        StepCapture("load override cl_x.cli", "", "", "", True),
        StepCapture("commit", "", "Commit succeeded by dnroot at X UTC", "", True),
    ]
    backup = _restore_env(monkeypatch, steps)
    resp = backup.restore_device("cl", "cl_20260101-000000.cli", confirm=True)
    assert resp["status"] == "error"
    assert any("download" in e for e in resp["errors"])


def test_restore_clean_path_is_ok(monkeypatch):
    steps = [
        StepCapture("set cli-no-confirm", "", "", "", True),
        StepCapture(
            "request file download config cl_x.cli protocol sftp ...",
            "", "", "", True,
        ),
        StepCapture("configure", "", "", "", True),
        StepCapture("load override cl_x.cli", "", "", "", True),
        StepCapture("commit", "", "Commit succeeded by dnroot at X UTC", "", True),
    ]
    backup = _restore_env(monkeypatch, steps)
    resp = backup.restore_device("cl", "cl_20260101-000000.cli", confirm=True)
    assert resp["status"] == "ok"
