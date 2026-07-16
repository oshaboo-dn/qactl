"""BGP-router ops for ``qactl spirent`` — add / status / delete on a device.

Adds a ``BgpRouterConfig`` to an emulated device, handling 4-byte ASNs
(``Enable4ByteAsNum`` + asdot ``AsNum4Byte``) and the two peer-address modes
(``UseGatewayAsDut`` = peer is the device gateway, or an explicit ``DutIpv4Addr``).

``--strict`` advertises BGP-BFD **strict-mode** by attaching a custom capability
with ``CapabilityType=74`` (draft-ietf-idr-bgp-bfd-strict-mode; the "Cap-74"
the DNOS interop tests use). ``--bfd`` flips ``EnableBfd``. Names/attrs verified
live against ``il-auto-containers`` 2026-07-16.
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


def _bgp_row(stc: Any, dev: str, bgp: str) -> Dict[str, Any]:
    strict = stc_ops.strict_capability(stc, bgp)
    row = {
        "device": stc.get(dev, "Name"),
        "bgp": bgp,
        "local_as": stc.get(bgp, "AsNum4Byte")
        if stc.get(bgp, "Enable4ByteAsNum") == "true" else stc.get(bgp, "AsNum"),
        "peer_as": stc.get(bgp, "DutAsNum4Byte")
        if stc.get(bgp, "Enable4ByteDutAsNum") == "true" else stc.get(bgp, "DutAsNum"),
        "use_gateway_as_peer": stc.get(bgp, "UseGatewayAsDut") == "true",
        "peer_ip": stc.get(dev, "Ipv4GatewayAddress")
        if stc.get(bgp, "UseGatewayAsDut") == "true" else stc.get(bgp, "DutIpv4Addr"),
        "bfd": stc.get(bgp, "EnableBfd") == "true",
        "strict_mode_cap74": strict is not None and stc.get(strict, "Active") == "true",
        "router_state": stc.get(bgp, "RouterState"),
        "gw_mac_resolve": stc.get(dev, "Ipv4GatewayMacResolveState"),
    }
    return row


def spirent_bgp_add(
    host: str,
    port: int,
    user: str,
    *,
    device: str,
    local_as: int,
    peer_as: Optional[int] = None,
    peer: Optional[str] = None,
    use_gateway: bool = True,
    bfd: bool = False,
    strict: bool = False,
) -> Dict[str, Any]:
    """Add / reconfigure a BGP router on ``device`` (idempotent)."""
    env = make_envelope(
        kind="spirent_bgp_add", host=host, port=port,
        request={"device": device, "local_as": local_as, "peer_as": peer_as,
                 "peer": peer, "use_gateway": use_gateway, "bfd": bfd,
                 "strict": strict},
    )
    try:
        sess = session_mod.get_session(host, port, user)
        env["session"] = sess.full_name
        stc = sess.stc
        proj = stc_ops.project(stc)
        dev = stc_ops.find_device_by_name(stc, proj, device)
        if dev is None:
            env["status"] = "error"
            env["errors"].append(f"no device named {device!r}")
            env["next_actions"].append("qactl spirent device create ...")
            return env
        existing = stc_ops.children(stc, dev, "BgpRouterConfig")
        bgp = existing[0] if existing else stc.create(
            "BgpRouterConfig", under=dev, IpVersion="IPV4")
        stc_ops.apply_as(stc, bgp, local=True, asn=int(local_as))
        stc_ops.apply_as(stc, bgp, local=False, asn=int(peer_as if peer_as else local_as))
        if use_gateway:
            stc.config(bgp, UseGatewayAsDut="TRUE")
        else:
            if not peer:
                env["status"] = "bad_argument"
                env["errors"].append("--peer required when --no-use-gateway")
                return env
            stc.config(bgp, UseGatewayAsDut="FALSE", DutIpv4Addr=peer)
        # Strict-mode (cap-74) gates BGP on BFD, so it implies BFD. Either way,
        # EnableBfd on the BGP router requires a BfdRouterConfig on the device.
        need_bfd = bfd or strict
        if need_bfd and not stc_ops.children(stc, dev, "BfdRouterConfig"):
            stc.create("BfdRouterConfig", under=dev)
        stc.config(bgp, EnableBfd="TRUE" if need_bfd else "FALSE")
        if strict:
            cap = stc_ops.strict_capability(stc, bgp)
            if cap is None:
                cap = stc.create("BgpCustomCapability", under=bgp,
                                 CapabilityType=stc_ops.STRICT_CAP_TYPE,
                                 CapLength="1", Capability="0")
            stc.config(cap, Active="TRUE")
        else:
            cap = stc_ops.strict_capability(stc, bgp)
            if cap is not None:
                stc.config(cap, Active="FALSE")
        stc.apply()
        env["result"] = _bgp_row(stc, dev, bgp)
    except Exception as exc:
        return _fail(env, exc)
    return env


def spirent_bgp_status(host: str, port: int, user: str,
                       *, device: Optional[str] = None) -> Dict[str, Any]:
    """Report BGP router state for one device or all devices in the session."""
    env = make_envelope(kind="spirent_bgp_status", host=host, port=port,
                        request={"device": device})
    try:
        sess = session_mod.get_session(host, port, user)
        env["session"] = sess.full_name
        stc = sess.stc
        proj = stc_ops.project(stc)
        rows = []
        for dev in stc_ops.devices(stc, proj):
            if device and stc.get(dev, "Name") != device:
                continue
            for bgp in stc_ops.children(stc, dev, "BgpRouterConfig"):
                rows.append(_bgp_row(stc, dev, bgp))
        env["result"] = {"count": len(rows), "routers": rows}
    except Exception as exc:
        return _fail(env, exc)
    return env
