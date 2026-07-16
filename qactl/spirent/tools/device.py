"""Emulated-device ops for ``qactl spirent`` — create / list / start / stop / delete.

Builds an STC emulated device with an ``Ipv4If → [VlanIf →] EthIIIf`` stack on
a reserved port, via STC's ``DeviceCreate`` one-shot (then binds the device to
the port with the ``AffiliationPort`` relation — ``DeviceCreate`` alone does not
set it, confirmed live 2026-07-16). Attribute/relation names mirror the proven
``dnstc`` model and were verified against ``il-auto-containers``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from qactl.spirent.client import SpirentConnectionError
from qactl.spirent.client import stc_ops
from qactl.spirent.core import session as session_mod
from qactl.spirent.core.envelope import make_envelope


def _fail(env: Dict[str, Any], exc: Exception) -> Dict[str, Any]:
    env["status"] = "error"
    env["errors"].append(str(exc)[:600])
    if isinstance(exc, SpirentConnectionError):
        env["next_actions"].append(
            "Check $SPIRENT_HOST / --host and that the STC REST server is up."
        )
    return env


def _device_row(stc: Any, dev: str) -> Dict[str, Any]:
    return {
        "device": dev,
        "name": stc.get(dev, "Name"),
        "router_id": stc.get(dev, "RouterId"),
        "ipv4": stc.get(dev, "Ipv4Address"),
        "prefix": stc.get(dev, "Ipv4Prefix"),
        "gateway": stc.get(dev, "Ipv4GatewayAddress"),
        "vlan": stc.get(dev, "Vlan1"),
        "active": stc.get(dev, "Active") == "true",
        "gw_mac_resolve": stc.get(dev, "Ipv4GatewayMacResolveState"),
    }


def spirent_device_create(
    host: str,
    port: int,
    user: str,
    *,
    port_location: str,
    name: str,
    ip: str,
    prefix: int,
    gateway: str,
    vlan: Optional[int] = None,
    mac: Optional[str] = None,
    router_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create (or reconfigure) an emulated IPv4 device on a reserved port."""
    env = make_envelope(
        kind="spirent_device_create", host=host, port=port,
        request={"port_location": port_location, "name": name, "ip": ip,
                 "prefix": prefix, "gateway": gateway, "vlan": vlan,
                 "mac": mac, "router_id": router_id},
    )
    try:
        sess = session_mod.get_session(host, port, user)
        env["session"] = sess.full_name
        stc = sess.stc
        proj = stc_ops.project(stc)
        port_ref = stc_ops.find_port_by_location(stc, proj, port_location)
        if port_ref is None:
            env["status"] = "error"
            env["errors"].append(f"no reserved port at {port_location}")
            env["next_actions"].append(
                f"qactl spirent port reserve --location {port_location}")
            return env
        dev = stc_ops.find_device_by_name(stc, proj, name)
        if dev is None:
            ifstack = "Ipv4If VlanIf EthIIIf" if vlan is not None else "Ipv4If EthIIIf"
            ifcount = "1 1 1" if vlan is not None else "1 1"
            r = stc.perform("DeviceCreate", ParentList=port_ref,
                            IfStack=ifstack, IfCount=ifcount, DeviceCount=1)
            dev = (r.get("ReturnList") or "").split()[0]
            stc.config(dev, **{"AffiliationPort-targets": port_ref})
        eth = stc_ops.children(stc, dev, "EthIIIf")
        if mac and eth:
            stc.config(eth[0], SourceMac=mac)
        if vlan is not None:
            vif = stc_ops.children(stc, dev, "VlanIf")
            if vif:
                stc.config(vif[0], VlanId=str(vlan))
        ip4 = stc_ops.children(stc, dev, "Ipv4If")
        if ip4:
            stc.config(ip4[0], Address=ip, PrefixLength=str(prefix), Gateway=gateway)
        stc.config(dev, Name=name, RouterId=router_id or ip)
        stc.apply()
        env["result"] = _device_row(stc, dev)
    except Exception as exc:
        return _fail(env, exc)
    return env


def spirent_device_list(host: str, port: int, user: str) -> Dict[str, Any]:
    """List emulated devices in this session with IP / VLAN / gateway state."""
    env = make_envelope(kind="spirent_device_list", host=host, port=port)
    try:
        sess = session_mod.get_session(host, port, user)
        env["session"] = sess.full_name
        stc = sess.stc
        proj = stc_ops.project(stc)
        rows = [_device_row(stc, d) for d in stc_ops.devices(stc, proj)]
        env["result"] = {"count": len(rows), "devices": rows}
    except Exception as exc:
        return _fail(env, exc)
    return env


def _set_state(host, port, user, *, name, command, kind) -> Dict[str, Any]:
    env = make_envelope(kind=kind, host=host, port=port, request={"name": name})
    try:
        sess = session_mod.get_session(host, port, user)
        env["session"] = sess.full_name
        stc = sess.stc
        proj = stc_ops.project(stc)
        dev = stc_ops.find_device_by_name(stc, proj, name)
        if dev is None:
            env["status"] = "error"
            env["errors"].append(f"no device named {name!r}")
            return env
        stc.perform(command, DeviceList=dev)
        stc.apply()
        env["result"] = _device_row(stc, dev)
    except Exception as exc:
        return _fail(env, exc)
    return env


def spirent_device_start(host, port, user, *, name) -> Dict[str, Any]:
    return _set_state(host, port, user, name=name,
                      command="DeviceStart", kind="spirent_device_start")


def spirent_device_stop(host, port, user, *, name) -> Dict[str, Any]:
    return _set_state(host, port, user, name=name,
                      command="DeviceStop", kind="spirent_device_stop")


def spirent_device_delete(host, port, user, *, name) -> Dict[str, Any]:
    env = make_envelope(kind="spirent_device_delete", host=host, port=port,
                        request={"name": name})
    try:
        sess = session_mod.get_session(host, port, user)
        env["session"] = sess.full_name
        stc = sess.stc
        proj = stc_ops.project(stc)
        dev = stc_ops.find_device_by_name(stc, proj, name)
        if dev is None:
            env["status"] = "warning"
            env["warnings"].append(f"no device named {name!r}")
            env["result"] = {"name": name, "deleted": False}
            return env
        stc.delete(dev)
        stc.apply()
        env["result"] = {"name": name, "device": dev, "deleted": True}
    except Exception as exc:
        return _fail(env, exc)
    return env
