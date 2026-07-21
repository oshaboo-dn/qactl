"""Thin Device42 CMDB client (read-only).

Device42 exposes two HTTP surfaces we use, both behind one Basic-auth
header (:class:`qactl.core.creds.Device42Config`):

* the **REST API** (``<rest_base>/api/1.0/...``) — rich per-device records
  including the ``End User`` owner and other custom fields; and
* the **DOQL** query endpoint (``.../services/data/v1.0/query/``) — arbitrary
  read-only SQL against Device42's views, which is the only surface that
  cleanly joins a device to its rack/room/building.

Both return decoded JSON; the tool layer shapes it into the qactl
envelope. The lab Device42 uses a self-signed cert, so TLS verification
is off (matching the ``curl -k`` recipe the workspace has always used).

DOQL string values are escaped by doubling single quotes; identifiers
(view/column names) are never user-supplied, so this is sufficient.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests
from urllib3.exceptions import InsecureRequestWarning

from qactl.core.creds import Device42Config


class Device42Error(RuntimeError):
    """Raised when a Device42 REST/DOQL request fails or returns non-2xx."""


def doql_quote(value: str) -> str:
    """Escape a string literal for a DOQL ``WHERE ... = '<value>'`` clause."""
    return value.replace("'", "''")


class Device42Client:
    def __init__(self, cfg: Device42Config, timeout: float = 45.0):
        self.cfg = cfg
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = cfg.auth
        self._session.verify = cfg.verify_tls
        if not cfg.verify_tls:
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    # -- REST -------------------------------------------------------------

    def rest_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET ``<rest_base><path>`` and return decoded JSON."""
        url = f"{self.cfg.rest_base}{path}"
        try:
            r = self._session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise Device42Error(f"could not reach Device42 REST at {url}: {e}") from None
        if r.status_code == 404:
            raise Device42Error(f"Device42 REST {path} -> HTTP 404 (not found)")
        if not r.ok:
            raise Device42Error(
                f"Device42 REST {path} -> HTTP {r.status_code}: {r.text[:200]}"
            )
        try:
            return r.json()
        except ValueError:
            raise Device42Error(f"Device42 REST {path} returned non-JSON body") from None

    # -- DOQL -------------------------------------------------------------

    def doql(self, sql: str) -> List[Dict[str, Any]]:
        """Run a read-only DOQL query and return the rows (list of dicts)."""
        try:
            r = self._session.post(
                self.cfg.endpoint,
                data={"query": sql, "output_type": "json"},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise Device42Error(f"could not reach Device42 DOQL endpoint: {e}") from None
        if not r.ok:
            # DOQL returns 500 on a bad column/view name — surface the query.
            raise Device42Error(
                f"Device42 DOQL query failed (HTTP {r.status_code}): {sql}"
            )
        try:
            data = r.json()
        except ValueError:
            raise Device42Error("Device42 DOQL returned non-JSON body") from None
        return data if isinstance(data, list) else []

    def close(self) -> None:
        self._session.close()

    @classmethod
    def connect(cls, *, timeout: float = 45.0, **overrides: Any) -> "Device42Client":
        return cls(Device42Config.resolve(**overrides), timeout=timeout)
