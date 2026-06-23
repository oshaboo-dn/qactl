"""Single-source DNOS device probe — parsers + show-command orchestration.

cli-mcp owns every SSH-to-DNOS interaction in the monorepo. This module
gives cli-mcp's tools (``manage_device(add)`` / ``manage_device(refresh)``)
a transport-agnostic place to keep:

- the regex parsers for ``show system`` (System Name / System-Id /
  System Type) and ``show interfaces management`` (mgmt0 IPv4),
- the :class:`DeviceProbe` dataclass that bundles the parsed result,
- the orchestration ("run two shows in this order, parse each") via
  :func:`probe_via`.

cli-mcp's ``dnctl.cli.core/session.py:probe_device`` builds a closure that runs
each show command on its pooled :class:`TransportRegistry` and hands
the closure to :func:`probe_via`. No paramiko code lives here — this
module is pure parsing + orchestration; the SSH I/O is the caller's
problem. netconf-mcp / restconf-mcp / gnmi-mcp do not call into here:
they read the canonical ``<repo>/devices/devices_mgmt0.json`` map
read-only via :mod:`dnctl.core.devices` and surface a "stale mgmt0,
call cli-mcp's manage_device(refresh)" error to the agent when their
cached IP stops responding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional


# ---------------------------------------------------------------------------
# Parsers (pure)
# ---------------------------------------------------------------------------

# DNOS ``show system`` puts the configured chassis name on a line of the
# form ``System Name: <NAME>, System-Id: <uuid>``. We capture <NAME> up to
# the next comma or end-of-line, allowing internal punctuation (``-``,
# ``_``, ``.``) but disallowing whitespace so we never absorb the column
# header that follows on a separate line.
_SYSTEM_NAME_RE = re.compile(
    r"^\s*System\s+Name\s*:\s*(?P<name>[^\s,][^,\n]*?)\s*(?:,|$)",
    re.MULTILINE | re.IGNORECASE,
)
# ``System-Id: <uuid>`` continues on the same line as System Name. We
# record it on the canonical map alongside the alias so a later
# manage_device(add) for a second NCC of the SAME chassis can verify
# the appended SN really belongs to the existing alias before merging.
_SYSTEM_ID_RE = re.compile(
    r"System[-\s]+Id\s*:\s*(?P<sid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)
# ``System Type: SA-40C8CD`` / ``System Type: CL-16``. Anything starting
# with ``SA`` maps to role ``SA`` (single-active chassis); ``CL`` maps to
# ``CL`` (cluster, dual-NCC). Other prefixes are unknown — the caller
# leaves expected_role unset.
_SYSTEM_TYPE_RE = re.compile(
    r"^\s*System\s+Type\s*:\s*(?P<type>[A-Za-z][A-Za-z0-9]*)",
    re.MULTILINE | re.IGNORECASE,
)
# ``show interfaces management`` prints a table with columns separated
# by ``|``; the mgmt0 row carries the IPv4/CIDR in column index 4
# (counting empty leading-pipe column as 0). Match an IPv4 token
# anywhere in that column.
_MGMT0_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})(?:/\d{1,2})?\b")

# GI-mode (golden-image / installer environment) discriminators. After a
# ``delete to GI`` + redeploy, ``show system`` emits a structurally
# different schema than operational DNOS: it prints ``Active NCC: <SN>``
# instead of ``System Name:`` / ``System Type:`` / ``Version: DNOS [...]``,
# and its inventory table carries a ``GI version`` column. Both schemas
# open with ``System status: running``, so that line alone is NOT a
# reliable "DNOS is up" signal — we key off the structural differences.
_GI_ACTIVE_NCC_RE = re.compile(
    r"^\s*Active\s+NCC\s*:\s*\S", re.MULTILINE | re.IGNORECASE
)
_DNOS_VERSION_RE = re.compile(
    r"^\s*Version\s*:\s*DNOS\s*\[", re.MULTILINE | re.IGNORECASE
)
_GI_VERSION_COL_RE = re.compile(r"\bGI\s+version\b", re.IGNORECASE)


def parse_system_name(show_system_output: str) -> Optional[str]:
    """Return the ``System Name`` token from a ``show system`` capture.

    Returns ``None`` when the line isn't present or is empty. Only the bare
    name is returned — the ``, System-Id: ...`` continuation is stripped.
    """
    match = _SYSTEM_NAME_RE.search(show_system_output)
    if not match:
        return None
    name = match.group("name").strip()
    return name or None


def parse_system_id(show_system_output: str) -> Optional[str]:
    """Return the ``System-Id`` UUID from a ``show system`` capture, lowercased."""
    match = _SYSTEM_ID_RE.search(show_system_output)
    if not match:
        return None
    return match.group("sid").lower()


def parse_expected_role(show_system_output: str) -> Optional[str]:
    """Map the ``System Type:`` prefix on ``show system`` output to ``"SA"`` / ``"CL"``.

    Returns ``None`` when the field is missing or doesn't start with
    one of the two known prefixes — callers should treat that as
    "leave expected_role unset" rather than guess.
    """
    match = _SYSTEM_TYPE_RE.search(show_system_output)
    if not match:
        return None
    head = match.group("type").upper()
    if head.startswith("CL"):
        return "CL"
    if head.startswith("SA"):
        return "SA"
    return None


def parse_mgmt0_ipv4(show_interfaces_management_output: str) -> Optional[str]:
    """Parse mgmt0's IPv4 address from ``show interfaces management`` output."""
    for line in show_interfaces_management_output.splitlines():
        if "| mgmt0" not in line:
            continue
        columns = [c.strip() for c in line.split("|")]
        if len(columns) < 6:
            continue
        match = _MGMT0_IPV4_RE.search(columns[4])
        if match:
            return match.group(1)
    return None


