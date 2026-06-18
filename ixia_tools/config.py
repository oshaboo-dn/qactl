"""Config-level tools: list / new / load / save.

The three mutating tools (``new`` / ``load`` / ``save``):
- take the per-session write lock from ``ixia_core.session.write_lock``,
- require ``confirm=True`` before doing anything (there is no undo).

File paths here are **server-local** paths — the filesystem on the
IxNetwork API server (e.g. ``C:\\Users\\dn\\Desktop\\ixia\\x.ixncfg`` on
Windows). ``ixia_load_config`` / ``ixia_save_config`` open the file
directly through the IxNetwork process, which has full filesystem
access and is NOT confined to the REST ``/files`` sandbox.

``ixia_list_configs`` is the only tool in this MCP that does not go
through REST: it shells out to ``ssh <host> powershell ...`` because
the REST ``/files`` endpoint is sandboxed to
``C:/Users/dn/AppData/Local/Ixia/...`` and refuses to enumerate the
folder where the canonical saved configs live (typically
``C:\\Users\\dn\\Desktop\\ixia\\``). SSH key auth must already be set
up; no passwords or secrets are read by the MCP.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from ixia.models import IxiaError, IxiaOperationError

from ixia_core.envelope import make_envelope, error_envelope
from ixia_core.session import (
    DEFAULT_PORT, DEFAULT_USER,
    get_session, write_lock, session_id_of,
)
from ixia_tools._vport_wait import (
    stuck_vport_summary,
    vport_state_snapshot,
    vports_not_ready,
    wait_for_vports_ready,
)


DEFAULT_CONFIG_FOLDER = r"C:\Users\dn\Desktop\ixia"


# Default vport-readiness wait for ``ixia_load_config``. Loaded configs
# put chassis ports through Reboot → connectedLinkUp transitions that
# typically complete in 30-60 s. Polling happens every 10 s (the
# ``_vport_wait.wait_for_vports_ready`` default), so with the 60 s
# default the polls land at t≈10, 20, 30, 40, 50, 60. Set to 0 to
# skip the wait entirely; bump on slow chassis.
DEFAULT_VPORT_WAIT_MS = 60_000


def ixia_list_configs(
    host: str,
    folder: str = DEFAULT_CONFIG_FOLDER,
    ssh_alias: Optional[str] = None,
    timeout_s: int = 10,
) -> Dict[str, Any]:
    """List ``.ixncfg`` files on the IxNetwork API server.

    Read-only inventory of saved configs you can hand to
    ``ixia_load_config``. Sorted newest-first by modification time.

    Implementation note: this is the only ixia-mcp tool that does NOT
    use the IxNetwork REST API. The REST ``/files`` endpoint is
    sandboxed to ``C:/Users/dn/AppData/Local/Ixia/...`` and refuses to
    enumerate ``C:\\Users\\dn\\Desktop\\ixia\\`` where the canonical
    lab configs live. So this tool shells out to
    ``ssh <ssh_alias|host> powershell -Command "Get-ChildItem ..."``
    and parses the JSON output. SSH key auth must be set up on the
    MCP host; no password is read by the MCP.

    Args:
        host: API-server hostname (matches every other ixia tool).
            Used as the SSH target unless ``ssh_alias`` overrides.
        folder: Windows path to enumerate. Default
            ``C:\\Users\\dn\\Desktop\\ixia``. Wildcards are appended
            internally as ``\\*.ixncfg``.
        ssh_alias: Override the SSH target if it differs from ``host``
            (e.g. an entry in ``~/.ssh/config``). Defaults to ``host``.
        timeout_s: SSH command timeout, default 10 s.

    Returns envelope with ``result = {folder, count, configs: [...]}``.
    Each entry: ``{name, size_bytes, mtime}`` where ``mtime`` is an
    ISO-8601 string in the API server's local timezone.
    """
    request = {
        "host": host,
        "folder": folder,
        "ssh_alias": ssh_alias,
        "timeout_s": timeout_s,
    }
    if not host or not isinstance(host, str):
        return error_envelope(
            "host must be a non-empty string.",
            kind="list_configs", status="bad_argument",
        )
    if not folder or not isinstance(folder, str):
        return error_envelope(
            "folder must be a non-empty Windows path.",
            kind="list_configs", host=host, status="bad_argument",
        )
    if not isinstance(timeout_s, int) or timeout_s <= 0:
        return error_envelope(
            "timeout_s must be a positive integer.",
            kind="list_configs", host=host, status="bad_argument",
        )

    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        return error_envelope(
            "ssh binary not found in PATH on the MCP host.",
            kind="list_configs", host=host,
            next_actions=["Install openssh-client on the MCP host."],
        )

    target = ssh_alias or host
    # Build the PowerShell command. Sort newest-first server-side so
    # the agent sees the most-recent config at index 0. Format mtime
    # as ISO-8601 (round-trippable, no PowerShell ``\/Date(...)\/``
    # weirdness in the JSON).
    #
    # NB: pass via ``powershell -EncodedCommand <base64-utf16le>``. The
    # default remote shell on Windows OpenSSH is cmd.exe, which would
    # otherwise parse pipes / quotes before powershell.exe ever sees
    # them. EncodedCommand sidesteps every layer of quoting.
    ps_cmd = (
        f"$ErrorActionPreference='Stop'; "
        f"Get-ChildItem -Path '{folder}\\*.ixncfg' "
        f"| Sort-Object LastWriteTime -Descending "
        f"| Select-Object Name, Length, "
        f"@{{N='Mtime'; E={{$_.LastWriteTime.ToString('o')}}}} "
        f"| ConvertTo-Json -Compress"
    )
    encoded = base64.b64encode(ps_cmd.encode("utf-16-le")).decode("ascii")

    env = make_envelope(kind="list_configs", host=host, request=request)

    try:
        proc = subprocess.run(
            [ssh_bin, "-o", "BatchMode=yes",
             "-o", f"ConnectTimeout={max(2, timeout_s // 2)}",
             target, "powershell", "-NoProfile", "-EncodedCommand", encoded],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        env["status"] = "error"
        env["errors"].append(
            f"ssh {target} timed out after {timeout_s} s."
        )
        env["next_actions"].append(
            f"Verify SSH reachability: `ssh -o BatchMode=yes {target} hostname`."
        )
        return env
    except FileNotFoundError:
        env["status"] = "error"
        env["errors"].append("ssh binary disappeared between probe and run.")
        return env

    if proc.returncode != 0:
        env["status"] = "error"
        stderr_tail = (proc.stderr or "").strip()[-500:]
        env["errors"].append(
            f"ssh {target} exited {proc.returncode}: {stderr_tail or '(no stderr)'}"
        )
        env["next_actions"].append(
            f"Verify SSH key auth + that the folder exists: "
            f"`ssh {target} powershell -Command \"Test-Path '{folder}'\"`."
        )
        return env

    raw = (proc.stdout or "").strip()
    if not raw or raw == "null":
        # Empty folder is a valid answer, not an error.
        env["result"] = {"folder": folder, "count": 0, "configs": []}
        return env

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        env["status"] = "error"
        env["errors"].append(
            f"Could not parse PowerShell JSON output: {e}. "
            f"First 200 chars: {raw[:200]!r}"
        )
        return env

    # PowerShell ConvertTo-Json returns a single object (not a list)
    # when there's exactly one match. Normalise to list.
    if isinstance(parsed, dict):
        parsed = [parsed]
    elif not isinstance(parsed, list):
        env["status"] = "error"
        env["errors"].append(
            f"Unexpected JSON shape from PowerShell: {type(parsed).__name__}"
        )
        return env

    configs: List[Dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        configs.append({
            "name": item.get("Name"),
            "size_bytes": item.get("Length"),
            "mtime": item.get("Mtime"),
        })

    env["result"] = {
        "folder": folder,
        "count": len(configs),
        "configs": configs,
    }
    return env


def _destructive_guard(
    *, kind: str, host: str, port: int, confirm: bool
) -> Dict[str, Any] | None:
    """Return an error envelope if ``confirm`` is not True; else None."""
    if confirm is True:
        return None
    return error_envelope(
        "This tool mutates the IxNetwork session and has no undo. "
        "Re-call with confirm=True after reviewing the arguments.",
        kind=kind, host=host, port=port,
        status="confirmation_required",
        next_actions=[
            "Re-invoke with confirm=True to proceed.",
        ],
    )


def ixia_new_config(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Clear the current IxNetwork session config (File > New).

    Wipes all topologies, device groups, traffic items, stat views.
    Ports / vport ownership is **preserved**. Re-run from a blank slate.

    Destructive — requires ``confirm=True``.
    """
    request = {"host": host, "port": port, "user": user, "confirm": confirm}
    guard = _destructive_guard(
        kind="new_config", host=host, port=port, confirm=confirm
    )
    if guard is not None:
        guard["request"] = request
        return guard

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="new_config",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="new_config", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        with write_lock(host, port, user):
            s.config.new()
        env["result"] = {"cleared": True}
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_load_config(
    host: str,
    server_path: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
    wait_for_vports_ms: int = DEFAULT_VPORT_WAIT_MS,
) -> Dict[str, Any]:
    """Load an ``.ixncfg`` file that already exists on the API server.

    Args:
        server_path: Absolute path on the API server's filesystem, e.g.
            ``C:\\Users\\dn\\Desktop\\ixia\\bgp-leak.ixncfg`` on Windows.
            This is **not** a path on the MCP host.
        confirm: Must be ``True``. Loading overwrites the current session
            config (but not vport ownership).
        wait_for_vports_ms: After IxNetwork finishes parsing the file,
            block until every assigned vport reaches
            ``connection_state=connectedLinkUp`` + ``link_state=up`` or
            until this deadline elapses (default 60 000 ms; polled
            every 10 s — first check at t≈10 s, last at t≈60 s). Set
            to ``0`` to return as soon as the load completes — the
            chassis ports are typically still ``Rebooting`` for ~30-60 s
            after that and calling ``ixia_protocols_start_all`` against
            them returns ``"No IP Address for Parent found!"``.

    Destructive — requires ``confirm=True``. Load itself takes 10-60 s
    for a large config; the vport-readiness wait adds whatever it takes
    for the chassis links to come up. The MCP call blocks until both
    finish (or the wait times out).
    """
    request = {
        "host": host, "port": port, "user": user,
        "server_path": server_path, "confirm": confirm,
        "wait_for_vports_ms": wait_for_vports_ms,
    }
    guard = _destructive_guard(
        kind="load_config", host=host, port=port, confirm=confirm
    )
    if guard is not None:
        guard["request"] = request
        return guard
    if not server_path or not isinstance(server_path, str):
        return error_envelope(
            "server_path must be a non-empty absolute path on the API server.",
            kind="load_config", host=host, port=port,
            status="bad_argument",
        )
    if not isinstance(wait_for_vports_ms, int) or wait_for_vports_ms < 0:
        return error_envelope(
            "wait_for_vports_ms must be a non-negative integer "
            "(milliseconds; 0 disables the post-load vport-readiness wait).",
            kind="load_config", host=host, port=port,
            status="bad_argument",
        )

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="load_config",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="load_config", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    t_load = time.time()
    try:
        with write_lock(host, port, user):
            s.config.load(server_path, local_file=False)
    except IxiaOperationError as e:
        env["status"] = "error"
        env["errors"].append(str(e))
        env["next_actions"].append(
            "Verify the path exists on the API server filesystem and is a "
            "valid .ixncfg (e.g. `dir` that path via cli-mcp or RDP)."
        )
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env

    load_elapsed_s = round(time.time() - t_load, 2)
    result: Dict[str, Any] = {
        "loaded_from": server_path,
        "load_elapsed_s": load_elapsed_s,
    }

    # Vport-readiness wait: polled outside the write lock — read-only
    # ``Vport.find()`` traffic, no concurrent writers needed.
    if wait_for_vports_ms == 0:
        try:
            snapshot = vport_state_snapshot(s)
        except Exception as e:
            snapshot = []
            env["warnings"].append(
                f"Vport state snapshot failed after load: "
                f"{type(e).__name__}: {str(e)[:200]}"
            )
        result["vports_ready"] = not vports_not_ready(snapshot)
        result["wait_elapsed_s"] = 0.0
        result["vports"] = snapshot
        env["result"] = result
        return env

    try:
        ready, snapshot, wait_elapsed_s = wait_for_vports_ready(
            s, timeout_s=wait_for_vports_ms / 1000.0,
        )
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(
            f"Vport-readiness wait failed: "
            f"{type(e).__name__}: {str(e)[:200]}"
        )
        env["result"] = result
        return env

    result["vports_ready"] = ready
    result["wait_elapsed_s"] = wait_elapsed_s
    result["vports"] = snapshot
    env["result"] = result

    if not ready:
        stuck = stuck_vport_summary(snapshot)
        env["status"] = "warning"
        env["warnings"].append(
            f"{len(stuck)} of {len(snapshot)} vport(s) did not reach "
            f"connection_state=connectedLinkUp + link_state=up within "
            f"{wait_for_vports_ms} ms after load: {stuck}. Protocol-start "
            "will likely fail with 'No IP Address for Parent found!'."
        )
        env["next_actions"].append(
            "Re-poll ixia_list_vports until connection_state=connectedLinkUp "
            "and link_state=up before calling ixia_protocols_start_all / "
            "ixia_topology_start; or re-run ixia_load_config with a larger "
            "wait_for_vports_ms."
        )

    return env


