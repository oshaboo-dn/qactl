"""Restart / switchover MCP tools — DESTRUCTIVE, all guarded by ``confirm=True``.

Six tools:

- ``kill_9_ncc_process`` — SIGKILL a routing daemon on the active NCC.
- ``request_system_restart`` — full-box restart (cold/warm/recovery).
- ``request_system_restart_nce`` — restart one cluster element.
- ``request_system_container_restart`` — restart one container on a node.
- ``request_system_process_restart`` — restart one process in a container.
- ``request_system_ncc_switchover`` — fail control over to the standby NCC.

The five ``request_system_*`` tools share the same two-layer safety
model: a Python ``confirm=False`` dry-run gate plus a per-channel
``set cli-no-confirm`` to bypass DNOS' interactive (Yes/No) prompt
without leaking the setting beyond the call. Both layers live in
``_restart_execute``.

``kill_9_ncc_process`` is the odd one out — it goes through
``run start shell`` (not the configure-style restart path) and uses the
shared ``run_linux_on_device`` runner.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Literal, Optional

from qactl.dnos.cli.core.envelope import error_response, make_response
from qactl.dnos.cli.core.errors import (
    KILL_NCC_NEXT_ACTION,
    REQUEST_NCC_SWITCHOVER_NEXT_ACTION,
    REQUEST_RESTART_NEXT_ACTION,
    detect_error,
)
from qactl.dnos.cli.core.logging import log_invocation, log_request
from qactl.dnos.cli.core.registry import transport_registry
from qactl.dnos.cli.core.session import (
    DEFAULT_CMD_TIMEOUT,
    DEFAULT_PASSWORD,
    DEFAULT_USER,
    ConnectError,
    connect_error_next_actions,
    run_sequence,
)
from qactl.dnos.cli.core.shell_exec import run_linux_on_device
from qactl.dnos.cli.vendors import CAP_RESTART, requires


_NCM_ID_RE = re.compile(r"^[ab][01]$")
_DECIMAL_ID_RE = re.compile(r"^\d+$")
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]+$")
# DNOS process names include prefixed forms like ``routing:bgpd``,
# ``infra:sshd``, ``core:nr_agent``, ``standby_routing:bgpd_standby`` — so
# the process-name pattern allows ``:`` on top of the container-name set.
_PROCESS_NAME_RE = re.compile(r"^[A-Za-z0-9._:\-]+$")


def _validate_node_id(node_role: str, node_id: str) -> Optional[str]:
    """Validate ``node_id`` against the role-specific shape.

    - ``ncc|ncp|ncf`` require a plain decimal integer.
    - ``ncm`` requires one of ``a0 / b0 / a1 / b1`` (per DNOS docs).
    """
    if not isinstance(node_id, str) or not node_id.strip():
        return "node_id must be a non-empty string."
    value = node_id.strip()
    if node_role == "ncm":
        if not _NCM_ID_RE.match(value):
            return (
                f"node_id for ncm must match [ab][01] (got {value!r}); "
                "valid values are a0, b0, a1, b1."
            )
    else:
        if not _DECIMAL_ID_RE.match(value):
            return (
                f"node_id for {node_role} must be a decimal integer (got {value!r})."
            )
    return None


def _validate_container_name(name: str) -> Optional[str]:
    if not isinstance(name, str) or not name.strip():
        return "container_name must be a non-empty string."
    if not _CONTAINER_NAME_RE.match(name.strip()):
        return (
            f"container_name must match [A-Za-z0-9._-]+ (got {name!r}); "
            "discover valid names with "
            "cli_crawler(path='request system container restart <role> <id> ')."
        )
    return None


def _validate_process_name(name: str) -> Optional[str]:
    if not isinstance(name, str) or not name.strip():
        return "process_name must be a non-empty string."
    if not _PROCESS_NAME_RE.match(name.strip()):
        return (
            f"process_name must match [A-Za-z0-9._:-]+ (got {name!r}); "
            "discover valid names with "
            "cli_crawler(path='request system process restart <role> <id> <container> ') "
            "or read them from `show system <role> <id>`."
        )
    return None


def _restart_execute(
    tool: str,
    command: str,
    confirm: bool,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    timeout: int,
    next_action: str = REQUEST_RESTART_NEXT_ACTION,
) -> Dict[str, Any]:
    """Shared execution path for every restart / switchover tool.

    Two behaviours based on ``confirm``:

    - ``confirm=False`` (default): DRY-RUN. No SSH is opened. Return an
      envelope with ``status="ok"``, the exact ``command`` that would be
      sent, and a warning telling the caller how to actually execute.
    - ``confirm=True``: Open a fresh channel via ``run_sequence``, run
      ``set cli-no-confirm`` then the restart command on that same
      channel, and close the channel. The ``cli-no-confirm`` setting is
      session-scoped, so the prompt bypass dies when the channel closes.

    Device errors detected in stdout are surfaced in ``errors`` with the
    tool-specific ``next_action`` appended (defaults to the generic restart
    grammar hint; switchover passes its own).
    """
    request = {
        "device": device, "host": host, "user": user,
        "command": command, "confirm": confirm,
    }

    if not confirm:
        response = make_response(
            device=device,
            host=host or "",
            command=command,
            warnings=[
                "Dry-run: confirm=False. Re-invoke with confirm=true to execute.",
            ],
        )
        log_request(tool, request, response)
        return response

    response = make_response(device=device, host=host, command=command)
    try:
        result = run_sequence(
            transport_registry,
            device=device, host=host, user=user, password=password,
            commands=["set cli-no-confirm", command],
            timeout=timeout,
        )
    except ConnectError as exc:
        response.update(
            status="connect_error",
            errors=[str(exc)],
            next_actions=connect_error_next_actions(exc),
        )
        log_request(tool, request, response)
        return response
    except Exception as exc:
        response.update(status="error", errors=[str(exc)])
        log_request(tool, request, response)
        return response

    response["host"] = result.host
    response["device"] = result.device or device
    response["stdout"] = result.output
    log_invocation(
        result.device or device,
        result.host,
        command,
        result.output,
        result.head_prompt_line,
        result.tail_prompt,
        steps=result.steps,
    )

    if not result.hit_prompt:
        response["status"] = "timeout"
        response["errors"].append(f"Timed out waiting for CLI prompt after {timeout}s.")
        response["next_actions"].append(
            "Retry with a larger timeout; the device may still be restarting.",
        )
        log_request(tool, request, response)
        return response

    is_err, err_lines = detect_error(result.output)
    if is_err:
        response["status"] = "error"
        response["errors"].extend(err_lines[-5:])
        response["next_actions"].append(next_action)

    log_request(tool, request, response)
    return response


@requires(CAP_RESTART)
def kill_9_ncc_process(
    process: Literal["bgpd", "zebra", "fibmgrd"],
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """SIGKILL a routing/forwarding daemon on the active NCC (DESTRUCTIVE).

    Enters ``run start shell`` (which transparently targets the active NCC,
    re-prompts for the SSH password, then lands you at a Linux shell on the
    routing engine), kills the named daemon by exact process name, and
    returns. The NCC process supervisor will respawn the daemon.

    The tool runs, in one interactive shell session:

        pgrep -x <process>
        kill -9 $(pgrep -x <process>) 2>/dev/null
        sleep 0.5
        pgrep -x <process>

    So ``stdout`` shows the old PID(s) on the first line and the respawned
    PID(s) (if any) on the last line — compare them to confirm the kill
    took effect.

    ``process`` is the exact Linux binary name:

      - ``bgpd``    — BGP daemon.
      - ``zebra``   — the RIB manager (aka ``rib-manager`` / ``rib-mgr``).
      - ``fibmgrd`` — the FIB manager daemon.

    Args:
        process: Daemon binary name to kill (exact match, ``pgrep -x``).
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot); also used to authenticate
                  the ``run start shell`` challenge.
        timeout: Per-step timeout seconds (covers each of the password
                 wait, shell-prompt wait, and command wait independently).
    """
    linux_cmd = (
        f"pgrep -x {process}; "
        f"kill -9 $(pgrep -x {process}) 2>/dev/null; "
        f"sleep 0.5; "
        f"pgrep -x {process}"
    )
    return run_linux_on_device(
        "kill_9_ncc_process", device, host, user, password,
        linux_cmd, timeout, KILL_NCC_NEXT_ACTION,
    )


@requires(CAP_RESTART)
def request_system_restart(
    mode: Literal["cold", "warm", "recovery"] = "cold",
    confirm: bool = False,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Restart the WHOLE system (DESTRUCTIVE).

    Runs the box-wide form of ``request system restart`` (no node args).
    For per-node restarts use ``request_system_restart_nce`` instead.

    Modes:

    - ``cold`` (default) — ``request system restart``. Full cold reboot of
      the entire chassis: NCP interfaces are shut down first, then every
      cluster node is restarted one by one. Hardware is power-cycled.
    - ``warm``             — ``request system restart warm``. Restarts only
      the applicative DNOS containers (management-engine, routing-engine,
      forwarding-engine, selector) across the whole system. Hardware is NOT
      power-cycled. NCM containers are not affected.
    - ``recovery``         — ``request system restart recovery``. Reboots
      into recovery mode, where only ``request system restart`` /
      ``... factory-default`` / ``... rollback`` are available. Requires
      ``techsupport`` role. Exiting recovery requires another system
      restart.

    Safety model (two independent layers):

    1. This tool's ``confirm`` argument (default False). With
       ``confirm=False`` we do NOT open SSH — we just return the exact
       command line we would have sent, as a dry-run preview. You must
       explicitly pass ``confirm=True`` to actually reboot.
    2. On the wire, ``set cli-no-confirm`` is sent on the same ephemeral
       channel right before the restart so DNOS does not block on the
       ``(Yes/No)?`` prompt. The setting is session-scoped, so it dies
       with the channel — the bypass never affects other tools.

    Args:
        mode: One of ``cold`` (default) / ``warm`` / ``recovery``.
        confirm: Must be ``True`` to actually execute. Default False returns
            a dry-run envelope containing the command that would be sent.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-command timeout seconds. The tool does NOT block until
            the device is fully back up; it just waits for the CLI prompt
            after the restart command is accepted.
    """
    if mode == "cold":
        command = "request system restart"
    elif mode == "warm":
        command = "request system restart warm"
    else:
        command = "request system restart recovery"
    return _restart_execute(
        "request_system_restart",
        command, confirm,
        device, host, user, password, timeout,
    )


