"""Unit tests for the pure-Python pcap de-duplication helper."""

import struct

from qactl.dnos.cli.core import capture_dedup as D

# LINKTYPE_LINUX_SLL2 — what modern `tcpdump -i any` emits. 20-byte cooked
# header; the ingress ifindex lives at bytes 4:8, so per-leg copies of the
# same frame differ in the header but share the network-layer payload.
_SLL2 = 276
_SLL = 113
_ETH = 1


def _global_header(linktype, magic=0xA1B2C3D4):
    return struct.pack("<IHHIIII", magic, 2, 4, 0, 65535, 0, linktype)


def _record(ts_sec, ts_usec, payload):
    return struct.pack("<IIII", ts_sec, ts_usec, len(payload), len(payload)) + payload


def _sll2(ifindex, l3):
    """A 20-byte SLL2 cooked header (only ifindex varies per leg) + L3 bytes."""
    hdr = struct.pack("<HHIHBB8s", 0x0800, 0, ifindex, 1, 0, 6, b"\x00" * 8)
    assert len(hdr) == 20
    return hdr + l3


def _count_records(data, linktype=_SLL2):
    off = D._GLOBAL_HEADER_LEN
    n = 0
    while off + D._RECORD_HEADER_LEN <= len(data):
        incl = struct.unpack("<I", data[off + 8:off + 12])[0]
        off += D._RECORD_HEADER_LEN + incl
        n += 1
    return n


def test_collapses_same_frame_across_netns_legs():
    l3 = b"\x45\x00" + b"IPPAYLOAD-A" + b"\x00" * 8
    body = (
        _record(100, 1000, _sll2(7, l3))       # leg 1 (sub-if)
        + _record(100, 1080, _sll2(9, l3))     # leg 2 (parent) — 80us later
        + _record(100, 1120, _sll2(3, l3))     # leg 3 — chained
    )
    raw = _global_header(_SLL2) + body
    out, kept, dropped = D.dedup_pcap_bytes(raw)
    assert (kept, dropped) == (1, 2)
    assert _count_records(out) == 1


def test_keeps_genuine_retransmit_outside_window():
    l3 = b"\x45\x00" + b"SAME-IP-PKT" + b"\x00" * 8
    body = (
        _record(100, 1000, _sll2(7, l3))
        + _record(100, 3000, _sll2(7, l3))     # 2ms later — a real resend
    )
    raw = _global_header(_SLL2) + body
    out, kept, dropped = D.dedup_pcap_bytes(raw)
    assert (kept, dropped) == (2, 0)
    assert out == raw  # nothing dropped → returned untouched


def test_distinct_frames_kept():
    body = (
        _record(100, 1000, _sll2(7, b"\x45\x00AAA"))
        + _record(100, 1010, _sll2(9, b"\x45\x00BBB"))
    )
    raw = _global_header(_SLL2) + body
    _out, kept, dropped = D.dedup_pcap_bytes(raw)
    assert (kept, dropped) == (2, 0)


def test_big_endian_and_sll_classic():
    # Big-endian magic + classic 16-byte SLL header, packet-type differs per
    # leg but the L3 payload is identical.
    l3 = b"\x45\x00" + b"CONTROLPLANE"
    def sll(pkttype, p):
        h = struct.pack(">HHH8sH", pkttype, 1, 6, b"\x00" * 8, 0x0800)
        assert len(h) == 16
        return h + p
    def rec(ts, us, payload):
        return struct.pack(">IIII", ts, us, len(payload), len(payload)) + payload
    gh = struct.pack(">IHHIIII", 0xA1B2C3D4, 2, 4, 0, 65535, 0, _SLL)
    raw = gh + rec(5, 100, sll(0, l3)) + rec(5, 150, sll(4, l3))
    _out, kept, dropped = D.dedup_pcap_bytes(raw)
    assert (kept, dropped) == (1, 1)


def test_nanosecond_resolution_window():
    # ns-resolution magic: divisor is 1e9, so 80_000 ns == 80us is inside 1ms.
    l3 = b"\x45\x00PKT"
    gh = struct.pack("<IHHIIII", 0xA1B23C4D, 2, 4, 0, 65535, 0, _SLL2)
    raw = gh + _record(1, 100_000, _sll2(7, l3)) + _record(1, 180_000, _sll2(9, l3))
    _out, kept, dropped = D.dedup_pcap_bytes(raw)
    assert (kept, dropped) == (1, 1)


def test_bad_magic_returned_untouched():
    raw = b"not a pcap file at all, really"
    out, kept, dropped = D.dedup_pcap_bytes(raw)
    assert out == raw and (kept, dropped) == (0, 0)


def test_truncated_record_kept_verbatim():
    # A header promising more bytes than are present: keep the remainder as-is.
    raw = _global_header(_SLL2) + struct.pack("<IIII", 1, 0, 999, 999) + b"short"
    out, kept, dropped = D.dedup_pcap_bytes(raw)
    assert out == raw and dropped == 0


def test_ethernet_single_iface_no_dupes():
    # A pinned iface lands EN10MB frames; distinct frames stay distinct.
    def eth(dst, p):
        return dst + b"\x00" * 6 + b"\x08\x00" + p
    body = (
        _record(1, 10, eth(b"\x01" * 6, b"one"))
        + _record(1, 20, eth(b"\x02" * 6, b"two"))
    )
    raw = _global_header(_ETH) + body
    _out, kept, dropped = D.dedup_pcap_bytes(raw)
    assert (kept, dropped) == (2, 0)
