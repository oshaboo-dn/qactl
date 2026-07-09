"""RESTCONF URI builder + DNOS-on-ODL quirks.

Two URI families are produced here:

* RFC-8040 standard:  ``/restconf/data/<module>:<container>/<list>=<key>/...``
  (used against any modern RESTCONF speaker, including future native DNOS).

* ODL legacy:         ``/restconf/operational/<module>:<container>/<list>/<key>/...``
  (used today against the lab's ODL controller — it accepts ``operational``
  and rejects RFC-8040 keyed-list ``=`` syntax with ``400 unknown-element``).

The endpoint config (``restconf_endpoints.json``) carries an
``uri_style`` field (``"legacy"`` or ``"rfc8040"``) that selects which
form to emit.

Module-name quirks live here too. The ``module_name_quirks`` map on each
endpoint translates the *YANG container element name* (``drivenets-top``)
to the *YANG module name* RESTCONF expects (``dn-top``). Without that
translation the agent gets ``module does not exist in mount point``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote


# Default mapping for any DriveNets endpoint. Override via per-endpoint
# config if a future build changes a module name.
DEFAULT_MODULE_QUIRKS: Dict[str, str] = {
    "drivenets-top": "dn-top",
}


def normalize_first_segment(first_seg: str, quirks: Optional[Dict[str, str]] = None) -> str:
    """Apply module-name quirks to the leading ``module:container`` segment.

    Accepts both ``container`` (no prefix) and ``module:container`` shapes
    and returns ``correct-module:container``.
    """
    q = dict(DEFAULT_MODULE_QUIRKS)
    if quirks:
        for k, v in quirks.items():
            if k.startswith("_"):
                continue
            q[k] = v

    if ":" in first_seg:
        mod, _, cont = first_seg.partition(":")
    else:
        mod, cont = first_seg, first_seg
    if cont in q:
        mod = q[cont]
    return f"{mod}:{cont}"


def join_path_segments(segments: Sequence[Any], style: str = "legacy") -> str:
    """Build the path portion of a RESTCONF URI from logical segments.

    Each segment is one of:

    * ``"container"``                  — plain container or leaf
    * ``"module:container"``           — namespaced container
    * ``("list", "key")`` or
      ``("list", ["k1", "k2"])``       — keyed list entry
      Compound keys are joined with ``,`` per RFC-8040.

    ``style="legacy"`` produces ``.../list/key`` (ODL pre-RFC-8040);
    ``style="rfc8040"`` produces ``.../list=key``.
    """
    parts: List[str] = []
    for seg in segments:
        if isinstance(seg, tuple):
            list_name, keys = seg
            keys_seq: Sequence[Any] = keys if isinstance(keys, (list, tuple)) else [keys]
            key_str = ",".join(quote(str(k), safe="") for k in keys_seq)
            if style == "rfc8040":
                parts.append(f"{quote(list_name, safe=':/')}={key_str}")
            else:
                parts.append(quote(list_name, safe=":/"))
                parts.append(key_str)
        else:
            parts.append(quote(str(seg), safe=":/"))
    return "/".join(parts)


def build_yang_path(
    segments: Sequence[Any],
    *,
    module_quirks: Optional[Dict[str, str]] = None,
    style: str = "legacy",
) -> str:
    """Apply module-name quirks to ``segments[0]`` and join the rest.

    Returns a path fragment like ``dn-top:drivenets-top/dn-system:system/...``
    suitable to append after ``yang-ext:mount`` (ODL) or ``data`` (native).
    """
    if not segments:
        return ""
    first = segments[0]
    if isinstance(first, tuple):
        list_name, keys = first
        first = (normalize_first_segment(list_name, module_quirks), keys)
    else:
        first = normalize_first_segment(str(first), module_quirks)
    return join_path_segments([first, *segments[1:]], style=style)


# --------------------------------------------------------------------------
# Endpoint-aware URL builders
# --------------------------------------------------------------------------


def build_odl_mount_url(base_url: str, mount_name: str) -> str:
    """`PUT/DELETE` URL for the ODL NETCONF-topology mount config of one node."""
    return (
        f"{base_url.rstrip('/')}/config/network-topology:network-topology/"
        f"topology/topology-netconf/node/{quote(mount_name, safe='')}"
    )


def build_odl_node_status_url(base_url: str, mount_name: str) -> str:
    """`GET` URL for the operational status of one ODL-mounted node."""
    return (
        f"{base_url.rstrip('/')}/operational/network-topology:network-topology/"
        f"topology/topology-netconf/node/{quote(mount_name, safe='')}"
    )


def build_data_url(
    *,
    base_url: str,
    mount_name: Optional[str],
    yang_segments: Sequence[Any],
    datastore: str = "operational",
    style: str = "legacy",
    module_quirks: Optional[Dict[str, str]] = None,
) -> str:
    """Compose a full RESTCONF data-tree URL.

    Native RESTCONF (``mount_name=None``):
        ``<base>/<datastore>/<yang-path>``
        or, for RFC-8040: ``<base>/data/<yang-path>?content=<...>``

    ODL proxy (``mount_name`` set):
        ``<base>/operational/network-topology:.../node/<mount>/yang-ext:mount/<yang-path>``
    """
    yang_path = build_yang_path(yang_segments, module_quirks=module_quirks, style=style)
    base = base_url.rstrip("/")
    if mount_name:
        # ODL proxy URL — datastore is implicit in the URL prefix
        prefix = "operational" if datastore != "config" else "config"
        return (
            f"{base}/{prefix}/network-topology:network-topology/"
            f"topology/topology-netconf/node/{quote(mount_name, safe='')}/"
            f"yang-ext:mount/{yang_path}"
        )
    # Native RESTCONF
    if style == "rfc8040":
        content = "nonconfig" if datastore != "config" else "config"
        return f"{base}/data/{yang_path}?content={content}"
    return f"{base}/{datastore}/{yang_path}"
