"""NETCONF RPC primitives and server-capability helpers."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from lxml import etree
from ncclient import manager
from ncclient.xml_ import to_ele

from .xml_payload import _strip_dn_top_wrapper

DN_TOP_NS = "http://drivenets.com/ns/yang/dn-top"
DN_SYSTEM_NS = "http://drivenets.com/ns/yang/dn-system"
DN_SYS_NCC_NS = "http://drivenets.com/ns/yang/dn-sys-ncc"
DN_INSTALL_NS = "http://drivenets.com/ns/yang/dn-sys-install"
YANG_LIBRARY_NS = "urn:ietf:params:xml:ns:yang:ietf-yang-library"
NETCONF_MONITORING_NS = "urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring"


def get_config(
    m: manager.Manager,
    source: str = "running",
    subtree: Optional[str] = None,
    dn_only: bool = False,
) -> str:
    """Get configuration. subtree is optional (e.g. <system/> or <interfaces/>).

    When dn_only=True and no subtree is given, filters to drivenets-top only
    (excludes OpenConfig and other top-level containers).
    The subtree can be passed with or without the <drivenets-top> wrapper --
    if present, it is stripped and re-wrapped to avoid double-wrapping.
    """
    filter_xml = None
    if subtree:
        inner = _strip_dn_top_wrapper(subtree.strip())
        filter_xml = (
            "subtree",
            f'<drivenets-top xmlns="{DN_TOP_NS}">{inner}</drivenets-top>',
        )
    elif dn_only:
        filter_xml = (
            "subtree",
            f'<drivenets-top xmlns="{DN_TOP_NS}"/>',
        )
    reply = m.get_config(source=source, filter=filter_xml)
    try:
        out = reply.data_xml
        if out is not None:
            return out
    except (TypeError, Exception):
        pass
    return reply.xml if reply.xml else "<rpc-reply/>"


def get(
    m: manager.Manager,
    subtree: Optional[str] = None,
) -> str:
    """NETCONF <get> for operational + config state.

    subtree filters under drivenets-top (e.g. '<system xmlns="..."/>').
    Without subtree returns all operational state.
    """
    filter_xml = None
    if subtree:
        inner = _strip_dn_top_wrapper(subtree.strip())
        filter_xml = (
            "subtree",
            f'<drivenets-top xmlns="{DN_TOP_NS}">{inner}</drivenets-top>',
        )
    reply = m.get(filter=filter_xml)
    try:
        out = reply.data_xml
        if out is not None:
            return out
    except (TypeError, Exception):
        pass
    return reply.xml if reply.xml else "<rpc-reply/>"


_SN_SUBTREE_NCC = (
    '<system xmlns="http://drivenets.com/ns/yang/dn-system">'
    '<nccs xmlns="http://drivenets.com/ns/yang/dn-sys-ncc">'
    "<ncc><config-items>"
    '<platform xmlns="http://drivenets.com/ns/yang/dn-platform">'
    "<oper-items><serial-number/></oper-items>"
    "</platform>"
    "</config-items></ncc>"
    "</nccs></system>"
)

_SN_SUBTREE_NCP = (
    '<system xmlns="http://drivenets.com/ns/yang/dn-system">'
    '<ncps xmlns="http://drivenets.com/ns/yang/dn-sys-ncp">'
    "<ncp><config-items>"
    '<platform xmlns="http://drivenets.com/ns/yang/dn-platform">'
    "<oper-items><serial-number/></oper-items>"
    "</platform>"
    "</config-items></ncp>"
    "</ncps></system>"
)

_ROLE_PROBE_SUBTREE = (
    f'<system xmlns="{DN_SYSTEM_NS}">'
    f'<nccs xmlns="{DN_SYS_NCC_NS}">'
    "<ncc><ncc-id/><oper-items><model/><oper-status/></oper-items></ncc>"
    "</nccs></system>"
)

_NCP_MODEL_RE = re.compile(r"^NCP-", re.IGNORECASE)
_CL_MODEL_RE = re.compile(r"^(DNC|NCC)-", re.IGNORECASE)


def get_device_role(m: manager.Manager) -> Dict[str, object]:
    """Classify the connected device as SA / CL / UNKNOWN using NCC oper-state.

    Issues one operational <get> against
    ``/drivenets-top/system/nccs/ncc/oper-items/{model,oper-status}`` and
    returns a dict:

        {
            "role": "SA" | "CL" | "UNKNOWN",
            "nccs": [{"ncc_id": str, "model": str, "oper_status": str}, ...],
            "reason": str,
        }

    Rules:
      - Require at least one ncc with oper-status == "active-up".
        If none -> UNKNOWN.
      - Exactly 1 ncc, model ^NCP-  -> SA.
      - >=2 nccs, all models ^(DNC|NCC)- -> CL.
      - Otherwise -> UNKNOWN.
    """
    result: Dict[str, object] = {"role": "UNKNOWN", "nccs": [], "reason": ""}
    try:
        xml_str = get(m, subtree=_ROLE_PROBE_SUBTREE)
        root = etree.fromstring(xml_str.encode("utf-8"))
    except Exception as exc:
        result["reason"] = f"role probe RPC failed: {exc}"
        return result

    nccs: List[Dict[str, str]] = []
    for ncc in root.iter(f"{{{DN_SYS_NCC_NS}}}ncc"):
        ncc_id_el = ncc.find(f"{{{DN_SYS_NCC_NS}}}ncc-id")
        oper_items = ncc.find(f"{{{DN_SYS_NCC_NS}}}oper-items")
        model = ""
        oper_status = ""
        if oper_items is not None:
            model_el = oper_items.find(f"{{{DN_SYS_NCC_NS}}}model")
            status_el = oper_items.find(f"{{{DN_SYS_NCC_NS}}}oper-status")
            if model_el is not None and model_el.text:
                model = model_el.text.strip()
            if status_el is not None and status_el.text:
                oper_status = status_el.text.strip()
        nccs.append({
            "ncc_id": (ncc_id_el.text.strip() if ncc_id_el is not None and ncc_id_el.text else ""),
            "model": model,
            "oper_status": oper_status,
        })
    result["nccs"] = nccs

    if not nccs:
        result["reason"] = "no <ncc> elements returned"
        return result

    active = [n for n in nccs if n["oper_status"] == "active-up"]
    if not active:
        result["reason"] = "no ncc with oper-status=active-up"
        return result

    models = [n["model"] for n in nccs]
    if len(nccs) == 1 and _NCP_MODEL_RE.match(models[0] or ""):
        result["role"] = "SA"
        result["reason"] = f"1 ncc model {models[0]!r}"
        return result
    if len(nccs) >= 2 and all(_CL_MODEL_RE.match(m or "") for m in models):
        result["role"] = "CL"
        result["reason"] = f"{len(nccs)} nccs, models={models}"
        return result

    result["reason"] = f"unrecognized ncc shape: count={len(nccs)}, models={models}"
    return result


def get_serial_numbers(m: manager.Manager, role: Optional[str] = None) -> List[str]:
    """Fetch serial numbers via NETCONF <get>.

    role="CL" -> query NCC (control plane nodes, may return multiple).
    role="SA" or other/None -> query NCP (line cards).
    Returns empty list if unavailable.
    """
    subtree = _SN_SUBTREE_NCC if role == "CL" else _SN_SUBTREE_NCP
    try:
        xml_str = get(m, subtree=subtree)
        root = etree.fromstring(xml_str.encode("utf-8"))
        nodes = root.xpath("//*[local-name()='serial-number']")
        return [n.text.strip() for n in nodes if n.text and n.text.strip()]
    except Exception:
        pass
    return []


def edit_config(
    m: manager.Manager,
    config_xml: str,
    target: str = "candidate",
    default_operation: Optional[str] = None,
) -> str:
    """Edit target datastore with payload under drivenets-top.

    ``default_operation`` is the NETCONF ``<default-operation>`` value
    applied to the whole edit-config (RFC 6241 §7.2). When ``None`` the
    server default applies (``merge``). Pass ``"replace"`` for
    override-style edits or ``"none"`` for annotation-only payloads.
    """
    payload = (
        '<config>'
        f'<drivenets-top xmlns="{DN_TOP_NS}">{config_xml}</drivenets-top>'
        "</config>"
    )
    kwargs: Dict[str, str] = {}
    if default_operation:
        kwargs["default_operation"] = default_operation
    reply = m.edit_config(target=target, config=payload, **kwargs)
    return reply.xml if reply.xml else "<ok/>"


def commit(m: manager.Manager) -> str:
    """Commit candidate changes."""
    reply = m.commit()
    return reply.xml if reply.xml else "<ok/>"


def discard_changes(m: manager.Manager) -> str:
    """Discard candidate changes."""
    reply = m.discard_changes()
    return reply.xml if reply.xml else "<ok/>"


def rollback(m: manager.Manager, index: int) -> str:
    """Load a previous commit into the candidate datastore via rollback RPC."""
    rpc_xml = f"<rollback><index>{index}</index></rollback>"
    reply = m.dispatch(to_ele(rpc_xml))
    return reply.xml if reply.xml else "<ok/>"


def render_hello_xml(capabilities: List[str]) -> str:
    """Render server capabilities in NETCONF hello XML shape."""
    caps = "".join(f"<capability>{cap}</capability>" for cap in capabilities)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<hello xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        f"<capabilities>{caps}</capabilities>"
        "</hello>"
    )


def supports_candidate(capabilities: List[str]) -> bool:
    """Check if server advertises candidate capability."""
    return any("urn:ietf:params:netconf:capability:candidate:1.0" == cap for cap in capabilities)


def require_candidate(m: manager.Manager) -> None:
    """Raise RuntimeError if the peer does not advertise the candidate capability."""
    caps = [str(c) for c in m.server_capabilities]
    if not supports_candidate(caps):
        raise RuntimeError("Server does not advertise candidate capability")


def get_dnos_version(m: manager.Manager) -> dict:
    """Query dn-sys-install current-stack to get DNOS version, build, and sub-build.

    Returns dict with keys: version, build, sub_build, package_name, raw_version.
    Raises RuntimeError if DNOS package not found.
    """
    subtree = (
        f'<system xmlns="{DN_SYSTEM_NS}">'
        f'<installation xmlns="{DN_INSTALL_NS}">'
        '<oper-items><current-stack/></oper-items>'
        '</installation></system>'
    )
    filter_xml = (
        "subtree",
        f'<drivenets-top xmlns="{DN_TOP_NS}">{subtree}</drivenets-top>',
    )
    reply = m.get(filter=filter_xml)
    xml_str = reply.xml if reply.xml else ""
    root = etree.fromstring(xml_str.encode("utf-8"))

    for entries in root.iter(f"{{{DN_INSTALL_NS}}}entries"):
        pkg_type_el = entries.find(f"{{{DN_INSTALL_NS}}}package-type")
        if pkg_type_el is not None and pkg_type_el.text == "DNOS":
            ver_el = entries.find(f"{{{DN_INSTALL_NS}}}package-version")
            name_el = entries.find(f"{{{DN_INSTALL_NS}}}package-name")
            raw_version = ver_el.text.strip() if ver_el is not None else ""
            package_name = name_el.text.strip() if name_el is not None else ""

            build = ""
            sub_build = ""
            after_priv = raw_version
            if "_priv." in raw_version:
                after_priv = raw_version.split("_priv.", 1)[1]
            if "_" in after_priv:
                build, sub_build = after_priv.split("_", 1)
            else:
                build = after_priv

            return {
                "raw_version": raw_version,
                "version": raw_version.split("_priv")[0] if "_priv" in raw_version else raw_version,
                "build": build,
                "sub_build": sub_build,
                "package_name": package_name,
            }

    raise RuntimeError("DNOS package not found in current-stack")


def get_yang_library(m: manager.Manager) -> List[dict]:
    """Query ietf-yang-library modules-state and return list of module dicts.

    Each dict has keys: name, revision, namespace, conformance_type, submodules.
    """
    filter_xml = (
        "subtree",
        f'<modules-state xmlns="{YANG_LIBRARY_NS}"/>',
    )
    reply = m.get(filter=filter_xml)
    xml_str = reply.xml if reply.xml else ""
    root = etree.fromstring(xml_str.encode("utf-8"))

    modules = []
    for mod in root.iter(f"{{{YANG_LIBRARY_NS}}}module"):
        name_el = mod.find(f"{{{YANG_LIBRARY_NS}}}name")
        rev_el = mod.find(f"{{{YANG_LIBRARY_NS}}}revision")
        ns_el = mod.find(f"{{{YANG_LIBRARY_NS}}}namespace")
        ct_el = mod.find(f"{{{YANG_LIBRARY_NS}}}conformance-type")
        submodules = []
        for sub in mod.iter(f"{{{YANG_LIBRARY_NS}}}submodule"):
            sub_name = sub.find(f"{{{YANG_LIBRARY_NS}}}name")
            sub_rev = sub.find(f"{{{YANG_LIBRARY_NS}}}revision")
            if sub_name is not None and sub_name.text:
                submodules.append({
                    "name": sub_name.text,
                    "revision": sub_rev.text if sub_rev is not None else "",
                })
        modules.append({
            "name": name_el.text if name_el is not None else "",
            "revision": rev_el.text if rev_el is not None else "",
            "namespace": ns_el.text if ns_el is not None else "",
            "conformance_type": ct_el.text if ct_el is not None else "",
            "submodules": submodules,
        })
    return modules


def get_schema_source(m: manager.Manager, identifier: str, version: str = "", fmt: str = "yang") -> str:
    """Fetch YANG module source via get-schema RPC (RFC 6022).

    Returns the YANG source text. Raises RPCError if module not found.
    """
    rpc_parts = [
        f'<get-schema xmlns="{NETCONF_MONITORING_NS}">',
        f"  <identifier>{identifier}</identifier>",
    ]
    if version:
        rpc_parts.append(f"  <version>{version}</version>")
    rpc_parts.append(f"  <format>yang</format>")
    rpc_parts.append("</get-schema>")
    rpc_xml = "\n".join(rpc_parts)

    reply = m.dispatch(to_ele(rpc_xml))
    root = etree.fromstring(reply.xml.encode("utf-8"))

    data_nodes = root.iter(f"{{{NETCONF_MONITORING_NS}}}data")
    for data_node in data_nodes:
        if data_node.text:
            return data_node.text
    return reply.xml
