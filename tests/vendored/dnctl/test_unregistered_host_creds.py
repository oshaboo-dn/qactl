"""Per-device creds for unregistered hosts and --user overrides (#79).

Stored ``[devices."<name>"]`` creds (written by ``setup --device``) must
be usable before the device is in the registry: the ``device add`` SSH
probe and ``--host`` / ``--host --user`` overrides resolve the stored
password instead of failing auth with the global ``[auth]`` account.
No device traffic, no real secrets.
"""

import json
import threading

import pytest

from qactl.dnos.core import config, credentials as creds


BOX = "DNAAS-SuperSpine-D04"

VENDOR_ENV_VARS = [v for pair in creds.VENDOR_ENV.values() for v in pair]


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Isolated config with creds for BOX, registry WITHOUT BOX."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[devices."{BOX}"]\nuser = "boxuser"\npassword = "boxpw"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("QACTL_CONFIG", str(cfg))
    for var in VENDOR_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    dev_map = tmp_path / "devices_mgmt0.json"
    dev_map.write_text(json.dumps({
        "devices": {"sa": {"expected_sns": ["10.0.0.9"]}}
    }), encoding="utf-8")
    monkeypatch.setenv("QACTL_DEVICES", str(dev_map))
    config.load_config.cache_clear()
    yield cfg
    config.load_config.cache_clear()


# --- resolver: host-keyed lookup, no registry entry needed ----------------

def test_host_only_call_uses_stored_creds(lab):
    got = creds.resolve_device_credentials(
        None, creds.DEFAULT_USER, creds.DEFAULT_PASSWORD, host=BOX,
    )
    assert got == ("boxuser", "boxpw")


def test_unregistered_device_name_uses_stored_creds(lab):
    got = creds.resolve_device_credentials(
        BOX, creds.DEFAULT_USER, creds.DEFAULT_PASSWORD,
    )
    assert got == ("boxuser", "boxpw")


def test_device_key_wins_over_host(lab):
    lab.write_text(
        lab.read_text(encoding="utf-8")
        + '[devices."other"]\nuser = "other-u"\npassword = "other-pw"\n',
        encoding="utf-8",
    )
    config.load_config.cache_clear()
    got = creds.resolve_device_credentials(
        "other", creds.DEFAULT_USER, creds.DEFAULT_PASSWORD, host=BOX,
    )
    assert got == ("other-u", "other-pw")


def test_no_stored_creds_passes_through(lab):
    got = creds.resolve_device_credentials(
        None, creds.DEFAULT_USER, creds.DEFAULT_PASSWORD, host="no-such-box",
    )
    assert got == (creds.DEFAULT_USER, creds.DEFAULT_PASSWORD)


# --- resolver: explicit --user borrows the matching stored password -------

def test_explicit_matching_user_borrows_stored_password(lab):
    got = creds.resolve_device_credentials(
        None, "boxuser", creds.DEFAULT_PASSWORD, host=BOX,
    )
    assert got == ("boxuser", "boxpw")


def test_explicit_other_user_does_not_borrow_password(lab):
    # Never cross-wire an explicit user with another account's password.
    got = creds.resolve_device_credentials(
        None, "someone-else", creds.DEFAULT_PASSWORD, host=BOX,
    )
    assert got == ("someone-else", creds.DEFAULT_PASSWORD)


def test_explicit_user_borrows_userless_stored_password(lab):
    lab.write_text('[devices."pw-only"]\npassword = "solepw"\n', encoding="utf-8")
    config.load_config.cache_clear()
    got = creds.resolve_device_credentials(
        None, "anyone", creds.DEFAULT_PASSWORD, host="pw-only",
    )
    assert got == ("anyone", "solepw")


def test_explicit_password_passes_through(lab):
    got = creds.resolve_device_credentials(
        None, creds.DEFAULT_USER, "cli-pw", host=BOX,
    )
    assert got == (creds.DEFAULT_USER, "cli-pw")
    got = creds.resolve_device_credentials(
        None, "cli-user", "cli-pw", host=BOX,
    )
    assert got == ("cli-user", "cli-pw")


# --- transport layer: --host X (no -d) authenticates with stored creds ----

def test_transport_registry_resolves_host_only(lab, monkeypatch):
    from qactl.dnos.cli.core import session

    seen = {}

    def fake_open_transport(device, host, user, password, connect_timeout):
        seen.update(user=user, password=password)
        raise session.ConnectError("stop here", transient=False)

    monkeypatch.setattr(session, "_open_transport", fake_open_transport)
    registry = session.TransportRegistry.__new__(session.TransportRegistry)
    registry._transports = {}
    registry._key_locks = {}
    registry._registry_lock = threading.Lock()
    registry._idle_max = 60
    with pytest.raises(session.ConnectError):
        registry.get(
            device=None, host=BOX,
            user=creds.DEFAULT_USER, password=creds.DEFAULT_PASSWORD,
        )
    assert seen == {"user": "boxuser", "password": "boxpw"}


# --- device add: the registration probe uses pre-stored creds -------------

@pytest.fixture
def devtools(monkeypatch):
    from qactl.dnos.cli.tools import devices as devtools

    monkeypatch.setattr(devtools, "log_request", lambda *a, **k: None)
    return devtools


def _capture_probe(devtools, monkeypatch):
    from qactl.dnos.cli.core.session import ConnectError

    seen = {}

    def fake_probe(registry, host, user="", password="", **kwargs):
        seen.update(host=host, user=user, password=password)
        raise ConnectError("stop after capture", transient=False)

    monkeypatch.setattr(devtools, "probe_device", fake_probe)
    return seen


def test_device_add_probe_uses_stored_creds(lab, devtools, monkeypatch):
    seen = _capture_probe(devtools, monkeypatch)
    resp = devtools.manage_device(operation="add", name=BOX, sn=BOX)
    assert resp["status"] == "error"  # probe aborted deliberately
    assert seen == {"host": BOX, "user": "boxuser", "password": "boxpw"}


def test_device_add_keys_creds_on_chosen_name_for_ip_host(lab, devtools, monkeypatch):
    # Creds stored under the chosen NAME apply even when --host is an IP.
    seen = _capture_probe(devtools, monkeypatch)
    resp = devtools.manage_device(operation="add", name=BOX, sn="10.1.2.3")
    assert resp["status"] == "error"
    assert seen == {"host": "10.1.2.3", "user": "boxuser", "password": "boxpw"}


def test_device_refresh_probe_uses_stored_creds(lab, devtools, monkeypatch):
    # refresh probes host-only by SN; creds are keyed on the registry name.
    lab.write_text(
        '[devices."sa"]\nuser = "sauser"\npassword = "sapw"\n',
        encoding="utf-8",
    )
    config.load_config.cache_clear()
    seen = _capture_probe(devtools, monkeypatch)
    resp = devtools.manage_device(operation="refresh", name="sa")
    assert resp["status"] == "error"  # probe aborted deliberately
    assert seen == {"host": "10.0.0.9", "user": "sauser", "password": "sapw"}