def ixia_save_config(
    host: str,
    server_path: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Save the current session config to an ``.ixncfg`` file on the API
    server.

    Args:
        server_path: Absolute path on the API server's filesystem. If the
            target file exists it is **overwritten** without prompt. The
            parent directory must already exist on the server.
        confirm: Must be ``True``. Writing the file is the destructive bit
            (overwrites silently); the session itself is not modified.

    Uses ``ixn.SaveConfig(Files(path, local_file=False))`` directly — no
    SFTP round-trip. Local-file download will come in a future tool.
    """
    request = {
        "host": host, "port": port, "user": user,
        "server_path": server_path, "confirm": confirm,
    }
    guard = _destructive_guard(
        kind="save_config", host=host, port=port, confirm=confirm
    )
    if guard is not None:
        guard["request"] = request
        return guard
    if not server_path or not isinstance(server_path, str):
        return error_envelope(
            "server_path must be a non-empty absolute path on the API server.",
            kind="save_config", host=host, port=port,
            status="bad_argument",
        )

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="save_config",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="save_config", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )

    try:
        from ixnetwork_restpy import Files
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"ixnetwork_restpy import failed: {e}")
        return env

    try:
        with write_lock(host, port, user):
            s.ixn.SaveConfig(Files(server_path, local_file=False))
        env["result"] = {"saved_to": server_path}
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        env["next_actions"].append(
            "Verify the parent directory exists on the API server and the "
            "user has write permission."
        )
        return env


def register(mcp) -> None:
    mcp.tool()(ixia_list_configs)
    mcp.tool()(ixia_new_config)
    mcp.tool()(ixia_load_config)
    mcp.tool()(ixia_save_config)
