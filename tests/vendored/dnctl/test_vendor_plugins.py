"""Multi-vendor plugin layer: registry, dialects, and the capability gate.

These are all offline tests — no device traffic. The capability gate is
exercised end-to-end (a tool short-circuits with a structured
``not implemented`` envelope before any SSH is attempted), and the DNOS
dialect is asserted to reproduce the legacy prompt detection so the DNOS
transport path stays unchanged.
"""

import inspect

import pytest

from dnctl.cli.core import shell
from dnctl.cli.tools import clear as clear_tool
from dnctl.cli.tools import devices as devices_tool
from dnctl.cli.tools import discovery
from dnctl.cli import vendors as V


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DNCTL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("DNCTL_DEVICES", raising=False)
    yield


# --------------------------------------------------------------------------
# Registry + capabilities
# --------------------------------------------------------------------------

def test_supported_vendors():
    assert set(V.supported_vendors()) == {"dnos", "cisco", "juniper"}


def test_dnos_supports_every_capability():
    dnos = V.get_plugin("dnos")
    for cap in V.ALL_CAPABILITIES:
        assert dnos.supports(cap), cap


def test_cisco_and_juniper_are_show_only():
    for name in ("cisco", "juniper"):
        plugin = V.get_plugin(name)
        assert plugin.supports(V.CAP_SHOW)
        assert not plugin.supports(V.CAP_CONFIGURE)
        assert not plugin.supports(V.CAP_CLEAR)
        assert not plugin.supports(V.CAP_RESTART)


def test_unknown_or_missing_vendor_falls_back_to_dnos():
    assert V.get_plugin(None).name == "dnos"
    assert V.get_plugin("").name == "dnos"
    assert V.get_plugin("arista").name == "dnos"


def test_vendor_resolution_is_case_insensitive():
    assert V.get_plugin("Cisco").name == "cisco"
    assert V.get_plugin("JUNIPER").name == "juniper"


# --------------------------------------------------------------------------
# Dialects: prompt detection per vendor
# --------------------------------------------------------------------------

def test_dnos_dialect_matches_legacy_prompt_detection():
    # The DNOS dialect must reproduce the pre-plugin (no-dialect) behaviour
    # exactly, so the DNOS transport path is unchanged.
    banner = "\r\nLAB-NCC0(21-Apr-2026-09:55:32)#"
    dnos = V.get_plugin("dnos").dialect
    assert shell.detect_prompt(banner) == shell.detect_prompt(banner, dialect=dnos)
    assert shell.detect_prompt(banner, dialect=dnos) == "LAB-NCC0#"


def test_cisco_dialect_detects_exec_and_config_prompts():
    cisco = V.get_plugin("cisco").dialect
    assert shell.detect_prompt("R1>", dialect=cisco) == "R1>"
    assert shell.detect_prompt("R1#", dialect=cisco) == "R1#"
    # config-mode context is normalised away so matching survives it
    assert shell.detect_prompt("R1(config)#", dialect=cisco) == "R1#"
    assert shell.detect_prompt("R1(config-if)#", dialect=cisco) == "R1#"


def test_cisco_dialect_detects_ios_xr_prompt():
    # IOS-XR carries a node/location prefix with '/' and ':' (verified
    # live against an ASR9k running 7.9.1).
    cisco = V.get_plugin("cisco").dialect
    p = "RP/0/RSP0/CPU0:cisco-asr9k#"
    assert shell.detect_prompt(p, dialect=cisco) == p
    assert shell.detect_prompt(
        "RP/0/RSP0/CPU0:cisco-asr9k(config)#", dialect=cisco
    ) == p


def test_juniper_dialect_detects_operational_prompt():
    juniper = V.get_plugin("juniper").dialect
    assert shell.detect_prompt("admin@mx204>", dialect=juniper) == "admin@mx204>"
    # juniper has no paren context to strip
    assert juniper.strip_paren_context is False


