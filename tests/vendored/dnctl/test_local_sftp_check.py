"""Local SFTP endpoint resolution + ``setup --check-local-sftp`` self-check.

No device traffic, no real secrets — the probe runs against a throwaway
loopback socket and the resolution against a tmp config file.
"""

import json
import socket
from contextlib import closing

import pytest
import typer

from qactl.dnos.core import config, local_sftp, setup_cmd


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    monkeypatch.setenv("QACTL_CONFIG", str(p))
    # Make sure no developer env leaks into the resolution under test.
    for var in (
        "QACTL_LOCAL_SFTP_HOST", "QACTL_LOCAL_SFTP_USER",
        "QACTL_LOCAL_SFTP_VRF", "QACTL_LOCAL_SFTP_PORT",
        "QACTL_LOCAL_SFTP_PASSWORD",
        "QACTL_LOCAL_SFTP_HOST", "QACTL_LOCAL_SFTP_USER",
        "QACTL_LOCAL_SFTP_VRF", "QACTL_LOCAL_SFTP_PORT",
        "QACTL_LOCAL_SFTP_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    config.load_config.cache_clear()
    yield p
    config.load_config.cache_clear()


def test_resolve_defaults_when_unset(cfg_file):
    s = local_sftp.resolve_local_sftp()
    assert s.host  # auto FQDN / hostname, never empty
    assert s.user  # current OS user
    assert s.vrf == "mgmt0"
    assert s.port == 22
    assert s.password is None
    assert s.password_set is False
    assert s.host_source == "default"


def test_resolve_reads_config(cfg_file):
    cfg_file.write_text(
        '[local]\n'
        'host = "lab-host.example"\n'
        'user = "me"\n'
        'vrf = "mgmt1"\n'
        'port = "2222"\n'
        'password = "sftp-pw"\n',
        encoding="utf-8",
    )
    config.load_config.cache_clear()
    s = local_sftp.resolve_local_sftp()
    assert (s.host, s.user, s.vrf, s.port) == ("lab-host.example", "me", "mgmt1", 2222)
    assert s.password_set is True
    assert s.host_source == "config:[local].host"


def test_resolve_bad_port_falls_back_to_22(cfg_file):
    cfg_file.write_text('[local]\nport = "not-a-number"\n', encoding="utf-8")
    config.load_config.cache_clear()
    assert local_sftp.resolve_local_sftp().port == 22


def test_probe_endpoint_listening_vs_closed():
    # A bound, listening loopback socket is reachable...
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        ok, detail = local_sftp.probe_endpoint("127.0.0.1", port, timeout=1.0)
        assert ok is True
        assert "connected" in detail
    # ...and that same port is closed once the socket is released.
    closed, detail = local_sftp.probe_endpoint("127.0.0.1", port, timeout=0.5)
    assert closed is False
    assert "cannot connect" in detail


def test_check_exits_nonzero_when_password_unset(cfg_file, monkeypatch, capsys):
    monkeypatch.setattr(local_sftp, "probe_endpoint", lambda *a, **k: (True, "connected"))
    with pytest.raises(typer.Exit) as exc:
        setup_cmd._check_local_sftp(as_json=True)
    assert exc.value.exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    pw = next(c for c in payload["checks"] if c["check"] == "password_configured")
    assert pw["ok"] is False


def test_check_exits_nonzero_when_endpoint_down(cfg_file, monkeypatch, capsys):
    cfg_file.write_text('[local]\npassword = "pw"\n', encoding="utf-8")
    config.load_config.cache_clear()
    monkeypatch.setattr(
        local_sftp, "probe_endpoint", lambda *a, **k: (False, "cannot connect"),
    )
    with pytest.raises(typer.Exit) as exc:
        setup_cmd._check_local_sftp(as_json=True)
    assert exc.value.exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    ep = next(c for c in payload["checks"] if c["check"] == "endpoint_listening")
    assert ep["ok"] is False


def test_check_passes_when_configured_and_listening(cfg_file, monkeypatch, capsys):
    cfg_file.write_text(
        '[local]\nhost = "h"\npassword = "pw"\n', encoding="utf-8",
    )
    config.load_config.cache_clear()
    monkeypatch.setattr(local_sftp, "probe_endpoint", lambda *a, **k: (True, "connected to h:22"))
    # No exception → exit 0.
    setup_cmd._check_local_sftp(as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert all(c["ok"] for c in payload["checks"])
    assert payload["endpoint"]["password_set"] is True
