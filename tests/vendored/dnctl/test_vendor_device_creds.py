"""Per-device / per-vendor credential resolution for registry devices (#70).

Vendor boxes (cisco / juniper / arista) don't speak the global DNOS
``[auth]`` account, so the session layer resolves creds per call:

    explicit flag > [devices."<name>"] config > <VENDOR>_* env > global

No device traffic, no real secrets.
"""

import json

import pytest

from qactl.dnos.core import config, credentials as creds, setup_cmd


VENDOR_ENV_VARS = [v for pair in creds.VENDOR_ENV.values() for v in pair]


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Isolated config + device map with one device per vendor."""
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("QACTL_CONFIG", str(cfg))
    for var in VENDOR_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    dev_map = tmp_path / "devices_mgmt0.json"
    dev_map.write_text(json.dumps({
        "devices": {
            "jun-rt02": {"vendor": "juniper", "expected_sns": ["10.0.0.2"]},
            "cisco-rt": {"vendor": "cisco", "expected_sns": ["10.0.0.1"]},
            "ar-sw01": {
                "vendor": "arista",
                "expected_sns": ["10.0.0.3"],
                "aliases": ["arista-a"],
            },
            "sa": {"expected_sns": ["10.0.0.9"]},  # DNOS (no vendor field)
        }
    }), encoding="utf-8")
    monkeypatch.setenv("QACTL_DEVICES", str(dev_map))
    config.load_config.cache_clear()
    yield cfg
    config.load_config.cache_clear()


def _resolve_defaults(device):
    return creds.resolve_device_credentials(
        device, creds.DEFAULT_USER, creds.DEFAULT_PASSWORD,
    )


def test_global_default_when_nothing_configured(lab):
    assert _resolve_defaults("jun-rt02") == (creds.DEFAULT_USER, creds.DEFAULT_PASSWORD)


def test_vendor_env_used_when_no_per_device(lab, monkeypatch):
    monkeypatch.setenv("JUNIPER_USER", "jlab")
    monkeypatch.setenv("JUNIPER_PASSWORD", "jpw")
    assert _resolve_defaults("jun-rt02") == ("jlab", "jpw")
    # Other vendors don't pick up juniper's env.
    assert _resolve_defaults("cisco-rt") == (creds.DEFAULT_USER, creds.DEFAULT_PASSWORD)


def test_empty_vendor_env_password_is_honoured(lab, monkeypatch):
    # Arista lab default is user + empty password — "" must not fall through.
    monkeypatch.setenv("ARISTA_USER", "admin")
    monkeypatch.setenv("ARISTA_PASSWORD", "")
    assert _resolve_defaults("ar-sw01") == ("admin", "")


def test_per_device_config_beats_vendor_env(lab, monkeypatch):
    lab.write_text(
        '[devices."jun-rt02"]\nuser = "boxuser"\npassword = "boxpw"\n',
        encoding="utf-8",
    )
    config.load_config.cache_clear()
    monkeypatch.setenv("JUNIPER_USER", "envuser")
    monkeypatch.setenv("JUNIPER_PASSWORD", "envpw")
    assert _resolve_defaults("jun-rt02") == ("boxuser", "boxpw")


def test_per_device_partial_falls_through_per_field(lab, monkeypatch):
    lab.write_text('[devices."jun-rt02"]\npassword = "boxpw"\n', encoding="utf-8")
    config.load_config.cache_clear()
    monkeypatch.setenv("JUNIPER_USER", "envuser")
    assert _resolve_defaults("jun-rt02") == ("envuser", "boxpw")


def test_secondary_alias_resolves_canonical_creds(lab):
    lab.write_text(
        '[devices."ar-sw01"]\nuser = "aruser"\npassword = "arpw"\n',
        encoding="utf-8",
    )
    config.load_config.cache_clear()
    assert _resolve_defaults("arista-a") == ("aruser", "arpw")


def test_explicit_flags_pass_through(lab, monkeypatch):
    lab.write_text(
        '[devices."jun-rt02"]\nuser = "boxuser"\npassword = "boxpw"\n',
        encoding="utf-8",
    )
    config.load_config.cache_clear()
    monkeypatch.setenv("JUNIPER_USER", "envuser")
    got = creds.resolve_device_credentials("jun-rt02", "cli-user", "cli-pw")
    assert got == ("cli-user", "cli-pw")


def test_dnos_and_unknown_devices_untouched(lab, monkeypatch):
    monkeypatch.setenv("CISCO_USER", "envuser")
    assert _resolve_defaults("sa") == (creds.DEFAULT_USER, creds.DEFAULT_PASSWORD)
    assert _resolve_defaults("no-such-box") == (creds.DEFAULT_USER, creds.DEFAULT_PASSWORD)
    assert _resolve_defaults(None) == (creds.DEFAULT_USER, creds.DEFAULT_PASSWORD)


def test_transport_registry_applies_resolution(lab, monkeypatch):
    from qactl.dnos.cli.core import session

    lab.write_text(
        '[devices."jun-rt02"]\nuser = "boxuser"\npassword = "boxpw"\n',
        encoding="utf-8",
    )
    config.load_config.cache_clear()

    seen = {}

    def fake_open_transport(device, host, user, password, connect_timeout):
        seen.update(user=user, password=password)
        raise session.ConnectError("stop here", transient=False)

    monkeypatch.setattr(session, "_open_transport", fake_open_transport)
    registry = session.TransportRegistry.__new__(session.TransportRegistry)
    registry._transports = {}
    registry._key_locks = {}
    import threading
    registry._registry_lock = threading.Lock()
    registry._idle_max = 60
    with pytest.raises(session.ConnectError):
        registry.get(
            device="jun-rt02", host=None,
            user=creds.DEFAULT_USER, password=creds.DEFAULT_PASSWORD,
        )
    assert seen == {"user": "boxuser", "password": "boxpw"}


def test_setup_device_writes_and_clears_nested_table(lab):
    setup_cmd._write_config({"devices": {"jun-rt02": {"user": "u1", "password": "p1"}}})
    got = config.load_config().get("devices")
    assert got == {"jun-rt02": {"user": "u1", "password": "p1"}}
    # Round-trip preserves other sections and supports "" deletion.
    setup_cmd._write_config({"auth": {"user": "dnroot"}})
    setup_cmd._write_config({"devices": {"jun-rt02": {"password": "p2"}}})
    got = config.load_config()
    assert got["devices"]["jun-rt02"] == {"user": "u1", "password": "p2"}
    assert got["auth"] == {"user": "dnroot"}
    setup_cmd._write_config({"devices": {"jun-rt02": {"user": "", "password": ""}}})
    assert "devices" not in config.load_config()


def test_setup_cli_device_flag(lab, tmp_path):
    from typer.testing import CliRunner
    import typer

    app = typer.Typer()
    app.command()(setup_cmd.setup)
    runner = CliRunner()
    result = runner.invoke(app, ["--device", "jun-rt02", "--user", "u", "--password", "p"])
    assert result.exit_code == 0, result.output
    assert config.load_config()["devices"]["jun-rt02"] == {"user": "u", "password": "p"}
    # Registry-unknown name still writes, with a typo note.
    result = runner.invoke(app, ["--device", "nope", "--password", "x"])
    assert result.exit_code == 0, result.output
    assert "not in the device registry" in result.output
    # --device with no cred flags is an error.
    result = runner.invoke(app, ["--device", "jun-rt02"])
    assert result.exit_code == 2


def _setup_runner():
    from typer.testing import CliRunner
    import typer

    app = typer.Typer()
    app.command()(setup_cmd.setup)
    return app, CliRunner()


def test_setup_password_dash_reads_stdin(lab):
    # Acceptance for #78: printf '%s' "$PW" | qactl setup ... --password -
    app, runner = _setup_runner()
    result = runner.invoke(
        app, ["--device", "jun-rt02", "--user", "u", "--password", "-"],
        input="s3cret",
    )
    assert result.exit_code == 0, result.output
    assert config.load_config()["devices"]["jun-rt02"] == {"user": "u", "password": "s3cret"}
    assert "s3cret" not in result.output


def test_setup_password_dash_strips_one_trailing_newline(lab):
    # echo adds a newline; only that one is stripped, inner ones survive.
    app, runner = _setup_runner()
    result = runner.invoke(app, ["--password", "-"], input="pw with\nnewline\n")
    assert result.exit_code == 0, result.output
    assert config.load_config()["auth"]["password"] == "pw with\nnewline"


def test_setup_password_dash_empty_stdin_is_error(lab):
    app, runner = _setup_runner()
    result = runner.invoke(app, ["--device", "jun-rt02", "--password", "-"], input="")
    assert result.exit_code == 2
    assert "stdin was empty" in result.output
    assert "devices" not in config.load_config()


def test_setup_only_one_dash_may_read_stdin(lab):
    app, runner = _setup_runner()
    result = runner.invoke(
        app, ["--password", "-", "--dnftp-password", "-"], input="pw",
    )
    assert result.exit_code == 2
    assert "only one secret flag" in result.output


def test_resolve_secret_prompts_hidden_on_tty(monkeypatch):
    class FakeTTY:
        def isatty(self):
            return True

    prompts = {}

    def fake_prompt(label, **kwargs):
        prompts.update(kwargs, label=label)
        return "typed-pw"

    monkeypatch.setattr(setup_cmd.sys, "stdin", FakeTTY())
    monkeypatch.setattr(setup_cmd.typer, "prompt", fake_prompt)
    assert setup_cmd._resolve_secret("-", "Password", {}) == "typed-pw"
    assert prompts == {"label": "Password", "hide_input": True}
    # Non-dash values pass through untouched (no prompt, no stdin).
    assert setup_cmd._resolve_secret("inline", "Password", {}) == "inline"
    assert setup_cmd._resolve_secret(None, "Password", {}) is None


def test_connect_error_hints_vendor_creds(lab, monkeypatch):
    import paramiko
    from qactl.dnos.cli.core import session

    def fail_auth(host, user, password, timeout):
        raise paramiko.AuthenticationException("Authentication failed.")

    monkeypatch.setattr(session, "_try_connect_host", fail_auth)
    monkeypatch.setattr(session, "DEVICE_HOSTS", {"jun-rt02": ["10.0.0.2"]})
    with pytest.raises(session.ConnectError) as exc:
        session._open_transport("jun-rt02", None, "u", "p", connect_timeout=1)
    assert "setup --device jun-rt02" in str(exc.value)
    assert "JUNIPER_USER" in str(exc.value)
