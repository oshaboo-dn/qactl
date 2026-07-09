"""Config / credential resolution — no device traffic, no real secrets."""

import importlib

import pytest

from qactl.dnctl.core import config


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    monkeypatch.setenv("DNCTL_CONFIG", str(p))
    config.load_config.cache_clear()
    yield p
    config.load_config.cache_clear()


def test_resolution_order_env_beats_config(cfg_file, monkeypatch):
    cfg_file.write_text('[dnftp]\npassword = "from-file"\n', encoding="utf-8")
    config.load_config.cache_clear()
    assert config.resolve("DNCTL_DNFTP_PASSWORD", "dnftp", "password", None) == "from-file"
    monkeypatch.setenv("DNCTL_DNFTP_PASSWORD", "from-env")
    assert config.resolve("DNCTL_DNFTP_PASSWORD", "dnftp", "password", None) == "from-env"


def test_default_used_when_unset(cfg_file):
    assert config.resolve("DNCTL_USER", "auth", "user", "dnroot") == "dnroot"
    assert config.resolve("DNCTL_DNFTP_PASSWORD", "dnftp", "password", None) is None


def test_resolved_source_labels(cfg_file, monkeypatch):
    assert config.resolved_source("DNCTL_USER", "auth", "user", "dnroot") == "default"
    assert config.resolved_source("DNCTL_DNFTP_PASSWORD", "dnftp", "password", None) == "unset"
    monkeypatch.setenv("DNCTL_USER", "me")
    assert config.resolved_source("DNCTL_USER", "auth", "user", "dnroot") == "env:DNCTL_USER"


def test_no_secret_baked_into_credentials(tmp_path, monkeypatch):
    # Hermetic: resolve against an empty config with no env overrides, so
    # the assertions reflect the *shipped defaults* and not whatever the
    # developer happens to have in ~/.config/dnctl/config.toml. The
    # import-time constants are re-resolved via importlib.reload under the
    # clean environment.
    monkeypatch.setenv("DNCTL_CONFIG", str(tmp_path / "empty.toml"))
    for var in (
        "DNCTL_USER", "DNCTL_PASSWORD", "DNCTL_SSH_KEY",
        "DNCTL_DNFTP_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    config.load_config.cache_clear()

    import qactl.dnctl.core.credentials as creds
    import qactl.dnctl.core.dnftp as dnftp
    creds = importlib.reload(creds)
    dnftp = importlib.reload(dnftp)

    # No secret literal baked into either source file.
    for mod in (creds, dnftp):
        src = importlib.util.find_spec(mod.__name__).origin
        assert "drive1234" not in open(src, encoding="utf-8").read()
    # No-config defaults: dnroot works, secrets are absent.
    assert creds.DEFAULT_USER == "dnroot"
    assert dnftp.DNFTP_PASSWORD in (None, "")
