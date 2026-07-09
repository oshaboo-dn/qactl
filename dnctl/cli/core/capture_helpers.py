"""Pure helpers for ``qactl cli capture`` — no device / channel I/O.

Ported from the standalone ``dn_capture.py`` (Zohar Keiserman): container
discovery, NCP-from-port-mirroring parsing, the device-side command
strings (tcpdump / wbox-cli / scp egress), the ``df`` / ``stat`` output
parsers, and the argument + filename validators. Everything here is a
pure function of its inputs, so it is unit-testable without a lab device;
the on-channel driving lives in :mod:`dnctl.cli.core.capture_driver` and
the orchestration in :mod:`dnctl.cli.tools.capture`.
"""

from __future__ import annotations

import os
import re
import shlex
from datetime import datetime
from typing import List, Optional, Tuple

# Capture modes the tool exposes. ``routing`` is the control-plane
# (routing-engine) capture; ``datapath`` drives the wbox-cli pcap engine
# on an NCP.
MODES: Tuple[str, ...] = ("routing", "datapath")

# Tokens that mean "run until stopped" in the original tool. In qactl's
# one-shot, non-interactive model there is no Ctrl+C, so an infinite
# capture is capped to :func:`max_duration_s` (the caller warns).
_INF_TOKENS = frozenset({"inf", "infinite", "forever", "0"})

# pcap filename prefix: keep it filesystem- and shell-safe. The final
# name is ``<prefix>_<device>_<YYYYmmdd_HHMMSS>.pcap``.
_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")

# routing-engine container names bake the serial in, e.g.
# ``CZ22500CW4_routing-engine.abc.def`` — the trailing dotted segments
# vary. (Verbatim from dn_capture.py.)
_ROUTING_ENGINE_CONTAINER_RE = re.compile(
    r"([\w-]+_routing-engine\.[a-z0-9]+\.[a-z0-9]+)", re.IGNORECASE,
)


def _env_int(name: str, legacy: Optional[str], default: int) -> int:
    """Positive int from ``name`` (or the legacy dn_capture env), else default."""
    for key in (name, legacy):
        if not key:
            continue
        raw = os.environ.get(key)
        if raw is None:
            continue
        try:
            val = int(raw)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return default


def max_pcap_mb() -> int:
    """Device-side pcap size cap (MB) for datapath capture.

    Mirrors the original ``DN_MAX_PCAP_MB`` knob (default 10 GB); also
    settable via ``QACTL_CAPTURE_MAX_PCAP_MB``.
    """
    return _env_int("QACTL_CAPTURE_MAX_PCAP_MB", "DN_MAX_PCAP_MB", 10240)


def min_free_gb() -> int:
    """Minimum free space on the device ``/tmp`` before a datapath capture.

    Mirrors the original ``DN_MIN_FREE_GB`` knob (default 15 GB); also
    settable via ``QACTL_CAPTURE_MIN_FREE_GB``.
    """
    return _env_int("QACTL_CAPTURE_MIN_FREE_GB", "DN_MIN_FREE_GB", 15)


def max_duration_s() -> int:
    """Hard cap applied to an ``inf``/``0`` (unbounded) duration request.

    qactl captures are one-shot and non-interactive — there is no Ctrl+C
    to stop an unbounded capture — so ``inf`` is clamped to this bound
    (default 1 hour; ``QACTL_CAPTURE_MAX_DURATION_S`` to override).
    """
    return _env_int("QACTL_CAPTURE_MAX_DURATION_S", None, 3600)


# --- argument validation ---------------------------------------------------


def validate_mode(mode: str) -> Optional[str]:
    """Return an error string if ``mode`` is not a known capture mode."""
    if mode not in MODES:
        return f"mode must be one of {list(MODES)} (got {mode!r})."
    return None


def parse_duration(raw: object) -> Tuple[Optional[int], Optional[str]]:
    """Parse ``--duration`` into ``(seconds, error)``.

    Returns ``(None, None)`` for the unbounded tokens (``inf`` / ``0`` /
    …) — the caller clamps that to :func:`max_duration_s`. A positive
    integer (as ``int`` or digit string) returns ``(seconds, None)``.
    Anything else returns ``(None, "<error>")``.
    """
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        return None, f"duration must be a positive integer or 'inf' (got {raw!r})."
    if isinstance(raw, int):
        if raw == 0:
            return None, None
        if raw > 0:
            return raw, None
        return None, f"duration must be a positive integer or 'inf' (got {raw!r})."
    s = str(raw).strip().lower()
    if s in _INF_TOKENS:
        return None, None
    if s.isdigit():
        n = int(s)
        return (None, None) if n == 0 else (n, None)
    return None, f"duration must be a positive integer or 'inf' (got {raw!r})."


