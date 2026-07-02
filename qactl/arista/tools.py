"""Arista tool layer: envelope-returning functions for both fronts.

Shared by the CLI (``qactl arista ...``) and the stdio MCP server.
Read-only for now (#62): interface status, LLDP neighbors, running
config, version. Everything is a plain ``show`` — no ``--yes`` gate
needed until a config-apply surface lands.

The immediate lab need is free-port discovery on client switches, so
``arista_interfaces`` also derives ``free_candidates``: ports whose link
is down (``notconnect``/``disabled``). Cross-check against LLDP before
cabling — a candidate with a neighbor entry is stale, not free.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from qactl.core.creds import CredentialError
from qactl.core.envelope import error_envelope, ok_envelope
from qactl.arista.client import AristaClient, AristaError


_FREE_LINK_STATUSES = {"notconnect", "disabled"}


def _natural_key(name: str) -> List[Any]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def _client(
    kind: str, host: str, *, timeout: float = 30.0, user: Optional[str] = None,
    password: Optional[str] = None, port: Optional[int] = None,
) -> Tuple[Optional[AristaClient], Optional[dict]]:
    try:
        return AristaClient.connect(
            host, timeout=timeout, user=user, password=password, port=port,
        ), None
    except CredentialError as e:
        return None, error_envelope(str(e), kind=kind, status="bad_argument")


def _run(
    kind: str, host: str, fn: Callable[[AristaClient], dict], *, timeout: float = 30.0,
    user: Optional[str] = None, password: Optional[str] = None,
    port: Optional[int] = None,
) -> dict:
    client, err = _client(kind, host, timeout=timeout, user=user,
                          password=password, port=port)
    if err is not None:
        return err
    try:
        return fn(client)
    except AristaError as e:
        return error_envelope(str(e), kind=kind)
    except Exception as e:  # noqa: BLE001
        return error_envelope(f"{kind} failed: {e}", kind=kind)
    finally:
        client.close()


def arista_interfaces(
    host: str, *, timeout: float = 30.0, user: Optional[str] = None,
    password: Optional[str] = None, port: Optional[int] = None,
) -> Dict[str, Any]:
    """Interface status on an Arista switch, with free-port candidates.

    Runs ``show interfaces status``. ``free_candidates`` lists ports whose
    link is notconnect/disabled — the ports safe to consider for new
    cabling once cross-checked against ``arista_lldp``.
    """
    def fn(c: AristaClient) -> dict:
        (data,) = c.run_cmds(["show interfaces status"])
        statuses: Dict[str, Any] = data.get("interfaceStatuses") or {}
        free = sorted(
            (name for name, st in statuses.items()
             if (st.get("linkStatus") or "").lower() in _FREE_LINK_STATUSES),
            key=_natural_key,
        )
        return ok_envelope(
            kind="arista_interfaces",
            result={
                "host": host, "count": len(statuses),
                "free_candidates": free, "interfaces": statuses,
            },
            next_actions=[
                f"Cross-check with `qactl arista lldp {host}` before cabling: "
                f"a free candidate that still shows an LLDP neighbor is stale, not free."
            ] if free else [],
        )
    return _run("arista_interfaces", host, fn, timeout=timeout, user=user,
                password=password, port=port)


def arista_lldp(
    host: str, *, timeout: float = 30.0, user: Optional[str] = None,
    password: Optional[str] = None, port: Optional[int] = None,
) -> Dict[str, Any]:
    """LLDP neighbors on an Arista switch (maps local ports to fabric/DUT peers)."""
    def fn(c: AristaClient) -> dict:
        (data,) = c.run_cmds(["show lldp neighbors"])
        neighbors = data.get("lldpNeighbors") or []
        return ok_envelope(kind="arista_lldp", result={
            "host": host, "count": len(neighbors), "neighbors": neighbors,
        })
    return _run("arista_lldp", host, fn, timeout=timeout, user=user,
                password=password, port=port)


def arista_config(
    host: str, interfaces: Optional[List[str]] = None, *, timeout: float = 30.0,
    user: Optional[str] = None, password: Optional[str] = None,
    port: Optional[int] = None,
) -> Dict[str, Any]:
    """Running config — whole box, or per-interface sections.

    ``show running-config`` has no JSON renderer, so this asks for raw
    CLI text; the result carries it verbatim.
    """
    def fn(c: AristaClient) -> dict:
        if interfaces:
            cmds = [f"show running-config interfaces {i}" for i in interfaces]
            out = c.run_cmds(cmds, fmt="text")
            sections = {i: (r.get("output") or "") for i, r in zip(interfaces, out)}
            return ok_envelope(kind="arista_config", result={
                "host": host, "sections": sections,
            })
        (data,) = c.run_cmds(["show running-config"], fmt="text")
        return ok_envelope(kind="arista_config", result={
            "host": host, "text": data.get("output") or "",
        })
    return _run("arista_config", host, fn, timeout=timeout, user=user,
                password=password, port=port)


def arista_version(
    host: str, *, timeout: float = 30.0, user: Optional[str] = None,
    password: Optional[str] = None, port: Optional[int] = None,
) -> Dict[str, Any]:
    """``show version`` — model, EOS version, serial; the connectivity sanity check."""
    def fn(c: AristaClient) -> dict:
        (data,) = c.run_cmds(["show version"])
        return ok_envelope(kind="arista_version", result={"host": host, **data})
    return _run("arista_version", host, fn, timeout=timeout, user=user,
                password=password, port=port)


def register(mcp) -> None:
    """Wire the Arista tools onto a FastMCP (or compatible) instance."""
    mcp.tool()(arista_interfaces)
    mcp.tool()(arista_lldp)
    mcp.tool()(arista_config)
    mcp.tool()(arista_version)
