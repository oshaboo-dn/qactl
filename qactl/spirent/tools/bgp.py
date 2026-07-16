"""BGP-router ops for ``qactl spirent`` — add / status / delete on a device.

Adds a ``BgpRouterConfig`` to an emulated device, handling 4-byte ASNs
(``Enable4ByteAsNum`` + asdot ``AsNum4Byte``) and the two peer-address modes
(``UseGatewayAsDut`` = peer is the device gateway, or an explicit ``DutIpv4Addr``).

``--strict`` sets up negotiated BGP-BFD **strict-mode** (draft-ietf-idr-bgp-bfd-
strict-mode "Cap-74") — a 0-octet ``BgpCustomCapability`` type 74, an explicit
MP address-family capability, and a control-plane-independent BFD session. See
``_configure_strict`` for why all three are required. ``--bfd`` flips
``EnableBfd``. Names/attrs verified live against ``il-auto-containers`` (STC
5.61) 2026-07-16.
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


def _configure_strict(stc: Any, dev: str, bgp: str,
                      use_gateway: bool, peer: Optional[str]) -> None:
    """Set up negotiated BGP-BFD strict-mode (Cap-74) on an STC BGP router.

    Learned live against ``il-auto-containers`` (STC 5.61) 2026-07-16 — a strict
    DUT (DNOS) only reaches Established with a Spirent peer when ALL THREE of
    these are in place; any one missing and it fails differently:

    1. Cap-74 with ``CapLength=0``. draft-ietf-idr-bgp-bfd-strict-mode §5 defines
       the capability as 0 octets and DNOS rejects any other length with
       NOTIFICATION 2/0 "length error: got N, expected exactly 0". STC's
       ``BgpCustomCapability`` DOES accept ``CapLength=0`` — but only if the
       ``Capability`` value attribute is left at its default; setting it to ""
       trips STC's "length should be between 1 and 4086 bytes" on apply.
    2. An explicit MP address-family capability (``CustomizedAfi=TRUE`` +
       ``BgpCapabilityConfig`` Afi/SubAfi). Without it the session Establishes
       but negotiates no AF and DNOS logs "Configured ... do not overlap with
       received MP capabilities".
    3. A control-plane-independent BFD session so STC transmits BFD regardless
       of BGP state. STC's default BFD is BGP-triggered, so against a strict DUT
       (which holds BGP until BFD is Up) it deadlocks; this session breaks it.
    """
    is_v6 = (stc.get(bgp, "IpVersion") or "").upper() == "IPV6"

    # (1) Cap-74, length 0.
    cap = stc_ops.strict_capability(stc, bgp)
    if cap is None:
        cap = stc.create("BgpCustomCapability", under=bgp,
                         CapabilityType=stc_ops.STRICT_CAP_TYPE)
    stc.config(cap, CapLength="0", Active="TRUE")

    # (2) MP address-family capability (IPv4-unicast = 1/1, IPv6-unicast = 2/1).
    stc.config(bgp, CustomizedAfi="TRUE")
    afi_caps = stc_ops.children(stc, bgp, "BgpCapabilityConfig")
    afi_cfg = afi_caps[0] if afi_caps else stc.create("BgpCapabilityConfig", under=bgp)
    for extra in afi_caps[1:]:
        stc.delete(extra)
    stc.config(afi_cfg, Afi="2" if is_v6 else "1", SubAfi="1", Active="TRUE")

    # (3) Control-plane-independent BFD session TXing to the DUT interface IP.
    if not use_gateway and peer:
        dut_ip = peer
    else:
        dut_ip = stc.get(dev, "Ipv6GatewayAddress" if is_v6 else "Ipv4GatewayAddress")
    bfd_rc = stc_ops.children(stc, dev, "BfdRouterConfig")[0]
    sess_type = ("BfdIpv6ControlPlaneIndependentSession" if is_v6
                 else "BfdIpv4ControlPlaneIndependentSession")
    nb_type = "Ipv6NetworkBlock" if is_v6 else "Ipv4NetworkBlock"
    sess_objs = stc_ops.children(stc, bfd_rc, sess_type)
    cpi = sess_objs[0] if sess_objs else stc.create(sess_type, under=bfd_rc)
    nbs = stc_ops.children(stc, cpi, nb_type)
    nb = nbs[0] if nbs else stc.create(nb_type, under=cpi)
    stc.config(nb, StartIpList=dut_ip)
    stc.config(cpi, Active="TRUE")


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
            _configure_strict(stc, dev, bgp, use_gateway, peer)
        else:
            cap = stc_ops.strict_capability(stc, bgp)
            if cap is not None:
                stc.config(cap, Active="FALSE")
        stc.apply()
        # A config apply re-stages the device's protocol block, which silently
        # halts an already-running control-plane-independent BFD session's TX
        # (it stays Active=true but stops emitting) until the device's protocols
        # are restarted. So if we just reconfigured a *running* BFD-enabled
        # device, bounce it — otherwise a strict DUT peer sits pending on a BFD
        # that never comes back Up. (Observed live 2026-07-16; DeviceStop/Start
        # is the reliable revive. Fresh, not-yet-started devices are left alone —
        # the later `device start` brings them up cleanly.)
        if need_bfd and stc.get(dev, "Active") == "true":
            stc.perform("DeviceStop", DeviceList=dev)
            stc.perform("DeviceStart", DeviceList=dev)
            # gateway ARP isn't auto-resolved after DeviceStart — kick it
            try:
                stc.perform("ArpNdStart", HandleList=dev)
            except Exception:
                pass
            stc.apply()
            env["restarted_device"] = True
        env["result"] = _bgp_row(stc, dev, bgp)
    except Exception as exc:
        return _fail(env, exc)
    return env


def spirent_bgp_send_pdu(host: str, port: int, user: str,
                         *, device: str, pdu_hex: str) -> Dict[str, Any]:
    """Send a raw, hand-crafted BGP PDU over an emulated router's session.

    The negative-testing workhorse: ``pdu_hex`` is a full BGP message as hex
    (16-byte marker included) — e.g. an OPEN with a malformed capability, a bad
    length, a truncated attribute — that STC transmits verbatim over the BGP TCP
    session via ``BgpCustomPdu`` + ``BgpSendCustomPduCommand``. Lets us fuzz the
    DUT's parser with byte shapes the object model can't express.

    Caveat (observed 2026-07-16): STC does NOT suppress its normal BGP while
    sending a custom PDU, so against an already-Established peer the inject tends
    to reset the TCP rather than be parsed as the session's OPEN. To land a
    crafted OPEN on the DUT's OpenSent parser, drive the peer so the DUT is
    mid-setup (or suppress the emulated router's own OPEN) before sending.
    """
    env = make_envelope(kind="spirent_bgp_send_pdu", host=host, port=port,
                        request={"device": device, "pdu_hex": pdu_hex})
    clean = "".join((pdu_hex or "").split()).lower()
    if clean.startswith("0x"):
        clean = clean[2:]
    if not clean or len(clean) % 2 or any(c not in "0123456789abcdef" for c in clean):
        env["status"] = "bad_argument"
        env["errors"].append("pdu_hex must be a non-empty, even-length hex string")
        return env
    byte_array = " ".join("0x" + clean[i:i + 2] for i in range(0, len(clean), 2))
    try:
        sess = session_mod.get_session(host, port, user)
        env["session"] = sess.full_name
        stc = sess.stc
        proj = stc_ops.project(stc)
        dev = stc_ops.find_device_by_name(stc, proj, device)
        if dev is None:
            env["status"] = "error"
            env["errors"].append(f"no device named {device!r}")
            return env
        bgps = stc_ops.children(stc, dev, "BgpRouterConfig")
        if not bgps:
            env["status"] = "error"
            env["errors"].append(f"device {device!r} has no BGP router")
            return env
        bgp = bgps[0]
        # fresh custom-PDU object each call (don't accumulate)
        for old in stc_ops.children(stc, bgp, "BgpCustomPdu"):
            stc.delete(old)
        pdu = stc.create("BgpCustomPdu", under=bgp)
        stc.config(pdu, Pdu=byte_array, Active="TRUE")
        stc.apply()
        result = stc.perform("BgpSendCustomPduCommand", CustomPduList=pdu)
        state = result.get("State") if isinstance(result, dict) else None
        env["result"] = {"device": device, "bytes": len(clean) // 2,
                         "state": state, "pdu_hex": clean}
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
