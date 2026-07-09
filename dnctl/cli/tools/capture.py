"""``qactl cli capture`` — native packet capture on DNOS devices.

Replaces the external ``dn_capture.py`` script with an in-tree, agent-safe
command. Two modes, matching the original:

- ``routing`` (control-plane / routing-engine): a ``timeout``-bounded
  ``tcpdump`` in the routing-engine container's ``inband_ns`` — captures
  in-band control-plane traffic (BGP/179, BFD, ISIS, ICMP, …). No device
  config or physical prerequisite.
- ``datapath``: the NCP ``wbox-cli`` pcap engine with a ``/tmp`` free-space
  preflight and a size cap. **Lab prerequisite (not automated):** datapath
  capture needs a physical loop cable (or a DNAAS mirror chain) steering
  datapath packets into the capture.

Wins over the standalone script:

1. **Multi-device** — ``-d cl -d sa`` captures on several registry devices
   concurrently, one pcap per device.
2. **No external hop** — the pcap egresses straight to *this* host over the
   existing device→local-sftp path (same endpoint ``cli backup`` uses),
   instead of bouncing through ``zkeiserman-dev``.
3. **Registry-aware** — host / creds come from the qactl registry, not a
   separate ``dn_devices.json``.

Contract: ``--json`` lossless envelope with per-device sub-results,
non-zero exit if any device fails, ``--yes`` for the destructive bits
(both modes write the device ``/tmp``; datapath also toggles ``wbox-cli``
state).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from dnctl.cli.core import capture_helpers as H
from dnctl.cli.core import capture_store
from dnctl.cli.core.capture_driver import (
    datapath_capture_on_channel,
    routing_capture_on_channel,
)
from dnctl.cli.core.envelope import error_response, make_response
from dnctl.cli.core.registry import transport_registry
from dnctl.cli.core.session import (
    DEFAULT_CMD_TIMEOUT,
    DEFAULT_PASSWORD,
    DEFAULT_USER,
    ConnectError,
    connect_error_next_actions,
    run_capture,
    run_once,
)
from dnctl.core.local_sftp import (
    LOCAL_SFTP_HOST,
    LOCAL_SFTP_PORT,
    LOCAL_SFTP_USER,
    LocalSftpNotConfigured,
    require_password,
)
from dnctl.cli.vendors import CAP_SHELL, requires

# A libpcap file with no packets is the 24-byte global header. Anything
# smaller means a truncated / failed transfer; exactly 24 is a valid but
# empty capture (warned, not failed).
_PCAP_MIN_BYTES = 24

# OOB management namespaces the device egress scp runs inside — cluster
# first, then standalone; the first that exists on the box is used.
_OOB_NAMESPACES = ["oob_ncc_ns", "oob_ns"]

CAPTURE_NEXT_ACTION = (
    "Check the device is reachable, the local-sftp endpoint is configured "
    "(`qactl setup --check-local-sftp`), and — for --mode datapath — that a "
    "loop cable / mirror chain is steering packets into the capture."
)

_NCP_SHOW_CMD = (
    'show config services | flatten | include "services port-mirroring session"'
)


def _resolve_ncp(
    device: str, user: str, password: str, timeout: int,
) -> tuple[str, Optional[str]]:
    """Resolve the datapath NCP from port-mirroring config (best-effort).

    Returns ``(ncp, warning)``. Falls back to ``"0"`` (standalone default)
    with a warning when the show can't be read or parsed.
    """
    try:
        inv = run_once(
            transport_registry, device=device, host=None,
            user=user, password=password, command=_NCP_SHOW_CMD, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - degrade to default
        return "0", f"NCP auto-detect failed ({exc}); defaulting to ncp 0."
    ncp = H.resolve_ncp_from_port_mirroring(inv.output)
    if ncp:
        return ncp, None
    return "0", "could not auto-detect NCP from port-mirroring; defaulting to ncp 0."


def _apply_local_filter(src: str, bpf: str) -> tuple[Optional[str], Optional[str]]:
    """Apply a BPF ``--filter`` locally to the landed pcap (``tcpdump -r``).

    Writes a sibling ``<stem>_filtered.pcap`` and returns
    ``(filtered_path, warning)``. The raw pcap is always kept. Returns
    ``(None, <warning>)`` when tcpdump is absent or the rewrite fails.

    The captures dir lives under ``~/.local/state`` — a *dot-directory* the
    stock Ubuntu ``tcpdump`` AppArmor profile explicitly denies (``audit deny
    @{HOME}/.*/** mrwkl``), so reading/writing the pcap there fails with
    "Permission denied" even though the file is owner-readable. We therefore
    stage the tcpdump read+write through a ``/tmp`` tempdir (allowed by the
    profile) and move the result into the captures dir with plain Python I/O,
    which is not AppArmor-confined.
    """
    if not shutil.which("tcpdump"):
        return None, "tcpdump not found on this host; --filter not applied."
    stem, ext = os.path.splitext(src)
    dst = f"{stem}_filtered{ext or '.pcap'}"
    pext = ext or ".pcap"
    try:
        with tempfile.TemporaryDirectory(prefix="qactl-bpf-") as td:
            tmp_src = os.path.join(td, f"in{pext}")
            tmp_dst = os.path.join(td, f"out{pext}")
            shutil.copyfile(src, tmp_src)
            proc = subprocess.run(
                H.build_local_bpf_cmd(tmp_src, tmp_dst, bpf),
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or "").strip().splitlines()
                return None, "local --filter failed: " + (detail[-1] if detail else "tcpdump error")
            shutil.move(tmp_dst, dst)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"local --filter failed: {exc}"
    return dst, None


def _capture_one(
    target: str,
    *,
    is_host: bool,
    mode: str,
    duration_s: Optional[int],
    name: str,
    bpf: Optional[str],
    iface: str,
    ncp: Optional[str],
    user: str,
    password: str,
    timeout: int,
    local_pw: str,
) -> Dict[str, Any]:
    """Run one device's capture and land + verify the pcap locally.

    ``target`` is a registry alias (``is_host=False``) or a raw host/IP
    (``is_host=True``); either way it also names the local pcap folder.
    """
    warnings: List[str] = []
    device = target if not is_host else None
    host = target if is_host else None
    folder = target
    pcap_name = H.make_pcap_name(name, folder)
    device_pcap = f"/tmp/{pcap_name}"

    store_err = capture_store.validate_device(folder)
    if store_err:
        return _sub(folder, "error", errors=[f"invalid target name: {store_err}"])
    try:
        capture_store.ensure_dir(device=folder)
    except (OSError, ValueError) as exc:
        return _sub(folder, "error", errors=[f"local capture dir unavailable: {exc}"])

    remote = capture_store.remote_path(pcap_name, device=folder)
    egress = H.build_scp_egress_cmd(
        device_pcap,
        user=LOCAL_SFTP_USER, host=LOCAL_SFTP_HOST,
        remote_dir=os.path.dirname(remote),
        port=int(LOCAL_SFTP_PORT or 22),
        netns_candidates=_OOB_NAMESPACES,
    )

    resolved_ncp = ncp
    if mode == "datapath" and not resolved_ncp and not is_host:
        resolved_ncp, ncp_warn = _resolve_ncp(device, user, password, timeout)
        if ncp_warn:
            warnings.append(ncp_warn)
    if mode == "datapath" and not resolved_ncp:
        resolved_ncp = "0"

    def _driver(channel, prompt, chost, dev):  # noqa: ANN001 - paramiko channel
        if mode == "routing":
            return routing_capture_on_channel(
                channel, prompt, device_host=chost, password=password,
                pcap_path=device_pcap, duration=int(duration_s),
                egress_cmd=egress, egress_password=local_pw, cmd_timeout=timeout,
                bpf=bpf, iface=iface,
            )
        return datapath_capture_on_channel(
            channel, prompt, ncp=str(resolved_ncp), password=password,
            pcap_path=device_pcap, duration=duration_s,
            egress_cmd=egress, egress_password=local_pw, cmd_timeout=timeout,
        )

    try:
        result = run_capture(
            transport_registry, device=device, host=host,
            user=user, password=password, driver=_driver,
        )
    except ConnectError as exc:
        return _sub(
            folder, "connect_error", errors=[str(exc)],
            next_actions=connect_error_next_actions(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return _sub(folder, "error", errors=[str(exc)])

    warnings.extend(result.get("warnings") or [])
    if not result.get("egress_ok"):
        return _sub(
            folder, "error",
            errors=[result.get("error") or "capture failed"],
            warnings=warnings, stages=result.get("stages"),
            next_actions=[CAPTURE_NEXT_ACTION],
        )

    stat = capture_store.stat_pcap(pcap_name, device=folder)
    if stat is None:
        return _sub(
            folder, "error",
            errors=[
                "egress reported success but the pcap is not present on this "
                f"host at {remote} — check the local sshd landing dir."
            ],
            warnings=warnings, next_actions=[CAPTURE_NEXT_ACTION],
        )
    if stat.size_bytes < _PCAP_MIN_BYTES:
        return _sub(
            folder, "error",
            errors=[f"landed pcap is only {stat.size_bytes} B (truncated transfer)."],
            warnings=warnings, next_actions=[CAPTURE_NEXT_ACTION],
        )
    if stat.size_bytes == _PCAP_MIN_BYTES:
        warnings.append("pcap contains no packets (header only).")

    # routing mode applies the BPF on the device (raw already scoped), so
    # there's no separate local re-filter to do. datapath mode has no
    # device-side BPF knob → fall back to a local post-download filter.
    filtered_path = None
    if bpf and mode == "datapath":
        filtered_path, filt_warn = _apply_local_filter(stat.path, bpf)
        if filt_warn:
            warnings.append(filt_warn)

    return _sub(
        folder, "ok",
        pcap_path=stat.path, bytes=stat.size_bytes,
        filtered_path=filtered_path, filter=bpf,
        mode=mode, ncp=(str(resolved_ncp) if mode == "datapath" else None),
        warnings=warnings, stages=result.get("stages"),
    )


def _sub(device: str, status: str, **extra: Any) -> Dict[str, Any]:
    """Build a per-device sub-result dict."""
    out: Dict[str, Any] = {"device": device, "status": status}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


@requires(CAP_SHELL)
def capture_devices(
    devices: Optional[List[str]] = None,
    host: Optional[str] = None,
    mode: str = "routing",
    duration: object = 30,
    name: str = "capture",
    bpf_filter: Optional[str] = None,
    iface: str = "any",
    ncp: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Capture packets on one or more DNOS devices; land one pcap per device.

    Args:
        devices: device aliases to capture on (repeatable ``-d``). Runs
            concurrently, one pcap per device. Either ``devices`` or
            ``host`` is required — there is no "capture the whole fleet"
            default (too expensive / destructive to trigger implicitly).
        host: raw host/IP for a single ad-hoc target (alternative to a
            registered ``devices`` entry).
        mode: ``routing`` (control-plane, default) or ``datapath``.
        duration: capture seconds (positive int), or ``inf``/``0`` for
            "as long as possible" — clamped to
            :func:`capture_helpers.max_duration_s` since a one-shot capture
            can't be Ctrl+C'd.
        name: pcap filename prefix; final name is
            ``<name>_<device>_<YYYYmmdd_HHMMSS>.pcap``.
        bpf_filter: a BPF filter (e.g. ``host 1.2.3.4``). For **routing**
            mode it is applied **on the device** so the landed pcap is
            already scoped; for **datapath** mode (no device BPF knob) it is
            applied locally after download (``tcpdump -r``), writing a
            sibling ``*_filtered.pcap`` and keeping the raw one.
        iface: routing mode only — tcpdump interface inside ``inband_ns``
            (default ``any``). ``any`` double-counts each packet across
            netns legs; pin the sub-if (e.g. ``g07008.0009``) for one clean
            copy per packet.
        ncp: datapath NCP override; when unset it is auto-detected from the
            device's port-mirroring config (falling back to 0).
        user/password/timeout: SSH params (per-step timeout).

    Returns an envelope whose ``captures`` list carries per-device
    ``{device, status, pcap_path, bytes, ...}``. Overall ``status`` is
    ``error`` if any device failed (non-zero exit), else ``ok``/``warning``.
    """
    mode_err = H.validate_mode(mode)
    if mode_err:
        return error_response(mode_err, next_action=CAPTURE_NEXT_ACTION)

    name_err = H.validate_name(name)
    if name_err:
        return error_response(name_err, next_action=CAPTURE_NEXT_ACTION)

    duration_s, dur_err = H.parse_duration(duration)
    if dur_err:
        return error_response(dur_err, next_action=CAPTURE_NEXT_ACTION)
    infinite = duration_s is None
    top_warnings: List[str] = []
    if infinite:
        duration_s = H.max_duration_s()
        top_warnings.append(
            f"unbounded duration clamped to {duration_s}s (a one-shot capture "
            "cannot be stopped interactively)."
        )

    from dnctl.core import devices as _dn_devices

    # (target, is_host) pairs. Aliases resolve to canonical so the pcap
    # folder + SSH-host lookup use the one true name; a raw --host passes
    # through untouched. -d wins if both are given.
    alias_targets = [
        (_dn_devices.resolve_canonical(d) or d, False)
        for d in (devices or []) if d
    ]
    targets = alias_targets or ([(host, True)] if host else [])
    if not targets:
        return error_response(
            "no capture target — pass at least one -d/--device (or --host).",
            next_action=CAPTURE_NEXT_ACTION,
        )

    # Gate on the local-sftp password once, up front — the device dials back
    # into this host to land the pcap; without it every egress would hang.
    try:
        local_pw = require_password()
    except LocalSftpNotConfigured as exc:
        return error_response(str(exc), next_action=CAPTURE_NEXT_ACTION)

    dp_duration = None if (infinite and mode == "datapath") else duration_s

    def _one(pair: tuple) -> Dict[str, Any]:
        tgt, is_host = pair
        return _capture_one(
            tgt, is_host=is_host, mode=mode, duration_s=dp_duration,
            name=name, bpf=bpf_filter, iface=iface, ncp=ncp,
            user=user, password=password, timeout=timeout, local_pw=local_pw,
        )

    if len(targets) == 1:
        captures = [_one(targets[0])]
    else:
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            captures = list(pool.map(_one, targets))

    ok = [c for c in captures if c.get("status") == "ok"]
    failed = [c for c in captures if c.get("status") not in ("ok",)]
    per_device_warn = any(c.get("warnings") for c in captures)

    if failed:
        status = "error"
    elif top_warnings or per_device_warn:
        status = "warning"
    else:
        status = "ok"

    return make_response(
        status=status, device=None, host="",
        command=f"cli capture --mode {mode}",
        warnings=top_warnings,
        next_actions=[CAPTURE_NEXT_ACTION] if failed else [],
        mode=mode,
        duration_s=duration_s,
        captures=captures,
        capture_count=len(ok),
        failed_count=len(failed),
    )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(capture_devices)