def parse_ncc_serials(show_system_output: str) -> List[str]:
    """Return the Serial Number of every NCC slot in a ``show system`` capture.

    DNOS ``show system`` ends with a pipe-delimited hardware-inventory
    table whose rows look like::

        | Type | Id | Admin | Operational | Model | Uptime | Description | Serial Number |
        | NCC  | 0  |       | standby-up  | X86   | ...    | dn-ncc-0    | CZ22500CW4    |
        | NCC  | 1  |       | active-up   | X86   | ...    | dn-ncc-1    | CZ22260685    |

    On a dual-NCC (CL) chassis BOTH NCC slots appear with their own
    serials; on an SA chassis there's a single NCC row. We return the
    serials of the rows whose ``Type`` column is exactly ``NCC``, in
    table order, skipping rows with an empty serial (an absent slot).

    The ``Type`` and ``Serial Number`` columns are located from the table
    header rather than by fixed offset, so the parser tolerates columns
    being added / reordered across DNOS versions. Returns ``[]`` when no
    NCC row with a serial is present (e.g. a Genesis-Image box or a
    non-DNOS device); the caller then falls back to the probed SN alone.
    """
    type_idx: Optional[int] = None
    serial_idx: Optional[int] = None
    serials: List[str] = []
    for line in show_system_output.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        if type_idx is None or serial_idx is None:
            lowered = [c.lower() for c in cells]
            if "type" in lowered and "serial number" in lowered:
                type_idx = lowered.index("type")
                serial_idx = lowered.index("serial number")
            continue
        if type_idx >= len(cells) or serial_idx >= len(cells):
            continue
        if cells[type_idx].upper() != "NCC":
            continue
        serial = cells[serial_idx]
        if serial and serial not in serials:
            serials.append(serial)
    return serials


def detect_system_mode(show_system_output: str) -> str:
    """Classify a ``show system`` capture as ``"operational"`` / ``"gi"`` / ``"unknown"``.

    Both the operational and GI-mode (golden-image installer environment)
    schemas open with ``System status: running``, so that line is useless
    as a discriminator — a box sitting in GI mode is *not* running
    operational DNOS. We instead key off the structural differences:

    - **operational**: carries ``System Type: ...`` and/or
      ``Version: DNOS [...]``.
    - **gi**: lacks both of those but carries ``Active NCC: <SN>`` and/or
      a ``GI version`` inventory column.
    - **unknown**: neither schema's markers are present (empty output, a
      DNOS error, or a non-DNOS device).

    Operational markers win when both appear, so the rare box that prints
    ``Active NCC:`` alongside a real ``Version: DNOS [...]`` is still
    reported as operational.
    """
    text = show_system_output or ""
    if _SYSTEM_TYPE_RE.search(text) or _DNOS_VERSION_RE.search(text):
        return "operational"
    if _GI_ACTIVE_NCC_RE.search(text) or _GI_VERSION_COL_RE.search(text):
        return "gi"
    return "unknown"


