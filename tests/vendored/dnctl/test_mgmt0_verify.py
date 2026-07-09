"""Issue #71 — verify the cached mgmt0 against the live chassis before
opening nc / gnmi / rc sessions.

A stale cached mgmt0 can point at a different box that still answers
NETCONF (observed on Hybrid-CL: every rpc-reply came back exit 0 while
the real chassis counted zero NETCONF sessions). The fix asks the chassis
itself for its CURRENT mgmt0 over the CLI transport (``show interfaces
management`` via the expected_sns SSH hosts), refreshes the registry on
mismatch, and REFUSES the session when verification can't run — the
probe authenticates with the cli group's own resolved creds (follow-up:
the first cut probed with empty creds and then proceeded unverified,
which is exactly the wrong-box failure mode the issue is about).

No device traffic — SSH is faked at the ``run_once`` seam.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qactl.dnctl.cli.core import mgmt0_verify


def _mgmt_table(ip: str) -> str:
    return (
        "| Interface | Admin state | Operational state | IPv4 Address | IPv6 Address |\n"
        f"| mgmt0     | enabled     | up                | {ip}/24      |              |\n"
    )


@pytest.fixture(autouse=True)
def _clear_memo():
    mgmt0_verify._recent.clear()
    yield
    mgmt0_verify._recent.clear()


@pytest.fixture
def device_map(tmp_path, monkeypatch):
    p = tmp_path / "devices_mgmt0.json"
    p.write_text(
        json.dumps(
            {
                "devices": {
                    "cl": {
                        "mgmt0": "10.0.0.1",
                        "expected_role": "CL",
                        "expected_sns": ["SN-CL-0", "SN-CL-1"],
                        "aliases": ["hybrid-cl"],
                    },
                    "bare": {"mgmt0": "10.0.0.2", "expected_role": "SA"},
                }
            }
        )
    )
    monkeypatch.setenv("DNCTL_DEVICES", str(p))
    return str(p)


def _devices_on_disk(path):
    return json.loads(open(path, encoding="utf-8").read())["devices"]


def _fake_run_once(ip: str, calls=None):
    def fake(registry, device, host, user, password, command, timeout):
        if calls is not None:
            calls.append((host, command))
        assert command == "show interfaces management"
        return SimpleNamespace(output=_mgmt_table(ip))
    return fake


# --- verifier unit behavior -------------------------------------------------

def test_match_is_verified_without_refresh(device_map, monkeypatch):
    monkeypatch.setattr(mgmt0_verify, "run_once", _fake_run_once("10.0.0.1"))
    out = mgmt0_verify.verify_device_mgmt0("cl", map_file=device_map)
    assert out.verified is True
    assert out.refreshed is False
    assert out.address == "10.0.0.1"
    assert out.warnings == []
    assert _devices_on_disk(device_map)["cl"]["mgmt0"] == "10.0.0.1"


def test_mismatch_refreshes_map_and_returns_live(device_map, monkeypatch):
    monkeypatch.setattr(mgmt0_verify, "run_once", _fake_run_once("10.9.9.9"))
    out = mgmt0_verify.verify_device_mgmt0("cl", map_file=device_map)
    assert out.verified is True
    assert out.refreshed is True
    assert out.address == "10.9.9.9"
    assert out.cached == "10.0.0.1"
    assert out.live == "10.9.9.9"
    # warning names BOTH addresses (issue #71: "fail loudly naming both")
    assert any("10.0.0.1" in w and "10.9.9.9" in w for w in out.warnings)
    assert _devices_on_disk(device_map)["cl"]["mgmt0"] == "10.9.9.9"


def test_unreachable_falls_back_to_cached_unverified(device_map, monkeypatch):
    def boom(registry, device, host, user, password, command, timeout):
        raise OSError("no route to host")
    monkeypatch.setattr(mgmt0_verify, "run_once", boom)
    out = mgmt0_verify.verify_device_mgmt0("cl", map_file=device_map)
    assert out.verified is False
    assert out.refreshed is False
    assert out.address == "10.0.0.1"
    assert any("UNVERIFIED" in w for w in out.warnings)
    assert _devices_on_disk(device_map)["cl"]["mgmt0"] == "10.0.0.1"


def test_second_sn_wins_when_first_fails(device_map, monkeypatch):
    def fake(registry, device, host, user, password, command, timeout):
        if host == "SN-CL-0":
            raise OSError("standby NCC unreachable")
        return SimpleNamespace(output=_mgmt_table("10.0.0.1"))
    monkeypatch.setattr(mgmt0_verify, "run_once", fake)
    out = mgmt0_verify.verify_device_mgmt0("cl", map_file=device_map)
    assert out.verified is True
    assert out.probed_host == "SN-CL-1"


def test_no_expected_sns_is_unverified(device_map, monkeypatch):
    def never(*a, **k):
        raise AssertionError("must not SSH without expected_sns")
    monkeypatch.setattr(mgmt0_verify, "run_once", never)
    out = mgmt0_verify.verify_device_mgmt0("bare", map_file=device_map)
    assert out.verified is False
    assert out.address == "10.0.0.2"
    assert any("expected_sns" in w for w in out.warnings)


def test_nickname_refreshes_canonical_entry(device_map, monkeypatch):
    monkeypatch.setattr(mgmt0_verify, "run_once", _fake_run_once("10.9.9.9"))
    out = mgmt0_verify.verify_device_mgmt0("hybrid-cl", map_file=device_map)
    assert out.device == "cl"
    devices = _devices_on_disk(device_map)
    assert devices["cl"]["mgmt0"] == "10.9.9.9"
    assert "hybrid-cl" not in devices  # no ghost canonical forked


def test_probe_resolves_creds_like_cli_group(device_map, monkeypatch):
    """Issue #71 follow-up: the probe must authenticate exactly like
    ``dnctl cli -d <device>`` — per-device/per-vendor creds resolved on
    the canonical alias — not with empty/unresolved creds."""
    seen = {}

    def fake_resolve(device, user, password):
        seen["resolve_args"] = (device, user, password)
        return "dev-user", "dev-pass"

    def fake_run(registry, device, host, user, password, command, timeout):
        seen["ssh_creds"] = (user, password)
        return SimpleNamespace(output=_mgmt_table("10.0.0.1"))

    monkeypatch.setattr(
        mgmt0_verify, "resolve_device_credentials", fake_resolve,
    )
    monkeypatch.setattr(mgmt0_verify, "run_once", fake_run)

    out = mgmt0_verify.verify_device_mgmt0("hybrid-cl", map_file=device_map)
    assert out.verified is True
    # Resolution is keyed on the CANONICAL alias with the cli group's own
    # defaults, so per-device [devices."cl"] creds layer exactly as they
    # do for `dnctl cli -d cl`.
    assert seen["resolve_args"] == (
        "cl", mgmt0_verify.DEFAULT_USER, mgmt0_verify.DEFAULT_PASSWORD,
    )
    assert seen["ssh_creds"] == ("dev-user", "dev-pass")


def test_require_verified_passes_through_verified_outcome():
    out = mgmt0_verify.Mgmt0Verification(
        device="cl", address="10.0.0.1", cached="10.0.0.1", verified=True,
    )
    assert mgmt0_verify.require_verified(out) is out


def test_require_verified_raises_with_diagnostics_and_escape_hatch():
    out = mgmt0_verify.Mgmt0Verification(
        device="cl", address="10.0.0.1", cached="10.0.0.1", verified=False,
        warnings=["CLI probe of ['SN-CL-0'] failed (auth)."],
    )
    with pytest.raises(mgmt0_verify.Mgmt0UnverifiedError) as ei:
        mgmt0_verify.require_verified(out)
    msg = str(ei.value)
    assert "CLI probe of ['SN-CL-0'] failed" in msg
    assert "--no-verify-mgmt0" in msg


def test_memo_probes_once_within_ttl(device_map, monkeypatch):
    calls = []
    monkeypatch.setattr(mgmt0_verify, "run_once", _fake_run_once("10.0.0.1", calls))
    mgmt0_verify.verify_device_mgmt0("cl", map_file=device_map)
    mgmt0_verify.verify_device_mgmt0("cl", map_file=device_map)
    assert len(calls) == 1
    mgmt0_verify.verify_device_mgmt0("cl", map_file=device_map, ttl=0)
    assert len(calls) == 2


# --- nc connect uses the verified address ----------------------------------

class _FakeMgr:
    def close_session(self):
        pass


def test_nc_connect_uses_live_mgmt0(device_map, monkeypatch):
    from qactl.dnctl.nc.core import session as ncsession

    monkeypatch.setattr(
        mgmt0_verify, "verify_device_mgmt0",
        lambda device, **kw: mgmt0_verify.Mgmt0Verification(
            device="cl", address="10.9.9.9", cached="10.0.0.1",
            live="10.9.9.9", verified=True, refreshed=True,
            warnings=["cached mgmt0='10.0.0.1' is stale"],
        ),
    )
    connected = []
    monkeypatch.setattr(
        ncsession, "_raw_connect",
        lambda host, port, user, password, hostkey_verify, timeout:
            connected.append(host) or _FakeMgr(),
    )
    monkeypatch.setattr(
        ncsession, "get_serial_numbers", lambda mgr, role: ["SN-CL-0"],
    )

    cr = ncsession.connect(device="cl", device_map_file=device_map)
    assert connected == ["10.9.9.9"]
    assert cr.host == "10.9.9.9"
    assert cr.mgmt0_verified is True
    assert cr.mgmt0_warnings == ["cached mgmt0='10.0.0.1' is stale"]
    assert cr.sn_verified is True


def test_nc_connect_refuses_unverified_mgmt0(device_map, monkeypatch):
    """Issue #71 follow-up: verification failure REFUSES the session —
    warn-and-proceed with the stale cached address was the exact failure
    mode the issue reported."""
    from qactl.dnctl.nc.core import session as ncsession

    monkeypatch.setattr(
        mgmt0_verify, "verify_device_mgmt0",
        lambda device, **kw: mgmt0_verify.Mgmt0Verification(
            device="cl", address="10.0.0.1", cached="10.0.0.1",
            verified=False,
            warnings=["could not verify cached mgmt0='10.0.0.1' ... UNVERIFIED."],
        ),
    )

    def never(*a, **k):
        raise AssertionError("must not open NETCONF to an unverified address")
    monkeypatch.setattr(ncsession, "_raw_connect", never)

    with pytest.raises(mgmt0_verify.Mgmt0UnverifiedError) as ei:
        ncsession.connect(device="cl", device_map_file=device_map)
    assert "--no-verify-mgmt0" in str(ei.value)


def test_nc_connect_fails_hard_on_verifier_crash(device_map, monkeypatch):
    from qactl.dnctl.nc.core import session as ncsession

    def crash(device, **kw):
        raise RuntimeError("paramiko exploded")
    monkeypatch.setattr(mgmt0_verify, "verify_device_mgmt0", crash)

    def never(*a, **k):
        raise AssertionError("must not connect after a verifier crash")
    monkeypatch.setattr(ncsession, "_raw_connect", never)

    with pytest.raises(RuntimeError, match="paramiko exploded"):
        ncsession.connect(device="cl", device_map_file=device_map)


# --- gnmi resolve_host uses the verified address ----------------------------

def test_gnmi_resolve_host_uses_live_mgmt0(device_map, monkeypatch):
    from qactl.dnctl.gnmi.core import session as gsession

    monkeypatch.setattr(
        mgmt0_verify, "verify_device_mgmt0",
        lambda device, **kw: mgmt0_verify.Mgmt0Verification(
            device="cl", address="10.9.9.9", cached="10.0.0.1",
            live="10.9.9.9", verified=True, refreshed=True,
            warnings=["stale mgmt0 refreshed"],
        ),
    )
    resolved = gsession.resolve_host(device="cl", host=None)
    assert resolved.host == "10.9.9.9"
    assert resolved.mgmt0_verified is True
    assert resolved.warnings == ["stale mgmt0 refreshed"]


def test_gnmi_resolve_host_refuses_unverified_mgmt0(device_map, monkeypatch):
    from qactl.dnctl.gnmi.core import session as gsession

    monkeypatch.setattr(
        mgmt0_verify, "verify_device_mgmt0",
        lambda device, **kw: mgmt0_verify.Mgmt0Verification(
            device="cl", address="10.0.0.1", cached="10.0.0.1",
            verified=False,
            warnings=["could not verify cached mgmt0='10.0.0.1' ... UNVERIFIED."],
        ),
    )
    with pytest.raises(mgmt0_verify.Mgmt0UnverifiedError) as ei:
        gsession.resolve_host(device="cl", host=None)
    assert "--no-verify-mgmt0" in str(ei.value)


def test_gnmi_resolve_host_skips_verification_for_raw_host(monkeypatch):
    from qactl.dnctl.gnmi.core import session as gsession

    def never(device, **kw):
        raise AssertionError("host= path must not CLI-probe")
    monkeypatch.setattr(mgmt0_verify, "verify_device_mgmt0", never)
    resolved = gsession.resolve_host(device=None, host="192.0.2.7")
    assert resolved.host == "192.0.2.7"
    assert resolved.warnings == []


# --- rc mount_add bakes the verified address into the ODL mount --------------

# --- --no-verify-mgmt0 escape hatch ------------------------------------------

def _forbid_verifier(monkeypatch):
    def never(device, **kw):
        raise AssertionError("verify_device_mgmt0 must not run with verify_mgmt0=False")
    monkeypatch.setattr(mgmt0_verify, "verify_device_mgmt0", never)


def test_nc_connect_skips_verification_when_disabled(device_map, monkeypatch):
    from qactl.dnctl.nc.core import session as ncsession

    _forbid_verifier(monkeypatch)
    monkeypatch.setattr(
        ncsession, "_raw_connect",
        lambda host, port, user, password, hostkey_verify, timeout: _FakeMgr(),
    )
    monkeypatch.setattr(
        ncsession, "get_serial_numbers", lambda mgr, role: ["SN-CL-0"],
    )

    cr = ncsession.connect(
        device="cl", device_map_file=device_map, verify_mgmt0=False,
    )
    assert cr.host == "10.0.0.1"
    assert cr.mgmt0_verified is False
    assert any("skipped" in w for w in cr.mgmt0_warnings)


def test_gnmi_resolve_host_skips_verification_when_disabled(device_map, monkeypatch):
    from qactl.dnctl.gnmi.core import session as gsession

    _forbid_verifier(monkeypatch)
    resolved = gsession.resolve_host(device="cl", host=None, verify_mgmt0=False)
    assert resolved.host == "10.0.0.1"
    assert resolved.mgmt0_verified is False
    assert any("skipped" in w for w in resolved.warnings)


def test_rc_mount_add_skips_verification_when_disabled(device_map, monkeypatch):
    from qactl.dnctl.rc.tools import mount as rcmount

    _forbid_verifier(monkeypatch)
    monkeypatch.setattr(
        rcmount, "get_endpoint",
        lambda name: {"kind": "odl", "base_url": "http://odl:8181", "auth": {}},
    )
    monkeypatch.setattr(rcmount, "get_device", lambda d: {"mgmt0": "10.0.0.1"})
    put_hosts = []
    monkeypatch.setattr(
        rcmount, "put_mount",
        lambda **kw: put_hosts.append(kw["host"]) or (201, "created"),
    )
    monkeypatch.setattr(
        rcmount, "wait_until_connected",
        lambda **kw: {"connection-status": "connected"},
    )

    env = rcmount.restconf_mount_add("cl", persist=False, verify_mgmt0=False)
    assert put_hosts == ["10.0.0.1"]
    assert any("skipped" in w for w in env["warnings"])


def test_ctx_flag_forwards_verify_mgmt0_to_tools():
    from qactl.dnctl.core import options as O

    seen = {}

    def tool(device=None, verify_mgmt0=True, **_kw):
        seen["verify_mgmt0"] = verify_mgmt0
        return {"status": "ok"}

    O.call(tool, O.build_ctx(device="cl", no_verify_mgmt0=True))
    assert seen["verify_mgmt0"] is False
    O.call(tool, O.build_ctx(device="cl"))
    assert seen["verify_mgmt0"] is True


def test_rc_mount_add_refuses_unverified_mgmt0(device_map, monkeypatch):
    from qactl.dnctl.rc.tools import mount as rcmount

    monkeypatch.setattr(
        rcmount, "get_endpoint",
        lambda name: {"kind": "odl", "base_url": "http://odl:8181", "auth": {}},
    )
    monkeypatch.setattr(rcmount, "get_device", lambda d: {"mgmt0": "10.0.0.1"})
    monkeypatch.setattr(
        mgmt0_verify, "verify_device_mgmt0",
        lambda device, **kw: mgmt0_verify.Mgmt0Verification(
            device="cl", address="10.0.0.1", cached="10.0.0.1",
            verified=False,
            warnings=["could not verify cached mgmt0='10.0.0.1' ... UNVERIFIED."],
        ),
    )

    def never(**kw):
        raise AssertionError("must not mount an unverified address on ODL")
    monkeypatch.setattr(rcmount, "put_mount", never)

    env = rcmount.restconf_mount_add("cl", persist=False)
    assert env["status"] == "error"
    assert any("--no-verify-mgmt0" in e for e in env["errors"])


def test_rc_mount_add_uses_live_mgmt0(device_map, monkeypatch):
    from qactl.dnctl.rc.tools import mount as rcmount

    monkeypatch.setattr(
        rcmount, "get_endpoint",
        lambda name: {"kind": "odl", "base_url": "http://odl:8181", "auth": {}},
    )
    monkeypatch.setattr(rcmount, "get_device", lambda d: {"mgmt0": "10.0.0.1"})
    monkeypatch.setattr(
        mgmt0_verify, "verify_device_mgmt0",
        lambda device, **kw: mgmt0_verify.Mgmt0Verification(
            device="cl", address="10.9.9.9", cached="10.0.0.1",
            live="10.9.9.9", verified=True, refreshed=True,
            warnings=["stale mgmt0 refreshed"],
        ),
    )
    put_hosts = []
    monkeypatch.setattr(
        rcmount, "put_mount",
        lambda **kw: put_hosts.append(kw["host"]) or (201, "created"),
    )
    monkeypatch.setattr(
        rcmount, "wait_until_connected",
        lambda **kw: {"connection-status": "connected", "elapsed_s": 0.1,
                      "available_caps": 1, "unavailable_caps": 0,
                      "host": "10.9.9.9", "port": 830},
    )

    env = rcmount.restconf_mount_add("cl", persist=False)
    assert env["status"] in ("ok", "warning")
    assert put_hosts == ["10.9.9.9"]
    assert "stale mgmt0 refreshed" in env["warnings"]