def validate_name(prefix: str) -> Optional[str]:
    """Return an error string if the pcap filename ``prefix`` is illegal."""
    if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
        return (
            "name (pcap prefix) must be 1-40 chars of [A-Za-z0-9._-] and "
            f"start alphanumeric (got {prefix!r})."
        )
    return None


def make_pcap_name(
    prefix: str, device: str, when: Optional[datetime] = None,
) -> str:
    """Build ``<prefix>_<device>_<YYYYmmdd_HHMMSS>.pcap`` (local time).

    The device segment lets a multi-device capture land one distinct,
    self-describing pcap per device.
    """
    ts = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{device}_{ts}.pcap"


# --- discovery / resolution parsers ---------------------------------------


def find_routing_engine_container(
    docker_output: str, device_host: Optional[str] = None,
) -> Optional[str]:
    """Pick the routing-engine container from ``docker ps`` output.

    Prefers a container whose serial prefix matches ``device_host`` (the
    name bakes in the serial, not the mgmt IP); otherwise returns the
    sole match, or the first when several are present. ``None`` if none
    match. (Ported from ``dn_capture.find_routing_engine_container``.)
    """
    if not docker_output:
        return None
    if device_host:
        m = re.search(
            re.escape(device_host) + r"_routing-engine\.[a-z0-9]+\.[a-z0-9]+",
            docker_output, re.IGNORECASE,
        )
        if m:
            return m.group(0)
    matches: List[str] = []
    for m in _ROUTING_ENGINE_CONTAINER_RE.findall(docker_output):
        if m not in matches:
            matches.append(m)
    return matches[0] if matches else None


def resolve_ncp_from_port_mirroring(output: str) -> Optional[str]:
    """Extract the NCP number from ``port-mirroring`` config output.

    Tries the same ordered patterns as the original tool (most specific
    first). Returns the NCP index as a string, or ``None`` if nothing
    matched. (Ported from ``dn_capture.detect_ncp_from_port_mirroring``,
    parse-only.)
    """
    if not output:
        return None
    for pat in (
        r"destination-interface\s+ge\d+-(\d+)/\d+/\d+",
        r"source-interface\s+ge\d+-(\d+)/\d+/\d+",
        r"ge\d+-(\d+)/\d+/\d+",
        r"ge\d+-(\d+)/",
    ):
        m = re.search(pat, output, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1)
    # Last-ditch: any X/0/Y where X is a plausible NCP index (< 100).
    for cand in re.findall(r"(\d+)/0/\d+", output):
        if int(cand) < 100:
            return cand
    return None


# --- device-side command builders -----------------------------------------


def build_re_tcpdump_cmd(
    container: str,
    pcap_path: str,
    duration: int,
    bpf: Optional[str] = None,
    iface: str = "any",
) -> str:
    """Control-plane capture line: tcpdump in the RE container's inband_ns.

    ``timeout <duration>`` makes it self-terminating (no Ctrl+C needed).

    ``iface`` selects the tcpdump interface (default ``any``). ``any`` sees
    every in-band interface but **double-counts** each packet — the same
    frame is recorded on every netns leg it crosses (a sub-if AND its
    parent, etc.), so Wireshark flags the copies as dup-ACKs. Pin a single
    interface (e.g. the sub-if ``g07008.0009`` for ``ge400-7/0/8.9``) to get
    exactly one copy per packet — what the CPU actually sent/received.

    ``bpf`` (optional) is appended as the trailing tcpdump filter
    expression, applied **on the device** so the pcap that lands is already
    scoped (a routing capture otherwise grabs the whole control plane). It
    is passed as a single quoted argument.
    """
    cmd = (
        f"docker exec {shlex.quote(container)} ip netns exec inband_ns "
        f"timeout {int(duration)} tcpdump -nqe -i {shlex.quote(iface or 'any')} "
        f"-w {shlex.quote(pcap_path)}"
    )
    if bpf and bpf.strip():
        cmd += f" {shlex.quote(bpf.strip())}"
    return cmd


def build_wbox_open_cmd(pcap_path: str) -> str:
    """wbox-cli line that opens the datapath pcap sink at ``pcap_path``."""
    return f"wbox-cli debug open pcap file {shlex.quote(pcap_path)}"


# Fixed wbox-cli control lines (no user input → plain constants).
WBOX_ENABLE_DROPPED = "wbox-cli debug dropped_packets enable"
WBOX_DISABLE_DROPPED = "wbox-cli debug dropped_packets disable"
WBOX_CLOSE_PCAP = "wbox-cli close pcap"
WBOX_DEBUG_CLOSE_PCAP = "wbox-cli debug close pcap"


