"""Interactive serial-console connect via a lab terminal server.

Reaching a device's serial console is not a plain ``ssh -p <port>``: you SSH
into the console server, then drive its **text menu** ("Port Access" → pick the
port) — and the menu layout differs per console-server model — before the raw
serial stream is bridged to your terminal. This module reproduces that flow
natively (paramiko + a raw-PTY passthrough), lifted from the proven
``console_db/console.py`` connect path.

This takes over the controlling terminal, so it only runs from an interactive
TTY; the CLI refuses (and falls back to a resolve-only lookup) when stdin is
not a TTY. Credentials come from :class:`qactl.core.creds.ConsoleServerConfig`.
"""

from __future__ import annotations

import re
import sys
import time

from qactl.core.creds import ConsoleServerConfig


class ConsoleError(RuntimeError):
    pass


_MENU_MARKERS = ("Select one:", "Main Menu")
_PORT_ACCESS_RE = re.compile(r"(?im)^\s*(\d+)\s*\.\s*Port\s+Access\b")
_BUSY_RE = re.compile(r"Exclusive mode and port busy|port\s+busy|in\s+use", re.I)


def _drain_until(chan, markers, tries=50, delay=0.1):
    buf = ""
    for _ in range(tries):
        if chan.recv_ready():
            buf += chan.recv(4096).decode("utf-8", errors="replace")
        if any(mrk in buf for mrk in markers):
            return buf, True
        time.sleep(delay)
    return buf, False


def connect(console_server: str, port_num: int, cfg: ConsoleServerConfig,
            *, timeout: float = 15.0) -> int:
    """Open an interactive serial-console session. Returns a process exit code.

    Blocks until the user detaches (Ctrl-D / EOF). Requires an interactive TTY.
    """
    try:
        import paramiko
    except ImportError:  # pragma: no cover - paramiko is a hard dep
        raise ConsoleError("paramiko is required for console connect.") from None
    import select
    import termios
    import tty

    if not sys.stdin.isatty():
        raise ConsoleError(
            "console connect needs an interactive terminal (stdin is not a TTY). "
            "Run it directly in your shell, or use --json to resolve the "
            "console server/port without connecting."
        )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(console_server, username=cfg.user, password=cfg.password,
                       timeout=timeout, look_for_keys=False, allow_agent=False)
    except paramiko.AuthenticationException:
        raise ConsoleError(
            f"SSH auth failed on {console_server} as {cfg.user!r}. "
            f"Set CONSOLE_CS_USER / CONSOLE_CS_PASSWORD."
        ) from None
    except Exception as e:  # noqa: BLE001
        raise ConsoleError(f"cannot connect to {console_server}: {e}") from None

    try:
        chan = client.invoke_shell(term="xterm", width=80, height=24)
        chan.settimeout(0.0)

        buf, ok = _drain_until(chan, _MENU_MARKERS)
        if not ok:
            raise ConsoleError(f"no console menu from {console_server} "
                               f"(got {buf[:160]!r})")

        # Menu slot for "Port Access" varies by model — parse, don't hardcode.
        m = _PORT_ACCESS_RE.search(buf)
        chan.send((m.group(1) if m else "3") + "\r")
        time.sleep(0.3)
        _drain_until(chan, ("Port Access", "port"), tries=30)

        chan.send(f"{port_num}\r")
        time.sleep(0.3)
        chan.send("\r")

        # Sniff for a "port busy" bounce before handing over the terminal.
        post = ""
        deadline = time.time() + 1.5
        while time.time() < deadline:
            if chan.recv_ready():
                post += chan.recv(8192).decode("utf-8", errors="replace")
            else:
                time.sleep(0.05)
        if _BUSY_RE.search(post):
            raise ConsoleError(
                f"{console_server} port {port_num} is busy (another user holds it)."
            )

        if post:
            sys.stdout.write(post)
            sys.stdout.flush()
        else:
            chan.send("\r")

        print(f"\r\n>>> Connected to {console_server} port {port_num} — "
              f"Ctrl-D to detach.\r", flush=True)

        oldtty = termios.tcgetattr(sys.stdin)
        try:
            tty.setraw(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
            while True:
                r, _, _ = select.select([chan, sys.stdin], [], [])
                if chan in r:
                    data = chan.recv(1024)
                    if not data:
                        break
                    sys.stdout.write(data.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                if sys.stdin in r:
                    x = sys.stdin.read(1)
                    if not x:
                        break
                    chan.send(x)
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, oldtty)
        return 0
    finally:
        client.close()
