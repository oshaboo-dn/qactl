"""Local NETCONF backup store round-trip — no device, no dnftp."""

import os

import pytest

from qactl.dnctl.nc.core import backup_store as bs


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("DNCTL_STATE_DIR", str(tmp_path))
    return tmp_path


def _payload():
    sections = [
        ("system", "<system><hostname>cl</hostname></system>" * 4),
        ("interfaces", "<interfaces><if>ge0</if></interfaces>" * 4),
    ]
    return sections


def test_roundtrip_pack_write_list_read_delete(state, tmp_path):
    filename = bs.make_filename("cl", "unit")
    basename = filename[: -len(".tar.gz")]
    tarball = bs.pack_sections(_payload(), arc_root=basename)

    written = bs.upload_bytes(tarball, filename, bucket="b1")
    assert written.device == "cl"
    assert written.bucket == "b1"
    assert os.path.isfile(written.path)
    assert written.path.startswith(str(state))
    assert written.size_bytes >= bs.MIN_BYTES

    assert bs.stat_backup(filename, bucket="b1") is not None
    listed = bs.list_backups(device="cl")
    assert any(b.filename == filename and b.bucket == "b1" for b in listed)
    assert "b1" in bs.list_buckets()

    dest = tmp_path / "dl" / filename
    bs.download_to_path(filename, str(dest), bucket="b1")
    out_dir = tmp_path / "ex"
    files = bs.extract_to_dir(str(dest), str(out_dir), expected_arc_root=basename)
    names = {os.path.basename(f) for f in files}
    assert names == {"system.xml", "interfaces.xml"}

    assert bs.delete_backup(filename, bucket="b1") is True
    assert bs.stat_backup(filename, bucket="b1") is None
    assert bs.delete_backup(filename, bucket="b1") is False


def test_rejects_noncanonical_names(state):
    with pytest.raises(ValueError):
        bs.upload_bytes(b"x" * 200, "not-a-canonical-name", bucket=None)
    with pytest.raises(ValueError):
        bs.delete_backup("../etc/passwd")


def test_no_dnftp_import(state):
    src = bs.__file__
    text = open(src, encoding="utf-8").read()
    assert "import paramiko" not in text
    assert "dnftp_sftp" not in text
