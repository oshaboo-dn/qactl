"""Unit tests for the pure capture helpers (no device I/O)."""

from datetime import datetime

import pytest

from qactl.dnos.cli.core import capture_helpers as H


# --- argument validation ---------------------------------------------------


def test_validate_mode():
    assert H.validate_mode("routing") is None
    assert H.validate_mode("datapath") is None
    assert "mode must be" in H.validate_mode("bogus")


@pytest.mark.parametrize("raw,expect", [
    (20, 20), ("20", 20), (30, 30),
    (0, None), ("0", None), ("inf", None), ("INFINITE", None), ("forever", None),
])
def test_parse_duration_ok(raw, expect):
    secs, err = H.parse_duration(raw)
    assert err is None
    assert secs == expect


@pytest.mark.parametrize("raw", [-5, "-5", "abc", 3.5, True, False])
def test_parse_duration_bad(raw):
    secs, err = H.parse_duration(raw)
    assert err is not None
    assert secs is None


def test_validate_name():
    assert H.validate_name("capture") is None
    assert H.validate_name("bgp-bfd_1") is None
    assert H.validate_name("bad name") is not None       # space
    assert H.validate_name("-lead") is not None          # must start alnum
    assert H.validate_name("x" * 41) is not None         # too long
    assert H.validate_name("") is not None


def test_make_pcap_name():
    when = datetime(2026, 7, 9, 13, 45, 1)
    assert H.make_pcap_name("cap", "cl", when) == "cap_cl_20260709_134501.pcap"


# --- discovery / resolution -----------------------------------------------


def test_find_container_prefers_serial_match():
    out = (
        "aa CZ22500CW4_routing-engine.abc.def Up 3 days\n"
        "bb OTHER_routing-engine.ghi.jkl Up 3 days\n"
    )
    assert H.find_routing_engine_container(out, "CZ22500CW4") == \
        "CZ22500CW4_routing-engine.abc.def"


def test_find_container_falls_back_to_first():
    out = "aa SN1_routing-engine.abc.def Up\n"
    assert H.find_routing_engine_container(out, "10.0.0.1") == \
        "SN1_routing-engine.abc.def"


def test_find_container_none():
    assert H.find_routing_engine_container("no match here", "x") is None
    assert H.find_routing_engine_container("", None) is None


@pytest.mark.parametrize("text,expect", [
    ("Name: inband_ns", True),
    ("oob_ns\ninband_ns (id: 1)\nmgmt_ns", True),
    ("oob_ns\ndefault", False),
    ("", False),
])
def test_has_inband_ns(text, expect):
    assert H.has_inband_ns(text) is expect


@pytest.mark.parametrize("iface,expect", [
    ("ge100-0/0/0", "e00000"),
    ("ge100-0/0/1", "e00001"),
    ("ge100-0/0/10", "e00010"),
    ("GE100-0/0/5", "e00005"),   # case-insensitive
    ("any", "any"),              # default passes through
    ("e00003", "e00003"),        # already mapped passes through
    ("", "any"),
])
def test_map_cdnos_iface(iface, expect):
    assert H.map_cdnos_iface(iface) == expect


@pytest.mark.parametrize("text,expect", [
    ("destination-interface ge400-7/0/8", "7"),
    ("source-interface ge100-3/0/1", "3"),
    ("something ge400-18/0/8.9 blah", "18"),
    ("only 6/0/2 here", "6"),
    ("nothing useful", None),
])
def test_resolve_ncp(text, expect):
    assert H.resolve_ncp_from_port_mirroring(text) == expect


_SYS_STANDALONE = (
    "| Type | Id | Admin   | Operational | Model   |\n"
    "| NCC  | 0  |         | active-up   | NCP-40C |\n"
    "| NCP  | 0  | enabled | up          | NCP-40C |\n"
)
_SYS_CLUSTER = (
    "| NCC  | 0  |         | active-up   | X86      |\n"
    "| NCC  | 1  |         | standby-up  | X86      |\n"
    "| NCF  | 0  | enabled | up          | NCF-48CD |\n"
    "| NCP  | 1  | enabled | up          | NCP-40C  |\n"
    "| NCP  | 2  | enabled | up          | NCP-40C  |\n"
    "| NCM  | A0 |         | disconnected| NCM-48X  |\n"
)


@pytest.mark.parametrize("text,expect", [
    (_SYS_STANDALONE, [0]),
    (_SYS_CLUSTER, [1, 2]),           # no ncp 0; NCM A0 ignored (alpha id)
    ("nothing useful", []),
    ("", []),
])
def test_resolve_ncps_from_system(text, expect):
    assert H.resolve_ncps_from_system(text) == expect


# --- command builders ------------------------------------------------------


def test_build_re_tcpdump_cmd():
    cmd = H.build_re_tcpdump_cmd("CT_routing-engine.a.b", "/tmp/x.pcap", 20)
    assert "docker exec CT_routing-engine.a.b" in cmd
    assert "ip netns exec inband_ns" in cmd
    assert "timeout 20 tcpdump -nqe -i any -w /tmp/x.pcap" in cmd
    assert cmd.rstrip().endswith("/tmp/x.pcap")  # no trailing BPF when unset


