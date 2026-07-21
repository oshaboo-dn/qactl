"""Thin PDU outlet-control client over SSH.

Switched PDUs expose a small interactive CLI over SSH. Two dialects are in the
lab, picked per host from :class:`qactl.core.creds.PduConfig` (rack-key match,
migration-safe):

* ``dev_outlet`` (default): ``dev outlet 1 <n> off|on|status`` — off reports
  ``Close``, on reports ``Open``.
* ``ol`` (APC-style): ``olOff|olOn|olStatus <n>`` — off/on by word.

Two passwords (primary + alt) are tried in order, matching the legacy console
tool. Logic ported from ``console_db/console.py``.
"""

from __future__ import annotations

import re
import time
from typing import Dict, Tuple

import paramiko

from qactl.core.creds import PduConfig, _pdu_rack_key


class PduError(RuntimeError):
    pass


class PduClient:
    def __init__(self, cfg: PduConfig, timeout: float = 15.0):
        self.cfg = cfg
        self.timeout = timeout

    # -- dialect ----------------------------------------------------------

    def dialect(self, pdu_host: str) -> str:
        return "ol" if _pdu_rack_key(pdu_host) in self.cfg.ol_hosts else "dev_outlet"

    # -- connection (primary then alt password) ---------------------------

    def _connect(self, pdu_host: str):
        last_err = None
        for pwd in (self.cfg.password, self.cfg.password_alt):
            if pwd == "" and last_err is not None:
                continue  # don't retry an empty alt after a real failure
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                cli.connect(pdu_host, username=self.cfg.user, password=pwd,
                            timeout=self.timeout, look_for_keys=False,
                            allow_agent=False)
            except paramiko.AuthenticationException as e:
                last_err = e
                continue
            except Exception as e:  # noqa: BLE001
                raise PduError(f"cannot reach PDU {pdu_host}: {e}") from None
            shell = cli.invoke_shell()
            time.sleep(2)
            if shell.recv_ready():
                shell.recv(65536)
            return cli, shell
        raise PduError(
            f"SSH auth failed on PDU {pdu_host} as {self.cfg.user!r} "
            f"(tried primary + alt password). Set CONSOLE_PDU_PASSWORD / "
            f"CONSOLE_PDU_PASSWORD_ALT."
        ) from last_err

    @staticmethod
    def _run(shell, cmd: str, wait: float = 2.0) -> str:
        shell.send(cmd + "\n")
        time.sleep(wait)
        out = ""
        while shell.recv_ready():
            out += shell.recv(65536).decode("utf-8", errors="replace")
        return out

    def _verb(self, dialect: str, verb: str, outlet: int) -> str:
        if dialect == "dev_outlet":
            return f"dev outlet 1 {outlet} {verb}"
        return {"off": f"olOff {outlet}", "on": f"olOn {outlet}",
                "status": f"olStatus {outlet}"}[verb]

    @staticmethod
    def _state(txt: str, dialect: str) -> str:
        t = txt.lower()
        if dialect == "dev_outlet":
            if "close" in t:
                return "off"
            if "open" in t:
                return "on"
            return "unknown"
        if re.search(r"\boff\b", t):
            return "off"
        if re.search(r"\bon\b", t):
            return "on"
        return "unknown"

    # -- operations -------------------------------------------------------

    def status(self, pdu_host: str, outlet: int) -> Tuple[str, str]:
        d = self.dialect(pdu_host)
        cli, shell = self._connect(pdu_host)
        try:
            raw = self._run(shell, self._verb(d, "status", outlet))
            return self._state(raw, d), raw.strip()
        finally:
            cli.close()

    def set_power(self, pdu_host: str, outlet: int, on: bool) -> Dict:
        d = self.dialect(pdu_host)
        want = "on" if on else "off"
        cli, shell = self._connect(pdu_host)
        try:
            self._run(shell, self._verb(d, want, outlet))
            time.sleep(2)
            state, raw = self._state_from(shell, d, outlet)
            return {"pdu": pdu_host, "outlet": outlet, "action": want,
                    "state": state, "ok": state == want, "raw": raw}
        finally:
            cli.close()

    def cycle(self, pdu_host: str, outlet: int, pause: float = 3.0) -> Dict:
        d = self.dialect(pdu_host)
        cli, shell = self._connect(pdu_host)
        try:
            self._run(shell, self._verb(d, "off", outlet))
            time.sleep(2)
            off_state, _ = self._state_from(shell, d, outlet)
            time.sleep(pause)
            self._run(shell, self._verb(d, "on", outlet))
            time.sleep(2)
            on_state, raw = self._state_from(shell, d, outlet)
            return {"pdu": pdu_host, "outlet": outlet, "action": "cycle",
                    "off_verified": off_state == "off",
                    "state": on_state, "ok": on_state == "on", "raw": raw}
        finally:
            cli.close()

    def _state_from(self, shell, dialect: str, outlet: int) -> Tuple[str, str]:
        raw = self._run(shell, self._verb(dialect, "status", outlet))
        return self._state(raw, dialect), raw.strip()
