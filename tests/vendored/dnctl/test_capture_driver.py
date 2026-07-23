"""Unit tests for the on-channel capture drivers (scripted fake channel)."""

from qactl.dnos.cli.core import capture_driver as D

SHELL = "\r\n(dev)root@routing_engine:/[2026-01-01 00:00:00][inband_ns]# "
DNOS = "DEV#"
PW = "localpw"


class ScriptedChannel:
    """Fake paramiko channel: each ``send`` yields a scripted response.

    ``handler(sent_line)`` returns the bytes-worth of text the device would
    emit in reply, which the driver then reads via ``recv``.
    """

    def __init__(self, handler):
        self.handler = handler
        self._buf = b""
        self.sent = []

    def settimeout(self, _t):
        pass

    def send(self, data):
        self.sent.append(data)
        resp = self.handler(data)
        if resp:
            self._buf += resp.encode()
        return len(data)

    def recv_ready(self):
        return bool(self._buf)

    def recv(self, n):
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk

    def close(self):
        pass


def _routing_handler(*, container_line, egress_reply="\r\nx.pcap 100%\r\n" + SHELL,
                     ls_reply="-rw-r--r-- 1 root root 40960 /tmp/x.pcap\r\n" + SHELL,
                     netns_reply="default\r\n" + SHELL,
                     pull_payload=b"\xd4\xc3\xb2\xa1pcapbytes"):
    import base64 as _b64
    b64 = _b64.b64encode(pull_payload).decode()
    def handler(data):
        s = data.strip()
        if s == "run start shell":
            return SHELL
        if s.startswith("docker ps"):
            return container_line + SHELL
        if s.startswith("ip netns list"):      # cdnos topology probe
            return netns_reply
        if "base64 -w0" in s:                   # cdnos pull over channel
            return f"QACTLPCAP_BEG_42\r\n{b64}\r\n\r\nQACTLPCAP_END_42\r\n" + SHELL
        if s.startswith("docker exec") or "tcpdump" in s:   # tcpdump completes
            return SHELL
        if s.startswith("ls -l"):
            return ls_reply
        if s.startswith("rm -f"):
            return SHELL
        if s.startswith("for ns") or "scp" in s:
            return "\r\ndn@host's password: "
        if s == PW:
            return egress_reply
        if s == "exit":
            return "\r\nlogout\r\n" + DNOS
        return SHELL
    return handler


def test_routing_happy_path():
    ch = ScriptedChannel(_routing_handler(
        container_line="aa CZ1_routing-engine.a.b Up\r\n"))
    res = D.routing_capture_on_channel(
        ch, DNOS, device_host="CZ1", password="devpw",
        pcap_path="/tmp/x.pcap", duration=1,
        egress_cmd="for ns in oob_ncc_ns oob_ns; do :; done; scp /tmp/x.pcap dn@host:/d/",
        egress_password=PW, cmd_timeout=2,
    )
    assert res["ok"] is True
    assert res["egress_ok"] is True
    assert res["container"] == "CZ1_routing-engine.a.b"


def test_routing_container_not_found():
    # No routing-engine container AND no inband_ns netns → hard fail (not cdnos).
    ch = ScriptedChannel(_routing_handler(
        container_line="nothing here\r\n", netns_reply="default\r\n" + SHELL))
    res = D.routing_capture_on_channel(
        ch, DNOS, device_host="CZ1", password="devpw",
        pcap_path="/tmp/x.pcap", duration=1,
        egress_cmd="scp ...", egress_password=PW, cmd_timeout=2,
    )
    assert res["ok"] is False
    assert "container not found" in res["error"]


def test_routing_cdnos_no_container_pulls_over_channel(tmp_path):
    # cdnos: docker ps finds nothing, but inband_ns exists → capture direct
    # and pull the pcap back over the channel (no scp push path exists).
    payload = b"\xd4\xc3\xb2\xa1cdnos-pcap-bytes"
    local = tmp_path / "car1.pcap"
    ch = ScriptedChannel(_routing_handler(
        container_line="\r\n",
        netns_reply="oob_ns\r\ninband_ns (id: 1)\r\n" + SHELL,
        pull_payload=payload))
    res = D.routing_capture_on_channel(
        ch, DNOS, device_host="car1", password="devpw",
        pcap_path="/tmp/x.pcap", duration=1, iface="ge100-0/0/5",
        egress_cmd="scp /tmp/x.pcap dn@host:/d/", egress_password=PW,
        cmd_timeout=2, local_pcap_path=str(local),
    )
    assert res["ok"] is True
    assert res["egress_ok"] is True
    assert "cdnos inband_ns" in res["stages"]
    assert "pulled pcap over channel" in res["stages"]
    # tcpdump ran in inband_ns directly (no docker exec), mapped iface e00005.
    tcpdump = next(s for s in ch.sent if "tcpdump" in s)
    assert "docker exec" not in tcpdump
    assert tcpdump.startswith("ip netns exec inband_ns ")
    assert "-i e00005" in tcpdump
    # the pcap was pulled over the channel (base64), not scp-pushed.
    assert any("base64 -w0" in s for s in ch.sent)
    assert not any(s.strip().startswith("scp ") for s in ch.sent)
    # and the decoded bytes were written to the local landing path verbatim.
    assert local.read_bytes() == payload


