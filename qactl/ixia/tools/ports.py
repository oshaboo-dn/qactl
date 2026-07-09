"""Physical chassis-port lifecycle: claim → connect → release (#52).

Before this module there was no qactl path to take a **free** chassis
port (e.g. ``100.64.0.56`` card 10 port 5) and bring it up as a vport.
The read-only tools (``ixia_list_vports`` / ``ixia_list_chassis``)
showed state; ``ixia_connect_check`` only probed reachability; ``topo
create`` built NGPF on top of *already-assigned* vports. The only way
to claim a port was raw REST (``vport`` location patch + ConnectPorts).

This fills that gap with three mutating tools:

- ``ixia_assign_port``   — bind ``chassis:card:port`` to a vport
  (create or rebind), take ownership, and (by default) connect it;
  optionally block until ``connectedLinkUp``.
- ``ixia_connect_ports`` — run ConnectPort on an already-assigned vport.
- ``ixia_release_port``  — drop ownership of a port (optionally delete
  the vport too).

All three are **mutating** and gated on ``confirm=True`` (CLI ``--yes``)
— they change shared session state on a chassis other clients may be
using. Seizing a port owned by someone else additionally needs
``force=True`` (CLI ``--force``).

Wire path
---------
The RestPy primitives used here are stable across IxNetwork builds:
``Vport.add(Name=)``, the ``Vport.Location`` attribute (the
``{chassisIp};{cardId};{portId}`` legacy form), ``Ixnetwork.AssignPorts``
(``Arg1=portNames``, ``Arg2=vports``, ``Arg3=clearOwnership``),
``Vport.ConnectPort()``, and ``Vport.ReleasePort()`` /
``Vport.UnassignPorts(Arg2=delete)``. The exact connect/ownership
behaviour is best confirmed against a live chassis — see the repo's
ssh-vs-cli rule (explore on the box, harden into the CLI).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from qactl.ixia.client.models import IxiaError

from qactl.ixia.core.envelope import make_envelope, error_envelope
from qactl.ixia.core.session import (
    DEFAULT_PORT, DEFAULT_USER,
    get_session, write_lock, session_id_of,
)
from qactl.ixia.tools._vport_wait import (
    READY_CONNECTION_STATE, READY_LINK_STATE,
    vport_state_snapshot, filter_vports, wait_for_vports_ready,
    stuck_vport_summary,
)


DEFAULT_CONNECT_WAIT_MS = 60_000


# ----------------------------------------------------------------------
# Parsing / lookup helpers
# ----------------------------------------------------------------------

def _parse_port_spec(spec: str) -> Tuple[str, str, str]:
    """Parse ``chassis:card:port`` into ``(chassis, card, port)``.

    The chassis is an IPv4 address or hostname (no colons), so a plain
    3-way colon split is unambiguous. ``card`` and ``port`` must be
    integers. Raises ``ValueError`` with an actionable message.
    """
    parts = (spec or "").strip().split(":")
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise ValueError(
            f"--port must be 'chassis:card:port' (e.g. 100.64.0.56:10:5), "
            f"got {spec!r}"
        )
    chassis, card, prt = (p.strip() for p in parts)
    if not card.isdigit() or not prt.isdigit():
        raise ValueError(
            f"card and port must be integers in {spec!r} "
            "(e.g. 100.64.0.56:10:5)"
        )
    return chassis, card, prt


def _location_string(chassis: str, card: str, prt: str) -> str:
    """Legacy ``{chassisIp};{cardId};{portId}`` form for ``Vport.Location``."""
    return f"{chassis};{card};{prt}"


def _assignment_key(chassis: str, card: str, prt: str) -> str:
    """``chassis:card:port`` — the string IxNetwork reports as ``AssignedTo``."""
    return f"{chassis}:{card}:{prt}"


def _find_chassis_port(ixn, chassis: str, card: str, prt: str):
    """Return the RestPy Port object for ``chassis:card:port`` or ``None``."""
    root = getattr(ixn, "AvailableHardware", None)
    if root is None:
        return None
    for ch in root.Chassis.find():
        ch_host = getattr(ch, "Hostname", "") or getattr(ch, "Ip", "")
        if ch_host != chassis:
            continue
        for cd in ch.Card.find():
            if str(getattr(cd, "CardId", "")) != str(card):
                continue
            for pt in cd.Port.find():
                if str(getattr(pt, "PortId", "")) == str(prt):
                    return pt
    return None


def _find_vport(ixn, *, name: Optional[str] = None,
                href: Optional[str] = None,
                assigned_to: Optional[str] = None):
    """Find the first vport matching name / href / AssignedTo, else ``None``."""
    for v in ixn.Vport.find():
        if href and getattr(v, "href", "") == href:
            return v
        if name and getattr(v, "Name", "") == name:
            return v
        if assigned_to and (getattr(v, "AssignedTo", "") or "") == assigned_to:
            return v
    return None


def _vport_row(v) -> Dict[str, Any]:
    return {
        "name": getattr(v, "Name", ""),
        "href": getattr(v, "href", ""),
        "assigned_to": getattr(v, "AssignedTo", "") or None,
        "connection_state": getattr(v, "ConnectionState", ""),
        "link_state": getattr(v, "State", ""),
    }


def _confirm_gate(kind: str, host: str, port: int, confirm: bool,
                  action: str) -> Optional[Dict[str, Any]]:
    """``confirm=True``-required envelope for the (non-delete) port mutators."""
    if confirm is True:
        return None
    return error_envelope(
        f"{action} mutates the live IxNetwork session. Re-call with "
        "confirm=True after reviewing the arguments.",
        kind=kind, host=host, port=port,
        status="confirmation_required",
        next_actions=["Re-invoke with confirm=True (CLI: --yes)."],
    )


# ----------------------------------------------------------------------
# assign
# ----------------------------------------------------------------------

def ixia_assign_port(
    host: str,
    port_spec: str,
    name: Optional[str] = None,
    connect: bool = True,
    wait: bool = False,
    wait_timeout_ms: int = DEFAULT_CONNECT_WAIT_MS,
    force: bool = False,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Claim a chassis port (``chassis:card:port``) onto a vport.

    Creates a vport (or rebinds an existing one named ``name``), points
    its ``Location`` at the physical port, and — unless
    ``connect=False`` — runs ``AssignPorts`` to take ownership and
    connect it. With ``wait=True`` it then blocks (up to
    ``wait_timeout_ms``) for the vport to reach
    ``connectedLinkUp`` + link ``up``.

    Refuses a port already owned by another client unless ``force=True``
    (which clears the existing owner). ``confirm=True`` (CLI ``--yes``)
    is required — this changes shared chassis state.

    Args:
        port_spec: ``chassis:card:port`` (e.g. ``100.64.0.56:10:5``).
        name: vport name to create or rebind. Defaults to a name
            derived from the port spec.
        connect: ConnectPorts after assigning (default True).
        wait: After connect, block until the vport is up.
        wait_timeout_ms: Deadline for ``wait`` (default 60000).
        force: Seize the port even if owned by another client.
    """
    request = {
        "host": host, "port": port, "user": user,
        "port_spec": port_spec, "name": name,
        "connect": connect, "wait": wait,
        "wait_timeout_ms": wait_timeout_ms, "force": force,
        "confirm": confirm,
    }
    try:
        chassis, card, prt = _parse_port_spec(port_spec)
    except ValueError as e:
        return error_envelope(
            str(e), kind="assign_port", host=host, port=port,
            status="bad_argument",
        )

    gate = _confirm_gate("assign_port", host, port, confirm, "assign_port")
    if gate is not None:
        return gate

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="assign_port",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="assign_port", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ixn = s.ixn
        pt = _find_chassis_port(ixn, chassis, card, prt)
        if pt is None:
            env["status"] = "error"
            env["errors"].append(
                f"Chassis port {_assignment_key(chassis, card, prt)} not "
                "found in AvailableHardware."
            )
            env["next_actions"].append(
                "Run `qactl ixia session chassis` to list known ports."
            )
            return env

        owner = (getattr(pt, "Owner", "") or "").strip()
        if owner and not force:
            env["status"] = "error"
            env["errors"].append(
                f"Port {_assignment_key(chassis, card, prt)} is owned by "
                f"{owner!r}; refusing to seize it."
            )
            env["next_actions"].append(
                "Re-run with --force to clear the current owner."
            )
            return env

        loc = _location_string(chassis, card, prt)
        vport_name = name or f"vport_{chassis}_{card}_{prt}"

        with write_lock(host, port, user):
            vp = _find_vport(ixn, name=vport_name)
            created = False
            if vp is None:
                vp = ixn.Vport.add(Name=vport_name)
                created = True
            vp.Location = loc
            if connect:
                ixn.AssignPorts([], [getattr(vp, "href", vp)], bool(force))

        result: Dict[str, Any] = {
            "vport": _vport_row(vp),
            "created_vport": created,
            "connected": bool(connect),
            "forced": bool(force),
            "port": {
                "chassis": chassis, "card": card, "port": prt,
                "location": loc,
                "previous_owner": owner or None,
            },
        }

        if connect and wait:
            href = getattr(vp, "href", "")
            ready, snap, elapsed = wait_for_vports_ready(
                s, timeout_s=max(0.0, wait_timeout_ms / 1000.0),
                only_hrefs=[href] if href else None,
            )
            result["ready"] = ready
            result["wait_elapsed_s"] = elapsed
            result["vport_state"] = snap
            if not ready:
                env["status"] = "warning"
                env["warnings"].append(
                    f"Port assigned, but vport did not reach "
                    f"{READY_CONNECTION_STATE}+{READY_LINK_STATE} within "
                    f"{wait_timeout_ms} ms."
                )
                env["next_actions"].append(
                    "Check cabling / the DUT side, then re-run "
                    "`qactl ixia session wait-vports`."
                )
                if snap:
                    env["warnings"].append(
                        f"current: {stuck_vport_summary(snap)}"
                    )

        env["result"] = result
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


