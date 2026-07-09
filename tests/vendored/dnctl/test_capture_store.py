"""Unit tests for the capture pcap landing store."""

import os

import pytest

from qactl.dnctl.cli.core import capture_store


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DNCTL_STATE_DIR", str(tmp_path))
    return tmp_path


def test_validate_device():
    assert capture_store.validate_device("cl") is None
    assert capture_store.validate_device("bad/name") is not None


def test_validate_name():
    assert capture_store.validate_name("cap_cl_20260709_134501.pcap") is None
    assert capture_store.validate_name("notpcap.txt") is not None
    assert capture_store.validate_name("../evil.pcap") is not None
    assert capture_store.validate_name("x.pcap.pcap") is None  # still ends .pcap


def test_remote_path_and_ensure_dir(tmp_path):
    capture_store.ensure_dir(device="cl")
    p = capture_store.remote_path("cap_cl_20260709_134501.pcap", device="cl")
    assert p.endswith("captures/cli/cl/cap_cl_20260709_134501.pcap")
    assert os.path.isdir(os.path.dirname(p))


def test_stat_pcap_roundtrip():
    capture_store.ensure_dir(device="cl")
    name = "cap_cl_20260709_134501.pcap"
    path = capture_store.remote_path(name, device="cl")
    with open(path, "wb") as fh:
        fh.write(b"\xd4\xc3\xb2\xa1" + b"\x00" * 100)
    stat = capture_store.stat_pcap(name, device="cl")
    assert stat is not None
    assert stat.device == "cl"
    assert stat.size_bytes == 104
    assert stat.path == path


def test_stat_pcap_absent():
    assert capture_store.stat_pcap("cap_cl_1.pcap", device="cl") is None


def test_ensure_dir_bad_device():
    with pytest.raises(ValueError):
        capture_store.ensure_dir(device="bad/name")
