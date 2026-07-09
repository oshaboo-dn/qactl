"""Bucket C — device-registry hardening (no device traffic).

Covers two classes of bug:

* secondary-alias operations corrupting the map — remove/save/refresh by
  a nickname used to no-op (and report success) or fork a ghost canonical
  key, because the alias wasn't resolved to its canonical key first.
* the map write being non-atomic (a crash mid-write truncated the JSON).
"""

from __future__ import annotations

import json
import os

import pytest

from qactl.dnos.core import devices as dn_devices


@pytest.fixture
def device_map_env(tmp_path, monkeypatch):
    """Point the *default* map path at a temp file seeded with sa + nickname."""
    p = tmp_path / "devices_mgmt0.json"
    p.write_text(
        json.dumps(
            {
                "devices": {
                    "sa": {
                        "mgmt0": "10.0.0.1",
                        "expected_sns": ["SN-SA"],
                        "aliases": ["spine-a"],
                    },
                    "cl": {"mgmt0": "10.0.0.2", "expected_sns": ["SN-CL"]},
                }
            }
        )
    )
    monkeypatch.setenv("QACTL_DEVICES", str(p))
    return str(p)


def _load(path):
    return json.loads(open(path, encoding="utf-8").read())["devices"]


# --- atomic write ----------------------------------------------------------

def test_write_is_atomic_no_temp_left(device_map_env):
    dn_devices.update_device("sa", mgmt0="10.9.9.9", path=device_map_env)
    d = os.path.dirname(device_map_env)
    leftovers = [f for f in os.listdir(d) if f.startswith(".devices_") and f.endswith(".tmp")]
    assert leftovers == []
    assert _load(device_map_env)["sa"]["mgmt0"] == "10.9.9.9"


def test_write_replaces_in_place_valid_json(device_map_env):
    # Many writes in a row never leave the file unparseable.
    for i in range(5):
        dn_devices.update_device("cl", mgmt0=f"10.0.0.{i}", path=device_map_env)
    assert _load(device_map_env)["cl"]["mgmt0"] == "10.0.0.4"


# --- session helpers: canonicalize nickname before mutating ----------------

def test_remove_device_host_by_nickname_removes_canonical(device_map_env):
    from qactl.dnos.cli.core import session

    changed, remaining = session.remove_device_host("spine-a")
    assert changed is True
    assert remaining == []
    devices = _load(device_map_env)
    assert "sa" not in devices            # canonical actually removed
    assert "spine-a" not in devices       # and no ghost key forked


def test_remove_single_sn_by_nickname_hits_canonical(device_map_env):
    from qactl.dnos.cli.core import session

    session.save_device_host("spine-a", "SN-SA2")  # canonical sa now has 2 SNs
    changed, remaining = session.remove_device_host("spine-a", "SN-SA")
    assert changed is True
    assert remaining == ["SN-SA2"]
    devices = _load(device_map_env)
    assert devices["sa"]["expected_sns"] == ["SN-SA2"]
    assert "spine-a" not in devices


def test_save_device_host_by_nickname_no_ghost(device_map_env):
    from qactl.dnos.cli.core import session

    added, hosts = session.save_device_host("spine-a", "SN-SA2")
    assert added is True
    devices = _load(device_map_env)
    assert "spine-a" not in devices                       # no ghost canonical
    assert set(devices["sa"]["expected_sns"]) == {"SN-SA", "SN-SA2"}


def test_save_device_host_new_name_creates_canonical(device_map_env):
    from qactl.dnos.cli.core import session

    added, hosts = session.save_device_host("brand-new", "SN-NEW")
    assert added is True
    devices = _load(device_map_env)
    assert devices["brand-new"]["expected_sns"] == ["SN-NEW"]


def test_refresh_cache_keys_by_canonical(device_map_env):
    from qactl.dnos.cli.core import session

    session.reload_device_hosts()
    # touching via the nickname must update the canonical cache key, never
    # leave a stale nickname entry.
    session.save_device_host("spine-a", "SN-SA2")
    assert "SN-SA2" in session.DEVICE_HOSTS["sa"]
    assert "spine-a" not in session.DEVICE_HOSTS
