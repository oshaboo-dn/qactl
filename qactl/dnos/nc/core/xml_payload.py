"""XML payload extraction/splitting and backup file-path helpers."""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from lxml import etree

DN_TOP_NS = "http://drivenets.com/ns/yang/dn-top"
IL_TZ = ZoneInfo("Asia/Jerusalem")

_DN_TOP_OPEN_RE = re.compile(
    r'^\s*<drivenets-top\s[^>]*>\s*', re.DOTALL
)
_DN_TOP_CLOSE_RE = re.compile(r'\s*</drivenets-top>\s*$')

_PH_SAFE_RE = re.compile(r"\{\{(\w+)\}\}")


def pretty_xml(xml_str: str, *, preserve_placeholders: bool = False) -> str:
    """Pretty-print an XML string using lxml.

    When preserve_placeholders is True, {{PLACEHOLDER}} markers are swapped
    to XML-safe tokens before parsing and restored after formatting.

    If strict parsing fails we retry with a recovering parser (``recover=True``)
    which tolerates stray text/undeclared entities typical of NETCONF replies
    wrapped in extra envelopes. If even that fails, we fall back to a naive
    line-break-on-``><`` so the caller still gets something readable instead
    of a 200 KB single line.
    """
    text = xml_str.strip() if xml_str else ""
    if not text:
        return text

    markers: dict[str, str] = {}
    if preserve_placeholders:
        def _sub(m: re.Match) -> str:
            tag = f"__PH_{m.group(1)}__"
            markers[tag] = m.group(0)
            return tag
        text = _PH_SAFE_RE.sub(_sub, text)

    def _restore(s: str) -> str:
        if markers:
            for tag, original in markers.items():
                s = s.replace(tag, original)
        return s

    try:
        root = etree.fromstring(text.encode("utf-8"))
        pretty = etree.tostring(
            root, pretty_print=True, xml_declaration=False, encoding="unicode",
        )
        return _restore(pretty.rstrip("\n"))
    except etree.XMLSyntaxError:
        pass

    try:
        parser = etree.XMLParser(recover=True)
        root = etree.fromstring(text.encode("utf-8"), parser)
        if root is not None:
            pretty = etree.tostring(
                root, pretty_print=True, xml_declaration=False,
                encoding="unicode",
            )
            return _restore(pretty.rstrip("\n"))
    except Exception:
        pass

    return _restore(_naive_pretty(text))


def _naive_pretty(text: str) -> str:
    """Last-ditch formatter: break between ``>`` and ``<`` and indent by depth.

    Not a real XML parser -- just converts a single-line blob into something
    scannable. Used only when both strict and recovering lxml parsers fail.
    """
    broken = text.replace("><", ">\n<")
    out_lines: list[str] = []
    depth = 0
    for line in broken.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        is_close = stripped.startswith("</")
        is_self = stripped.endswith("/>") or (
            stripped.startswith("<") and stripped.endswith(">")
            and "</" in stripped
        )
        if is_close:
            depth = max(0, depth - 1)
        out_lines.append("  " * depth + stripped)
        if not is_close and not is_self and stripped.startswith("<"):
            depth += 1
    return "\n".join(out_lines)


def _strip_dn_top_wrapper(xml_str: str) -> str:
    """Strip <drivenets-top xmlns="...">...</drivenets-top> wrapper if present."""
    s = xml_str.strip()
    if not s.startswith("<drivenets-top"):
        return s
    s = _DN_TOP_OPEN_RE.sub("", s, count=1)
    s = _DN_TOP_CLOSE_RE.sub("", s, count=1)
    return s.strip()