def parse_gi_inventory(show_system_output: str) -> List[dict]:
    """Parse the per-node GI-mode inventory rows from a ``show system`` capture.

    GI mode prints a pipe-delimited table whose columns include
    ``Status`` / ``BaseOS version`` / ``GI version`` (and ``ONIE version``
    / ``FW MU version``) instead of the operational
    ``Admin | Operational | Uptime | ...`` set::

        | Type | Id | Status | ... | ONIE version | FW MU version | BaseOS version | GI version |
        | NCC  | 0  | stable | ... | 2022.08_...   | N/A           | 2.2630318015   | 26.3.0.50_priv... |

    Returns one dict per data row in table order, keyed by a normalised
    subset of columns (``type`` / ``id`` / ``status`` / ``serial_number``
    / ``onie_version`` / ``fw_mu_version`` / ``baseos_version`` /
    ``gi_version``), each present only when its column exists and the cell
    is non-empty. Returns ``[]`` when no GI inventory table is found, so
    callers can treat an empty list as "nothing structured to surface".
    """
    col_map = {
        "type": "type",
        "id": "id",
        "status": "status",
        "serial number": "serial_number",
        "onie version": "onie_version",
        "fw mu version": "fw_mu_version",
        "baseos version": "baseos_version",
        "gi version": "gi_version",
    }
    header: Optional[List[str]] = None
    rows: List[dict] = []
    for line in show_system_output.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        lowered = [c.lower() for c in cells]
        if header is None:
            if "gi version" in lowered and "type" in lowered:
                header = lowered
            continue
        # Skip the markdown-style separator row (``+----+----+...``).
        if all(set(c) <= {"-", "+", ":", ""} for c in cells):
            continue
        row: dict = {}
        for idx, key in enumerate(header):
            field_name = col_map.get(key)
            if field_name is None or idx >= len(cells):
                continue
            value = cells[idx]
            if value:
                row[field_name] = value
        if row.get("type", "").upper() in {"NCC", "NCP", "NCM", "NCF"}:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class DeviceProbe:
    """Parsed result of a fresh DNOS probe.

    ``system_name`` is the chassis's configured name (populated when the
    device answered the ``show system`` line). It is ``None`` for a box
    that has no System Name to expose — e.g. one sitting in GI mode
    (golden-image installer, DNOS not yet deployed); callers that allow
    that (``probe_via(allow_missing_name=True)``) must supply their own
    alias. ``system_id`` / ``expected_role`` / ``mgmt0`` are best-effort
    and ``None`` when the device didn't expose them or we couldn't parse
    them. ``ncc_serials`` is the list of NCC slot serials from the
    ``show system`` hardware table (both NCCs on a CL chassis, one on an
    SA, ``[]`` when none were parseable) — it lets a single
    ``manage_device(add)`` enroll a dual-NCC chassis's standby NCC
    alongside the one that was SSHed; it is populated from the GI-mode
    inventory table too. ``mode`` is the
    :func:`detect_system_mode` classification
    (``"operational"`` / ``"gi"`` / ``"unknown"``) so the caller can tell
    a genuine GI-mode chassis from unparseable / non-DNOS output.
    """

    system_name: Optional[str] = None
    system_id: Optional[str] = None
    expected_role: Optional[str] = None
    mgmt0: Optional[str] = None
    ncc_serials: List[str] = field(default_factory=list)
    mode: str = "operational"


# ---------------------------------------------------------------------------
# Orchestration (transport-agnostic)
# ---------------------------------------------------------------------------


def probe_via(
    run_show: Callable[[str], str],
    *,
    allow_missing_name: bool = False,
) -> DeviceProbe:
    """Run the canonical DNOS probe via ``run_show`` and parse the result.

    ``run_show(cmd)`` MUST return the device's textual response to
    ``cmd``. The closure encapsulates whatever transport the caller
    wants — cli-mcp passes a closure over its pooled
    ``TransportRegistry``; we do not own SSH here.

    Raises ``RuntimeError`` when ``show system`` doesn't yield a
    parseable ``System Name`` — unless ``allow_missing_name`` is set, in
    which case ``system_name`` comes back ``None`` and the caller must
    supply its own alias (the GI-mode registration path does this and
    falls back to the probed SN). The mgmt0 step is best-effort: on
    error or empty output, ``mgmt0`` is ``None`` and the call still
    succeeds.
    """
    sys_out = run_show("show system")
    name = parse_system_name(sys_out)
    if not name and not allow_missing_name:
        raise RuntimeError(
            "could not parse 'System Name:' from `show system` output; "
            "device may not be a DNOS chassis or the response is unexpected."
        )

    mgmt0_out = ""
    try:
        mgmt0_out = run_show("show interfaces management")
    except Exception:  # noqa: BLE001 - mgmt0 is best-effort
        mgmt0_out = ""

    return DeviceProbe(
        system_name=name,
        system_id=parse_system_id(sys_out),
        expected_role=parse_expected_role(sys_out),
        mgmt0=parse_mgmt0_ipv4(mgmt0_out) if mgmt0_out else None,
        ncc_serials=parse_ncc_serials(sys_out),
        mode=detect_system_mode(sys_out),
    )


__all__ = [
    "DeviceProbe",
    "parse_system_name",
    "parse_system_id",
    "parse_expected_role",
    "parse_mgmt0_ipv4",
    "parse_ncc_serials",
    "detect_system_mode",
    "parse_gi_inventory",
    "probe_via",
]
