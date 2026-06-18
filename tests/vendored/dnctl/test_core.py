"""Unit tests for the shared front-end glue (no device traffic)."""

import io
import json
from unittest import mock

import pytest

from dnctl.core import confirm
from dnctl.core import devices as dn_devices
from dnctl.core import output, payload


def test_exit_code_mapping():
    assert output.exit_code_for({"status": "ok"}) == 0
    assert output.exit_code_for({"status": "error"}) == 1
    assert output.exit_code_for({"status": "connect_error"}) == 2
    assert output.exit_code_for({"status": "timeout"}) == 3
    assert output.exit_code_for({"status": "weird"}) == 1
    assert output.exit_code_for({}) == 0  # no status -> success
    assert output.exit_code_for("not a dict") == 0


def test_emit_json_is_lossless(capsys):
    env = {"status": "ok", "result": {"a": 1, "b": ["x", "y"]}, "warnings": ["w"]}
    code = output.emit(env, as_json=True)
    out = capsys.readouterr().out
    import json
    assert json.loads(out) == env
    assert code == 0


def test_emit_text_body_to_stdout_diagnostics_to_stderr(capsys):
    env = {"status": "ok", "stdout": "hello\n", "warnings": ["careful"], "errors": []}
    output.emit(env, as_json=False)
    cap = capsys.readouterr()
    assert cap.out == "hello\n"
    assert "warning: careful" in cap.err


def test_resolve_body_inline_file_stdin(tmp_path, monkeypatch):
    assert payload.resolve_body("inline", None) == "inline"

    f = tmp_path / "p.xml"
    f.write_text("from-file")
    assert payload.resolve_body(None, str(f)) == "from-file"

    monkeypatch.setattr("sys.stdin", io.StringIO("from-stdin"))
    assert payload.resolve_body("-", None) == "from-stdin"


def test_resolve_body_required_raises():
    with pytest.raises(payload.PayloadError):
        payload.resolve_body(None, None)
    assert payload.resolve_body(None, None, required=False) is None


# --- confirm gate ----------------------------------------------------------


def test_confirm_yes_proceeds():
    assert confirm.ensure("do x", yes=True, as_json=False) is True


def test_confirm_interactive_prompt_to_stderr(monkeypatch, capsys):
    # On a TTY the prompt goes to stderr and stdout stays clean, matching
    # the native qactl / ixiactl gates (keyed on stdin+stderr).
    err = mock.Mock(isatty=lambda: True)
    monkeypatch.setattr("sys.stdin", mock.Mock(isatty=lambda: True))
    monkeypatch.setattr("sys.stderr", err)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert confirm.ensure("delete x", yes=False, as_json=False) is True
    written = "".join(c.args[0] for c in err.write.call_args_list)
    assert "Proceed? [y/N]" in written
    assert capsys.readouterr().out == ""


def test_confirm_off_tty_refuses(capsys):
    # pytest's captured stdin/stderr are non-TTYs → refuse, no blocking.
    assert confirm.ensure("delete x", yes=False, as_json=True) is False
    payload_out = json.loads(capsys.readouterr().out)
    assert payload_out["status"] == "error"
    assert any("--yes" in n for n in payload_out["next_actions"])


# --- device aliases (nicknames) -------------------------------------------


@pytest.fixture
def device_map(tmp_path):
    """A canonical device map file path, pre-seeded with two devices."""
    p = tmp_path / "devices_mgmt0.json"
    p.write_text(
        json.dumps(
            {
                "devices": {
                    "sa": {"mgmt0": "10.0.0.1", "expected_sns": ["SN-SA"]},
                    "cl": {"mgmt0": "10.0.0.2", "expected_sns": ["SN-CL"]},
                }
            }
        )
    )
    return str(p)


def test_add_alias_then_resolve(device_map):
    assert dn_devices.add_alias("spine-a", "sa", path=device_map) is True
    # idempotent re-add
    assert dn_devices.add_alias("spine-a", "sa", path=device_map) is False

    assert dn_devices.resolve_canonical("spine-a", path=device_map) == "sa"
    # canonical key still wins / resolves to itself
    assert dn_devices.resolve_canonical("sa", path=device_map) == "sa"
    # nickname reaches the same mgmt0 + entry as the canonical name
    assert dn_devices.resolve_mgmt0("spine-a", path=device_map) == "10.0.0.1"
    assert dn_devices.get_device_entry("spine-a", path=device_map)["mgmt0"] == "10.0.0.1"
    assert dn_devices.get_aliases("sa", path=device_map) == ["spine-a"]
    # secondary aliases are NOT canonical aliases
    assert dn_devices.list_device_aliases(path=device_map) == ["cl", "sa"]


def test_add_alias_rejects_collisions(device_map):
    # cannot shadow a canonical device key
    with pytest.raises(ValueError):
        dn_devices.add_alias("cl", "sa", path=device_map)
    # cannot alias an unknown canonical device
    with pytest.raises(ValueError):
        dn_devices.add_alias("nick", "nope", path=device_map)
    # cannot steal a nickname already owned by another device
    dn_devices.add_alias("edge", "cl", path=device_map)
    with pytest.raises(ValueError):
        dn_devices.add_alias("edge", "sa", path=device_map)


def test_remove_alias(device_map):
    dn_devices.add_alias("spine-a", "sa", path=device_map)
    assert dn_devices.remove_alias("spine-a", path=device_map) == "sa"
    # gone now: resolves to nothing, and removing again is a no-op
    assert dn_devices.resolve_canonical("spine-a", path=device_map) is None
    assert dn_devices.remove_alias("spine-a", path=device_map) is None
    # the canonical device survives the nickname removal
    assert dn_devices.resolve_mgmt0("sa", path=device_map) == "10.0.0.1"


def test_unknown_name_resolves_to_none(device_map):
    assert dn_devices.resolve_canonical("ghost", path=device_map) is None
    assert dn_devices.resolve_mgmt0("ghost", path=device_map) is None
    assert dn_devices.get_device_entry("ghost", path=device_map) is None