def test_build_re_tcpdump_cmd_with_bpf():
    cmd = H.build_re_tcpdump_cmd("CT", "/tmp/x.pcap", 20, "host 1.2.3.4")
    # BPF appended after -w <file>, quoted as one trailing expression
    assert cmd.endswith("-w /tmp/x.pcap 'host 1.2.3.4'")


def test_build_re_tcpdump_cmd_blank_bpf_ignored():
    cmd = H.build_re_tcpdump_cmd("CT", "/tmp/x.pcap", 20, "   ")
    assert cmd.rstrip().endswith("/tmp/x.pcap")


def test_build_re_tcpdump_cmd_default_iface_any():
    cmd = H.build_re_tcpdump_cmd("CT", "/tmp/x.pcap", 20)
    assert "-i any" in cmd


def test_build_re_tcpdump_cmd_pinned_iface():
    cmd = H.build_re_tcpdump_cmd("CT", "/tmp/x.pcap", 20, None, "g07008.0009")
    assert "-i g07008.0009" in cmd
    assert "-i any" not in cmd


def test_build_re_tcpdump_cmd_iface_and_bpf():
    cmd = H.build_re_tcpdump_cmd("CT", "/tmp/x.pcap", 20, "host 1.2.3.4", "g07008.0009")
    assert "-i g07008.0009" in cmd
    assert cmd.endswith("'host 1.2.3.4'")


def test_build_re_tcpdump_cmd_cdnos_no_container():
    # container=None (cdnos): no `docker exec` prefix, runs in inband_ns direct.
    cmd = H.build_re_tcpdump_cmd(None, "/tmp/x.pcap", 20, None, "e00005")
    assert "docker exec" not in cmd
    assert cmd.startswith("ip netns exec inband_ns ")
    assert "timeout 20 tcpdump -nqe -i e00005 -w /tmp/x.pcap" in cmd


def test_build_re_tcpdump_cmd_cdnos_with_bpf():
    cmd = H.build_re_tcpdump_cmd(None, "/tmp/x.pcap", 20, "host 1.2.3.4", "e00000")
    assert "docker exec" not in cmd
    assert cmd.endswith("-w /tmp/x.pcap 'host 1.2.3.4'")


def test_build_wbox_open_cmd():
    assert H.build_wbox_open_cmd("/tmp/x.pcap") == \
        "wbox-cli debug open pcap file /tmp/x.pcap"


def test_build_scp_egress_picks_namespace_and_removes():
    cmd = H.build_scp_egress_cmd(
        "/tmp/x.pcap", user="dn", host="host.local",
        remote_dir="/state/captures/cli/cl",
        netns_candidates=["oob_ncc_ns", "oob_ns"],
    )
    assert "for ns in oob_ncc_ns oob_ns" in cmd
    assert 'ip netns exec "$ns" scp' in cmd
    assert "StrictHostKeyChecking=no" in cmd
    assert "dn@host.local:" in cmd
    assert cmd.rstrip().endswith("&& rm -f /tmp/x.pcap")


def test_build_scp_egress_nonstandard_port():
    cmd = H.build_scp_egress_cmd(
        "/tmp/x.pcap", user="dn", host="h", remote_dir="/d", port=2222,
    )
    assert "scp -P 2222 " in cmd


def test_build_local_bpf_cmd():
    argv = H.build_local_bpf_cmd("/in.pcap", "/out.pcap", "tcp port 179")
    assert argv == ["tcpdump", "-r", "/in.pcap", "-w", "/out.pcap", "tcp port 179"]


# --- output parsers --------------------------------------------------------


DF = (
    "Filesystem      1B-blocks         Used       Avail Use% Mounted on\n"
    "/dev/sda1  1000000000000  50000000000  900000000000   6% /tmp\n"
)


def test_parse_df():
    assert H.parse_df_used_bytes(DF) == 50000000000
    assert H.parse_df_free_bytes(DF) == 900000000000
    assert H.parse_df_free_bytes("garbage") is None


def test_parse_stat_size():
    assert H.parse_stat_size("40960\n") == 40960
    assert H.parse_stat_size("stat: cannot stat\n") is None


# --- env knobs -------------------------------------------------------------


def test_env_knobs(monkeypatch):
    monkeypatch.setenv("QACTL_CAPTURE_MAX_PCAP_MB", "2048")
    monkeypatch.setenv("DN_MIN_FREE_GB", "5")
    monkeypatch.setenv("QACTL_CAPTURE_MAX_DURATION_S", "120")
    assert H.max_pcap_mb() == 2048
    assert H.min_free_gb() == 5
    assert H.max_duration_s() == 120


def test_env_knob_defaults(monkeypatch):
    for k in ("QACTL_CAPTURE_MAX_PCAP_MB", "DN_MAX_PCAP_MB",
              "QACTL_CAPTURE_MIN_FREE_GB", "DN_MIN_FREE_GB"):
        monkeypatch.delenv(k, raising=False)
    assert H.max_pcap_mb() == 10240
    assert H.min_free_gb() == 15
