"""Bucket F — backup/restore polish (no device traffic)."""

from __future__ import annotations

from qactl.dnctl.cli.core import errors


def _fake_backupfile(device, size):
    from qactl.dnctl.cli.core.backup_store import BackupFile

    return BackupFile(
        filename=f"{device}_20260101-000000.cli",
        device=device,
        timestamp_utc="2026-01-01T00:00:00Z",
        description=None,
        bucket=None,
        size_bytes=size,
        path=f"/tmp/{device}.cli",
    )


def test_backup_next_actions_point_at_local_host():
    # The stale text told agents to debug dnftp; config backups go local.
    assert "DNCTL_LOCAL_SFTP" in errors.BACKUP_NEXT_ACTION
    assert "DNCTL_LOCAL_SFTP" in errors.RESTORE_NEXT_ACTION
    assert "THIS host" in errors.BACKUP_NEXT_ACTION


def test_read_backup_rejects_oversize(monkeypatch):
    from qactl.dnctl.cli.tools import backup

    monkeypatch.setattr(backup.backup_store, "validate_device", lambda d: None)
    monkeypatch.setattr(backup.backup_store, "validate_bucket", lambda b: None)
    big = backup._READ_BACKUP_MAX_BYTES + 1
    monkeypatch.setattr(
        backup.backup_store, "stat_backup",
        lambda *a, **k: _fake_backupfile("cl", big),
    )

    def _boom(*a, **k):
        raise AssertionError("download_bytes must not run for oversize files")

    monkeypatch.setattr(backup.backup_store, "download_bytes", _boom)
    resp = backup.read_backup("cl_20260101-000000.cli", "cl")
    assert resp["status"] == "error"
    assert any("cap" in e for e in resp["errors"])


def test_read_backup_reads_small(monkeypatch):
    from qactl.dnctl.cli.tools import backup

    monkeypatch.setattr(backup.backup_store, "validate_device", lambda d: None)
    monkeypatch.setattr(backup.backup_store, "validate_bucket", lambda b: None)
    monkeypatch.setattr(
        backup.backup_store, "stat_backup",
        lambda *a, **k: _fake_backupfile("cl", 100),
    )
    monkeypatch.setattr(
        backup.backup_store, "download_bytes",
        lambda *a, **k: b"system name cl\n",
    )
    resp = backup.read_backup("cl_20260101-000000.cli", "cl")
    assert resp["status"] == "ok"
    assert resp["content"] == "system name cl\n"
