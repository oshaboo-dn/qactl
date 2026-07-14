"""Persistent SSH-session daemon — one transport per device across invocations.

Every ``qactl`` invocation is a fresh process, so the in-process
``TransportRegistry`` re-authenticates SSH on every command. DNOS sshd
rate-limits rapid successive connections (10/min), which back-to-back
qactl calls trip constantly (see ``connect-retries.jsonl``). This module
moves the registry into a small per-user daemon that outlives
invocations: the CLI becomes a thin client that ships ``run_*`` calls
over a unix socket and gets the :class:`~.session.Invocation` back,
while the daemon keeps one warm SSH transport per ``(device, user)``.

Opt-in and zero-break by design:

* Routing only happens when :func:`enabled` says so — the
  ``QACTL_SESSION_DAEMON`` env var (``1``/``0`` wins either way) or the
  ``<state>/cli/session-daemon.enabled`` marker file (managed by
  ``qactl cli session on|off``).
* Any client-side failure to reach the daemon (not running, spawn
  failed, version skew) silently falls back to the direct in-process
  connect path — behaviour is then exactly today's.
* The daemon executes the *same* ``session.run_*`` code; only the
  process holding the paramiko objects changes.

Protocol: one JSON line per request over a per-user unix socket
(``<state>/cli/session-daemon.sock``, mode 0600), one JSON line back,
connection closed. Long-running commands simply keep the connection
open until done — the client reads with no deadline, matching direct
execution semantics.

Request::

    {"proto": 1, "version": "<qactl version>", "op": "run_once",
     "kwargs": {...}}

Response::

    {"ok": true, "invocation": {...}}                       # run_* ops
    {"ok": true, ...}                                       # ping/status/shutdown
    {"ok": false, "error": {"type": "ConnectError",
                            "message": "...", "transient": false}}

Ops: ``run_once`` / ``run_sequence`` / ``run_sequence_pw`` /
``run_probes`` / ``run_ncm_cli`` (mirroring :mod:`.session`), plus
``ping``, ``status``, ``shutdown``.

Callables can't cross the socket: ``stop_predicate`` arguments travel
as *names* (the well-known predicates carry a ``daemon_name`` attribute
at their definition site) and are re-resolved server-side; a predicate
without a name makes the client skip routing for that call.
``run_capture`` (channel-driver callable, long stateful captures) is
never routed.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

PROTO = 1

ROLE_ENV = "QACTL_SESSION_DAEMON_ROLE"          # "server" inside the daemon
ENABLE_ENV = "QACTL_SESSION_DAEMON"             # "1"/"0" beats the marker file
SOCK_ENV = "QACTL_SESSION_SOCK"                 # socket path override (tests)
AUTOSPAWN_ENV = "QACTL_SESSION_DAEMON_AUTOSPAWN"  # "0" disables spawn-on-miss
IDLE_ENV = "QACTL_SESSION_DAEMON_IDLE"          # idle-exit seconds

DEFAULT_IDLE_EXIT = 3600.0  # daemon exits after this long with no requests
_CONNECT_TIMEOUT = 1.0      # per-attempt client connect timeout
_SPAWN_WAIT = 3.0           # total budget to wait for a just-spawned daemon

_RUN_OPS = ("run_once", "run_sequence", "run_sequence_pw", "run_probes", "run_ncm_cli")


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("qactl")
    except Exception:
        return "0"


# --- paths / gating ---------------------------------------------------------


def socket_path() -> str:
    override = os.environ.get(SOCK_ENV)
    if override:
        return override
    from qactl.dnos.core import paths as _paths

    return str(_paths.state_dir("cli") / "session-daemon.sock")


def _marker_path() -> str:
    from qactl.dnos.core import paths as _paths

    return str(_paths.state_dir("cli") / "session-daemon.enabled")


def enabled() -> bool:
    """Should this process route ``run_*`` calls through the daemon?

    The daemon process itself must never route (it would call itself), so
    the server role env kills routing outright. Otherwise the env knob
    wins over the marker file so a single invocation can force either
    behaviour.
    """
    if os.environ.get(ROLE_ENV) == "server":
        return False
    raw = os.environ.get(ENABLE_ENV)
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return os.path.exists(_marker_path())


def set_enabled(on: bool) -> str:
    """Flip the marker file; returns its path."""
    path = _marker_path()
    if on:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("enabled by `qactl cli session on`\n")
    else:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    return path


# --- invocation (de)serialisation -------------------------------------------


def invocation_to_dict(inv: Any) -> Dict[str, Any]:
    return {
        "output": inv.output,
        "hit_prompt": inv.hit_prompt,
        "head_prompt_line": inv.head_prompt_line,
        "tail_prompt": inv.tail_prompt,
        "host": inv.host,
        "device": inv.device,
        "steps": [
            {
                "command": s.command,
                "head_prompt_line": s.head_prompt_line,
                "output": s.output,
                "tail_prompt": s.tail_prompt,
                "hit_prompt": s.hit_prompt,
                "line_buffer": s.line_buffer,
            }
            for s in inv.steps
        ],
    }


def invocation_from_dict(data: Dict[str, Any]) -> Any:
    from qactl.dnos.cli.core.session import Invocation, StepCapture

    return Invocation(
        output=data["output"],
        hit_prompt=data["hit_prompt"],
        head_prompt_line=data["head_prompt_line"],
        tail_prompt=data["tail_prompt"],
        host=data["host"],
        device=data["device"],
        steps=[
            StepCapture(
                command=s["command"],
                head_prompt_line=s["head_prompt_line"],
                output=s["output"],
                tail_prompt=s["tail_prompt"],
                hit_prompt=s["hit_prompt"],
                line_buffer=s.get("line_buffer", ""),
            )
            for s in data.get("steps", [])
        ],
    )


# --- client ------------------------------------------------------------------


class DaemonUnavailable(Exception):
    """Daemon can't be reached — caller should fall back to direct connect."""


