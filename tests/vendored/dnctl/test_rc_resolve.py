"""RESTCONF device<->mount resolution (regression: stale MCP aliases).

The lifted ``rc`` group used to key its ODL mounts on the old short MCP
aliases (``cl`` / ``sa``) while the shared device map moved to canonical
System-Name keys (``OHADZS-CL`` / ``OHADZS-SA``). That drift silently
broke every device-addressed command (``rc get -d``, ``rc resolve``,
``rc mount status --device``, ``rc devices``). These tests lock the seed
data to real devices and prove the lookup canonicalises both sides.
"""

import json

import pytest


def test_seed_mounts_reference_known_devices():
    """Every mount in the bundled seed must name a device that exists.

    Guards against re-introducing a stale alias (the bug that left every
    device showing ``mounted: false``).
    """
    from qactl.dnos.core import paths

    eps = json.loads((paths.DATA_DIR / "restconf_endpoints.json").read_text())
    devmap = json.loads((paths.DATA_DIR / "devices_mgmt0.json").read_text())

    known = set(devmap.get("devices", {}))
    for entry in devmap.get("devices", {}).values():
        for alias in (entry.get("aliases") or []):
            known.add(alias)

    for ep_alias, cfg in eps.get("endpoints", {}).items():
        for mount_name, mcfg in (cfg.get("mounts") or {}).items():
            dev = mcfg.get("device")
            assert dev in known, (
                f"endpoint {ep_alias!r} mount {mount_name!r} points at "
                f"unknown device {dev!r} (stale MCP alias?)"
            )


@pytest.fixture
def rc_env(tmp_path, monkeypatch):
    """Isolated device map + RESTCONF endpoints registry."""
    devmap = tmp_path / "devices.json"
    devmap.write_text(
        json.dumps(
            {
                "devices": {
                    "BIG-CL": {"mgmt0": "1.1.1.1", "aliases": ["cl"]},
                    "BIG-SA": {"mgmt0": "2.2.2.2"},
                }
            }
        )
    )
    monkeypatch.setenv("QACTL_DEVICES", str(devmap))

    eps = tmp_path / "endpoints.json"

    from qactl.dnos.rc.core import registry

    monkeypatch.setattr(registry, "_ENDPOINTS_PATH", eps)
    return registry, eps


def _write_eps(path, mounts):
    path.write_text(
        json.dumps(
            {"endpoints": {"odl": {"kind": "odl", "base_url": "http://x", "mounts": mounts}}}
        )
    )


def test_find_mount_matches_canonical_and_alias(rc_env):
    registry, eps = rc_env
    _write_eps(eps, {"BIG-CL": {"device": "BIG-CL"}})

    # canonical name hits the mount
    assert registry.find_mount("BIG-CL")[1] == "BIG-CL"
    # a secondary alias resolves to the same mount
    assert registry.find_mount("cl")[1] == "BIG-CL"
    # an unknown device finds nothing
    assert registry.find_mount("ghost") == (None, None, None)


def test_find_mount_matches_when_stored_as_alias(rc_env):
    """A mount stored under an old alias still resolves to the canonical -d."""
    registry, eps = rc_env
    _write_eps(eps, {"M": {"device": "cl"}})

    assert registry.find_mount("cl")[1] == "M"
    assert registry.find_mount("BIG-CL")[1] == "M"


def test_resolve_ok_and_missing(rc_env):
    registry, eps = rc_env
    _write_eps(eps, {"BIG-CL": {"device": "BIG-CL"}})

    from qactl.dnos.rc.tools.devices import restconf_resolve

    ok = restconf_resolve(device="cl")
    assert ok["status"] == "ok"
    assert ok["result"]["endpoint"] == "odl"
    assert ok["result"]["mount_name"] == "BIG-CL"

    # known device, no mount -> CLI-shaped hint (no MCP tool-call syntax)
    missing = restconf_resolve(device="BIG-SA")
    assert missing["status"] == "error"
    hints = " ".join(missing["next_actions"])
    assert "qactl rc mount add" in hints
    assert "restconf_mount_add(" not in hints


def test_mount_status_resolves_device_alias(rc_env, monkeypatch):
    """Issue #73: ``mount status cl`` must query the CL-RC mount, not 'cl'."""
    registry, eps = rc_env
    _write_eps(eps, {"CL-RC": {"device": "cl"}})

    from qactl.dnos.rc.tools import mount as mount_tools

    queried = []

    def fake_status(*, node_id, **kw):
        queried.append(node_id)
        return {"http_status": 200, "connection-status": "connected"}

    monkeypatch.setattr(mount_tools, "get_node_status", fake_status)

    # positional arg as device alias resolves to the registered mount
    env = mount_tools.restconf_mount_status(mount_name="cl", endpoint="odl")
    assert env["status"] == "ok"
    assert queried == ["CL-RC"]

    # canonical device name resolves too
    env = mount_tools.restconf_mount_status(mount_name="BIG-CL", endpoint="odl")
    assert queried[-1] == "CL-RC"

    # an exact mount name is passed through untouched
    env = mount_tools.restconf_mount_status(mount_name="CL-RC", endpoint="odl")
    assert queried[-1] == "CL-RC"

    # a name that is neither mount nor alias still goes to ODL as-is
    env = mount_tools.restconf_mount_status(mount_name="ghost", endpoint="odl")
    assert queried[-1] == "ghost"
