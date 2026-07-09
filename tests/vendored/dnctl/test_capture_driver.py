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
                     ls_reply="-rw-r--r-- 1 root root 40960 /tmp/x.pcap\r\n" + SHELL):
    def handler(data):
        s = data.strip()
        if s == "run start shell":
            return SHELL
        if s.startswith("docker ps"):
            return container_line + SHELL
        if s.startswith("docker exec"):        # tcpdump completes
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
    ch = ScriptedChannel(_routing_handler(container_line="nothing here\r\n"))
    res = D.routing_capture_on_channel(
        ch, DNOS, device_host="CZ1", password="devpw",
        pcap_path="/tmp/x.pcap", duration=1,
        egress_cmd="scp ...", egress_password=PW, cmd_timeout=2,
    )
    assert res["ok"] is False
    assert "container not found" in res["error"]


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
