"""Unit tests for the capture_devices orchestrator (mocked device I/O)."""

import os

import pytest

from qactl.dnos.cli.tools import capture as cap
from qactl.dnos.core.local_sftp import LocalSftpNotConfigured


@pytest.fixture
def _pw(monkeypatch):
    """Pretend the local-sftp password is configured."""
    monkeypatch.setattr(cap, "require_password", lambda: "localpw")


@pytest.fixture
def _one(monkeypatch):
    """Capture _capture_one calls and return a scripted status per target."""
    calls = []

    def _fake(target, *, is_host, mode, duration_s, name, bpf, iface, ncp,
              user, password, timeout, local_pw):
        calls.append({"target": target, "is_host": is_host, "mode": mode,
                      "duration_s": duration_s, "iface": iface})
        status = "error" if target == "boom" else "ok"
        sub = {"device": target, "status": status}
        if status == "ok":
            sub.update(pcap_path=f"/state/{target}.pcap", bytes=40960)
        else:
            sub.update(errors=["capture failed"])
        return sub

    monkeypatch.setattr(cap, "_capture_one", _fake)
    return calls


# --- argument validation (no device contact) -------------------------------


def test_bad_mode():
    r = cap.capture_devices(devices=["cl"], mode="bogus")
    assert r["status"] == "error"
    assert "mode must be" in r["errors"][0]


def test_bad_name():
    r = cap.capture_devices(devices=["cl"], name="bad name")
    assert r["status"] == "error"


def test_bad_duration():
    r = cap.capture_devices(devices=["cl"], duration="-5")
    assert r["status"] == "error"


def test_no_target():
    r = cap.capture_devices(devices=[], mode="routing")
    assert r["status"] == "error"
    assert "no capture target" in r["errors"][0]


def test_local_sftp_unconfigured(monkeypatch):
    def _raise():
        raise LocalSftpNotConfigured("no password")
    monkeypatch.setattr(cap, "require_password", _raise)
    r = cap.capture_devices(devices=["cl"], mode="routing")
    assert r["status"] == "error"
    assert "no password" in r["errors"][0]


# --- orchestration ---------------------------------------------------------


def test_single_device_ok(_pw, _one):
    r = cap.capture_devices(devices=["dev1"], mode="routing", duration="20")
    assert r["status"] == "ok"
    assert r["capture_count"] == 1
    assert r["failed_count"] == 0
    assert r["captures"][0]["device"] == "dev1"
    assert _one[0]["duration_s"] == 20


def test_multi_device_partial_failure(_pw, _one):
    r = cap.capture_devices(devices=["dev1", "boom"], mode="routing")
    assert r["status"] == "error"           # any failure fails the whole op
    assert r["capture_count"] == 1
    assert r["failed_count"] == 1
    targets = {c["device"] for c in r["captures"]}
    assert targets == {"dev1", "boom"}


def test_host_target_flag(_pw, _one):
    cap.capture_devices(host="10.0.0.9", mode="routing")
    assert _one[0]["is_host"] is True
    assert _one[0]["target"] == "10.0.0.9"


def test_infinite_duration_clamped_and_warned(_pw, _one, monkeypatch):
    monkeypatch.setenv("QACTL_CAPTURE_MAX_DURATION_S", "600")
    r = cap.capture_devices(devices=["dev1"], mode="routing", duration="inf")
    assert r["status"] == "warning"
    assert r["duration_s"] == 600
    assert any("clamped" in w for w in r["warnings"])
    # routing gets the clamped concrete duration, not None
    assert _one[0]["duration_s"] == 600


def test_datapath_infinite_passes_none_to_driver(_pw, _one):
    cap.capture_devices(devices=["dev1"], mode="datapath", duration="0")
    assert _one[0]["duration_s"] is None


# --- local BPF filter ------------------------------------------------------


def test_apply_local_filter_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(cap.shutil, "which", lambda _n: "/usr/bin/tcpdump")
    src = tmp_path / "x.pcap"
    src.write_bytes(b"\xd4\xc3\xb2\xa1rawpcap")
    seen = {}

    def _fake_run(argv, *a, **k):
        seen["argv"] = list(argv)
        # tcpdump -r <in> -w <out> <bpf>: emulate the rewrite by creating -w
        with open(argv[argv.index("-w") + 1], "wb") as fh:
            fh.write(b"filtered")

        class _Proc:
            returncode = 0
            stderr = ""

        return _Proc()

    monkeypatch.setattr(cap.subprocess, "run", _fake_run)
    dst, warn = cap._apply_local_filter(str(src), "tcp port 179")
    assert warn is None
    assert dst.endswith("x_filtered.pcap")
    assert os.path.exists(dst)  # tmp output was moved into the captures dir

    # AppArmor fix: tcpdump must be handed a /tmp staging path, never the
    # ~/.local/state dot-dir pcap it would be denied from reading/writing.
    rpath = seen["argv"][seen["argv"].index("-r") + 1]
    wpath = seen["argv"][seen["argv"].index("-w") + 1]
    assert rpath != str(src)
    assert wpath != dst
    assert "qactl-bpf-" in rpath and "qactl-bpf-" in wpath


def test_apply_local_filter_no_tcpdump(monkeypatch):
    monkeypatch.setattr(cap.shutil, "which", lambda _n: None)
    dst, warn = cap._apply_local_filter("/x.pcap", "tcp")
    assert dst is None
    assert "tcpdump not found" in warn


def test_apply_local_filter_tcpdump_error(monkeypatch, tmp_path):
    monkeypatch.setattr(cap.shutil, "which", lambda _n: "/usr/bin/tcpdump")
    src = tmp_path / "x.pcap"
    src.write_bytes(b"\xd4\xc3\xb2\xa1rawpcap")

    class _Proc:
        returncode = 1
        stderr = "tcpdump: syntax error"

    monkeypatch.setattr(cap.subprocess, "run", lambda *a, **k: _Proc())
    dst, warn = cap._apply_local_filter(str(src), "bad bpf")
    assert dst is None
    assert "syntax error" in warn
