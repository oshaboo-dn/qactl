"""Pure-Python de-duplication for routing-mode ``-i any`` captures.

A ``qactl cli capture --mode routing`` runs ``tcpdump -i any`` inside the
routing-engine ``inband_ns``. ``-i any`` sees *every* interface in that
namespace, so a single control-plane frame is recorded **2-3×** — once per
netns leg it crosses (a sub-interface AND its parent, etc.). Wireshark then
flags the copies as dup-ACKs and every dissection has to be hand-deduped by
(timestamp, direction, message), which is error-prone and has bitten us
repeatedly.

This module collapses those copies **after download**, entirely in Python
(no ``tcpdump``/``scapy`` dependency, so it is not AppArmor-confined and is
unit-testable without a lab device). It parses the classic libpcap file
format, strips the link-layer (SLL/SLL2/Ethernet) header off each record so
the per-leg differences (SLL2 carries the ingress ``ifindex``; SLL carries
the packet-type) don't defeat the match, and drops any record whose
network-layer payload is byte-identical to one already kept within a small
time window (default 1 ms). The raw pcap is always preserved; the deduped
copy is written to a ``*_dedup.pcap`` sibling.

Only the legacy ``.pcap`` format is handled (what ``tcpdump -w`` writes by
default). A pcapng file, an unknown magic, or an unparsable body is returned
untouched so we never corrupt a capture we don't fully understand.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

# libpcap global-header magics (µs vs ns timestamp resolution, either
# endianness). The nanosecond variants only change the fractional divisor.
_MAGICS = {
    0xA1B2C3D4: ("<", 1_000_000),   # µs, little-endian
    0xD4C3B2A1: (">", 1_000_000),   # µs, big-endian
    0xA1B23C4D: ("<", 1_000_000_000),  # ns, little-endian
    0x4D3CB2A1: (">", 1_000_000_000),  # ns, big-endian
}

# Link-layer header length (bytes) for the link types ``tcpdump -i any`` can
# emit. Stripping this off leaves the network-layer packet, which is what is
# identical across netns legs.
#   1   LINKTYPE_ETHERNET   (a single pinned iface — no dupes, but handle it)
#   113 LINKTYPE_LINUX_SLL  (classic "any"; 16-byte cooked header)
#   276 LINKTYPE_LINUX_SLL2 (modern "any", libpcap >= 1.10; 20-byte header)
_LL_HEADER_LEN: Dict[int, int] = {1: 14, 113: 16, 276: 20}

_GLOBAL_HEADER_LEN = 24
_RECORD_HEADER_LEN = 16

# Default proximity window: netns legs of the *same* frame land within
# microseconds of each other, so 1 ms comfortably groups them while leaving a
# genuine retransmit (>1 ms later) as its own packet.
_DEFAULT_WINDOW_S = 0.001


def _parse_global_header(data: bytes) -> Optional[Tuple[str, int, int]]:
    """Return ``(endian, frac_divisor, linktype)`` or ``None`` if not a pcap."""
    if len(data) < _GLOBAL_HEADER_LEN:
        return None
    magic = struct.unpack("<I", data[:4])[0]
    spec = _MAGICS.get(magic)
    if spec is None:
        return None
    endian, divisor = spec
    linktype = struct.unpack(endian + "I", data[20:24])[0]
    return endian, divisor, linktype


def dedup_pcap_bytes(
    data: bytes, window_s: float = _DEFAULT_WINDOW_S,
) -> Tuple[bytes, int, int]:
    """De-duplicate ``-i any`` copies in a raw ``.pcap`` byte string.

    Returns ``(deduped_bytes, kept, dropped)``. A record is dropped when its
    network-layer payload (link-layer header stripped) is byte-identical to
    one already kept whose timestamp is within ``window_s`` seconds — i.e.
    the same frame seen on another netns leg. The global header and every
    kept record are preserved verbatim.

    On any format we don't fully understand (bad magic, truncated record,
    unexpected length), the input is returned unchanged with ``dropped=0`` so
    a capture is never corrupted.
    """
    parsed = _parse_global_header(data)
    if parsed is None:
        return data, 0, 0
    endian, divisor, linktype = parsed
    ll_len = _LL_HEADER_LEN.get(linktype, 0)

    out: List[bytes] = [data[:_GLOBAL_HEADER_LEN]]
    # Payload key -> timestamp (seconds) of the last kept/collapsed copy.
    last_seen: Dict[bytes, float] = {}
    kept = dropped = 0
    off = _GLOBAL_HEADER_LEN
    n = len(data)

    while off < n:
        if off + _RECORD_HEADER_LEN > n:
            # Trailing garbage / truncated record header: keep the remainder
            # verbatim rather than guess, and stop.
            out.append(data[off:])
            break
        ts_sec, ts_frac, incl_len, orig_len = struct.unpack(
            endian + "IIII", data[off:off + _RECORD_HEADER_LEN],
        )
        rec_end = off + _RECORD_HEADER_LEN + incl_len
        if rec_end > n:
            out.append(data[off:])
            break
        record = data[off:rec_end]
        payload = data[off + _RECORD_HEADER_LEN + ll_len:rec_end]
        ts = ts_sec + ts_frac / divisor

        prev = last_seen.get(payload)
        if prev is not None and abs(ts - prev) <= window_s:
            # Same frame on another leg (or a chained 3rd leg): drop, but
            # advance the window anchor so a burst collapses to one copy.
            dropped += 1
            last_seen[payload] = ts
        else:
            out.append(record)
            last_seen[payload] = ts
            kept += 1
        off = rec_end

    if dropped == 0:
        return data, kept, 0
    return b"".join(out), kept, dropped


__all__ = ["dedup_pcap_bytes"]