class DaemonDiedMidRequest(Exception):
    """Connection broke after the request was sent — do NOT blindly rerun."""


def _read_line(sock: socket.socket) -> bytes:
    chunks: List[bytes] = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if chunk.endswith(b"\n"):
            break
    return b"".join(chunks)


def call_daemon(op: str, kwargs: Optional[Dict[str, Any]] = None, spawn: bool = True) -> Dict[str, Any]:
    """One request/response round-trip; spawns the daemon on first miss.

    Raises :class:`DaemonUnavailable` when no daemon can be reached and
    :class:`DaemonDiedMidRequest` when the connection broke after the
    request went out (the daemon may or may not have executed it).
    """
    payload = json.dumps(
        {"proto": PROTO, "version": _version(), "op": op, "kwargs": kwargs or {}}
    ).encode("utf-8") + b"\n"

    sock = _connect(spawn=spawn)
    sent = False
    try:
        sock.sendall(payload)
        sent = True
        sock.settimeout(None)  # long ops (config pushes, tar loads) run for minutes+
        raw = _read_line(sock)
    except OSError as exc:
        if sent:
            raise DaemonDiedMidRequest(str(exc)) from exc
        raise DaemonUnavailable(str(exc)) from exc
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if not raw:
        raise DaemonDiedMidRequest("daemon closed the connection without a response")
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise DaemonDiedMidRequest(f"unparseable daemon response: {exc}") from exc


def _connect(spawn: bool) -> socket.socket:
    path = socket_path()
    last: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(_CONNECT_TIMEOUT)
            sock.connect(path)
            return sock
        except OSError as exc:
            last = exc
            if attempt == 2 or not spawn or not _autospawn_allowed():
                break
            _spawn_daemon()
            deadline = time.time() + _SPAWN_WAIT
            while time.time() < deadline:
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(_CONNECT_TIMEOUT)
                    sock.connect(path)
                    return sock
                except OSError as exc2:
                    last = exc2
                    time.sleep(0.1)
            break
    raise DaemonUnavailable(f"cannot reach session daemon at {path}: {last}")


def _autospawn_allowed() -> bool:
    raw = os.environ.get(AUTOSPAWN_ENV)
    return raw is None or raw.strip().lower() not in ("0", "false", "no", "off")