def extract_payload_for_edit(xml_text: str) -> str:
    """Accept raw payload or wrapped NETCONF RPC and return payload under dn-top.

    ``edit_config`` always re-wraps the returned text in
    ``<config><drivenets-top>...</drivenets-top></config>``, so the caller
    must hand us *inner* XML (children of ``drivenets-top``). When the agent
    passes a bare ``<drivenets-top>...</drivenets-top>`` block we strip the
    wrapper here to avoid double-wrapping at the RPC layer.
    """
    if "<rpc" not in xml_text and "<config" not in xml_text:
        return _strip_dn_top_wrapper(xml_text)

    try:
        root = etree.fromstring(xml_text.encode("utf-8"))
    except Exception:
        return _strip_dn_top_wrapper(xml_text)

    config_nodes = root.xpath("//*[local-name()='config']")
    if not config_nodes:
        return _strip_dn_top_wrapper(xml_text)

    config_node = config_nodes[0]
    children = list(config_node)
    if not children:
        return ""

    # If config already contains drivenets-top, unwrap one level.
    if etree.QName(children[0]).localname == "drivenets-top":
        return "".join(
            etree.tostring(child, encoding="unicode")
            for child in list(children[0])
        ).strip()

    return "".join(
        etree.tostring(child, encoding="unicode")
        for child in children
    ).strip()


def read_payloads(files: List[str]) -> List[str]:
    """Read one or more XML payload files."""
    payloads: List[str] = []
    for file_path in files:
        with open(file_path, "r", encoding="utf-8") as f:
            payloads.append(extract_payload_for_edit(f.read().strip()))
    return payloads


# Restore order for direct children of <drivenets-top>.
# Providers (leafref / reference targets) come first, consumers after,
# and the global `apply-groups` leaf-list goes last so its leafrefs into
# config-groups resolve at commit time.
SECTION_ORDER = [
    "config-groups",
    "access-lists",
    "system",
    "qos",
    "interfaces",
    "routing-options",
    "routing-policy",
    "forwarding-options",
    "protocols",
    "multicast",
    "network-services",
    "services",
    "tracking-policy",
    "debug",
    "apply-groups",
]


def extract_dn_top_payloads(xml_text: str) -> List[tuple]:
    """Extract per-section payloads from a get-config rpc-reply (or backup file).

    Returns list of (section_name, payload_str) tuples sorted in dependency order.
    Namespace prefixes declared on ancestor elements are injected so identity-ref
    text values remain valid.
    """
    root = etree.fromstring(xml_text.encode("utf-8"))
    dn_top_nodes = root.xpath("//*[local-name()='drivenets-top']")
    if not dn_top_nodes:
        raise ValueError("No <drivenets-top> found in XML")

    dn_top = dn_top_nodes[0]
    children = list(dn_top)
    if not children:
        return []

    ancestor_ns = {}
    node = dn_top
    while node is not None:
        for prefix, uri in node.nsmap.items():
            if prefix is not None and prefix not in ancestor_ns:
                ancestor_ns[prefix] = uri
        node = node.getparent()

    sections = []
    for child in children:
        name = etree.QName(child).localname
        serialized = etree.tostring(child, encoding="unicode")
        missing = []
        for prefix, uri in ancestor_ns.items():
            if f"{prefix}:" in serialized and f"xmlns:{prefix}=" not in serialized:
                missing.append(f'xmlns:{prefix}="{uri}"')
        if missing:
            gt = serialized.index(">")
            if serialized[gt - 1] == "/":
                gt -= 1
            serialized = serialized[:gt] + " " + " ".join(missing) + serialized[gt:]
        sections.append((name, serialized))

    def sort_key(item):
        try:
            return SECTION_ORDER.index(item[0])
        except ValueError:
            return len(SECTION_ORDER)

    sections.sort(key=sort_key)
    return sections


def build_section_xml(original_root: etree._Element, section: etree._Element) -> str:
    """Wrap a single section element back into <rpc-reply><data><drivenets-top>.

    Preserves namespace declarations from the original <data> element so that
    identity-ref prefixes (e.g. iana-if-type:softwareLoopback) remain valid.
    """
    orig_data = original_root.xpath("//*[local-name()='data']")[0]
    data_nsmap = {k: v for k, v in orig_data.nsmap.items() if k is not None}

    nsmap = {"nc": "urn:ietf:params:xml:ns:netconf:base:1.0"}
    rpc_reply = etree.Element("{urn:ietf:params:xml:ns:netconf:base:1.0}rpc-reply", nsmap=nsmap)
    data_nsmap.pop("nc", None)
    data_el = etree.SubElement(rpc_reply, "data", nsmap=data_nsmap)

    for attr_name, attr_val in orig_data.attrib.items():
        data_el.set(attr_name, attr_val)

    dn_top = etree.SubElement(data_el, f"{{{DN_TOP_NS}}}drivenets-top")
    dn_top.append(section)

    return etree.tostring(rpc_reply, pretty_print=True, encoding="unicode", xml_declaration=False)


