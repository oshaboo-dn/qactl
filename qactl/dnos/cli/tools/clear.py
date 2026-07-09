"""``clear`` MCP tool.

Single generic wrapper for DNOS ``clear ...`` operational commands —
the state-clearing counterpart of :func:`qactl.cli.tools.discovery.show`.

Like ``show``, this tool is hard-gated: the input must start with the
literal verb ``clear`` and include at least one subcommand. There is no
per-subtree argument schema — pass the full command verbatim, exactly
as emitted by ``cli_crawler(path='clear ...')``. The tool deliberately
does not introspect the subcommand because the DNOS ``clear`` tree is
broad (arp / bgp / isis / evpn / interfaces / qos / mpls / ldp / rsvp /
mgmt / system / …) and most leaves take free-text identifiers
(neighbor IP, interface name, VRF, instance, MAC, VNI) that have no
useful pre-flight check on the MCP side.

Unlike ``show``, every ``clear`` call mutates device runtime state
(ARP table flush, BGP soft/hard clear, IS-IS adjacency reset, EVPN MAC
flush, counter zeroing, …). There is no commit / rollback — once the
prompt comes back, the side-effect has happened.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from qactl.dnos.cli.core.envelope import error_response
from qactl.dnos.cli.core.errors import CLEAR_NEXT_ACTION
from qactl.dnos.cli.core.runner import _run_on_device
from qactl.dnos.cli.core.session import DEFAULT_CMD_TIMEOUT, DEFAULT_PASSWORD, DEFAULT_USER
from qactl.dnos.cli.core.validation import _validate_clear_command
from qactl.dnos.cli.vendors import CAP_CLEAR, requires


@requires(CAP_CLEAR)
def clear(
    command: str,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Run an operational ``clear`` command on the device.

    Pass the full command verbatim, exactly as emitted by
    ``cli_crawler(path='clear ...')``. The first token must be
    ``clear``; bare ``clear`` is rejected (every DNOS clear action
    needs at least one subcommand).

    **State-changing — no dry-run.** Every successful call mutates
    runtime state on the device (ARP / FIB / BGP session / IS-IS
    adjacency / EVPN MAC table / counters / …). There is no commit or
    rollback path; the only way to undo is to wait for the protocols
    to re-converge or to manually re-create the state. Always pin the
    exact target (peer IP, interface, VRF, instance, MAC) with
    ``cli_crawler`` first, especially for high-blast-radius leaves
    like ``clear bgp neighbor *`` or ``clear isis instance ... process``.

    Examples (each is a real DNOS leaf — discover deeper variants with
    ``cli_crawler(path='clear ...')``):

      - command="clear arp"
            -> flush the device-wide IPv4 ARP table.
      - command="clear arp interfaces ge100-0/0/1"
            -> flush ARP only on one interface (verify exact syntax
               with ``cli_crawler(path='clear arp interfaces')``).
      - command="clear bgp neighbor 10.0.0.1 soft in"
            -> soft-reconfig inbound for one BGP peer (no session
               teardown). Use ``cli_crawler(path='clear bgp neighbor')``
               to enumerate ``soft`` / hard-reset variants on this
               build.
      - command="clear isis neighbor"
            -> reset IS-IS adjacencies (instance / interface
               selectors live one level deeper — crawl
               ``clear isis neighbor`` for the exact grammar).
      - command="clear evpn mac-table"
            -> flush locally learned EVPN MAC addresses (per-instance
               or per-VLAN selectors via
               ``cli_crawler(path='clear evpn mac-table')``).
      - command="clear interfaces counters"
            -> zero interface counters chassis-wide.

    Configuration changes (``set`` / ``delete`` / ``commit``) must go
    through ``edit_config`` instead — this tool only runs operational
    ``clear`` commands.

    On DNOS errors ("% Unknown command", "Invalid input", "Incomplete
    command", etc.) the result is returned with ``status="error"`` and
    ``next_actions`` pointing at ``cli_crawler(path='clear ...')`` so
    the caller can pin the missing token before re-trying.

    Args:
        command: Full operational command, must start with ``clear``
            (e.g. ``clear arp``, ``clear bgp neighbor 1.2.3.4``,
            ``clear evpn mac-table``).
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-command timeout seconds.
    """
    full, err = _validate_clear_command(command)
    if err:
        return error_response(
            err, device=device, host=host, command=(command or "").strip(),
            next_action=CLEAR_NEXT_ACTION,
        )
    return _run_on_device(
        "clear", device, host, user, password,
        full, timeout, CLEAR_NEXT_ACTION,
    )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(clear)