def _spawn_daemon() -> None:
    """Launch a detached daemon process; failures are the caller's fallback."""
    from qactl.dnos.core import paths as _paths

    log_path = _paths.ensure_state_dir("cli") / "session-daemon.log"
    env = dict(os.environ)
    env[ROLE_ENV] = "server"
    try:
        with open(log_path, "ab") as fh:
            subprocess.Popen(
                [sys.executable, "-m", "qactl.dnos.cli.core.session_daemon"],
                stdin=subprocess.DEVNULL,
                stdout=fh,
                stderr=fh,
                start_new_session=True,
                close_fds=True,
                env=env,
            )
    except OSError:
        pass


def try_run_via_daemon(op: str, kwargs: Dict[str, Any]) -> Optional[Any]:
    """Route one ``run_*`` call through the daemon.

    Returns the deserialised :class:`~.session.Invocation`, or ``None``
    when the caller should fall back to the direct path (daemon
    unreachable / version skew). Errors the daemon reported are
    re-raised as their original exception types so envelope mapping in
    the tools is unchanged. A connection that breaks mid-request maps to
    a transient :class:`~.session.ConnectError` rather than a silent
    rerun — the command may have executed.
    """
    from qactl.dnos.cli.core.session import ConnectError, UnknownDeviceError

    try:
        resp = call_daemon(op, kwargs)
    except DaemonUnavailable:
        return None
    except DaemonDiedMidRequest as exc:
        raise ConnectError(
            f"session daemon died mid-request ({exc}); the command may or may "
            "not have run on the device — check device state before retrying. "
            "`qactl cli session status` inspects the daemon.",
            transient=True,
        ) from exc

    if resp.get("ok"):
        return invocation_from_dict(resp["invocation"])

    err = resp.get("error") or {}
    etype = err.get("type", "")
    message = err.get("message", "session daemon error")
    if etype == "version-mismatch":
        # A stale daemon from a previous install — retire it and run direct;
        # the next invocation spawns a fresh one from the current code.
        try:
            call_daemon("shutdown", spawn=False)
        except Exception:
            pass
        return None
    if etype == "UnknownDeviceError":
        raise UnknownDeviceError(message)
    if etype == "ConnectError":
        raise ConnectError(message, transient=bool(err.get("transient")))
    if etype == "ValueError":
        raise ValueError(message)
    raise RuntimeError(f"{etype or 'DaemonError'}: {message}")


# --- server ------------------------------------------------------------------


def _resolve_predicate(name: Optional[str]):
    if name is None:
        return None
    if name == "detect_error":
        from qactl.dnos.cli.core.errors import detect_error

        return lambda step: detect_error(step.output)[0]
    if name == "rejected_statement":
        from qactl.dnos.cli.core.edit_helpers import stop_on_rejected_statement

        return stop_on_rejected_statement
    raise ValueError(f"unknown stop_predicate name: {name!r}")


def _execute(op: str, kwargs: Dict[str, Any]) -> Any:
    """Run one ``run_*`` op against the daemon's shared registry."""
    from qactl.dnos.cli.core import session as _session
    from qactl.dnos.cli.core.registry import transport_registry

    kw = dict(kwargs)
    # The daemon snapshots DEVICE_HOSTS at startup, so a device registered
    # *after* it started (e.g. `qactl cli device add <name> --host <ip>`) is
    # invisible here and resolution wrongly raises "not in the device registry"
    # even though the canonical map on disk has it. On a miss, re-read just that
    # device from the map (a targeted refresh — avoids the transient empty window
    # a full reload_device_hosts() clear() would open across handler threads).
    dev = kw.get("device")
    if dev and dev not in _session.DEVICE_HOSTS:
        _session._refresh_alias_in_cache(dev)
    if "stop_predicate" in kw:
        kw["stop_predicate"] = _resolve_predicate(kw["stop_predicate"])
    if op == "run_sequence_pw":
        kw["commands"] = [(c, p) for c, p in kw["commands"]]
    elif op == "run_probes":
        kw["probes"] = [(prefix, key) for prefix, key in kw["probes"]]
    fn = getattr(_session, op)
    return fn(transport_registry, **kw)