def test_cisco_dialect_does_not_match_dnos_prompt_as_juniper():
    # A bare DNOS prompt must not be read as a juniper user@host prompt.
    juniper = V.get_plugin("juniper").dialect
    assert shell.detect_prompt("LAB-NCC0#", dialect=juniper) is None


def test_page_off_commands_per_vendor():
    assert V.get_plugin("dnos").dialect.page_off == ("set cli-terminal-length 0",)
    assert V.get_plugin("cisco").dialect.page_off == ("terminal length 0",)
    assert "set cli screen-length 0" in V.get_plugin("juniper").dialect.page_off


# --------------------------------------------------------------------------
# Error detection per vendor
# --------------------------------------------------------------------------

def test_cisco_detects_invalid_input():
    is_err, lines = V.get_plugin("cisco").detect_error(
        "% Invalid input detected at '^' marker."
    )
    assert is_err and lines


def test_juniper_detects_error_prefix():
    is_err, _ = V.get_plugin("juniper").detect_error("error: syntax error")
    assert is_err


def test_clean_output_is_not_an_error():
    for name in ("dnos", "cisco", "juniper"):
        is_err, _ = V.get_plugin(name).detect_error("interface ge-0/0/0 is up")
        assert not is_err


# --------------------------------------------------------------------------
# Capability gate decorator
# --------------------------------------------------------------------------

def test_requires_blocks_unsupported_without_calling(monkeypatch):
    calls = []

    @V.requires(V.CAP_CONFIGURE)
    def fake_tool(device=None, host=None):
        calls.append(device)
        return {"status": "ok"}

    monkeypatch.setattr(
        "dnctl.cli.vendors.gate.plugin_for_device",
        lambda device, host=None: V.get_plugin("cisco"),
    )
    resp = fake_tool(device="r1")
    assert resp["status"] == "error"
    assert resp["unsupported"] is True
    assert resp["vendor"] == "cisco"
    assert resp["capability"] == V.CAP_CONFIGURE
    assert calls == []  # the tool body never ran


def test_requires_allows_supported(monkeypatch):
    @V.requires(V.CAP_SHOW)
    def fake_tool(device=None, host=None):
        return {"status": "ok", "ran": True}

    monkeypatch.setattr(
        "dnctl.cli.vendors.gate.plugin_for_device",
        lambda device, host=None: V.get_plugin("cisco"),
    )
    assert fake_tool(device="r1")["ran"] is True


def test_requires_preserves_signature():
    # O.call / FastMCP introspect the wrapped signature.
    params = list(inspect.signature(discovery.show).parameters)
    assert params[:3] == ["command", "device", "host"]


# --------------------------------------------------------------------------
# End-to-end: a registered cisco device is gated for non-show tools
# --------------------------------------------------------------------------

def _add_cisco(sn="10.0.0.1"):
    return devices_tool.manage_device(operation="add", sn=sn, vendor="cisco")


def test_clear_is_not_implemented_on_cisco_device():
    _add_cisco("10.0.0.1")
    resp = clear_tool.clear(command="clear arp", device="10.0.0.1")
    assert resp["status"] == "error"
    assert resp["unsupported"] is True
    assert resp["vendor"] == "cisco"
    assert any("not implemented" in e for e in resp["errors"])


def test_show_passes_gate_on_cisco_device(monkeypatch):
    # show is supported on cisco, so the gate must let it through to the
    # device runner (which we stub out — no SSH in tests).
    _add_cisco("10.0.0.2")
    monkeypatch.setattr(
        discovery, "_run_on_device",
        lambda *a, **k: {"status": "ok", "stub": True},
    )
    resp = discovery.show(command="show version", device="10.0.0.2")
    assert resp.get("stub") is True
    assert not resp.get("unsupported")


def test_clear_passes_gate_on_dnos_host(monkeypatch):
    # A host-only call (no registry device) resolves to DNOS, which
    # supports clear — the gate must let it through.
    monkeypatch.setattr(
        clear_tool, "_run_on_device",
        lambda *a, **k: {"status": "ok", "stub": True},
    )
    resp = clear_tool.clear(command="clear arp", host="1.2.3.4")
    assert resp.get("stub") is True
