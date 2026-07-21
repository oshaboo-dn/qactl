"""Device42 tool layer: envelope-returning functions for both fronts.

Shared by the CLI (``qactl d42 ...``) and the stdio MCP server. Read-only:
device inventory + owner, rack/room/building placement, and PDU power feeds.
Every lookup accepts a device **name or serial** — the migration to the new
hostname scheme ({Site}{NN}-{ROLE}-{RACK}) does not touch serials, and
rack/PDU are read from Device42 fields (never derived from the device name).

(Serial-console resolution also reads Device42, but it backs the top-level
``qactl console`` command and lives in ``qactl.console.tools``, not here.)
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from qactl.core.creds import CredentialError
from qactl.core.envelope import error_envelope, ok_envelope
from qactl.device42.client import Device42Client, Device42Error, doql_quote


# Curated top-level device fields worth surfacing (the raw record has ~50).
_DEVICE_FIELDS = (
    "name", "serial_no", "asset_no", "category", "customer", "type",
    "in_service", "service_level", "os", "manufacturer", "hw_model",
    "last_updated", "notes",
)


def _client(kind: str) -> Tuple[Optional[Device42Client], Optional[dict]]:
    try:
        return Device42Client.connect(), None
    except CredentialError as e:
        return None, error_envelope(str(e), kind=kind, status="bad_argument")


def _run(kind: str, fn: Callable[[Device42Client], dict]) -> dict:
    client, err = _client(kind)
    if err is not None:
        return err
    try:
        return fn(client)
    except Device42Error as e:
        return error_envelope(str(e), kind=kind)
    except Exception as e:  # noqa: BLE001
        return error_envelope(f"{kind} failed: {e}", kind=kind)
    finally:
        client.close()


def _resolve_name(client: Device42Client, query: str) -> Optional[str]:
    """Resolve a device *name or serial* to its canonical Device42 name."""
    q = doql_quote(query.strip())
    rows = client.doql(
        f"SELECT name FROM view_device_v1 "
        f"WHERE name = '{q}' OR serial_no = '{q}' LIMIT 1"
    )
    return rows[0]["name"] if rows else None


def _owner(detail: Dict[str, Any]) -> Optional[str]:
    for cf in detail.get("custom_fields") or []:
        if cf.get("key") == "End User":
            return cf.get("value") or None
    return None


def d42_device(query: str) -> Dict[str, Any]:
    """Look up a lab device in Device42 by **name or serial**.

    Returns the curated inventory record — category, customer/scrum, serial,
    owner (the ``End User`` custom field), management IPs, in-service state,
    notes — plus the full raw ``custom_fields`` list.
    """
    def fn(c: Device42Client) -> dict:
        name = _resolve_name(c, query)
        if name is None:
            return error_envelope(
                f"no Device42 device matches name or serial {query!r}.",
                kind="d42_device", status="bad_argument",
                next_actions=["Check the exact name/serial in the Device42 web UI "
                              "(https://device42.dev.drivenets.net)."],
            )
        from urllib.parse import quote

        det = c.rest_get(f"/api/1.0/devices/name/{quote(name)}/")
        record = {k: det.get(k) for k in _DEVICE_FIELDS}
        record["owner"] = _owner(det)
        record["ip_addresses"] = [
            ip.get("ip") for ip in (det.get("ip_addresses") or []) if ip.get("ip")
        ]
        record["custom_fields"] = det.get("custom_fields") or []
        return ok_envelope(kind="d42_device", result=record, next_actions=[
            f"Rack/room/U placement: `qactl d42 rack {name}`.",
        ])
    return _run("d42_device", fn)


def d42_rack(query: str) -> Dict[str, Any]:
    """Look up a lab device's physical placement in Device42 by **name or serial**.

    Reads rack / row / room / building / U-position straight from Device42's
    fields via a single DOQL join — migration-proof (never parses the device
    name for the rack).
    """
    def fn(c: Device42Client) -> dict:
        name = _resolve_name(c, query)
        if name is None:
            return error_envelope(
                f"no Device42 device matches name or serial {query!r}.",
                kind="d42_rack", status="bad_argument",
            )
        q = doql_quote(name)
        rows = c.doql(
            "SELECT d.name AS device, d.serial_no, d.start_at AS u_position, "
            "r.name AS rack, r.row AS rack_row, rm.name AS room, b.name AS building "
            "FROM view_device_v1 d "
            "LEFT JOIN view_rack_v1 r ON d.calculated_rack_fk = r.rack_pk "
            "LEFT JOIN view_room_v1 rm ON r.room_fk = rm.room_pk "
            "LEFT JOIN view_building_v1 b ON rm.building_fk = b.building_pk "
            f"WHERE d.name = '{q}' LIMIT 1"
        )
        placement = rows[0] if rows else {"device": name}
        warnings = None
        if not placement.get("rack"):
            warnings = [f"{name} is not mounted in a rack in Device42 "
                        f"(likely a spare/unracked board)."]
        return ok_envelope(kind="d42_rack", result=placement, warnings=warnings)
    return _run("d42_rack", fn)


def _normalize_outlet(raw: str) -> Optional[int]:
    """Map a Device42 PDU port_name to the lab's outlet number.

    Mirrors ``parse_pdu_info()`` in the DNOS e2e utils / the console tool's
    merge: a leading ``B`` means the second bank (add 12), a leading ``A`` (or
    any other letter) is dropped, a bare number is taken as-is. Returns ``None``
    for anything unparseable (the raw ``outlet`` is still reported).
    """
    s = (raw or "").strip()
    if not s:
        return None
    if s[0].isalpha():
        rest = s[1:]
        if not rest.isdigit():
            return None
        return int(rest) + 12 if s[0].upper() == "B" else int(rest)
    return int(s) if s.isdigit() else None


def d42_power(query: str) -> Dict[str, Any]:
    """Look up a lab device's PDU power feed(s) in Device42 by **name or serial**.

    Reads the structured power-port relationship (``view_pduports_v1``) live —
    every PSU's PDU, outlet, and PDU model. This is real relationship data, not
    name-encoding, so it stays correct through the hostname migration (the PDU
    names it returns are already the new ``{Site}{NN}-PDU-{RACK}-{N}`` scheme).
    Read-only: reports the feeds; it does not switch power.
    """
    def fn(c: Device42Client) -> dict:
        name = _resolve_name(c, query)
        if name is None:
            return error_envelope(
                f"no Device42 device matches name or serial {query!r}.",
                kind="d42_power", status="bad_argument",
            )
        q = doql_quote(name)
        rows = c.doql(
            "SELECT d.name AS device, pdu.name AS pdu, pp.port_name AS outlet, "
            "pm.name AS model "
            "FROM view_pduports_v1 pp "
            "LEFT JOIN view_pdu_v1 pdu ON pp.pdu_fk = pdu.pdu_pk "
            "LEFT JOIN view_device_v2 d ON d.device_pk = pp.psu_device_fk "
            "LEFT JOIN view_pdumodel_v1 pm ON pdu.pdumodel_fk = pm.pdumodel_pk "
            f"WHERE d.name = '{q}' ORDER BY pdu.name, pp.port_name"
        )
        feeds = [
            {
                "pdu": r.get("pdu"),
                "outlet": r.get("outlet"),
                "outlet_number": _normalize_outlet(r.get("outlet") or ""),
                "model": r.get("model"),
            }
            for r in rows if r.get("pdu")
        ]
        warnings = None
        if not feeds:
            warnings = [f"{name} has no PDU power-port mapping in Device42."]
        return ok_envelope(
            kind="d42_power",
            result={"device": name, "feed_count": len(feeds), "feeds": feeds},
            warnings=warnings,
        )
    return _run("d42_power", fn)


def register(mcp) -> None:
    """Wire the Device42 tools onto a FastMCP (or compatible) instance."""
    mcp.tool()(d42_device)
    mcp.tool()(d42_rack)
    mcp.tool()(d42_power)