# Dispatch table; tests monkeypatch entries to stub out real SSH.
_EXECUTORS: Dict[str, Any] = {op: _execute for op in _RUN_OPS}


class _State:
    """Liveness bookkeeping shared between handler threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.last_activity = time.time()
        self.started = time.time()
        self.requests = 0

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.requests += 1
            self.last_activity = time.time()

    def leave(self) -> None:
        with self.lock:
            self.active = max(0, self.active - 1)
            self.last_activity = time.time()

    def idle_for(self) -> float:
        with self.lock:
            if self.active:
                return 0.0
            return time.time() - self.last_activity


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:  # noqa: D102 - socketserver contract
        server: "_Server" = self.server  # type: ignore[assignment]
        raw = self.rfile.readline()
        if not raw:
            return
        server.state.enter()
        try:
            resp = self._dispatch(raw)
        finally:
            server.state.leave()
        try:
            self.wfile.write(json.dumps(resp).encode("utf-8") + b"\n")
        except OSError:
            pass

    def _dispatch(self, raw: bytes) -> Dict[str, Any]:
        server: "_Server" = self.server  # type: ignore[assignment]
        try:
            req = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            return {"ok": False, "error": {"type": "BadRequest", "message": str(exc)}}

        if req.get("proto") != PROTO or req.get("version") != server.version:
            return {
                "ok": False,
                "error": {
                    "type": "version-mismatch",
                    "message": (
                        f"daemon {server.version}/proto {PROTO} vs client "
                        f"{req.get('version')}/proto {req.get('proto')}"
                    ),
                },
            }

        op = req.get("op")
        kwargs = req.get("kwargs") or {}
        if op == "ping":
            return {"ok": True, "version": server.version, "pid": os.getpid()}
        if op == "status":
            from qactl.dnos.cli.core.registry import transport_registry

            return {
                "ok": True,
                "version": server.version,
                "pid": os.getpid(),
                "uptime_s": int(time.time() - server.state.started),
                "requests": server.state.requests,
                "transports": transport_registry.list_open(),
            }
        if op == "shutdown":
            threading.Thread(target=server.shutdown, daemon=True).start()
            return {"ok": True, "shutdown": True}
        if op not in _EXECUTORS:
            return {"ok": False, "error": {"type": "BadRequest", "message": f"unknown op {op!r}"}}

        try:
            inv = _EXECUTORS[op](op, kwargs)
            return {"ok": True, "invocation": invocation_to_dict(inv)}
        except Exception as exc:  # noqa: BLE001 - every error must cross the wire typed
            return {
                "ok": False,
                "error": {
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                    "transient": bool(getattr(exc, "transient", False)),
                },
            }


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str):
        self.state = _State()
        self.version = _version()
        super().__init__(path, _Handler)


def make_server(path: str) -> _Server:
    """Bind the unix socket (0600) and return the (unstarted) server."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    server = _Server(path)
    os.chmod(path, 0o600)
    return server


def _idle_exit_seconds() -> float:
    raw = os.environ.get(IDLE_ENV)
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return DEFAULT_IDLE_EXIT


def serve() -> int:
    """Daemon entrypoint: single-instance lock, serve until idle-exit."""
    import fcntl

    os.environ[ROLE_ENV] = "server"
    from qactl.dnos.core import paths as _paths

    state = _paths.ensure_state_dir("cli")
    lock_fh = open(state / "session-daemon.lock", "w")  # noqa: SIM115 - held for process lifetime
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return 0  # another daemon already runs — nothing to do
        raise

    path = socket_path()
    server = make_server(path)
    print(f"session-daemon: pid={os.getpid()} version={server.version} sock={path}", flush=True)

    idle_max = _idle_exit_seconds()

    def _watchdog() -> None:
        while True:
            time.sleep(30)
            if server.state.idle_for() > idle_max:
                print(f"session-daemon: idle > {idle_max:.0f}s, exiting", flush=True)
                server.shutdown()
                return

    threading.Thread(target=_watchdog, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            os.unlink(path)
        except OSError:
            pass
        from qactl.dnos.cli.core.registry import transport_registry

        transport_registry.close_all(reason="daemon-exit")
    return 0


if __name__ == "__main__":
    sys.exit(serve())
