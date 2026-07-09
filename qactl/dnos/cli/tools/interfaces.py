"""Aggregated per-interface view (issue #42).

A single tool that folds together what otherwise takes 3–4 separate DNOS
``show`` commands plus manual correlation by interface name:

- ``show interfaces``             — state / addressing (admin, operational,
                                    IPv4/IPv6, VLAN, MTU, network-service,
                                    bundle-id)
- ``show interfaces description`` — per-interface description
- ``show lldp neighbors``         — LLDP neighbor (system name + remote
                                    interface + TTL)
- ``show isis interfaces detail`` /
  ``show ospf interfaces detail``  — IGP adjacency (instance / level /
                                    state / metric / neighbor count)

All four/five shows run on **one** SSH channel (via
:func:`qactl.cli.core.session.run_sequence`), so the common "list the
interfaces" flow costs a single round-trip + auth instead of four. The
results are joined by interface name into one object per interface with
nested ``state`` / ``description`` / ``lldp`` / ``igp`` blocks, surfaced
both as a ``--json`` payload (``interfaces`` key) and a compact text table.

Sub-interfaces (e.g. ``ge400-7/0/8.6``) carry their own IP + IGP but their
description and LLDP neighbor live on the parent physical port, so those
two blocks fall back to the parent when the sub-interface has none of its
own (the fallback is flagged via ``*_inherited_from``).
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from qactl.dnos.cli.core.envelope import make_response
from qactl.dnos.cli.core.errors import SHOW_NEXT_ACTION, detect_error
from qactl.dnos.cli.core.logging import log_invocation, log_request
from qactl.dnos.cli.vendors import CAP_INTERFACES, requires
from qactl.dnos.cli.core.registry import transport_registry
from qactl.dnos.cli.core.session import (
    ConnectError,
    connect_error_next_actions,
    DEFAULT_CMD_TIMEOUT,
    DEFAULT_PASSWORD,
    DEFAULT_USER,
    run_sequence,
)

# The source shows, in the order they run on the shared channel. The
# interface table is authoritative for the interface list + ordering;
# the rest are best-effort joins.
SHOW_INTERFACES = "show interfaces"
SHOW_DESCRIPTION = "show interfaces description"
SHOW_LLDP = "show lldp neighbors"
SHOW_ISIS = "show isis interfaces detail"
SHOW_OSPF = "show ospf interfaces detail"

_SOURCE_COMMANDS = [
    SHOW_INTERFACES,
    SHOW_DESCRIPTION,
    SHOW_LLDP,
    SHOW_ISIS,
    SHOW_OSPF,
]


# ---------------------------------------------------------------------------
# Pipe-table parsing (shared by interfaces / description / lldp)
# ---------------------------------------------------------------------------


def _is_separator_row(cells: List[str]) -> bool:
    """True for the markdown-style ``+----+----+`` divider row."""
    return all(set(c) <= {"-", "+", ":", ""} for c in cells)


def _header_index(headers: List[str], label: str) -> Optional[int]:
    """Index of the header cell that equals ``label`` (normalised), or None."""
    for idx, head in enumerate(headers):
        if head == label:
            return idx
    return None


def _iter_pipe_rows(text: str):
    """Yield ``(headers, cells)`` for each data row of a DNOS ``|`` table.

    The header is the first ``|`` row carrying an ``Interface`` column;
    every later non-separator ``|`` row is yielded with the located
    headers. Cells are stripped; both are lowercased only for the header
    so callers can match column labels case-insensitively while keeping
    the raw cell values intact.
    """
    headers: Optional[List[str]] = None
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        lowered = [c.lower() for c in cells]
        if headers is None:
            if _header_index(lowered, "interface") is not None:
                headers = lowered
            continue
        if _is_separator_row(cells):
            continue
        yield headers, cells


def parse_interfaces_table(text: str) -> "OrderedDict[str, Dict[str, str]]":
    """Parse ``show interfaces`` into ``{name: {state fields}}`` in table order.

    Columns are located from the header (Interface / Admin / Operational /
    IPv4 Address / IPv6 Address / VLAN / MTU / Network-Service / Bundle-Id),
    so added or reordered columns across DNOS versions don't break the
    parse. Only non-empty fields are kept per interface.
    """
    col_map = {
        "interface": "interface",
        "admin": "admin",
        "operational": "operational",
        "ipv4 address": "ipv4",
        "ipv6 address": "ipv6",
        "vlan": "vlan",
        "mtu": "mtu",
        "network-service": "network_service",
        "bundle-id": "bundle_id",
    }
    out: "OrderedDict[str, Dict[str, str]]" = OrderedDict()
    for headers, cells in _iter_pipe_rows(text):
        name_idx = _header_index(headers, "interface")
        if name_idx is None or name_idx >= len(cells):
            continue
        name = cells[name_idx]
        if not name:
            continue
        record: Dict[str, str] = {}
        for label, key in col_map.items():
            if key == "interface":
                continue
            idx = _header_index(headers, label)
            if idx is not None and idx < len(cells) and cells[idx]:
                record[key] = cells[idx]
        out[name] = record
    return out


def parse_interfaces_description(text: str) -> Dict[str, str]:
    """Parse ``show interfaces description`` into ``{name: description}``.

    Only interfaces with a non-empty description are returned.
    """
    out: Dict[str, str] = {}
    for headers, cells in _iter_pipe_rows(text):
        name_idx = _header_index(headers, "interface")
        desc_idx = _header_index(headers, "description")
        if name_idx is None or desc_idx is None:
            continue
        if name_idx >= len(cells) or desc_idx >= len(cells):
            continue
        name = cells[name_idx]
        desc = cells[desc_idx]
        if name and desc:
            out[name] = desc
    return out


def parse_lldp_table(text: str) -> Dict[str, Dict[str, str]]:
    """Parse ``show lldp neighbors`` into ``{local_iface: {neighbor, ...}}``.

    Keyed by the local interface; value carries ``neighbor`` (system
    name), ``neighbor_interface`` (remote port) and ``ttl``. Rows with no
    neighbor system name (an up link with nothing learned yet) are
    skipped, so ``lldp`` ends up ``null`` for those interfaces.
    """
    out: Dict[str, Dict[str, str]] = {}
    for headers, cells in _iter_pipe_rows(text):
        iface_idx = _header_index(headers, "interface")
        name_idx = _header_index(headers, "neighbor system name")
        port_idx = _header_index(headers, "neighbor interface")
        ttl_idx = _header_index(headers, "neighbor ttl")
        if iface_idx is None or name_idx is None:
            continue
        if iface_idx >= len(cells) or name_idx >= len(cells):
            continue
        local = cells[iface_idx]
        neighbor = cells[name_idx]
        if not local or not neighbor:
            continue
        record: Dict[str, str] = {"neighbor": neighbor}
        if port_idx is not None and port_idx < len(cells) and cells[port_idx]:
            record["neighbor_interface"] = cells[port_idx]
        if ttl_idx is not None and ttl_idx < len(cells) and cells[ttl_idx]:
            record["ttl"] = cells[ttl_idx]
        out[local] = record
    return out


# ---------------------------------------------------------------------------
# IGP detail parsing (block format, shared shape for ISIS + OSPF)
# ---------------------------------------------------------------------------

_ISIS_INSTANCE_RE = re.compile(r"^Instance\s+(?P<inst>\S+):\s*$")
_INSTANCE_LEVEL_RE = re.compile(r"^\s+Instance Level:\s*(?P<lvl>\S+)")
# ``Interface: ge400-7/0/8.6, State: Up, Active`` (mode is Active/Passive,
# and may be absent on some rows).
_IGP_IFACE_RE = re.compile(
    r"^\s+Interface:\s*(?P<if>[^,\s]+)\s*,\s*State:\s*(?P<state>[^,\s]+)"
    r"(?:\s*,\s*(?P<mode>[^,\s]+))?"
)
_IFACE_LEVEL_RE = re.compile(r"\bLevel:\s*(?P<lvl>\S+)")
_METRIC_RE = re.compile(r"\bMetric:\s*(?P<metric>\d+)")
_NEIGHBORS_RE = re.compile(r"\bActive neighbors:\s*(?P<n>\d+)")
_NOT_ENABLED_RE = re.compile(r"not\s+enabled", re.IGNORECASE)


def parse_isis_interfaces(text: str) -> Dict[str, Dict[str, Any]]:
    """Parse ``show isis interfaces detail`` into ``{name: {igp fields}}``.

    Walks the per-instance / per-interface block layout DNOS prints,
    capturing for each interface: ``instance`` name, ISIS ``level``,
    operational ``state`` (Up/Down), ``passive`` (bool, the
    Active/Passive flag on the Interface line), first ``metric`` seen in
    the level information, and ``neighbors`` (the ``Active neighbors``
    count). Returns ``{}`` when ISIS isn't enabled / no interface blocks
    are present.
    """
    if not text or _NOT_ENABLED_RE.search(text):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    instance: Optional[str] = None
    instance_level: Optional[str] = None
    cur: Optional[Dict[str, Any]] = None

    def _flush() -> None:
        if cur is not None:
            out[cur.pop("_name")] = cur

    for line in text.splitlines():
        m_inst = _ISIS_INSTANCE_RE.match(line)
        if m_inst:
            _flush()
            cur = None
            instance = m_inst.group("inst")
            instance_level = None
            continue
        m_lvl = _INSTANCE_LEVEL_RE.match(line)
        if m_lvl and cur is None:
            instance_level = m_lvl.group("lvl")
            continue
        m_if = _IGP_IFACE_RE.match(line)
        if m_if:
            _flush()
            mode = (m_if.group("mode") or "").strip().lower()
            cur = {
                "_name": m_if.group("if"),
                "protocol": "isis",
                "instance": instance,
                "level": instance_level,
                "state": m_if.group("state"),
                "passive": mode == "passive",
                "metric": None,
                "neighbors": None,
            }
            continue
        if cur is None:
            continue
        # Inside an interface block: refine level, metric, neighbor count.
        if "Type:" in line:
            m_iflvl = _IFACE_LEVEL_RE.search(line)
            if m_iflvl:
                cur["level"] = m_iflvl.group("lvl")
        if cur["metric"] is None:
            m_metric = _METRIC_RE.search(line)
            if m_metric:
                cur["metric"] = int(m_metric.group("metric"))
        m_nbr = _NEIGHBORS_RE.search(line)
        if m_nbr:
            cur["neighbors"] = int(m_nbr.group("n"))
    _flush()
    return out


def parse_ospf_interfaces(text: str) -> Dict[str, Dict[str, Any]]:
    """Parse ``show ospf interfaces detail`` into ``{name: {igp fields}}``.

    Returns ``{}`` when OSPF isn't running (DNOS prints ``OSPF Routing
    Process not enabled``). When enabled, DNOS mirrors the ISIS block
    layout closely (``Instance ...:`` / ``Interface: <name>, State:
    ...`` / ``Metric:`` / ``Active neighbors:``), so the ISIS walker is
    reused and the protocol tag rewritten. Best-effort: any field the
    build doesn't expose stays ``None``.
    """
    if not text or _NOT_ENABLED_RE.search(text):
        return {}
    parsed = parse_isis_interfaces(text)
    for record in parsed.values():
        record["protocol"] = "ospf"
    return parsed


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------


def _parent_of(name: str) -> Optional[str]:
    """Physical/parent interface of a sub-interface (``ge..8.6`` → ``ge..8``)."""
    return name.split(".", 1)[0] if "." in name else None


def _description_for(
    name: str, desc_map: Dict[str, str]
) -> Tuple[str, Optional[str]]:
    own = desc_map.get(name, "")
    if own:
        return own, None
    parent = _parent_of(name)
    if parent and desc_map.get(parent):
        return desc_map[parent], parent
    return "", None


def _lldp_for(
    name: str, lldp_map: Dict[str, Dict[str, str]]
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    if name in lldp_map:
        return lldp_map[name], None
    parent = _parent_of(name)
    if parent and parent in lldp_map:
        return lldp_map[parent], parent
    return None, None


def build_interface_view(
    interfaces_out: str,
    description_out: str,
    lldp_out: str,
    isis_out: str,
    ospf_out: str,
) -> "OrderedDict[str, Dict[str, Any]]":
    """Join the five show outputs into one object per interface (table order).

    The ``show interfaces`` table is authoritative for the interface set
    and ordering; each interface gets nested ``state`` / ``description`` /
    ``lldp`` / ``igp`` blocks. Description and LLDP fall back to the parent
    physical port for sub-interfaces that carry none of their own (flagged
    via ``description_inherited_from`` / ``lldp_inherited_from``). ``igp``
    prefers ISIS, falling back to OSPF when only OSPF has the interface.
    """
    state_map = parse_interfaces_table(interfaces_out)
    desc_map = parse_interfaces_description(description_out)
    lldp_map = parse_lldp_table(lldp_out)
    isis_map = parse_isis_interfaces(isis_out)
    ospf_map = parse_ospf_interfaces(ospf_out)

    view: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for name, state in state_map.items():
        entry: Dict[str, Any] = {"state": state}

        desc, desc_from = _description_for(name, desc_map)
        entry["description"] = desc
        if desc_from:
            entry["description_inherited_from"] = desc_from

        lldp, lldp_from = _lldp_for(name, lldp_map)
        entry["lldp"] = lldp
        if lldp_from and lldp is not None:
            entry["lldp_inherited_from"] = lldp_from

        entry["igp"] = isis_map.get(name) or ospf_map.get(name) or None
        view[name] = entry
    return view


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def _render_text(view: "OrderedDict[str, Dict[str, Any]]") -> str:
    """Compact one-line-per-interface summary for non-``--json`` output."""
    lines: List[str] = []
    for name, entry in view.items():
        st = entry.get("state") or {}
        bits = [name]
        admin = st.get("admin", "")
        oper = st.get("operational", "")
        if admin or oper:
            bits.append(f"{admin or '?'}/{oper or '?'}")
        if st.get("ipv4"):
            bits.append(st["ipv4"])
        if st.get("vlan"):
            bits.append(f"vlan {st['vlan']}")
        desc = entry.get("description") or ""
        if desc:
            bits.append(f'desc="{desc}"')
        lldp = entry.get("lldp")
        if lldp:
            nbr = lldp.get("neighbor", "")
            port = lldp.get("neighbor_interface", "")
            bits.append(f"lldp={nbr}" + (f"({port})" if port else ""))
        igp = entry.get("igp")
        if igp:
            seg = f"igp={igp.get('protocol', '?')}"
            if igp.get("instance"):
                seg += f" {igp['instance']}"
            if igp.get("level"):
                seg += f" {igp['level']}"
            if igp.get("passive"):
                seg += " passive"
            if igp.get("metric") is not None:
                seg += f" metric {igp['metric']}"
            if igp.get("neighbors") is not None:
                seg += f" nbrs {igp['neighbors']}"
            bits.append(seg)
        lines.append("  ".join(bits))
    return "\n".join(lines) + ("\n" if lines else "")


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@requires(CAP_INTERFACES)
def interfaces(
    interface: Optional[str] = None,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Aggregated per-interface view: state + description + LLDP + IGP.

    Runs ``show interfaces``, ``show interfaces description``,
    ``show lldp neighbors``, ``show isis interfaces detail`` and
    ``show ospf interfaces detail`` on one SSH channel and joins them by
    interface name — one object per interface with nested ``state`` /
    ``description`` / ``lldp`` / ``igp`` blocks. This replaces the common
    3–4 ``show`` round-trips + manual name correlation with a single call.

    The ``interfaces`` key on the response is an ordered map keyed by
    interface name (``--json`` is pipe-to-``jq`` friendly). Pass
    ``interface`` to scope the result to a single interface.

    IGP auto-picks whichever protocol the box runs: ISIS is preferred and
    OSPF fills in interfaces only OSPF reports; a box running neither just
    gets ``igp: null`` everywhere.

    Args:
        interface: Optional single interface to filter to (e.g.
            ``ge400-7/0/8.6``). Omit for every interface.
        device: Device alias from the registry.
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-command timeout seconds.
    """
    request = {"device": device, "host": host, "user": user, "command": "interfaces"}
    response = make_response(device=device, host=host, command="interfaces")

    try:
        result = run_sequence(
            transport_registry,
            device=device,
            host=host,
            user=user,
            password=password,
            commands=_SOURCE_COMMANDS,
            timeout=timeout,
        )
    except ConnectError as exc:
        response.update(
            status="connect_error",
            errors=[str(exc)],
            next_actions=connect_error_next_actions(exc),
        )
        log_request("interfaces", request, response)
        return response
    except Exception as exc:  # noqa: BLE001 - surface any transport failure cleanly
        response.update(status="error", errors=[str(exc)])
        log_request("interfaces", request, response)
        return response

    response["host"] = result.host
    response["device"] = result.device or device

    outputs: Dict[str, str] = {step.command: step.output for step in result.steps}
    log_invocation(
        result.device or device,
        result.host,
        " ; ".join(_SOURCE_COMMANDS),
        result.output,
        result.head_prompt_line,
        result.tail_prompt,
        steps=result.steps,
    )

    # The interface table is the spine. If it didn't come back (timeout
    # before it ran, or a DNOS error on it), there's nothing to join.
    interfaces_out = outputs.get(SHOW_INTERFACES, "")
    if not interfaces_out:
        response.update(
            status="timeout",
            errors=[f"'{SHOW_INTERFACES}' produced no output within {timeout}s."],
            next_actions=["Retry with a larger --timeout."],
        )
        log_request("interfaces", request, response)
        return response
    is_err, err_lines = detect_error(interfaces_out)
    if is_err:
        response.update(status="error", errors=err_lines[-5:], next_actions=[SHOW_NEXT_ACTION])
        log_request("interfaces", request, response)
        return response

    view = build_interface_view(
        interfaces_out,
        outputs.get(SHOW_DESCRIPTION, ""),
        outputs.get(SHOW_LLDP, ""),
        outputs.get(SHOW_ISIS, ""),
        outputs.get(SHOW_OSPF, ""),
    )

    # A best-effort source that timed out / errored is noted, not fatal —
    # the join just leaves that block empty.
    for cmd in (SHOW_DESCRIPTION, SHOW_LLDP, SHOW_ISIS, SHOW_OSPF):
        if cmd not in outputs:
            response["warnings"].append(
                f"'{cmd}' did not run (channel stopped early); "
                f"its block is omitted from the join."
            )

    if interface is not None:
        if interface not in view:
            response.update(
                status="error",
                errors=[f"Interface {interface!r} not found in '{SHOW_INTERFACES}'."],
                next_actions=[
                    f"Run interfaces without an interface filter to list valid names, "
                    f"or check the name via '{SHOW_INTERFACES}'."
                ],
            )
            log_request("interfaces", request, response)
            return response
        view = OrderedDict([(interface, view[interface])])

    response["interfaces"] = view
    response["interface_count"] = len(view)
    response["stdout"] = _render_text(view)
    log_request("interfaces", request, response)
    return response


def register(mcp) -> None:
    """Wire this module's tool onto a FastMCP instance."""
    mcp.tool()(interfaces)


__all__ = [
    "interfaces",
    "build_interface_view",
    "parse_interfaces_table",
    "parse_interfaces_description",
    "parse_lldp_table",
    "parse_isis_interfaces",
    "parse_ospf_interfaces",
    "register",
]