# ----------------------------------------------------------------------
# connect-ports
# ----------------------------------------------------------------------

def ixia_connect_ports(
    host: str,
    vport: str,
    wait: bool = False,
    wait_timeout_ms: int = DEFAULT_CONNECT_WAIT_MS,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Run ConnectPort on an already-assigned vport (by name or href).

    Use this when a vport already has a ``Location`` (assignment) but is
    ``assignedUnconnected`` and you just need to (re)connect the
    physical link. ``confirm=True`` (CLI ``--yes``) required.
    """
    request = {
        "host": host, "port": port, "user": user,
        "vport": vport, "wait": wait,
        "wait_timeout_ms": wait_timeout_ms, "confirm": confirm,
    }
    if not (vport or "").strip():
        return error_envelope(
            "--vport is required (a vport name or href).",
            kind="connect_ports", host=host, port=port,
            status="bad_argument",
        )

    gate = _confirm_gate("connect_ports", host, port, confirm, "connect_ports")
    if gate is not None:
        return gate

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="connect_ports",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="connect_ports", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ixn = s.ixn
        target = vport.strip()
        vp = _find_vport(ixn, name=target, href=target)
        if vp is None:
            env["status"] = "error"
            env["errors"].append(f"vport {target!r} not found.")
            env["next_actions"].append(
                "Run `qactl ixia session vports` to list vports."
            )
            return env

        with write_lock(host, port, user):
            vp.ConnectPort()

        result: Dict[str, Any] = {"vport": _vport_row(vp)}

        if wait:
            href = getattr(vp, "href", "")
            ready, snap, elapsed = wait_for_vports_ready(
                s, timeout_s=max(0.0, wait_timeout_ms / 1000.0),
                only_hrefs=[href] if href else None,
            )
            result["ready"] = ready
            result["wait_elapsed_s"] = elapsed
            result["vport_state"] = snap
            if not ready:
                env["status"] = "warning"
                env["warnings"].append(
                    f"ConnectPort issued, but vport did not reach "
                    f"{READY_CONNECTION_STATE}+{READY_LINK_STATE} within "
                    f"{wait_timeout_ms} ms."
                )

        env["result"] = result
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


# ----------------------------------------------------------------------
# release
# ----------------------------------------------------------------------

def ixia_release_port(
    host: str,
    port_spec: Optional[str] = None,
    vport: Optional[str] = None,
    delete: bool = False,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Drop ownership of a port (optionally delete its vport).

    Identify the target by ``port_spec`` (``chassis:card:port``, matched
    against the vport's ``AssignedTo``) or by ``vport`` (name or href).
    By default this releases the hardware port but keeps the vport
    object; ``delete=True`` removes the vport too. ``confirm=True``
    (CLI ``--yes``) required.
    """
    request = {
        "host": host, "port": port, "user": user,
        "port_spec": port_spec, "vport": vport,
        "delete": delete, "confirm": confirm,
    }
    if not (port_spec or "").strip() and not (vport or "").strip():
        return error_envelope(
            "Provide --port (chassis:card:port) or --vport (name|href).",
            kind="release_port", host=host, port=port,
            status="bad_argument",
        )

    assigned_to = None
    if (port_spec or "").strip():
        try:
            chassis, card, prt = _parse_port_spec(port_spec)
            assigned_to = _assignment_key(chassis, card, prt)
        except ValueError as e:
            return error_envelope(
                str(e), kind="release_port", host=host, port=port,
                status="bad_argument",
            )

    gate = _confirm_gate("release_port", host, port, confirm, "release_port")
    if gate is not None:
        return gate

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="release_port",
            host=host, port=port, status="connect_error",
        )

    env = make_envelope(
        kind="release_port", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        ixn = s.ixn
        target = (vport or "").strip() or None
        vp = _find_vport(
            ixn, name=target, href=target, assigned_to=assigned_to,
        )
        if vp is None:
            env["status"] = "error"
            ident = target or assigned_to
            env["errors"].append(f"No vport found for {ident!r}.")
            env["next_actions"].append(
                "Run `qactl ixia session vports` / `... chassis` to check."
            )
            return env

        row = _vport_row(vp)
        with write_lock(host, port, user):
            if delete:
                vp.UnassignPorts(Arg2=True)
            else:
                vp.ReleasePort()

        env["result"] = {"vport": row, "deleted_vport": bool(delete)}
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(ixia_assign_port)
    mcp.tool()(ixia_connect_ports)
    mcp.tool()(ixia_release_port)
