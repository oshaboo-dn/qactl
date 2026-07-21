"""Power tool layer: resolve a device's PDU feed(s), then act on the outlet(s).

Targets come from Device42's power-port relationship (a device name/serial
resolves to one outlet per PSU — a dual-PSU box has two, and a real power-cycle
must hit both), or from an explicit ``--pdu``/``--outlet`` for a manual action.
The outlet passed to the PDU is the normalized number (bank ``B`` → +12), the
same value the PDU CLI expects.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from qactl.core.creds import CredentialError, PduConfig
from qactl.core.envelope import error_envelope, ok_envelope
from qactl.power.client import PduClient, PduError


def _device_targets(kind: str, query: str) -> Tuple[Optional[str],
                                                    List[Dict[str, Any]],
                                                    List[str], Optional[dict]]:
    """Resolve a device name/serial to ``[{pdu, outlet}]`` via Device42."""
    from qactl.device42.client import Device42Client
    from qactl.device42.tools import _resolve_name, power_feeds

    try:
        client = Device42Client.connect()
    except CredentialError as e:
        return None, [], [], error_envelope(str(e), kind=kind, status="bad_argument")
    try:
        name = _resolve_name(client, query)
        if name is None:
            return None, [], [], error_envelope(
                f"no Device42 device matches name or serial {query!r}.",
                kind=kind, status="bad_argument")
        feeds = power_feeds(client, name)
    except Exception as e:  # noqa: BLE001
        return None, [], [], error_envelope(f"{kind} lookup failed: {e}", kind=kind)
    finally:
        client.close()

    targets, warnings = [], []
    for f in feeds:
        if f["outlet_number"] is None:
            warnings.append(f"skipped {f['pdu']} outlet {f['outlet']!r} "
                            f"(unparseable outlet number).")
            continue
        targets.append({"pdu": f["pdu"], "outlet": f["outlet_number"]})
    if not targets and not warnings:
        warnings.append(f"{name} has no PDU power-port mapping in Device42 — "
                        f"use --pdu/--outlet to act manually.")
    return name, targets, warnings, None


def _resolve_targets(kind: str, query: Optional[str], pdu: Optional[str],
                     outlet: Optional[int]):
    if pdu is not None or outlet is not None:
        if not (pdu and outlet is not None):
            return None, [], [], error_envelope(
                "manual power op needs both --pdu and --outlet.",
                kind=kind, status="bad_argument")
        return None, [{"pdu": pdu, "outlet": outlet}], [], None
    if not query:
        return None, [], [], error_envelope(
            "give a device name/serial, or --pdu and --outlet for a manual op.",
            kind=kind, status="bad_argument")
    return _device_targets(kind, query)


def _client() -> Tuple[Optional[PduClient], Optional[dict]]:
    try:
        return PduClient(PduConfig.resolve()), None
    except CredentialError as e:
        return None, error_envelope(str(e), kind="power", status="bad_argument")


def _act(kind: str, query, pdu, outlet, fn) -> Dict[str, Any]:
    device, targets, warnings, err = _resolve_targets(kind, query, pdu, outlet)
    if err is not None:
        return err
    if not targets:
        return ok_envelope(kind=kind, result={"device": device, "results": []},
                           warnings=warnings or None)
    client, cerr = _client()
    if cerr is not None:
        return cerr
    results, errors = [], []
    for t in targets:
        try:
            results.append(fn(client, t["pdu"], int(t["outlet"])))
        except PduError as e:
            errors.append(str(e))
            results.append({"pdu": t["pdu"], "outlet": t["outlet"],
                            "ok": False, "error": str(e)})
    env = ok_envelope(
        kind=kind,
        result={"device": device, "target_count": len(targets), "results": results},
        warnings=warnings or None,
    )
    if errors or any(not r.get("ok", False) for r in results):
        env["status"] = "error" if errors else "warning"
        env["errors"].extend(errors)
    return env


def power_status(query=None, *, pdu=None, outlet=None) -> Dict[str, Any]:
    """Report each outlet's live on/off state (read-only)."""
    def fn(c: PduClient, host, ol):
        state, raw = c.status(host, ol)
        return {"pdu": host, "outlet": ol, "state": state, "ok": state != "unknown",
                "raw": raw}
    return _act("power_status", query, pdu, outlet, fn)


def power_set(on: bool, query=None, *, pdu=None, outlet=None) -> Dict[str, Any]:
    """Switch each outlet on or off (DESTRUCTIVE)."""
    kind = "power_on" if on else "power_off"
    return _act(kind, query, pdu, outlet, lambda c, h, o: c.set_power(h, o, on))


def power_cycle(query=None, *, pdu=None, outlet=None, pause: float = 3.0) -> Dict[str, Any]:
    """Power-cycle each outlet: off → pause → on (DESTRUCTIVE)."""
    return _act("power_cycle", query, pdu, outlet,
                lambda c, h, o: c.cycle(h, o, pause=pause))