def build_scp_egress_cmd(
    pcap_path: str,
    *,
    user: str,
    host: str,
    remote_dir: str,
    port: int = 22,
    netns_candidates: Optional[List[str]] = None,
    remove_on_success: bool = True,
) -> str:
    """Render the in-shell scp that pushes the pcap to the local-sftp host.

    The device is the scp client. When ``netns_candidates`` is given, the
    scp runs inside the first OOB management namespace that actually
    exists on the box (``oob_ncc_ns`` on a cluster, ``oob_ns`` on a
    standalone) — picked on-device so we don't need a separate
    deployment-type probe — exactly the namespaces the original tool
    switched into for mgmt reachability. It is a single command line, so
    the DNOS shell prompt returns cleanly afterwards (no nested
    interactive ``bash`` to track). The file lands at
    ``<user>@<host>:<remote_dir>/<basename>``. Host-key checking is
    disabled so the first-connect ``yes/no`` prompt can't hang the
    non-interactive session. On success the device-side pcap is removed
    (atomic move) so a crashed run can't leave ``/tmp`` full.

    Replaces the original tool's scp→zkeiserman-dev hop with qactl's
    device→local-sftp egress (same endpoint ``cli backup`` dials back
    into); the password is fed at the prompt by the driver.
    """
    remote = f"{user}@{host}:{shlex.quote(remote_dir.rstrip('/') + '/')}"
    port_opt = f"-P {int(port)} " if int(port) != 22 else ""
    scp = (
        f"scp {port_opt}-o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null {shlex.quote(pcap_path)} {remote}"
    )
    if netns_candidates:
        cands = " ".join(shlex.quote(n) for n in netns_candidates)
        # Pick the first namespace that exists, then exec scp in it. If none
        # match, $ns stays the last candidate and scp fails loudly.
        scp = (
            f'for ns in {cands}; do ip netns list 2>/dev/null | '
            f'grep -qw "$ns" && break; done; ip netns exec "$ns" {scp}'
        )
    if remove_on_success:
        return f"{scp} && rm -f {shlex.quote(pcap_path)}"
    return scp


def _parse_df_field(text: str, idx: int) -> Optional[int]:
    """Return column ``idx`` (used=2 / avail=3) from a ``df -B1`` data line.

    Scans for a line whose size/used/avail columns are all integers, so a
    wrapped device name or the header row is skipped. ``df -B1`` reports
    bytes, so no unit math is needed.
    """
    for line in (text or "").replace("\r", "").splitlines():
        parts = line.split()
        if (
            len(parts) >= 4
            and parts[1].isdigit()
            and parts[2].isdigit()
            and parts[3].isdigit()
        ):
            return int(parts[idx])
    return None


def parse_df_used_bytes(text: str) -> Optional[int]:
    """Used bytes on the filesystem from ``df -B1 <mount>`` output."""
    return _parse_df_field(text, 2)


def parse_df_free_bytes(text: str) -> Optional[int]:
    """Available bytes on the filesystem from ``df -B1 <mount>`` output."""
    return _parse_df_field(text, 3)


def parse_stat_size(text: str) -> Optional[int]:
    """File size in bytes from ``stat -c %s <file>`` output (first int line)."""
    for line in (text or "").replace("\r", "").splitlines():
        s = line.strip()
        if s.isdigit():
            return int(s)
    return None


def build_local_bpf_cmd(src: str, dst: str, bpf: str) -> List[str]:
    """argv for a local ``tcpdump -r`` re-write applying a BPF filter.

    Used for **datapath** captures, whose wbox-cli path has no BPF knob, so
    ``--filter`` is applied on egress: read ``src``, write matching packets
    to ``dst``. (routing captures filter on the device instead — see
    ``build_re_tcpdump_cmd``.) Returns an argv list (no shell), so the BPF
    is passed as one argument.
    """
    return ["tcpdump", "-r", src, "-w", dst, bpf]


__all__ = [
    "MODES",
    "max_pcap_mb",
    "min_free_gb",
    "max_duration_s",
    "validate_mode",
    "parse_duration",
    "validate_name",
    "make_pcap_name",
    "find_routing_engine_container",
    "resolve_ncp_from_port_mirroring",
    "build_re_tcpdump_cmd",
    "build_wbox_open_cmd",
    "parse_df_used_bytes",
    "parse_df_free_bytes",
    "parse_stat_size",
    "WBOX_ENABLE_DROPPED",
    "WBOX_DISABLE_DROPPED",
    "WBOX_CLOSE_PCAP",
    "WBOX_DEBUG_CLOSE_PCAP",
    "build_scp_egress_cmd",
    "build_local_bpf_cmd",
]
