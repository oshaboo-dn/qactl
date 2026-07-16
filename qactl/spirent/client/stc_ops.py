"""Small shared helpers over the raw ``stcrestclient`` STC handle.

Used by the ``qactl.spirent.tools.*`` modules (port / device / bgp) so the
project/port/device lookups and the 4-byte-AS encoding live in one place.
All functions take a connected ``StcHttp`` handle (``sess.stc``).
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

_OFFLINE_MARKERS = ("localhost", "127.0.0.1", "offline", "null")

# STC ``AsNum`` is a 2-byte field; anything above this is a 4-byte ASN that
# must go in the ``*4Byte`` attribute (asdot notation) with the 4-byte flag on.
AS_2BYTE_MAX = 65535


def is_local(location: str) -> bool:
    return any(m in (location or "").lower() for m in _OFFLINE_MARKERS)


def children(stc: Any, handle: str, kind: str) -> List[str]:
    kids = stc.get(handle, f"children-{kind}")
    return kids.split() if isinstance(kids, str) and kids else []


def project(stc: Any) -> str:
    """Return the project handle, creating one only if none exists."""
    existing = stc.get("system1", "children-Project")
    handle = existing.split()[0] if isinstance(existing, str) and existing else ""
    return handle or stc.create("project", under="system1")


def ports(stc: Any, proj: str) -> List[str]:
    return children(stc, proj, "Port")


def find_port_by_location(stc: Any, proj: str, location: str) -> Optional[str]:
    for p in ports(stc, proj):
        if stc.get(p, "Location") == location:
            return p
    return None


def devices(stc: Any, proj: str) -> List[str]:
    return children(stc, proj, "EmulatedDevice")


def find_device_by_name(stc: Any, proj: str, name: str) -> Optional[str]:
    for d in devices(stc, proj):
        if stc.get(d, "Name") == name:
            return d
    return None


def link_status(stc: Any, port_ref: str) -> Optional[str]:
    phy = stc.get(port_ref, "activephy-Targets")
    return stc.get(phy, "LinkStatus") if phy else None


def as_dot(asn: int) -> str:
    """4-byte ASN in asdot notation: ``high.low`` (e.g. 100001 -> '1.34465')."""
    return f"{asn // 65536}.{asn % 65536}"


def apply_as(stc: Any, bgp_ref: str, *, local: bool, asn: int) -> None:
    """Set a BGP AS (local or DUT), handling the 2-byte vs 4-byte split.

    ``local`` selects the local-AS attribute set; otherwise the DUT-AS set.
    """
    if local:
        num_attr, num4_attr, flag_attr = "AsNum", "AsNum4Byte", "Enable4ByteAsNum"
    else:
        num_attr, num4_attr, flag_attr = "DutAsNum", "DutAsNum4Byte", "Enable4ByteDutAsNum"
    if asn > AS_2BYTE_MAX:
        stc.config(bgp_ref, **{flag_attr: "TRUE", num4_attr: as_dot(asn)})
    else:
        stc.config(bgp_ref, **{flag_attr: "FALSE", num_attr: str(asn)})


# BGP-BFD strict-mode is advertised as BGP capability code 74
# (draft-ietf-idr-bgp-bfd-strict-mode). On STC it is a BgpCustomCapability.
STRICT_CAP_TYPE = "74"


def strict_capability(stc: Any, bgp_ref: str) -> Optional[str]:
    """Return the existing strict-mode (cap-74) custom capability, if any."""
    for cap in children(stc, bgp_ref, "BgpCustomCapability"):
        if stc.get(cap, "CapabilityType") == STRICT_CAP_TYPE:
            return cap
    return None
