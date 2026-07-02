"""Thin Arista EOS eAPI client (JSON-RPC 2.0 over HTTP/S).

eAPI is the native EOS management API: one ``runCmds`` method that takes
a list of CLI commands and returns one structured result per command
(``format="json"``), or the raw CLI text (``format="text"``, needed for
commands without a JSON renderer such as ``show running-config``).

Lab switches ship self-signed certificates, so TLS verification is off —
same trust model as the SSH-based DNOS groups, which pin nothing either.
Credentials come from :class:`qactl.core.creds.AristaConfig`.
"""

from __future__ import annotations

from typing import Any, List

import requests
import urllib3

from qactl.core.creds import AristaConfig

# Self-signed lab certs — see module docstring.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class AristaError(RuntimeError):
    pass


class AristaClient:
    def __init__(self, cfg: AristaConfig, timeout: float = 30.0):
        self.cfg = cfg
        self.timeout = timeout
        self._session = requests.Session()
        self._session.auth = (cfg.user, cfg.password)
        self._session.verify = False

    def run_cmds(self, cmds: List[str], fmt: str = "json") -> List[Any]:
        """Run ``cmds`` on the switch; one result entry per command."""
        r = self._session.post(
            self.cfg.url,
            json={
                "jsonrpc": "2.0",
                "method": "runCmds",
                "params": {"version": 1, "cmds": list(cmds), "format": fmt},
                "id": "qactl",
            },
            timeout=self.timeout,
        )
        if r.status_code == 401:
            raise AristaError(
                f"eAPI authentication failed on {self.cfg.host} as {self.cfg.user!r} "
                f"(HTTP 401). Set ARISTA_USER / ARISTA_PASSWORD or pass --user/--password."
            )
        r.raise_for_status()
        d = r.json()
        err = d.get("error")
        if err:
            data = err.get("data") or []
            cli_errors = [
                m for item in data if isinstance(item, dict)
                for m in (item.get("errors") or [])
            ]
            detail = f": {'; '.join(cli_errors)}" if cli_errors else ""
            raise AristaError(
                f"eAPI error {err.get('code')} on {self.cfg.host}: "
                f"{err.get('message')}{detail}"
            )
        result = d.get("result")
        if not isinstance(result, list):
            raise AristaError(
                f"eAPI returned no result list on {self.cfg.host} "
                f"(is this an EOS box with 'management api http-commands' enabled?)"
            )
        return result

    @classmethod
    def connect(cls, host: str, *, timeout: float = 30.0, **overrides: Any) -> "AristaClient":
        return cls(AristaConfig.resolve(host, **overrides), timeout=timeout)
