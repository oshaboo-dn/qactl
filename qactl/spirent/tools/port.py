"""Physical-port ops for ``qactl spirent`` — reserve / release / status.

Talks to the STC REST server through the raw ``stcrestclient`` primitives
(``create`` / ``config`` / ``perform`` / ``get`` / ``apply`` / ``delete``)
exposed on the connected session — no full object model needed. The exact
command + attribute names mirror cheetah's proven ``dnstc`` ``StcPort`` and
were confirmed live against ``il-auto-containers`` on 2026-07-16:

- reserve: ``AttachPorts(PortList, AutoConnect=TRUE, RevokeOwner=force)`` then
  ``apply()``; the active PHY appears only after attach — read link via
  ``get(port, "activephy-Targets")`` then ``get(phy, "LinkStatus")``.
- release: ``ReleasePort(portList)``.

Port **location** is ``//<chassis>/<slot>/<port>`` where ``<chassis>`` is best
given as the chassis **IP** (a bare name may resolve to the wrong cached
chassis). Example: ``//100.64.3.238/6/13``.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from qactl.spirent.client import SpirentConnectionError
from qactl.spirent.client.stc_ops import (
    find_port_by_location as _find_by_location,
    is_local as _is_local,
    link_status as _link_status,
    ports as _ports,
    project as _project,
)
from qactl.spirent.core import session as session_mod
from qactl.spirent.core.envelope import make_envelope


def _fail(env: Dict[str, Any], exc: Exception) -> Dict[str, Any]:
    env["status"] = "error"
    env["errors"].append(str(exc)[:600])
    if isinstance(exc, SpirentConnectionError):
        env["next_actions"].append(
            "Check $SPIRENT_HOST / --host and that the STC REST server is up; "
            "`pip install qactl[spirent]` if stcrestclient is missing."
        )
    return env


def _port_row(stc: Any, port_ref: str) -> Dict[str, Any]:
    online = stc.get(port_ref, "Online")
    active = stc.get(port_ref, "Active")
    return {
        "port": port_ref,
        "name": stc.get(port_ref, "Name"),
        "location": stc.get(port_ref, "Location"),
        "link_status": _link_status(stc, port_ref),
        "online": online == active == "true",
    }


def spirent_reserve_port(
    host: str,
    port: int,
    user: str,
    *,
    location: str,
    name: Optional[str] = None,
    force: bool = False,
    wait_up: bool = True,
    timeout: int = 40,
) -> Dict[str, Any]:
    """Reserve (attach) the physical port at ``location`` in our STC session."""
    env = make_envelope(
        kind="spirent_port_reserve", host=host, port=port,
        request={"location": location, "name": name, "force": force,
                 "wait_up": wait_up, "timeout": timeout},
    )
    try:
        sess = session_mod.get_session(host, port, user)
        env["session"] = sess.full_name
        stc = sess.stc
        proj = _project(stc)
        ref = _find_by_location(stc, proj, location)
        if ref is None:
            ref = stc.create("port", under=proj, Name=name or f"qactl {location}")
            stc.config(ref, Location=location)
        elif name:
            stc.config(ref, Name=name)
        if not _is_local(location):
            stc.perform(
                "AttachPorts", PortList=ref,
                AutoConnect="TRUE", RevokeOwner="TRUE" if force else "FALSE",
            )
            stc.apply()
            link = None
            deadline = max(1, int(timeout)) if wait_up else 1
            for _ in range(deadline):
                link = _link_status(stc, ref)
                if link == "UP" or not wait_up:
                    break
                time.sleep(1)
            if wait_up and link != "UP":
                env["status"] = "warning"
                env["warnings"].append(
                    f"port attached but link not UP after {timeout}s (link={link})"
                )
        env["result"] = _port_row(stc, ref)
    except Exception as exc:
        return _fail(env, exc)
    return env


def spirent_release_port(
    host: str,
    port: int,
    user: str,
    *,
    location: str,
) -> Dict[str, Any]:
    """Release the physical port reserved at ``location`` (DESTRUCTIVE)."""
    env = make_envelope(
        kind="spirent_port_release", host=host, port=port,
        request={"location": location},
    )
    try:
        sess = session_mod.get_session(host, port, user)
        env["session"] = sess.full_name
        stc = sess.stc
        proj = _project(stc)
        ref = _find_by_location(stc, proj, location)
        if ref is None:
            env["status"] = "warning"
            env["warnings"].append(f"no port at {location} in this session")
            env["result"] = {"location": location, "released": False}
            return env
        if not _is_local(location):
            stc.perform("ReleasePort", portList=ref)
            stc.apply()
        env["result"] = {"location": location, "port": ref, "released": True}
    except Exception as exc:
        return _fail(env, exc)
    return env


def spirent_port_status(host: str, port: int, user: str) -> Dict[str, Any]:
    """List the ports in our STC session with location + link state."""
    env = make_envelope(kind="spirent_port_status", host=host, port=port)
    try:
        sess = session_mod.get_session(host, port, user)
        env["session"] = sess.full_name
        stc = sess.stc
        proj = _project(stc)
        rows = [_port_row(stc, p) for p in _ports(stc, proj)]
        env["result"] = {"count": len(rows), "ports": rows}
    except Exception as exc:
        return _fail(env, exc)
    return env
