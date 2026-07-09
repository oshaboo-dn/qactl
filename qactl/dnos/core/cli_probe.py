"""Single-source DNOS device probe — parsers + show-command orchestration.

cli-mcp owns every SSH-to-DNOS interaction in the monorepo. This module
gives cli-mcp's tools (``manage_device(add)`` / ``manage_device(refresh)``)
a transport-agnostic place to keep:

- the regex parsers for ``show system`` (System Name / System-Id /
  System Type) and ``show interfaces management`` (mgmt0 IPv4),
- the :class:`DeviceProbe` dataclass that bundles the parsed result,
- the orchestration ("run two shows in this order, parse each") via
  :func:`probe_via`.

cli-mcp's ``qactl.cli.core/session.py:probe_device`` builds a closure that runs
each show command on its pooled :class:`TransportRegistry` and hands
the closure to :func:`probe_via`. No paramiko code lives here — this
module is pure parsing + orchestration; the SSH I/O is the caller's
problem. netconf-mcp / restconf-mcp / gnmi-mcp do not call into here:
they read the canonical ``<repo>/devices/devices_mgmt0.json`` map
read-only via :mod:`qactl.core.devices` and surface a "stale mgmt0,
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

# ``System status: <value>`` — the raw machine-state line. Values seen live:
# ``running`` (healthy) and ``running (insufficient-ncfs)`` (degraded,
# SW-279187 episode 2026-07-02). Recovery mode is expected to surface here
# too (``System status: recovery``). NOT to be confused with the
# ``Recovery-mode: supported`` line, which is a *capability* flag present on
# every healthy box — the regex is anchored to the ``System status:`` label
# so the capability line can never match.
_SYSTEM_STATUS_RE = re.compile(
    r"^\s*System\s+status\s*:\s*(?P<status>.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
# Active-recovery marker outside the status line (banner / free-text form,
# e.g. "system is in recovery mode"). Requires the two words separated by
# whitespace so the hyphenated capability label ``Recovery-mode:`` — present
# on every healthy ``show system`` — can never match.
_RECOVERY_MODE_TEXT_RE = re.compile(r"\brecovery\s+mode\b", re.IGNORECASE)


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


def parse_system_status(show_system_output: str) -> Optional[str]:
    """Return the raw ``System status:`` value from a ``show system`` capture.

    E.g. ``"running"`` or ``"running (insufficient-ncfs)"``. Returns ``None``
    when the line is absent (GI-mode variants, non-DNOS output, errors).
    Only the ``System status:`` line matches — the ``Recovery-mode:``
    capability line is a different label and never leaks in.
    """
    match = _SYSTEM_STATUS_RE.search(show_system_output or "")
    if not match:
        return None
    return match.group("status").strip() or None


def classify_system_state(show_system_output: str) -> str:
    """Map a ``show system`` capture to one stable machine-state enum value.

    The enum (issue #66): ``running`` | ``running-degraded`` | ``recovery``
    | ``gi`` | ``unknown`` (connect-path failures add ``unreachable``,
    assigned by the caller — a failed connect has no output to classify).

    - ``recovery`` — the ``System status:`` line mentions recovery, or the
      body carries an active-recovery marker ("... recovery mode ...").
      The ``Recovery-mode: supported`` *capability* line on every healthy
      box never triggers this (different label, hyphenated).
    - ``gi`` — golden-image installer schema; its bare
      ``System status: running`` reflects the installer, not DNOS.
    - ``running`` / ``running-degraded`` — operational schema with a clean
      ``running`` status vs. a qualified one
      (``running (insufficient-ncfs)``, ...).
    - ``unknown`` — anything else, including a bare status line with no
      recognisable schema around it (the GI lesson: that line alone proves
      nothing).
    """
    text = show_system_output or ""
    status = parse_system_status(text)
    if status and "recovery" in status.lower():
        return "recovery"
    if _RECOVERY_MODE_TEXT_RE.search(text):
        return "recovery"
    mode = detect_system_mode(text)
    if mode == "gi":
        return "gi"
    if mode == "operational" and status:
        low = status.lower()
        if low == "running":
            return "running"
        if low.startswith("running"):
            return "running-degraded"
    return "unknown"


# ``show lldp neighbors`` location discovery (issue #40)
# -----------------------------------------------------
# A device's physical rack / DNAAS leaf is encoded in the names of its
# LLDP neighbors:
#   - the mgmt0 neighbor is the mgmt switch, e.g. ``IL-SW-B13`` => rack B13
#   - each data/fabric port neighbors a DNAAS leaf, e.g.
#     ``DNAAS-LEAF-B13 ge100-0/0/16`` => homed on leaf B13
# Both signals encode the same rack token (a letter-then-digits suffix
# like ``B13``). We parse the pipe-delimited ``show lldp neighbors`` table
# (same table style as ``show system``) by locating columns from the
# header, so the parser tolerates columns being added / reordered across
# DNOS versions, then classify each row as the mgmt switch or a fabric
# leaf by its local interface name.

# A rack token is a short alpha prefix followed by 1-3 digits (``B13``,
# ``A7``, ``AB12``); we never invent one from a token that doesn't match.
_RACK_TOKEN_RE = re.compile(r"^[A-Za-z]{1,3}\d{1,3}$")


def rack_from_name(name: Optional[str]) -> Optional[str]:
    """Extract the rack token (e.g. ``B13``) from a switch / leaf name.

    Both ``IL-SW-B13`` (mgmt switch) and ``DNAAS-LEAF-B13`` (fabric leaf)
    carry the rack as a trailing ``-``/``_``-separated component shaped
    like a short alpha prefix + digits. We scan components from the end
    and return the first that matches, upper-cased. Returns ``None`` when
    no component looks like a rack token.
    """
    if not name or not isinstance(name, str):
        return None
    for token in reversed(re.split(r"[-_]", name.strip())):
        if _RACK_TOKEN_RE.fullmatch(token):
            return token.upper()
    return None


def _find_header_idx(
    headers: List[str], includes: tuple, excludes: tuple = ()
) -> Optional[int]:
    """First header index containing any ``includes`` substring and no ``excludes``."""
    for idx, head in enumerate(headers):
        if any(inc in head for inc in includes) and not any(
            exc in head for exc in excludes
        ):
            return idx
    return None


def parse_lldp_neighbors(show_lldp_neighbors_output: str) -> List[dict]:
    """Parse ``show lldp neighbors`` into ``{local_interface, neighbor, remote_port}`` rows.

    DNOS prints a pipe-delimited table whose header carries a local
    interface column, a neighbor system-name column, and a neighbor port
    column. Column positions are located from the header (by keyword)
    rather than fixed offset, so added / reordered columns across DNOS
    versions don't break the parser. Rows whose local interface or
    neighbor name is empty are skipped; returns ``[]`` when no recognised
    table is present.
    """
    iface_idx: Optional[int] = None
    name_idx: Optional[int] = None
    port_idx: Optional[int] = None
    rows: List[dict] = []
    for line in show_lldp_neighbors_output.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        lowered = [c.lower() for c in cells]
        if iface_idx is None or name_idx is None:
            i = _find_header_idx(
                lowered,
                ("local interface", "local port", "interface", "local intf"),
                excludes=("neighbor", "remote", "system"),
            )
            n = _find_header_idx(
                lowered,
                ("system name", "neighbor", "device id", "chassis name",
                 "remote system"),
                excludes=("port",),
            )
            if i is not None and n is not None:
                iface_idx, name_idx = i, n
                port_idx = _find_header_idx(
                    lowered,
                    ("port id", "neighbor port", "remote port", "port"),
                    excludes=("local",),
                )
            continue
        # Skip the markdown-style separator row (``+----+----+...``).
        if all(set(c) <= {"-", "+", ":", ""} for c in cells):
            continue
        if iface_idx >= len(cells) or name_idx >= len(cells):
            continue
        local_iface = cells[iface_idx]
        neighbor = cells[name_idx]
        if not local_iface or not neighbor:
            continue
        remote_port = (
            cells[port_idx]
            if port_idx is not None and port_idx < len(cells)
            else ""
        )
        rows.append(
            {
                "local_interface": local_iface,
                "neighbor": neighbor,
                "remote_port": remote_port,
            }
        )
    return rows


def derive_location(show_lldp_neighbors_output: str) -> "LldpLocation":
    """Classify ``show lldp neighbors`` rows into rack / mgmt switch / fabric leaves.

    The mgmt0 neighbor (local interface containing ``mgmt``) is the mgmt
    switch; every other neighbor is a fabric leaf link recorded as
    ``{leaf, local_port, remote_port}``. The rack token is taken from the
    mgmt switch name first (the more reliable signal), falling back to the
    fabric leaf names. Disagreements (multiple leaf racks, or mgmt vs leaf
    mismatch) are surfaced as warnings rather than silently resolved.
    """
    rows = parse_lldp_neighbors(show_lldp_neighbors_output)
    mgmt_switch: Optional[str] = None
    fabric_leaf: List[dict] = []
    warnings: List[str] = []
    for row in rows:
        if "mgmt" in row["local_interface"].lower():
            if mgmt_switch is None:
                mgmt_switch = row["neighbor"]
        else:
            fabric_leaf.append(
                {
                    "leaf": row["neighbor"],
                    "local_port": row["local_interface"],
                    "remote_port": row["remote_port"],
                }
            )

    mgmt_rack = rack_from_name(mgmt_switch)
    leaf_racks = sorted(
        {r for r in (rack_from_name(e["leaf"]) for e in fabric_leaf) if r}
    )
    leaf_rack = leaf_racks[0] if leaf_racks else None
    if len(leaf_racks) > 1:
        warnings.append(
            f"fabric leaves resolve to multiple racks {leaf_racks}; "
            f"using {leaf_rack}"
        )
    if mgmt_rack and leaf_rack and mgmt_rack != leaf_rack:
        warnings.append(
            f"mgmt switch rack {mgmt_rack!r} disagrees with fabric leaf "
            f"rack {leaf_rack!r}; using mgmt switch rack"
        )

    return LldpLocation(
        rack=mgmt_rack or leaf_rack,
        mgmt_switch=mgmt_switch,
        fabric_leaf=fabric_leaf,
        warnings=warnings,
    )


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
class LldpLocation:
    """Physical location of a device, derived from ``show lldp neighbors``.

    ``rack`` is the rack token (e.g. ``B13``) decoded from the mgmt switch
    or fabric leaf names; ``mgmt_switch`` is the mgmt0 LLDP neighbor's name
    (e.g. ``IL-SW-B13``); ``fabric_leaf`` is one ``{leaf, local_port,
    remote_port}`` dict per data-port neighbor (the DNAAS leaf this device
    is homed on, plus the cabling). ``warnings`` carries any ambiguity
    encountered while deriving the rack (multiple racks, mgmt-vs-leaf
    mismatch). Any field is ``None`` / empty when LLDP didn't surface it.
    """

    rack: Optional[str] = None
    mgmt_switch: Optional[str] = None
    fabric_leaf: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


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
    ``location`` is the :class:`LldpLocation` derived from
    ``show lldp neighbors`` (rack / mgmt switch / fabric leaves); it is
    ``None`` unless the probe was run with ``discover_location=True`` and
    LLDP returned usable output.
    """

    system_name: Optional[str] = None
    system_id: Optional[str] = None
    expected_role: Optional[str] = None
    mgmt0: Optional[str] = None
    ncc_serials: List[str] = field(default_factory=list)
    mode: str = "operational"
    location: Optional[LldpLocation] = None


# ---------------------------------------------------------------------------
# Orchestration (transport-agnostic)
# ---------------------------------------------------------------------------


def probe_via(
    run_show: Callable[[str], str],
    *,
    allow_missing_name: bool = False,
    discover_location: bool = False,
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

    ``discover_location`` (default ``False``) adds a third best-effort
    ``show lldp neighbors`` step whose output is classified into
    ``DeviceProbe.location`` (rack / mgmt switch / fabric leaves). On
    error or empty output ``location`` stays ``None`` — the caller's
    registration is never aborted by LLDP discovery failing.
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

    location: Optional[LldpLocation] = None
    if discover_location:
        lldp_out = ""
        try:
            lldp_out = run_show("show lldp neighbors")
        except Exception:  # noqa: BLE001 - location is best-effort
            lldp_out = ""
        if lldp_out:
            location = derive_location(lldp_out)

    return DeviceProbe(
        system_name=name,
        system_id=parse_system_id(sys_out),
        expected_role=parse_expected_role(sys_out),
        mgmt0=parse_mgmt0_ipv4(mgmt0_out) if mgmt0_out else None,
        ncc_serials=parse_ncc_serials(sys_out),
        mode=detect_system_mode(sys_out),
        location=location,
    )


__all__ = [
    "DeviceProbe",
    "LldpLocation",
    "parse_system_name",
    "parse_system_id",
    "parse_expected_role",
    "parse_mgmt0_ipv4",
    "parse_ncc_serials",
    "detect_system_mode",
    "parse_system_status",
    "classify_system_state",
    "parse_gi_inventory",
    "parse_lldp_neighbors",
    "rack_from_name",
    "derive_location",
    "probe_via",
]
