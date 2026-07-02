"""Thin Arista EOS client over SSH.

The lab Arista boxes don't run eAPI (port 443/80 refused; EOS 4.16-era
images), so commands go over SSH instead: a non-interactive exec of
``enable\\n<cmd> | json`` returns exactly the same JSON payloads eAPI's
``runCmds`` would, and plain text for commands without a JSON renderer
(``show running-config``). ``enable`` is prefixed because config reads
require privileged mode; it produces no output of its own.

``run_cmds(cmds, fmt)`` keeps eAPI's contract — one result entry per
command, dicts for ``json``, ``{"output": text}`` for ``text`` — so the
tool layer doesn't care about the transport.

Host keys are auto-accepted (lab switches, same trust model as the
SSH-based DNOS groups). Credentials come from
:class:`qactl.core.creds.AristaConfig`.
"""

from __future__ import annotations

import json
from typing import Any, List, Tuple

import paramiko

from qactl.core.creds import AristaConfig


class AristaError(RuntimeError):
    pass


class AristaClient:
    def __init__(self, cfg: AristaConfig, timeout: float = 30.0):
        self.cfg = cfg
        self.timeout = timeout
        self._ssh: paramiko.SSHClient | None = None

    def _connection(self) -> paramiko.SSHClient:
        if self._ssh is None:
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                cli.connect(
                    self.cfg.host, port=self.cfg.port, username=self.cfg.user,
                    password=self.cfg.password, look_for_keys=False,
                    allow_agent=False, timeout=self.timeout,
                )
            except paramiko.AuthenticationException:
                raise AristaError(
                    f"SSH authentication failed on {self.cfg.host} as "
                    f"{self.cfg.user!r}. Set ARISTA_USER / ARISTA_PASSWORD "
                    f"or pass --user/--password."
                ) from None
            except (paramiko.SSHException, OSError) as e:
                raise AristaError(
                    f"could not reach {self.cfg.host}:{self.cfg.port} over SSH: {e}"
                ) from None
            self._ssh = cli
        return self._ssh

    def _exec(self, command: str) -> Tuple[int, str]:
        _, stdout, _ = self._connection().exec_command(command, timeout=self.timeout)
        out = stdout.read().decode("utf-8", "replace")
        return stdout.channel.recv_exit_status(), out

    def run_cmds(self, cmds: List[str], fmt: str = "json") -> List[Any]:
        """Run ``cmds`` on the switch; one result entry per command."""
        results: List[Any] = []
        for cmd in cmds:
            piped = f"{cmd} | json" if fmt == "json" else cmd
            rc, out = self._exec(f"enable\n{piped}")
            if rc != 0:
                errs = [ln.strip() for ln in out.splitlines()
                        if ln.lstrip().startswith("%")]
                raise AristaError(
                    f"EOS rejected {cmd!r} on {self.cfg.host}: "
                    f"{'; '.join(errs) or out.strip()[:200]}"
                )
            if fmt == "json":
                try:
                    results.append(json.loads(out))
                except ValueError:
                    raise AristaError(
                        f"{cmd!r} on {self.cfg.host} returned no parseable JSON "
                        f"(got {out.strip()[:120]!r})"
                    ) from None
            else:
                results.append({"output": out})
        return results

    def close(self) -> None:
        if self._ssh is not None:
            self._ssh.close()
            self._ssh = None

    @classmethod
    def connect(cls, host: str, *, timeout: float = 30.0, **overrides: Any) -> "AristaClient":
        return cls(AristaConfig.resolve(host, **overrides), timeout=timeout)