@requires(CAP_RESTART)
def request_system_restart_nce(
    node_role: Literal["ncc", "ncp", "ncm", "ncf"],
    node_id: str,
    mode: Literal["cold", "warm", "force"] = "cold",
    confirm: bool = False,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Restart a single cluster element — NCC / NCP / NCM / NCF (DESTRUCTIVE).

    Runs ``request system restart {role} {id} [warm|force]``. For a
    full-box restart use ``request_system_restart`` instead.

    Arguments:

    - ``node_role`` — ``ncc``, ``ncp``, ``ncm``, or ``ncf``.
    - ``node_id``  — cluster-element id. **String**, because DNOS uses
      non-numeric ids for NCM (``a0``, ``b0``, ``a1``, ``b1``). For
      ``ncc``/``ncp``/``ncf`` pass a decimal like ``"0"``, ``"1"``, etc.
      Valid ids depend on the device topology — check with
      ``show_system`` or ``cli_crawler`` if unsure.
    - ``mode``:
        - ``cold`` (default) — power reset of the node (``request system
          restart {role} {id}``).
        - ``warm`` — restart only the DNOS applicative containers on that
          node (``... warm``). **Not valid for ncm.**
        - ``force`` — remotely enforce a cold restart via IPMI
          connectivity (``... force``); use when the node is hung and the
          normal restart will not take. **Not valid for ncm or ncc.**

    Safety: same two layers as ``request_system_restart`` — Python-side
    ``confirm=True`` gate AND per-channel ``set cli-no-confirm``.

    Args:
        node_role: Cluster element type.
        node_id: Element id (string; ncm uses a0/b0/a1/b1).
        mode: One of ``cold`` / ``warm`` / ``force``.
        confirm: Must be ``True`` to actually execute; default is a dry-run.
        device: Device alias.
        host: Raw hostname/IP (alternative to device).
        user: SSH username.
        password: SSH password.
        timeout: Per-command timeout seconds.
    """
    if (err := _validate_node_id(node_role, node_id)):
        return error_response(
            err, device=device, host=host,
            next_action=REQUEST_RESTART_NEXT_ACTION,
        )
    if mode == "warm" and node_role == "ncm":
        return error_response(
            "mode='warm' is not applicable to ncm (per DNOS docs).",
            device=device, host=host,
            next_action=REQUEST_RESTART_NEXT_ACTION,
        )
    if mode == "force" and node_role in ("ncm", "ncc"):
        return error_response(
            f"mode='force' is not applicable to {node_role} "
            "(force uses IPMI; only NCP/NCF are supported).",
            device=device, host=host,
            next_action=REQUEST_RESTART_NEXT_ACTION,
        )

    parts = ["request", "system", "restart", node_role, node_id.strip()]
    if mode == "warm":
        parts.append("warm")
    elif mode == "force":
        parts.append("force")
    command = " ".join(parts)

    return _restart_execute(
        "request_system_restart_nce",
        command, confirm,
        device, host, user, password, timeout,
    )


@requires(CAP_RESTART)
def request_system_container_restart(
    node_role: Literal["ncc", "ncp", "ncm", "ncf"],
    node_id: str,
    container_name: str,
    confirm: bool = False,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Restart a single container on a cluster element (DESTRUCTIVE).

    Runs ``request system container restart {role} {id} {container_name}``.
    Use this instead of a full node restart to minimise blast radius when
    only one container is misbehaving.

    Valid container names are device-, release-, and node-type-specific,
    so there is no enum here. Discover them with::

        cli_crawler(path='request system container restart {role} {id} ')

    Args:
        node_role: Cluster element type (ncc/ncp/ncm/ncf).
        node_id: Element id (string; ncm uses a0/b0/a1/b1).
        container_name: Exact container name to restart. Validated to
            match ``[A-Za-z0-9._-]+``. Use ``cli_crawler`` to discover.
        confirm: Must be ``True`` to actually execute; default is a dry-run.
        device: Device alias.
        host: Raw hostname/IP (alternative to device).
        user: SSH username.
        password: SSH password.
        timeout: Per-command timeout seconds.
    """
    if (err := _validate_node_id(node_role, node_id)):
        return error_response(
            err, device=device, host=host,
            next_action=REQUEST_RESTART_NEXT_ACTION,
        )
    if (err := _validate_container_name(container_name)):
        return error_response(
            err, device=device, host=host,
            next_action=REQUEST_RESTART_NEXT_ACTION,
        )

    command = (
        f"request system container restart {node_role} "
        f"{node_id.strip()} {container_name.strip()}"
    )
    return _restart_execute(
        "request_system_container_restart",
        command, confirm,
        device, host, user, password, timeout,
    )


@requires(CAP_RESTART)
def request_system_process_restart(
    node_role: Literal["ncc", "ncp", "ncf"],
    node_id: str,
    container_name: str,
    process_name: str,
    confirm: bool = False,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Restart a single process inside a container on a cluster element (DESTRUCTIVE).

    Runs ``request system process restart {role} {id} {container} {process}``.
    All four arguments are required — to restart a whole container use
    ``request_system_container_restart`` instead, and to restart a whole
    node use ``request_system_restart_nce``.

    ``node_role`` is limited to ``ncc`` / ``ncp`` / ``ncf`` (DNOS does not
    support process restart on NCM).

    Per DNOS docs: if the target process is "non-restartable", the command
    will still succeed but will cause the whole enclosing container to
    restart — so the blast radius can silently widen from one process to
    one container.

    Discover valid names:

    - Container list for a node: ``cli_crawler(path='request system process restart <role> <id> ')``.
    - Process list inside a container: one level deeper, or read the
      ``| Process Name | ...`` table from ``show system <role> <id>``.

    Process names may include colon-prefixed forms such as
    ``routing:bgpd``, ``infra:sshd``, ``core:nr_agent``, or
    ``standby_routing:bgpd_standby``.

    Safety: same two layers as the other restart tools — Python-side
    ``confirm=True`` gate AND per-channel ``set cli-no-confirm``.

    Args:
        node_role: Cluster element type (ncc/ncp/ncf; no ncm).
        node_id: Element id (decimal string).
        container_name: Exact container housing the process.
        process_name: Exact process to restart (validated against
            ``[A-Za-z0-9._:-]+``).
        confirm: Must be ``True`` to actually execute; default is a dry-run.
        device: Device alias.
        host: Raw hostname/IP (alternative to device).
        user: SSH username.
        password: SSH password.
        timeout: Per-command timeout seconds.
    """
    if (err := _validate_node_id(node_role, node_id)):
        return error_response(
            err, device=device, host=host,
            next_action=REQUEST_RESTART_NEXT_ACTION,
        )
    if (err := _validate_container_name(container_name)):
        return error_response(
            err, device=device, host=host,
            next_action=REQUEST_RESTART_NEXT_ACTION,
        )
    if (err := _validate_process_name(process_name)):
        return error_response(
            err, device=device, host=host,
            next_action=REQUEST_RESTART_NEXT_ACTION,
        )

    command = (
        f"request system process restart {node_role} "
        f"{node_id.strip()} {container_name.strip()} {process_name.strip()}"
    )
    return _restart_execute(
        "request_system_process_restart",
        command, confirm,
        device, host, user, password, timeout,
    )


@requires(CAP_RESTART)
def request_system_ncc_switchover(
    confirm: bool = False,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Trigger an NCC switchover on a dual-NCC chassis (DESTRUCTIVE).

    Runs ``request system ncc switchover``. The currently-active NCC hands
    control to the standby and itself restarts; management plane is briefly
    unavailable while the standby takes over. Use only on dual-NCC chassis
    that have a healthy standby (``show system ncc``).

    Channel behaviour: the SSH session is expected to drop mid-command as
    the active NCC changes. That surfaces here as ``status="timeout"`` —
    which is the **expected happy path** for this tool, not an error. The
    transport_registry will reconnect to the new active on the next call,
    provided the device alias lists both NCCs in the canonical
    ``devices_mgmt0.json`` map (e.g. ``OHADZS-CL`` lists
    ``dn40-cl-301a-ncc0`` and ``dn40-cl-301a-ncc1`` so the fallthrough
    finds whichever is now active).

    Safety: same two layers as the other ``request_system_*`` tools —
    Python-side ``confirm=True`` gate AND per-channel ``set cli-no-confirm``.

    Args:
        confirm: Must be ``True`` to actually execute. Default False returns
            a dry-run envelope containing the command that would be sent.
        device: Device alias (must be a dual-NCC chassis, e.g. ``cl``).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-command timeout seconds. Does not block until the new
            active is fully up; on a real switchover, expect timeout —
            confirm afterwards with a fresh ``show system ncc`` call.
    """
    return _restart_execute(
        "request_system_ncc_switchover",
        "request system ncc switchover",
        confirm,
        device, host, user, password, timeout,
        next_action=REQUEST_NCC_SWITCHOVER_NEXT_ACTION,
    )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(kill_9_ncc_process)
    mcp.tool()(request_system_restart)
    mcp.tool()(request_system_restart_nce)
    mcp.tool()(request_system_container_restart)
    mcp.tool()(request_system_process_restart)
    mcp.tool()(request_system_ncc_switchover)
