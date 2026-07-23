"""On-channel capture drivers for ``qactl cli capture`` (routing + datapath).

These functions drive a single, already-initialised paramiko channel (a
fresh ``run start shell`` session) through the exact device-side steps the
standalone ``dn_capture.py`` performed — but shaped for qactl's one-shot,
non-interactive model (bounded ``timeout``/size cap instead of Ctrl+C) and
egressing to the local-sftp host instead of the zkeiserman-dev hop.

The channel is driven with the same low-level readers the rest of the CLI
uses (:mod:`qactl.cli.core.shell`), so prompt handling matches every other
shell-exec path. A driver returns a plain result dict — no envelope
shaping here; the tool layer (:mod:`qactl.cli.tools.capture`) verifies the
landed file and builds the per-device sub-result.

Note: the on-device sequencing is ported faithfully from the proven
standalone tool and covered by unit tests against a scripted fake channel,
but end-to-end behaviour needs a live DNOS device to confirm.
"""

from __future__ import annotations

import base64
import re
import shlex
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from qactl.dnos.cli.core import capture_helpers as H
from qactl.dnos.cli.core.shell import (
    ends_with_shell_prompt,
    read_until_prompt,
    read_until_shell_prompt,
    strip_ansi,
)

# Marker the RE / datapath shell entry prints once the Linux prompt is up.
_PASSWORD_RE_TEXT = "password:"

# base64-pull framing markers. Emitted via a shell variable + PID (``$$``)
# so the literal marker text appears only in the command *output*, never in
# the echoed command line (which shows ``${M}_BEG_$$`` unexpanded) — the
# parser can then match the real markers unambiguously.
_PULL_MARKER = "QACTLPCAP"
_PULL_RE = re.compile(
    _PULL_MARKER + r"_BEG_\d+\s+(.*?)\s+" + _PULL_MARKER + r"_END_\d+", re.S,
)


