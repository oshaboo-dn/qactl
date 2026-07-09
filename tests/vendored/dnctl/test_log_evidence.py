"""`--log` evidence capture: append full raw command output to a file.

Covers the tee-like behaviour added for device read commands — a header
plus the verbatim envelope ``stdout`` is appended (never overwritten), it
works independently of ``--json`` (it captures the raw text payload, not
the envelope), and a write failure degrades to a warning rather than
failing the command.
"""

import pytest
import typer
from typer.testing import CliRunner

from qactl.dnos.__main__ import app
from qactl.dnos.core import options as O
from qactl.dnos.core.context import Ctx

runner = CliRunner()


def _ctx(log):
    return Ctx(device="cl", log=str(log))


def test_append_writes_header_and_raw_stdout(tmp_path):
    f = tmp_path / "run.md"
    O._append_log(
        {"status": "ok", "device": "cl", "command": "show bgp summary", "stdout": "raw line 1\nraw line 2\n"},
        _ctx(f),
    )
    text = f.read_text()
    assert "device=cl" in text
    assert "cmd='show bgp summary'" in text
    assert "raw line 1\nraw line 2\n" in text
    # header sits immediately above the verbatim body
    assert text.startswith("# =====")


def test_body_wrapped_in_code_fence(tmp_path):
    f = tmp_path / "run.md"
    O._append_log(
        {"status": "ok", "command": "show bgp summary", "stdout": "raw line 1\nraw line 2\n"},
        _ctx(f),
    )
    text = f.read_text()
    # header, blank line, opening fence, verbatim body, closing fence
    assert "=====\n\n```\nraw line 1\nraw line 2\n```\n" in text


def test_body_without_trailing_newline_still_fenced(tmp_path):
    f = tmp_path / "run.md"
    O._append_log({"status": "ok", "command": "show", "stdout": "no-newline"}, _ctx(f))
    assert f.read_text().endswith("```\nno-newline\n```\n")


def test_backtick_run_in_body_uses_longer_fence(tmp_path):
    f = tmp_path / "run.md"
    O._append_log({"status": "ok", "command": "show", "stdout": "a\n```\nb\n"}, _ctx(f))
    text = f.read_text()
    # the inner ``` must not terminate the block: outer fence is longer
    assert "````\na\n```\nb\n````\n" in text


def test_repeated_calls_accumulate(tmp_path):
    f = tmp_path / "run.md"
    O._append_log({"status": "ok", "command": "show a", "stdout": "AAA\n"}, _ctx(f))
    O._append_log({"status": "ok", "command": "show b", "stdout": "BBB\n"}, _ctx(f))
    text = f.read_text()
    assert "AAA" in text and "BBB" in text
    assert text.count("# =====") == 2


def test_result_xml_fallback_for_config_reads(tmp_path):
    f = tmp_path / "run.md"
    O._append_log({"status": "ok", "command": "show config", "result_xml": "<data/>\n"}, _ctx(f))
    assert "<data/>" in f.read_text()


def test_noop_without_log_flag(tmp_path):
    f = tmp_path / "run.md"
    O._append_log({"status": "ok", "stdout": "x\n"}, Ctx(device="cl"))
    assert not f.exists()


def test_creates_missing_parent_dirs(tmp_path):
    f = tmp_path / "nested" / "deep" / "run.md"
    O._append_log({"status": "ok", "command": "show", "stdout": "y\n"}, _ctx(f))
    assert f.read_text().endswith("y\n```\n")


def test_write_failure_degrades_to_warning(tmp_path):
    # Target a path whose parent is a file → mkdir/open fails.
    clash = tmp_path / "afile"
    clash.write_text("x")
    result = {"status": "ok", "command": "show", "stdout": "z\n", "warnings": []}
    O._append_log(result, _ctx(clash / "run.md"))
    assert any("--log" in w for w in result["warnings"])


def test_finish_writes_log_alongside_json(tmp_path):
    f = tmp_path / "run.md"
    result = {"status": "ok", "device": "cl", "command": "show ver", "stdout": "VERSION\n"}
    with pytest.raises(typer.Exit):
        O.finish(result, Ctx(device="cl", json=True, log=str(f)))
    assert "VERSION" in f.read_text()


@pytest.mark.parametrize("cmd", ["show", "show-config", "system", "interfaces", "ping", "traces", "trace", "events", "accounting", "netconf-accounting"])
def test_read_commands_expose_log_flag(cmd):
    r = runner.invoke(app, ["cli", cmd, "--help"])
    assert r.exit_code == 0
    assert "--log" in r.stdout
