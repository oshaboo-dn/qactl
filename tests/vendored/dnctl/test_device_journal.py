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


def test_journal_gnmi_get_labelled_and_body_persisted(tmp_path, monkeypatch):
    # gnmi envelopes carry no `command`/`stdout`; the journal must derive
    # the label from kind+request and persist the envelope as the body (#83).
    root = _journal_root(tmp_path, monkeypatch)
    env = {
        "status": "ok", "device": "cl", "host": "10.0.0.1", "port": 50051,
        "tls_mode": "insecure", "kind": "get",
        "request": {"path": "/drivenets-top/system/ncps", "encoding": "json"},
        "result": {"notification": [{"update": [{"path": "system", "val": {"x": 1}}]}]},
        "warnings": [], "errors": [], "next_actions": [],
    }
    O._append_journal(env, Ctx(device="cl"))
    text = _today_file(root, "cl").read_text()
    assert "cmd='gnmi get /drivenets-top/system/ncps'" in text
    assert "/drivenets-top/system/ncps" in text
    assert '"notification"' in text  # response envelope persisted


def test_journal_gnmi_set_labelled_with_paths(tmp_path, monkeypatch):
    root = _journal_root(tmp_path, monkeypatch)
    env = {
        "status": "error", "device": "cl", "host": "10.0.0.1", "port": 50051,
        "tls_mode": "insecure", "kind": "set",
        "request": {
            "update": [{"path": "/a/b", "val": {"x": 1}}],
            "replace": [], "delete": ["/c/d"], "confirm": True,
        },
        "result": None, "warnings": [],
        "errors": ["RpcError: boom"], "next_actions": [],
    }
    O._append_journal(env, Ctx(device="cl"))
    text = _today_file(root, "cl").read_text()
    assert "cmd='gnmi set /a/b (+1 more)'" in text
    assert "status=error" in text
    assert "RpcError: boom" in text  # error persisted in body


def test_journal_rc_write_labelled_and_body_persisted(tmp_path, monkeypatch):
    root = _journal_root(tmp_path, monkeypatch)
    url = "http://odl:8181/restconf/config/net/node/CL/yang-ext:mount/drivenets-top/system"
    env = {
        "status": "ok", "device": "Hybrid-CL", "endpoint": "odl-lab1",
        "base_url": "http://odl:8181/restconf", "kind": "put",
        "request": {"method": "PUT", "segments": ["drivenets-top", "system"],
                    "url": url, "payload": {"system": {"name": "x"}}},
        "result": {"http_status": 200, "url": url, "response": ""},
        "warnings": [], "errors": [], "next_actions": [],
    }
    O._append_journal(env, Ctx(device="Hybrid-CL"))
    text = _today_file(root, "Hybrid-CL").read_text()
    assert f"cmd='rc put {url}'" in text
    assert '"http_status": 200' in text
    assert '"payload"' in text  # request body persisted


def test_journal_nc_edit_error_labelled_and_body_persisted(tmp_path, monkeypatch):
    # nc edit results have no result_xml; the payload + device rpc-error
    # must land in the body (#83).
    root = _journal_root(tmp_path, monkeypatch)
    env = {
        "action": "edit", "host": "10.0.0.1", "port": 830, "user": "u",
        "session_id": "s1", "timestamp": "t", "device": "cl",
        "status": "edit_error", "op": "replace",
        "device_error": "Unknown element 'foo'",
        "applied_xml": "<network-services><foo/></network-services>",
    }
    O._append_journal(env, Ctx(device="cl"))
    text = _today_file(root, "cl").read_text()
    assert "cmd='nc edit op=replace'" in text
    assert "status=edit_error" in text
    assert "Unknown element" in text
    assert "network-services" in text


def test_journal_nc_read_keeps_result_xml_and_gets_label(tmp_path, monkeypatch):
    root = _journal_root(tmp_path, monkeypatch)
    env = {
        "action": "show", "host": "10.0.0.1", "port": 830, "user": "u",
        "session_id": "s1", "timestamp": "t", "device": "cl",
        "status": "ok", "kind": "get-config",
        "filter_xml": "<drivenets-top/>",
        "result_xml": "<rpc-reply><data/></rpc-reply>",
    }
    O._append_journal(env, Ctx(device="cl"))
    text = _today_file(root, "cl").read_text()
    assert "cmd='nc show (get-config)'" in text
    assert "<rpc-reply><data/></rpc-reply>" in text


def test_finish_writes_journal(tmp_path, monkeypatch):
    root = _journal_root(tmp_path, monkeypatch)
    result = {"status": "ok", "device": "cl", "command": "show ver", "stdout": "VERSION\n"}
    with pytest.raises(typer.Exit):
        O.finish(result, Ctx(device="cl", json=True))
    assert "VERSION" in _today_file(root, "cl").read_text()