def test_routing_cdnos_pull_failure_reports_error(tmp_path):
    # inband_ns present but the base64 read comes back with no markers → fail.
    ch = ScriptedChannel(_routing_handler(
        container_line="\r\n",
        netns_reply="inband_ns\r\n" + SHELL))
    # Override the pull reply to omit markers.
    orig = ch.handler
    def handler(data):
        if "base64 -w0" in data.strip():
            return "garbage no markers\r\n" + SHELL
        return orig(data)
    ch.handler = handler
    res = D.routing_capture_on_channel(
        ch, DNOS, device_host="car1", password="devpw",
        pcap_path="/tmp/x.pcap", duration=1,
        egress_cmd="scp ...", egress_password=PW, cmd_timeout=2,
        local_pcap_path=str(tmp_path / "x.pcap"),
    )
    assert res["ok"] is False
    assert res["egress_ok"] is False
    assert "channel" in res["error"]


def test_routing_egress_failure():
    ch = ScriptedChannel(_routing_handler(
        container_line="aa CZ1_routing-engine.a.b Up\r\n",
        egress_reply="\r\nConnection refused\r\n" + SHELL))
    res = D.routing_capture_on_channel(
        ch, DNOS, device_host="CZ1", password="devpw",
        pcap_path="/tmp/x.pcap", duration=1,
        egress_cmd="scp /tmp/x.pcap dn@host:/d/", egress_password=PW, cmd_timeout=2,
    )
    assert res["ok"] is False
    assert res["egress_ok"] is False


def test_routing_pcap_not_created():
    ch = ScriptedChannel(_routing_handler(
        container_line="aa CZ1_routing-engine.a.b Up\r\n",
        ls_reply="ls: cannot access '/tmp/x.pcap': No such file or directory\r\n" + SHELL))
    res = D.routing_capture_on_channel(
        ch, DNOS, device_host="CZ1", password="devpw",
        pcap_path="/tmp/x.pcap", duration=1,
        egress_cmd="scp ...", egress_password=PW, cmd_timeout=2,
    )
    assert res["ok"] is False
    assert "not created" in res["error"]


# --- datapath --------------------------------------------------------------

DF_OK = (
    "Filesystem 1B-blocks Used Avail Use% Mounted\n"
    "/dev/x 1000000000000 5000000000 900000000000 1% /tmp\n" + SHELL
)
DF_FULL = (
    "Filesystem 1B-blocks Used Avail Use% Mounted\n"
    "/dev/x 1000000000000 999000000000 1000000000 99% /tmp\n" + SHELL
)


def _datapath_handler(*, df_reply=DF_OK, stat_size="40960",
                      open_reply="opened pcap\r\n" + SHELL):
    def handler(data):
        s = data.strip()
        if s.startswith("run start shell ncp"):
            return SHELL
        if s.startswith("df -B1"):
            return df_reply
        if s.startswith("stat -c"):
            return stat_size + "\r\n" + SHELL
        if "open pcap file" in s:
            return open_reply
        if s.startswith("wbox-cli"):
            return SHELL
        if s.startswith("ls -l"):
            return "-rw-r--r-- 1 root root " + stat_size + " /tmp/x.pcap\r\n" + SHELL
        if s.startswith("rm -f"):
            return SHELL
        if s.startswith("for ns") or "scp" in s:
            return "\r\npassword: "
        if s == PW:
            return "\r\nx.pcap 100%\r\n" + SHELL
        if s == "exit":
            return "\r\n" + DNOS
        return SHELL
    return handler


def test_datapath_happy_path():
    ch = ScriptedChannel(_datapath_handler())
    res = D.datapath_capture_on_channel(
        ch, DNOS, ncp="7", password="devpw", pcap_path="/tmp/x.pcap",
        duration=1, egress_cmd="scp /tmp/x.pcap dn@host:/d/",
        egress_password=PW, cmd_timeout=2, sleep=lambda _s: None,
    )
    assert res["ok"] is True
    assert res["egress_ok"] is True
    assert res["device_bytes"] == 40960
    assert not res.get("warnings")


def test_datapath_preflight_full_tmp():
    ch = ScriptedChannel(_datapath_handler(df_reply=DF_FULL))
    res = D.datapath_capture_on_channel(
        ch, DNOS, ncp="0", password="devpw", pcap_path="/tmp/x.pcap",
        duration=1, egress_cmd="scp ...", egress_password=PW,
        cmd_timeout=2, sleep=lambda _s: None, min_free_gb=15,
    )
    assert res["ok"] is False
    assert "insufficient free space" in res["error"]


def test_datapath_no_bytes_warns():
    ch = ScriptedChannel(_datapath_handler(stat_size="0"))
    res = D.datapath_capture_on_channel(
        ch, DNOS, ncp="0", password="devpw", pcap_path="/tmp/x.pcap",
        duration=1, egress_cmd="scp /tmp/x.pcap dn@host:/d/",
        egress_password=PW, cmd_timeout=2, sleep=lambda _s: None,
    )
    # egress still succeeds, but a loop-cable warning is surfaced.
    assert any("loop cable" in w for w in res["warnings"])
