"""Read / write tools (``netconf_get``, ``netconf_edit``).

The caller supplies the full XML payload; this module forwards it to the
NETCONF server and returns the full reply. ``--out-file`` additionally
writes the result to a path. The actual edit-config + commit +
discard-on-failure flow lives in
:func:`dnctl.nc.core.change_ops.edit_from_xml`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from lxml import etree
from ncclient.xml_ import to_ele

from dnctl.nc.core.change_ops import edit_from_xml
from dnctl.nc.core.device_log import _begin, _log_action, _log_event
from dnctl.nc.core.netconf_rpc import get, get_config
from dnctl.nc.core.results import _base_result, _error_result
from dnctl.nc.core.session import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    ROOT_DIR,
    _connect_device,
    _extract_rpc_command,
    _resolve_operation_file,
    _session_id,
)
from dnctl.nc.core.xml_payload import pretty_xml, write_output


def netconf_get(
    xml: str,
    host: Optional[str] = None,
    device: Optional[str] = None,
    oper: bool = False,
    source: str = "running",
    root: str = "auto",
    out_file: Optional[str] = None,
    rpc_file: Optional[str] = None,
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    no_verify: bool = True,
    verify_mgmt0: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Read config or operational data from a device.

    The agent supplies the full subtree filter XML (including the
    ``<drivenets-top xmlns="...">...</drivenets-top>`` wrapper). The server
    forwards it unchanged to ``<get-config>`` (default) or ``<get>``
    (``oper=True``). A bare filter in a DriveNets namespace is wrapped
    under ``drivenets-top``; a non-DriveNets top element (OpenConfig,
    IETF, ...) is sent as-is since those trees are siblings of
    drivenets-top. Override with ``root``: ``"auto"`` (default) |
    ``"dn-top"`` | ``"none"``. Otherwise the filter is not validated or
    rewritten -- the device is the final authority. Use ``nc schema`` if you need the raw
    ``.yang`` source while composing the filter.

    Example::

        netconf_get(
            device="sa",
            xml='<drivenets-top xmlns="http://drivenets.com/ns/yang/dn-top">'
                '  <network-services><vrfs><vrf>'
                '    <vrf-name>default</vrf-name>'
                '    <protocols><ldp-top/></protocols>'
                '  </vrf></vrfs></network-services>'
                '</drivenets-top>',
        )

    ``rpc_file`` is an escape hatch to dispatch a raw RPC from a file.
    """
    sid = _session_id()

    if xml and rpc_file:
        return _error_result(
            "show", sid,
            ValueError("Provide xml= OR rpc_file=, not both."),
        )
    if not xml and not rpc_file:
        return _error_result(
            "show", sid,
            ValueError("Provide xml= (subtree filter) or rpc_file=."),
        )

    if xml:
        try:
            etree.fromstring(xml.encode("utf-8"))
        except etree.XMLSyntaxError as e:
            return _error_result(
                "show", sid,
                ValueError(f"xml= must be well-formed XML (parse error: {e})."),
            )

    if root not in ("auto", "dn-top", "none"):
        return _error_result(
            "show", sid,
            ValueError("root= must be one of: auto, dn-top, none."),
        )

    warnings: List[str] = []

    try:
        with _connect_device(host, device, port, user, password, no_verify, timeout, verify_mgmt0) as cr:
            log_path = _begin(cr, sid, "show", device=device)
            m = cr.mgr

            if rpc_file:
                rpc_path = _resolve_operation_file(rpc_file, category="show")
                with rpc_path.open("r", encoding="utf-8") as f:
                    rpc_xml = f.read().strip()
                rpc_command = _extract_rpc_command(rpc_xml)
                result_xml = m.dispatch(to_ele(etree.tostring(rpc_command, encoding="unicode"))).xml
                kind = "rpc"
                used_file = str(rpc_path)
            elif oper:
                result_xml = get(m, subtree=xml, root=root)
                kind = "get"
                used_file = None
            else:
                result_xml = get_config(m, source=source, subtree=xml, root=root)
                kind = "get-config"
                used_file = None

            if out_file:
                output_path = Path(out_file) if Path(out_file).is_absolute() else ROOT_DIR / out_file
                write_output(str(output_path), result_xml)
            _log_action(log_path, "action", action=kind, source=source,
                        rpc_file=used_file)
            _log_event(log_path, sid, "end", status="ok")

            pretty = pretty_xml(result_xml)
            payload = {
                "status": "ok",
                "kind": kind,
                "source": source,
                "rpc_file": used_file,
                "out_file": out_file,
                "filter_xml": xml,
                "warnings": warnings,
                "result_xml": pretty,
                "result_truncated": False,
                "result_total_chars": len(pretty),
            }
            return _base_result("show", cr, sid, payload)
    except Exception as e:
        return _error_result("show", sid, e)


def netconf_edit(
    xml: str,
    host: Optional[str] = None,
    device: Optional[str] = None,
    op: str = "merge",
    comment: Optional[str] = None,
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    no_verify: bool = True,
    verify_mgmt0: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Atomic edit: edit-config candidate + commit.

    Supply the full config XML payload inline via ``xml=``.

    ``op`` controls the NETCONF operation applied to the top element:
    ``merge`` (default), ``replace``, ``remove``, ``delete``, ``create``.
    The server annotates the top element with ``nc:operation="op"``
    (no-op for merge), runs ``edit-config target=candidate``, and commits.
    Any commit failure triggers ``discard-changes`` so the device is left
    clean.

    For mixed payloads (e.g. merge some leaves while deleting others),
    leave ``op="merge"`` and annotate individual sub-elements inline with
    ``nc:operation="delete"`` / ``"remove"`` / ``"replace"``. The
    per-element annotation wins over the top-level ``op``.

    The payload is not validated or rewritten -- the device is the final
    authority. If you need the raw ``.yang`` source while composing, use
    ``nc schema``.
    """
    sid = _session_id()

    if not xml:
        return _error_result(
            "edit", sid, ValueError("Provide xml= with the payload."),
        )

    return edit_from_xml(
        host=host, device=device, xml=xml, op=op, comment=comment,
        port=port, user=user, password=password,
        no_verify=no_verify, verify_mgmt0=verify_mgmt0, timeout=timeout,
    )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(netconf_get)
    mcp.tool()(netconf_edit)
