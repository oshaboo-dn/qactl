"""``run_command`` tool — issue an operational ``run ...`` command verbatim.

DNOS's ``run`` prefix reaches operational commands from either CLI mode.
The structured tools already model the common ones (``run ping`` →
``ping``, ``run start shell`` → ``shell``); this passthrough carries the
rest — ``traceroute`` and its ``traceroute mpls isis|bgp-car`` variants,
``monitor``, and other run-scope diagnostics — so their transcript can be
captured for evidence. It runs one command on an ephemeral channel and
returns the standard envelope.

Like ``show``, this is an operational read: it is NOT gated on ``--yes``.
The two run-scope families that CAN mutate the device or open an
interactive session — ``run start shell`` and ``run request ...`` — are
refused by the validator and redirected to their dedicated, ``--yes``-
gated tools, so the passthrough never becomes an ungated write path.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from qactl.dnos.cli.core.envelope import error_response
from qactl.dnos.cli.core.errors import RUN_NEXT_ACTION
from qactl.dnos.cli.core.runner import _run_on_device
from qactl.dnos.cli.core.session import DEFAULT_CMD_TIMEOUT, DEFAULT_PASSWORD, DEFAULT_USER
from qactl.dnos.cli.core.validation import _validate_run_command
from qactl.dnos.cli.vendors import CAP_RUN, requires


@requires(CAP_RUN)
def run_command(
    command: str,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Run an operational ``run ...`` command on the device.

    Pass the full command verbatim, e.g.:
      - command="run traceroute 10.0.0.1"
      - command="run traceroute mpls isis <prefix>"
      - command="run traceroute mpls bgp-car <prefix>"

    The command must start with ``run``. ``run start shell ...`` and
    ``run request ...`` are rejected with a pointer to ``qactl cli shell``
    / ``qactl cli raw --yes``, which keep the destructive gate.

    On DNOS errors ("% Unknown command", "Invalid input", ...) the result
    is returned with status="error" and ``next_actions`` pointing at the
    discovery tools (``qactl cli search run <keywords>`` /
    ``qactl cli crawl 'run ...'``) for the correct syntax.

    Args:
        command: Full operational command, must start with ``run``.
        device: Device alias from the registry.
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-command timeout seconds; widen for a slow multi-hop
            traceroute.
    """
    full, err = _validate_run_command(command)
    if err:
        return error_response(
            err, device=device, host=host, command=(command or "").strip(),
            next_action=RUN_NEXT_ACTION,
        )
    return _run_on_device(
        "run", device, host, user, password,
        full, timeout, RUN_NEXT_ACTION,
    )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(run_command)
