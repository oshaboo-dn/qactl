"""Local cli backup store round-trip + command builders — no device, no dnftp.

The cli backup store landed on dnftp historically; #6 moved it to the local
filesystem (the device SFTPs the saved config to *this* host). These tests
exercise the local layout, the device-prefix safety filter, orphan
detection, and the upload/download command builders that target self.
"""

import os

import pytest

from dnctl.cli.core import backup_store as bs
from dnctl.core import local_sftp
from dnctl.core.dnftp import build_download_command, build_upload_command


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("DNCTL_STATE_DIR", str(tmp_path))
    return tmp_path


def _write(device, bucket, filename, data=b"running config text\n" * 8):
    """Mimic the device SFTP-uploading a saved config to the local path."""
    bs.ensure_dir(device=device, bucket=bucket)
    path = bs.remote_path(filename, device=device, bucket=bucket)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def test_roundtrip_write_stat_list_read_delete(state):
    filename = bs.make_filename("cl", "unit")
    path = _write("cl", "b1", filename)

    assert os.path.isfile(path)
    assert path.startswith(str(state))

    stat = bs.stat_backup(filename, device="cl", bucket="b1")
    assert stat is not None
    assert stat.device == "cl"
    assert stat.bucket == "b1"
    assert stat.size_bytes > 0
    assert stat.path == path

    listed = bs.list_backups(device="cl")
    assert any(b.filename == filename and b.bucket == "b1" for b in listed)
    assert "b1" in bs.list_buckets(device="cl")
    assert "cl" in bs.list_buckets()

    assert bs.download_bytes(filename, device="cl", bucket="b1").startswith(
        b"running config text"
    )

    assert bs.delete_backup(filename, device="cl", bucket="b1") is True
    assert bs.stat_backup(filename, device="cl", bucket="b1") is None
    assert bs.delete_backup(filename, device="cl", bucket="b1") is False


def test_root_bucket(state):
    filename = bs.make_filename("sa")
    _write("sa", None, filename)
    listed = bs.list_backups(device="sa")
    assert any(b.filename == filename and b.bucket is None for b in listed)


def test_device_prefix_filter_skips_misfiled(state):
    # A file whose in-name device prefix is 'sa' physically sitting under the
    # 'cl' device folder must not show up when listing 'cl'.
    misfiled = bs.make_filename("sa", "oops")
    _write("cl", None, misfiled)
    listed = bs.list_backups(device="cl")
    assert all(b.filename != misfiled for b in listed)


def test_orphans_surfaced(state):
    bs.ensure_dir(device="cl")
    # Non-canonical file directly under the device dir.
    with open(os.path.join(bs._device_dir("cl"), "junk.txt"), "wb") as fh:
        fh.write(b"x")
    orphans = bs.list_orphans()
    assert "cl/junk.txt" in orphans


def test_rejects_noncanonical_names(state):
    with pytest.raises(ValueError):
        bs.download_bytes("not-a-canonical-name", device="cl")
    with pytest.raises(ValueError):
        bs.delete_backup("../etc/passwd", device="cl")


def test_no_paramiko_or_dnftp_sftp(state):
    text = open(bs.__file__, encoding="utf-8").read()
    assert "import paramiko" not in text
    assert "dnftp_sftp" not in text


def test_build_commands_target_self_and_dnftp_default():
    # Local target: explicit user/host stitched into the remote URI.
    up = build_upload_command(
        kind="config", local_name="cl__20260101-000000.md",
        remote_path="/home/me/.local/state/dnctl/backups/cli/cl/cl__20260101-000000.md",
        vrf="mgmt0", user="me", host="myhost",
    )
    assert "me@myhost:/home/me/.local/state/dnctl/backups/cli/cl/" in up
    assert up.startswith("request file upload config ")
    assert up.endswith("protocol sftp vrf mgmt0")

    down = build_download_command(
        kind="config", local_name="cl__20260101-000000.md",
        remote_path="/p/cl__20260101-000000.md", user="me", host="myhost",
    )
    assert down.startswith("request file download me@myhost:/p/")

    # Default (no user/host) still points at dnftp for tech-support etc.
    ts = build_upload_command(
        kind="tech-support", local_name="t.tgz", remote_path="/p/t.tgz",
    )
    assert "dn@dnftp:/p/t.tgz" in ts


def test_require_password_gate(monkeypatch):
    monkeypatch.setattr(local_sftp, "LOCAL_SFTP_PASSWORD", None)
    with pytest.raises(local_sftp.LocalSftpNotConfigured):
        local_sftp.require_password()
    monkeypatch.setattr(local_sftp, "LOCAL_SFTP_PASSWORD", "s3cret")
    assert local_sftp.require_password() == "s3cret"
