"""Always-on per-device daily journal.

Every device command tees its full raw output to
``<QACTL_DEVICE_LOG_DIR>/<device>/<YYYY-MM-DD>.md`` without anyone passing
``--log``. It is keyed by device, accumulates across calls, records the
status in the header, only fires when a device can be identified, and
never lets a write failure break the command.
"""

from datetime import datetime, timezone

import pytest
import typer

from dnctl.core import options as O
from dnctl.core.context import Ctx


def _journal_root(tmp_path, monkeypatch):
    root = tmp_path / "device-logs"
    monkeypatch.setenv("QACTL_DEVICE_LOG_DIR", str(root))
    return root


def _today_file(root, device):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return root / device / f"{day}.md"


def test_journal_written_keyed_by_device(tmp_path, monkeypatch):
    root = _journal_root(tmp_path, monkeypatch)
    O._append_journal(
        {"status": "ok", "device": "cl", "command": "show bgp summary", "stdout": "raw\n"},
        Ctx(device="cl"),
    )
    f = _today_file(root, "cl")
    text = f.read_text()
    assert "device=cl" in text
    assert "cmd='show bgp summary'" in text
    assert "status=ok" in text
    assert "```\nraw\n```\n" in text


def test_journal_accumulates_across_calls(tmp_path, monkeypatch):
    root = _journal_root(tmp_path, monkeypatch)
    O._append_journal({"status": "ok", "device": "cl", "command": "show a", "stdout": "AAA\n"}, Ctx(device="cl"))
    O._append_journal({"status": "ok", "device": "cl", "command": "show b", "stdout": "BBB\n"}, Ctx(device="cl"))
    text = _today_file(root, "cl").read_text()
    assert "AAA" in text and "BBB" in text
    assert text.count("# =====") == 2


def test_journal_separates_devices(tmp_path, monkeypatch):
    root = _journal_root(tmp_path, monkeypatch)
    O._append_journal({"status": "ok", "device": "cl", "command": "show", "stdout": "C\n"}, Ctx(device="cl"))
    O._append_journal({"status": "ok", "device": "sa", "command": "show", "stdout": "S\n"}, Ctx(device="sa"))
    assert "C" in _today_file(root, "cl").read_text()
    assert "S" in _today_file(root, "sa").read_text()


def test_journal_falls_back_to_host(tmp_path, monkeypatch):
    root = _journal_root(tmp_path, monkeypatch)
    O._append_journal({"status": "ok", "host": "10.0.0.5", "command": "show", "stdout": "x\n"}, Ctx(host="10.0.0.5"))
    assert _today_file(root, "10.0.0.5").exists()


def test_journal_records_error_status(tmp_path, monkeypatch):
    root = _journal_root(tmp_path, monkeypatch)
    O._append_journal({"status": "error", "device": "cl", "command": "show", "errors": ["boom"]}, Ctx(device="cl"))
    assert "status=error" in _today_file(root, "cl").read_text()


def test_journal_skipped_without_device(tmp_path, monkeypatch):
    root = _journal_root(tmp_path, monkeypatch)
    O._append_journal({"status": "ok", "command": "registry list"}, Ctx())
    assert not root.exists()


def test_journal_sanitizes_device_name(tmp_path, monkeypatch):
    root = _journal_root(tmp_path, monkeypatch)
    O._append_journal({"status": "ok", "device": "a/b c", "command": "show", "stdout": "x\n"}, Ctx(device="a/b c"))
    assert (root / "a_b_c").is_dir()


def test_journal_write_failure_never_raises(tmp_path, monkeypatch):
    # Point the root at a file so mkdir fails; the call must still return.
    clash = tmp_path / "afile"
    clash.write_text("x")
    monkeypatch.setenv("QACTL_DEVICE_LOG_DIR", str(clash))
    O._append_journal({"status": "ok", "device": "cl", "command": "show", "stdout": "z\n"}, Ctx(device="cl"))


def test_finish_writes_journal(tmp_path, monkeypatch):
    root = _journal_root(tmp_path, monkeypatch)
    result = {"status": "ok", "device": "cl", "command": "show ver", "stdout": "VERSION\n"}
    with pytest.raises(typer.Exit):
        O.finish(result, Ctx(device="cl", json=True))
    assert "VERSION" in _today_file(root, "cl").read_text()
