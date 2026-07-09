"""IxNetwork API server discovery — port and session detection."""

from __future__ import annotations

import logging
import re
import socket
from typing import Any, Optional

import paramiko

from .models import IxiaConnectionError

log = logging.getLogger(__name__)


def check_port(host: str, port: int, timeout: float = 3.0) -> bool:
    """Quick TCP connect check. Returns True if *port* is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def discover_api_port(
    host: str,
    ssh_user: Optional[str] = None,
    ssh_password: Optional[str] = None,
    default_ports: tuple[int, ...] = (443, 11009),
    timeout: float = 5.0,
) -> int:
    """Discover the IxNetwork REST API port on a given host.

    Strategy (in order):
    1. Try each default port with a quick TCP connect check
    2. SSH to the host and discover via PowerShell (Get-NetTCPConnection)
    3. SSH fallback: netstat + tasklist correlation
    4. SSH last resort: check IxNetwork install path, use 11009

    Args:
        host: Hostname or IP of the IxNetwork API server.
        ssh_user: SSH username for discovery fallback.
        ssh_password: SSH password.
        default_ports: Ports to try first (443 for Linux, 11009 for Windows).
        timeout: TCP connect timeout in seconds.

    Returns:
        Discovered port number.

    Raises:
        IxiaConnectionError: If no port can be discovered.
    """
    for port in default_ports:
        log.debug("Probing %s:%d", host, port)
        if check_port(host, port, timeout=timeout):
            log.info("Port %d open on %s (TCP fast-path)", port, host)
            return port

    if not ssh_user:
        raise IxiaConnectionError(
            f"No default port open on {host} (tried {default_ports}) "
            "and no ssh_user provided for SSH-based discovery."
        )

    log.info("No default port open on %s; falling back to SSH discovery", host)
    port = _ssh_discover_port(host, ssh_user, ssh_password)

    if port is None:
        raise IxiaConnectionError(
            f"Could not discover IxNetwork API port on {host}. "
            "Ensure IxNetwork is running and SSH credentials are correct."
        )

    return port


def _ssh_discover_port(
    host: str, user: str, password: Optional[str] = None
) -> Optional[int]:
    """SSH to host and discover IxNetwork API port.

    Uses paramiko. Tries PowerShell, then netstat+tasklist, then install
    path check. Returns port number or None if not found.
    """
    try:
        client = _ssh_connect(host, user, password)
    except Exception as exc:
        log.warning("SSH connection to %s failed: %s", host, exc)
        return None

    try:
        port = _try_powershell(client)
        if port is not None:
            return port

        port = _try_netstat(client)
        if port is not None:
            return port

        return _try_install_path(client)
    finally:
        client.close()


def _ssh_connect(
    host: str, user: str, password: Optional[str] = None
) -> Any:
    """Open paramiko SSH connection. Supports password, key-based, and 'none' auth."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    if password:
        client.connect(
            host,
            username=user,
            password=password,
            timeout=30,
            allow_agent=False,
            look_for_keys=False,
        )
        return client

    try:
        client.connect(
            host,
            username=user,
            timeout=30,
            allow_agent=True,
            look_for_keys=True,
        )
    except (paramiko.SSHException, paramiko.AuthenticationException):
        # Last-ditch: "none" auth (some Windows SSH servers accept this)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        transport = paramiko.Transport((host, 22))
        transport.connect(username=user)
        transport.auth_none(user)
        client._transport = transport  # noqa: SLF001

    return client


def _ssh_exec(
    client: Any, cmd: str, timeout: int = 15
) -> tuple[str, str, int]:
    """Run command over SSH, return (stdout, stderr, exit_code)."""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_code: int = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    return out, err, exit_code


# ---------------------------------------------------------------------------
# SSH discovery strategies
# ---------------------------------------------------------------------------

def _try_powershell(client: Any) -> Optional[int]:
    """Get-NetTCPConnection filtered to IxNetwork/Ixia processes."""
    ps_cmd = (
        'powershell -NoProfile -Command "'
        "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
        "ForEach-Object { $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue; "
        "if ($p -and ($p.ProcessName -like '*IxNetwork*' -or $p.ProcessName -like '*Ixia*')) "
        '{ $_.LocalPort } }"'
    )
    out, err, _ = _ssh_exec(client, ps_cmd)
    if err or not out:
        return None

    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            port = int(line)
            log.info("PowerShell discovery found port %d", port)
            return port
    return None


def _try_netstat(client: Any) -> Optional[int]:
    """Correlate netstat LISTENING entries with IxNetwork PIDs from tasklist."""
    tasklist_out, _, _ = _ssh_exec(client, "tasklist", timeout=10)

    pid_to_proc: dict[str, str] = {}
    for line in tasklist_out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            pid_to_proc[parts[1]] = parts[0].lower()

    ixn_pids = {
        pid
        for pid, name in pid_to_proc.items()
        if "ixnetwork" in name or "ixia" in name
    }
    if not ixn_pids:
        return None

    netstat_out, _, _ = _ssh_exec(
        client, "netstat -ano | findstr LISTENING", timeout=10
    )

    for line in netstat_out.splitlines():
        match = re.search(r":(\d+)\s+.*?LISTENING\s+(\d+)", line)
        if match:
            port_str, pid = match.group(1), match.group(2)
            if pid in ixn_pids and port_str.isdigit():
                port = int(port_str)
                log.info("netstat discovery found port %d (PID %s)", port, pid)
                return port
    return None


def _try_install_path(client: Any) -> Optional[int]:
    """If IxNetwork is installed in a known path, assume default port 11009."""
    paths = (
        r"C:\Program Files (x86)\Ixia\IxNetwork\version.txt",
        r"C:\Program Files\Ixia\IxNetwork\version.txt",
    )
    for path in paths:
        _, _, exit_code = _ssh_exec(client, f'type "{path}" 2>nul', timeout=5)
        if exit_code == 0:
            log.info("IxNetwork install detected at %s; assuming port 11009", path)
            return 11009
    return None
