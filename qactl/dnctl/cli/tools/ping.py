"""``run_ping_ipv4`` MCP tool.

Wraps the DNOS ``run ping`` command with full grammar coverage and
range-checks every numeric argument against the device's CLI limits.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from qactl.dnctl.cli.core.envelope import error_response
from qactl.dnctl.cli.core.errors import RUN_PING_NEXT_ACTION
from qactl.dnctl.cli.core.runner import _run_on_device
from qactl.dnctl.cli.core.session import DEFAULT_PASSWORD, DEFAULT_USER
from qactl.dnctl.cli.core.validation import _int_in, _num_in, _validate_token
from qactl.dnctl.cli.vendors import CAP_PING, requires


# ``N% packet loss`` summary line emitted by the device's ping. 100% loss
# means every echo timed out — the command ran fine but the ping itself
# failed, so the envelope must NOT report success (the prompt came back
# cleanly, so detect_error sees nothing).
_PACKET_LOSS_RE = re.compile(r"(\d+(?:\.\d+)?)%\s*packet\s*loss", re.IGNORECASE)


def _ping_total_loss(output: str) -> bool:
    """True iff the ping summary reports 100% packet loss."""
    if not output:
        return False
    last = None
    for m in _PACKET_LOSS_RE.finditer(output):
        last = m.group(1)
    if last is None:
        return False
    try:
        return float(last) >= 100.0
    except ValueError:
        return False


@requires(CAP_PING)
def run_ping_ipv4(
    dest: str,
    device: Optional[str] = None,
    host: Optional[str] = None,
    count: Optional[int] = None,
    size: Optional[int] = None,
    interval: Optional[float] = None,
    vrf: Optional[str] = None,
    source_interface: Optional[str] = None,
    dscp: Optional[int] = None,
    df: bool = False,
    skip_source_check: bool = False,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """Run an IPv4 ICMP ping from the device, blocking until it finishes.

    Builds and executes a DNOS ``run ping`` command. The grammar supported by
    this tool mirrors the device:

        run ping <dest> [vrf <vrf>]
                        [count <count>] [size <size>] [interval <interval>]
                        [source-interface <source_interface>] [dscp <dscp>]
                        [df] [skip-source-check]

    Count / size / interval / dscp are range-checked to match the CLI limits
    (count 1..1000000, size 1..65507, interval 0.001..86400 sec, dscp 0..56).

    The MCP session-level timeout defaults to roughly
    ``ceil(count * interval) + 15s`` (min 30s) so that the command finishes
    and the prompt is seen before the read deadline. Override with ``timeout``
    if you pass a long-running combination.

    Args:
        dest: IPv4 address or hostname to ping.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        count: Number of echo requests (1..1000000, default device: 5).
        size: Payload size in bytes (1..65507).
        interval: Seconds between requests (0.001..86400).
        vrf: VRF name. On DNOS the management VRF is "mgmt0" (NOT "mgmt");
             the global/default VRF has no name (omit this arg).
        source_interface: Interface name used as source.
        dscp: DSCP value (0..56).
        df: Set the Don't-Fragment bit.
        skip_source_check: Skip the kernel source-address check.
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-command timeout seconds; defaults to count*interval + 15.
    """
    def _bad(msg: str) -> Dict[str, Any]:
        return error_response(
            msg, device=device, host=host, next_action=RUN_PING_NEXT_ACTION,
        )

    if (e := _validate_token("dest", dest)):
        return _bad(e)

    parts: List[str] = ["run", "ping", dest.strip()]

    if vrf is not None:
        if (e := _validate_token("vrf", vrf)):
            return _bad(e)
        parts += ["vrf", vrf.strip()]

    if count is not None:
        if (e := _int_in("count", count, 1, 1_000_000)):
            return _bad(e)
        parts += ["count", str(count)]

    if size is not None:
        if (e := _int_in("size", size, 1, 65507)):
            return _bad(e)
        parts += ["size", str(size)]

    if interval is not None:
        if (e := _num_in("interval", interval, 0.001, 86400)):
            return _bad(e)
        parts += ["interval", str(interval)]

    if source_interface is not None:
        if (e := _validate_token("source_interface", source_interface)):
            return _bad(e)
        parts += ["source-interface", source_interface.strip()]

    if dscp is not None:
        if (e := _int_in("dscp", dscp, 0, 56)):
            return _bad(e)
        parts += ["dscp", str(dscp)]

    if df:
        parts.append("df")
    if skip_source_check:
        parts.append("skip-source-check")

    command = " ".join(parts)

    if timeout is None:
        eff_count = count if count is not None else 5
        eff_interval = float(interval) if interval is not None else 1.0
        timeout = max(30, int(eff_count * eff_interval) + 15)

    response = _run_on_device(
        "run_ping_ipv4", device, host, user, password,
        command, timeout, RUN_PING_NEXT_ACTION,
    )
    # A clean prompt return with 100% packet loss is still a failed ping;
    # surface it instead of letting status stay "ok".
    if response.get("status") == "ok" and _ping_total_loss(response.get("stdout") or ""):
        response["status"] = "error"
        response.setdefault("errors", []).append(
            "ping reported 100% packet loss — the destination did not "
            "answer any echo request."
        )
        response.setdefault("next_actions", []).append(RUN_PING_NEXT_ACTION)
    return response


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(run_ping_ipv4)