def split_config_to_sections(xml_text: str) -> List[tuple]:
    """Split a full get-config response into per-section ``(name, xml)`` pairs.

    Each ``xml`` is a complete ``<rpc-reply><data><drivenets-top>``
    wrapper containing exactly one section (the same on-disk format
    :func:`split_config_to_dir` writes), so the result can be fed
    straight to :func:`load_restore_sections` after extraction.
    """
    root = etree.fromstring(xml_text.encode("utf-8"))
    dn_top_nodes = root.xpath("//*[local-name()='drivenets-top']")
    if not dn_top_nodes:
        raise ValueError("No <drivenets-top> found in XML")

    dn_top = dn_top_nodes[0]
    children = list(dn_top)
    if not children:
        raise ValueError("<drivenets-top> has no children")

    return [
        (etree.QName(child).localname, build_section_xml(root, child))
        for child in children
    ]


def split_config_to_dir(xml_text: str, out_dir: str) -> List[str]:
    """Split a full get-config response into per-section XML files in out_dir.

    Returns list of written file paths.
    """
    sections = split_config_to_sections(xml_text)
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for local_name, section_xml in sections:
        path = os.path.join(out_dir, f"{local_name}.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(section_xml)
        paths.append(path)
    return paths


def load_restore_sections(path: str) -> List[tuple]:
    """Load restore sections from a file or directory.

    For a directory: reads each *.xml file, extracts payloads, sorts by SECTION_ORDER.
    For a file: extracts all sections from a single rpc-reply XML.
    Returns list of (section_name, payload_str) tuples in dependency order.
    """
    if os.path.isdir(path):
        sections = []
        for xml_file in sorted(os.listdir(path)):
            if not xml_file.endswith(".xml"):
                continue
            file_path = os.path.join(path, xml_file)
            with open(file_path, "r", encoding="utf-8") as f:
                xml_text = f.read()
            sections.extend(extract_dn_top_payloads(xml_text))

        def sort_key(item):
            try:
                return SECTION_ORDER.index(item[0])
            except ValueError:
                return len(SECTION_ORDER)

        sections.sort(key=sort_key)
        return sections
    else:
        with open(path, "r", encoding="utf-8") as f:
            xml_text = f.read()
        return extract_dn_top_payloads(xml_text)


def _netconf_root() -> str:
    """Return the netconf/ directory (parent of qactl.nc.core/)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_backup_root() -> str:
    """Return netconf-local backups directory."""
    return os.path.join(_netconf_root(), "backups")


def backup_date_token() -> str:
    """Return date token for backup folder."""
    return datetime.now(IL_TZ).strftime("%Y-%m-%d")


def backup_time_token() -> str:
    """Return time token for backup file name (HH:MM)."""
    return datetime.now(IL_TZ).strftime("%H:%M")


def safe_log_token(value: str) -> str:
    """Return a filesystem-safe token (used for log filenames and backup dirs)."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "unknown"


def default_backup_dir(backup_root: str, host: str, device_name: Optional[str] = None) -> str:
    """Generate default backup directory path: backups/<date>/<time>-<device>/"""
    device_token = safe_log_token(device_name) if device_name else safe_log_token(host)
    return os.path.join(backup_root, backup_date_token(), f"{backup_time_token()}-{device_token}")


_BACKUP_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BACKUP_ENTRY_RE = re.compile(r"^(\d{2}:\d{2})-(.+)$")


def parse_backup_dir_name(date_name: str, entry_name: str) -> Optional[tuple]:
    """Inverse of default_backup_dir: parse a backups/<date>/<entry> pair.

    Returns (date, time, device_token) if both components match the format
    produced by default_backup_dir, else None.
    """
    if not _BACKUP_DATE_RE.match(date_name):
        return None
    m = _BACKUP_ENTRY_RE.match(entry_name)
    if not m:
        return None
    return date_name, m.group(1), m.group(2)


def write_output(path: str, content: str) -> None:
    """Write command output to a file path."""
    output_dir = os.path.dirname(path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