def _result(
    *,
    ok: bool,
    error: Optional[str] = None,
    egress_ok: bool = False,
    stages: Optional[List[str]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": ok,
        "error": error,
        "egress_ok": egress_ok,
        "stages": stages or [],
    }
    out.update(extra)
    return out


def _read_until_password_or_shell(
    channel: Any, overall_timeout: float,
) -> Tuple[str, str]:
    """Read until either a ``Password:`` challenge or a Linux shell prompt.

    Returns ``(text, kind)`` where ``kind`` is ``"password"``, ``"shell"``,
    or ``"timeout"``.
    """
    buf: List[str] = []
    start = time.time()
    while True:
        if channel.recv_ready():
            buf.append(channel.recv(65535).decode("utf-8", errors="replace"))
            text = "".join(buf)
            clean = strip_ansi(text)
            if clean.rstrip().lower().endswith(_PASSWORD_RE_TEXT):
                return text, "password"
            if ends_with_shell_prompt(text):
                return text, "shell"
        else:
            if time.time() - start > overall_timeout:
                return "".join(buf), "timeout"
            time.sleep(0.05)


def _enter_shell(
    channel: Any, entry: str, password: str, timeout: float,
) -> Tuple[bool, str]:
    """Enter ``run start shell`` (or a variant), answering the password prompt."""
    channel.send(entry + "\n")
    text, kind = _read_until_password_or_shell(channel, timeout)
    if kind == "password":
        channel.send(password + "\n")
        _, ok = read_until_shell_prompt(channel, overall_timeout=timeout)
        return ok, text
    if kind == "shell":
        return True, text
    return False, text


def _clean(raw: str, cmd: str) -> str:
    """Strip ANSI, the echoed command line, and the trailing shell prompt."""
    lines = strip_ansi(raw).replace("\x07", "").splitlines()
    stripped = cmd.strip()
    while lines and stripped and stripped in lines[0]:
        lines = lines[1:]
    while lines and lines[-1].strip() == "":
        lines.pop()
    if lines and lines[-1].rstrip().endswith("#"):
        lines.pop()
    return "\n".join(lines)


def _run(channel: Any, cmd: str, timeout: float) -> Tuple[str, bool]:
    """Send one Linux command; return ``(clean_output, hit_prompt)``."""
    channel.send(cmd + "\n")
    raw, hit = read_until_shell_prompt(channel, overall_timeout=timeout)
    return _clean(raw, cmd), hit


def _run_with_password(
    channel: Any,
    cmd: str,
    password: str,
    timeout: float,
    max_answers: int = 3,
) -> Tuple[str, bool]:
    """Run a command that may raise a ``Password:`` prompt (scp egress).

    Answers up to ``max_answers`` password prompts, then waits for the
    shell prompt. Returns ``(clean_output, hit_prompt)``.
    """
    channel.send(cmd + "\n")
    answers = 0
    buf: List[str] = []
    while True:
        text, kind = _read_until_password_or_shell(channel, timeout)
        buf.append(text)
        if kind == "password" and answers < max_answers:
            channel.send(password + "\n")
            answers += 1
            continue
        return _clean("".join(buf), cmd), (kind == "shell")


def _exit_shell(channel: Any, dnos_prompt: str, timeout: float) -> None:
    """Best-effort return to the DNOS prompt so the channel closes cleanly."""
    try:
        channel.send("exit\n")
        read_until_prompt(channel, dnos_prompt, overall_timeout=timeout)
    except Exception:
        pass


def _looks_like_error(text: str) -> bool:
    low = text.lower()
    return any(
        k in low
        for k in ("no such file", "permission denied", "denied",
                  "connection refused", "cannot", "not found", "failed")
    )


def _pull_via_base64(
    channel: Any,
    remote_path: str,
    local_path: str,
    timeout: float,
) -> Tuple[bool, Optional[str]]:
    """Pull a device file to ``local_path`` by streaming base64 over the shell.

    The containerlab cdnos node has no network path back to the agent (so the
    device→local-sftp scp push can't work) and DNOS SSH exposes no SFTP
    subsystem (so a paramiko ``open_sftp`` pull fails). The one channel that
    *does* work is the interactive shell we already hold — so ``base64 -w0``
    the pcap on the device, read the single line back between unique markers,
    and decode it locally. Returns ``(ok, error)``.
    """
    q = shlex.quote(remote_path)
    # ``M`` + ``$$`` keep the printed markers out of the echoed command line.
    cmd = (
        f"M={_PULL_MARKER}; echo ${{M}}_BEG_$$; base64 -w0 {q}; echo; "
        f"echo ${{M}}_END_$$"
    )
    channel.send(cmd + "\n")
    raw, hit = read_until_shell_prompt(channel, overall_timeout=timeout)
    if not hit:
        return False, "timed out reading pcap back over the shell channel."
    m = _PULL_RE.search(strip_ansi(raw))
    if not m:
        return False, "could not read the pcap back over the shell channel."
    b64 = re.sub(r"\s+", "", m.group(1))
    if not b64:
        return False, "pcap read back empty over the shell channel."
    try:
        data = base64.b64decode(b64, validate=True)
    except (ValueError, Exception) as exc:  # noqa: BLE001
        return False, f"failed to decode pcap stream: {exc}"
    try:
        with open(local_path, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        return False, f"failed to write pulled pcap locally: {exc}"
    return True, None


# --- routing (control-plane) ----------------------------------------------


def routing_capture_on_channel(
    channel: Any,
    dnos_prompt: str,
    *,
    device_host: Optional[str],
    password: str,
    pcap_path: str,
    duration: int,
    egress_cmd: str,
    egress_password: str,
    cmd_timeout: float = 30.0,
    bpf: Optional[str] = None,
    iface: str = "any",
    local_pcap_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Drive a control-plane (routing-engine) capture on one channel.

    Steps (faithful to ``dn_capture.routing_engine_capture``): enter
    ``run start shell``, find the routing-engine container, clear any
    stale target file, run a ``timeout``-bounded tcpdump in the RE's
    ``inband_ns``, verify the pcap exists, then scp it to the local-sftp
    host (via the OOB namespace) and remove it device-side on success.

    Two topologies are supported. A real NCC chassis runs DNOS in a nested
    ``routing-engine`` container discovered via ``docker ps`` (the tcpdump is
    ``docker exec``'d into it), and its scp egress runs inside an OOB
    management namespace (``egress_cmd``). A cdnos / single-container node *is*
    the container — there is no nested routing-engine container — so
    ``inband_ns`` is visible directly at the ``run start shell`` level; there
    the tcpdump runs in ``inband_ns`` directly and the port name is mapped
    ``ge100-0/0/N`` → ``e0000N``.

    Egress also differs. A cdnos containerlab node has no network path back to
    the agent (the device→local-sftp scp push can't reach it) and DNOS SSH
    exposes no SFTP subsystem, so the pcap is *pulled* to ``local_pcap_path``
    over this same shell channel via ``base64`` (:func:`_pull_via_base64`).
    An NCC chassis pushes with scp over its OOB namespace as before.
    """
    stages: List[str] = []

    ok, _ = _enter_shell(channel, "run start shell", password, cmd_timeout)
    if not ok:
        return _result(ok=False, error="could not enter `run start shell`.",
                       stages=stages)
    stages.append("entered run start shell")

    docker_out, hit = _run(channel, "docker ps | grep routing-engine", cmd_timeout)
    container = H.find_routing_engine_container(docker_out, device_host)
    cdnos = False
    if not container:
        # No nested routing-engine container: this may be a cdnos node, where
        # DNOS runs in a netns inside the single container and L3 lives in a
        # local ``inband_ns``. Detect that (inband_ns present at this level)
        # and capture there directly instead of failing.
        netns_out, _ = _run(channel, "ip netns list", cmd_timeout)
        if not H.has_inband_ns(netns_out):
            _exit_shell(channel, dnos_prompt, cmd_timeout)
            return _result(ok=False,
                           error="routing-engine container not found in `docker ps`.",
                           stages=stages)
        cdnos = True
    stages.append("cdnos inband_ns" if cdnos else f"container={container}")

    # Only remove the specific target path (never a blanket /tmp/*.pcap
    # sweep — that could clobber another capture on the same box).
    _run(channel, f"rm -f {pcap_path}", cmd_timeout)

    cap_iface = H.map_cdnos_iface(iface) if cdnos else iface
    tcpdump_cmd = H.build_re_tcpdump_cmd(
        None if cdnos else container, pcap_path, duration, bpf, cap_iface)
    # The command blocks for ~duration (self-terminating via `timeout`),
    # plus RE/tcpdump setup latency — give it a generous margin.
    _out, hit = _run(channel, tcpdump_cmd, timeout=duration + cmd_timeout + 15)
    if not hit:
        _exit_shell(channel, dnos_prompt, cmd_timeout)
        return _result(ok=False,
                       error=f"tcpdump did not complete within {duration}s (+margin).",
                       stages=stages, container=container)
    stages.append("tcpdump completed")

    ls_out, _ = _run(channel, f"ls -l {pcap_path}", cmd_timeout)
    if "No such file" in ls_out:
        _exit_shell(channel, dnos_prompt, cmd_timeout)
        return _result(ok=False, error="pcap not created on device.",
                       stages=stages, container=container)

    if cdnos:
        # Pull the pcap back over this channel (no push path exists), then
        # remove it device-side on success — the tool verifies the landed file.
        if not local_pcap_path:
            _exit_shell(channel, dnos_prompt, cmd_timeout)
            return _result(ok=False,
                           error="cdnos capture needs a local landing path to pull into.",
                           stages=stages)
        # base64 of the whole pcap streams over the channel; scale the read
        # window with the on-device size so a large capture isn't cut short.
        pull_timeout = max(cmd_timeout, 120) + duration
        egress_ok, pull_err = _pull_via_base64(
            channel, pcap_path, local_pcap_path, pull_timeout)
        if egress_ok:
            _run(channel, f"rm -f {pcap_path}", cmd_timeout)
        stages.append("pulled pcap over channel" if egress_ok else "pull failed")
        _exit_shell(channel, dnos_prompt, cmd_timeout)
        return _result(
            ok=egress_ok,
            error=None if egress_ok else (pull_err or "pcap pull over channel failed."),
            egress_ok=egress_ok,
            stages=stages,
            container=container,
        )

    # NCC chassis: scp push over the OOB management namespace.
    egress_out, eg_hit = _run_with_password(
        channel, egress_cmd, egress_password, timeout=max(cmd_timeout, 90),
    )
    egress_ok = eg_hit and not _looks_like_error(egress_out)
    stages.append("egress ok" if egress_ok else "egress failed")

    _exit_shell(channel, dnos_prompt, cmd_timeout)
    return _result(
        ok=egress_ok,
        error=None if egress_ok else "scp egress to local-sftp host failed.",
        egress_ok=egress_ok,
        stages=stages,
        container=container,
        egress_output=egress_out[-2000:],
    )


# --- datapath --------------------------------------------------------------


def datapath_capture_on_channel(
    channel: Any,
    dnos_prompt: str,
    *,
    ncp: str,
    password: str,
    pcap_path: str,
    duration: Optional[int],
    egress_cmd: str,
    egress_password: str,
    cmd_timeout: float = 30.0,
    min_free_gb: Optional[int] = None,
    max_pcap_mb: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Drive a datapath (wbox-cli) capture on one channel.

    Steps (faithful to ``dn_capture.datapath_capture``): enter
    ``run start shell ncp <n>``, remove leftovers, preflight ``/tmp`` free
    space, toggle ``wbox-cli debug dropped_packets``, open the pcap sink
    (retry on "already open"), hold for ``duration`` while polling the
    size against the cap, close the sink, verify, then scp to the
    local-sftp host and remove device-side on success.

    Lab prerequisite (NOT automated): datapath capture needs a physical
    loop cable (or a DNAAS mirror chain) steering datapath packets into
    the capture. If the sink opens but no bytes accrue, that wiring is the
    likely cause — surfaced as a warning in the result.
    """
    stages: List[str] = []
    min_free = min_free_gb if min_free_gb is not None else H.min_free_gb()
    max_mb = max_pcap_mb if max_pcap_mb is not None else H.max_pcap_mb()
    hold = duration if duration is not None else H.max_duration_s()

    ok, _ = _enter_shell(channel, f"run start shell ncp {ncp}", password, cmd_timeout)
    if not ok:
        return _result(ok=False,
                       error=f"could not enter `run start shell ncp {ncp}`.",
                       stages=stages)
    stages.append(f"entered datapath shell ncp {ncp}")

    _run(channel, f"rm -f {pcap_path}", cmd_timeout)

    # /tmp free-space preflight — bail cleanly rather than crash the CLI on
    # a full partition mid-capture.
    df_out, _ = _run(channel, "df -B1 /tmp", cmd_timeout)
    free_bytes = H.parse_df_free_bytes(df_out)
    if free_bytes is not None:
        free_gb = free_bytes / (1024 ** 3)
        if free_gb < min_free:
            _exit_shell(channel, dnos_prompt, cmd_timeout)
            return _result(
                ok=False,
                error=(
                    f"insufficient free space on /tmp: {free_gb:.1f} GB free, "
                    f"{min_free} GB required (cap {max_mb / 1024:.1f} GB). "
                    "Clean up /tmp/*.pcap on the device and retry."
                ),
                stages=stages,
            )
        stages.append(f"/tmp free {free_gb:.1f} GB")

    # Reset any prior sink, then enable dropped-packet capture.
    _run(channel, H.WBOX_CLOSE_PCAP, cmd_timeout)
    _run(channel, H.WBOX_DEBUG_CLOSE_PCAP, cmd_timeout)
    _run(channel, H.WBOX_DISABLE_DROPPED, cmd_timeout)
    _run(channel, H.WBOX_ENABLE_DROPPED, cmd_timeout)

    open_cmd = H.build_wbox_open_cmd(pcap_path)
    opened = False
    for _ in range(3):
        out, _ = _run(channel, open_cmd, cmd_timeout)
        low = out.lower()
        if "trying to open pcap when it is open" in low:
            _run(channel, H.WBOX_CLOSE_PCAP, cmd_timeout)
            _run(channel, H.WBOX_DEBUG_CLOSE_PCAP, cmd_timeout)
            continue
        if "error" in low and "pcap" in low:
            _exit_shell(channel, dnos_prompt, cmd_timeout)
            return _result(ok=False, error=f"wbox-cli failed to open pcap: {out[-300:]}",
                           stages=stages)
        opened = True
        break
    if not opened:
        _exit_shell(channel, dnos_prompt, cmd_timeout)
        return _result(ok=False, error="wbox-cli could not open the pcap sink.",
                       stages=stages)
    stages.append("wbox pcap opened")

    # Baseline /tmp usage, then hold — wbox-cli doesn't flush incrementally,
    # so size comes from a df-usage delta (fall back to file stat).
    du_out, _ = _run(channel, "df -B1 /tmp", cmd_timeout)
    initial_used = H.parse_df_used_bytes(du_out) or 0
    max_bytes = max_mb * 1024 * 1024
    size_cap_hit = False
    poll = 5
    waited = 0
    last_size = 0
    while waited < hold:
        sleep(min(poll, hold - waited))
        waited += poll
        st_out, _ = _run(channel, f"stat -c %s {pcap_path} 2>/dev/null", cmd_timeout)
        file_b = H.parse_stat_size(st_out) or 0
        du_out, _ = _run(channel, "df -B1 /tmp", cmd_timeout)
        used_now = H.parse_df_used_bytes(du_out)
        df_delta = 0 if used_now is None else max(0, used_now - initial_used)
        last_size = max(file_b, df_delta)
        if last_size >= max_bytes:
            size_cap_hit = True
            break
    stages.append(f"held ~{waited}s, ~{last_size} B")

    _run(channel, H.WBOX_DEBUG_CLOSE_PCAP, cmd_timeout)
    _run(channel, H.WBOX_DISABLE_DROPPED, cmd_timeout)

    ls_out, _ = _run(channel, f"ls -l {pcap_path}", cmd_timeout)
    if "No such file" in ls_out:
        _exit_shell(channel, dnos_prompt, cmd_timeout)
        return _result(ok=False, error="pcap not created on device.", stages=stages)

    warnings: List[str] = []
    if last_size == 0:
        warnings.append(
            "no bytes captured — datapath capture needs a physical loop cable "
            "(or a DNAAS mirror chain) steering packets into the capture; check "
            "the lab wiring."
        )

    egress_out, eg_hit = _run_with_password(
        channel, egress_cmd, egress_password, timeout=max(cmd_timeout, 120),
    )
    egress_ok = eg_hit and not _looks_like_error(egress_out)
    stages.append("egress ok" if egress_ok else "egress failed")

    _exit_shell(channel, dnos_prompt, cmd_timeout)
    return _result(
        ok=egress_ok,
        error=None if egress_ok else "scp egress to local-sftp host failed.",
        egress_ok=egress_ok,
        stages=stages,
        warnings=warnings,
        size_cap_hit=size_cap_hit,
        device_bytes=last_size,
        egress_output=egress_out[-2000:],
    )


__all__ = [
    "routing_capture_on_channel",
    "datapath_capture_on_channel",
]
