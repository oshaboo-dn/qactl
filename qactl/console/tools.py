"""Console-lookup tool: resolve a device's serial-console coordinates.

The console server + port come from Device42's netport cable relationship, so
this reuses the Device42 client/resolver — but it's an implementation detail of
``qactl console``, not a ``qactl d42`` lookup surface. Device42's console field
is free-text; a device whose entry isn't the clean ``"Console<N> @ console-X"``
form comes back unmapped (with the raw text) and must be connected manually.
"""

from __future__ import annotations

import re
from typing import Any, Dict

from qactl.core.envelope import error_envelope, ok_envelope
from qactl.device42.client import doql_quote
from qactl.device42.tools import _resolve_name, _run

# Clean console mapping: verbose_name like "Console9 @ console-b08".
_CONSOLE_RE = re.compile(r"[Cc]onsole\s*(\d+)\s*@\s*console-([A-Za-z0-9-]+)")


def console_resolve(query: str) -> Dict[str, Any]:
    """Resolve a lab device's serial-console server + port from Device42.

    Parses the console port's ``verbose_name`` (``"Console9 @ console-b08"`` →
    server ``CONSOLE-B08``, port ``9``). Returns an unmapped result (with the
    raw text) when Device42 has no cleanly-parseable entry.
    """
    def fn(c) -> dict:
        name = _resolve_name(c, query)
        if name is None:
            return error_envelope(
                f"no Device42 device matches name or serial {query!r}.",
                kind="console_resolve", status="bad_argument",
            )
        q = doql_quote(name)
        rows = c.doql(
            "SELECT sd.name AS dev, tp.verbose_name AS vn "
            "FROM view_netport_v1 tp "
            "LEFT JOIN view_netport_v1 sp ON (tp.netport_pk = sp.remote_netport_fk "
            "OR tp.remote_netport_fk = sp.netport_pk) "
            "LEFT JOIN view_device_v2 sd ON sd.device_pk = sp.device_fk "
            "WHERE (sp.port LIKE '%Console%' OR sp.port LIKE '%console%') "
            f"AND sd.name LIKE '{q}%'"
        )
        for r in rows:
            m = _CONSOLE_RE.search(r.get("vn") or "")
            if m:
                return ok_envelope(kind="console_resolve", result={
                    "device": name,
                    "console_server": "CONSOLE-" + m.group(2).upper(),
                    "port": int(m.group(1)),
                    "source": "device42",
                    "raw": r.get("vn"),
                })
        return ok_envelope(
            kind="console_resolve",
            result={"device": name, "console_server": None, "port": None,
                    "unparsed": [r.get("vn") for r in rows if r.get("vn")]},
            warnings=[f"{name}: no cleanly-parseable console mapping in Device42 — "
                      f"connect with `qactl console --server <CS> --port <N>`."],
        )
    return _run("console_resolve", fn)
