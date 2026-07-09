"""Trace-log MCP tools (``list_traces`` / ``get_trace``).

DNOS writes per-subsystem trace logs under ``/core/traces/routing_engine/``
(NCC) and per-subsystem dirs under ``/core/traces/`` (NCP: ``datapath``,
``dnos-agent``, ``gi-agent``, ...). These two tools list and read them via
``run start shell``, with the same time-window / grep / tail filters as
the accounting tools — except trace timestamps are device-LOCAL, not UTC.
The MCP probes the device's TZ once per device (``date +%z``, cached in
process memory) and converts the user's UTC ``since`` / ``until`` to
device-local seconds-precision before the awk filter runs.

A small set of presets (``bgp`` / ``isis`` / ``zebra`` / ``fibmgr`` /
``wb_agent``) wraps the well-known shell-entry + base-dir + filename
combinations so callers don't have to know the on-disk layout. Without a
preset the caller passes the raw ``ncc`` / ``ncp`` / ``container``
selectors and (for ``get_trace``) a ``name``.
"""

from __future__ import annotations

import re
import shlex
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from qactl.dnctl.cli.core.envelope import error_response
from qactl.dnctl.cli.core.errors import GET_TRACE_NEXT_ACTION, LIST_TRACES_NEXT_ACTION
from qactl.dnctl.cli.core.log_filters import normalize_accounting_ts, validate_grep_pattern
from qactl.dnctl.cli.core.registry import transport_registry
from qactl.dnctl.cli.core.session import (
    DEFAULT_CMD_TIMEOUT,
    DEFAULT_PASSWORD,
    DEFAULT_USER,
    run_once,
)
from qactl.dnctl.cli.core.shell_exec import _build_shell_entry, run_linux_on_device
from qactl.dnctl.cli.vendors import CAP_LOGS, requires


_TRACES_DEFAULT_DIR = "/core/traces/routing_engine"
# NCPs have no routing_engine dir; their traces live in per-subsystem dirs
# directly under /core/traces/ (datapath, dnos-agent, gi-agent, gi-manager,
# node-manager, ...). Non-preset ncp calls root here and list recursively.
_TRACES_NCP_ROOT = "/core/traces"
_TRACES_MAX_BYTES = 500_000
_TRACES_TAIL_DEFAULT = 500
_TRACES_TAIL_MAX = 50_000
_TRACES_LIST_MAX_ENTRIES = 200
_TRACES_LIST_ABS_MAX = 5000

# Named trace presets. Each preset fixes (shell-entry context, base directory,
# primary filename) for a well-known DNOS subsystem so callers can ask for
# "bgp traces" without knowing the on-disk layout. Start small and grow as
# users hit gaps; the master ``/core/traces/`` log is tracked in TODO.md.
#
#   shell_context = "ncc_default"  → run on the active NCC (or user-supplied
#                                    ``ncc``); ``ncp`` / ``container`` are
#                                    rejected.
#   shell_context = "ncp"          → run on a line card; ``ncp=<id>`` is
#                                    required; ``ncc`` / ``container`` are
#                                    rejected.
_TRACE_TARGETS: Dict[str, Dict[str, str]] = {
    "bgp":      {"shell": "ncc_default",
                 "dir":   "/core/traces/routing_engine",
                 "primary": "bgpd_traces"},
    "isis":     {"shell": "ncc_default",
                 "dir":   "/core/traces/routing_engine",
                 "primary": "isisd_traces"},
    "zebra":    {"shell": "ncc_default",
                 "dir":   "/core/traces/routing_engine",
                 "primary": "zebra_traces"},
    "fibmgr":   {"shell": "ncc_default",
                 "dir":   "/core/traces/routing_engine",
                 "primary": "fibmgrd_traces"},
    "wb_agent": {"shell": "ncp",
                 "dir":   "/core/traces/datapath",
                 "primary": "wb_agent"},
}
_TRACE_TARGET_NAMES = tuple(_TRACE_TARGETS.keys())

# Trace filenames seen in the wild include colons (session timestamps) and
# dots (rotation suffix), so the whitelist is [A-Za-z0-9._:-]+ (still no
# slashes, still no '..'):
_TRACE_NAME_RE = re.compile(r"^[A-Za-z0-9._:\-]+$")
_TRACE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._:\-]+$")
_TZ_OFFSET_RE = re.compile(r"^([+\-])(\d{2})(\d{2})$")
# stderr from a failed listing pipeline (``ls: cannot access ...: No such
# file or directory``, ``find: ...``); list lines always start with a date.
_TRACES_LIST_ERR_RE = re.compile(r"^(ls|find|awk|sort|grep|head):\s")


# Device-TZ cache. Populated lazily on the first trace-tool call that needs
# to convert a user-supplied UTC timestamp into the device's local wall-clock
# (traces print ``...+03:00``-style local time, not ``Z``). DST transitions
# during the MCP lifetime are rare; restart the server if they happen.
_DEVICE_TZ_CACHE: Dict[str, int] = {}   # key -> offset minutes east of UTC
_DEVICE_TZ_LOCK = threading.Lock()


def _get_device_tz_offset_min(
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    timeout: int,
) -> Tuple[Optional[int], Optional[str]]:
    """Fetch ``date +%z`` inside ``run start shell`` once per device and
    cache the offset. Returns (offset_minutes, error)."""
    key = device or host or ""
    with _DEVICE_TZ_LOCK:
        if key in _DEVICE_TZ_CACHE:
            return _DEVICE_TZ_CACHE[key], None

    try:
        result = run_once(
            transport_registry,
            device=device, host=host, user=user, password=password,
            command="date +%z",
            timeout=timeout,
            mode="shell_exec",
        )
    except Exception as exc:
        return None, f"could not probe device timezone: {exc}"

    offset: Optional[int] = None
    for line in (result.output or "").splitlines():
        m = _TZ_OFFSET_RE.match(line.strip())
        if m:
            sign = 1 if m.group(1) == "+" else -1
            offset = sign * (int(m.group(2)) * 60 + int(m.group(3)))
            break
    if offset is None:
        return None, (
            "could not parse device timezone from "
            f"{(result.output or '')[:64]!r}; expected ±HHMM."
        )

    with _DEVICE_TZ_LOCK:
        _DEVICE_TZ_CACHE[key] = offset
    return offset, None


def _convert_utc_bound_to_device_local(
    bound: str, *, offset_minutes: int, upper: bool,
) -> Optional[str]:
    """Parse a UTC ``since``/``until`` (same grammar as accounting) and render
    it as a device-local ``YYYY-MM-DDTHH:MM:SS`` string (seconds precision, no
    TZ suffix). Returns ``None`` on invalid input.
    """
    norm = normalize_accounting_ts(bound, upper=upper)
    if norm is None:
        return None
    try:
        dt = datetime.strptime(norm, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None
    local = dt + timedelta(minutes=offset_minutes)
    return local.strftime("%Y-%m-%dT%H:%M:%S")


def _resolve_trace_target(
    target: Optional[str],
    ncc: Optional[str],
    ncp: Optional[str],
    container: Optional[str],
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Resolve the ``target`` preset (or the free-form ncc/ncp/container args
    when ``target`` is ``None``) into ``(shell_entry, base_dir, primary_file,
    error)``. ``primary_file`` is ``None`` when no target was supplied.
    """
    if target is None:
        shell_entry, err = _build_shell_entry(ncc, ncp, container)
        if err:
            return None, None, None, err
        # NCPs don't have /core/traces/routing_engine (issue #81); root at
        # /core/traces so the per-subsystem dirs (datapath, dnos-agent, ...)
        # are reachable. NCC-side contexts keep the routing_engine default.
        base_dir = _TRACES_NCP_ROOT if ncp is not None else _TRACES_DEFAULT_DIR
        return shell_entry, base_dir, None, None

    meta = _TRACE_TARGETS.get(target)
    if meta is None:
        valid = ", ".join(_TRACE_TARGET_NAMES)
        return None, None, None, f"target must be one of: {valid}."

    if meta["shell"] == "ncc_default":
        if ncp is not None:
            return None, None, None, (
                f"target={target!r} runs on the NCC; do not pass ncp."
            )
        shell_entry, err = _build_shell_entry(ncc, None, container)
        if err:
            return None, None, None, err
        return shell_entry, meta["dir"], meta["primary"], None

    if meta["shell"] == "ncp":
        if ncc is not None or container is not None:
            return None, None, None, (
                f"target={target!r} runs on an NCP; pass ncp=<id>, "
                "do not pass ncc/container."
            )
        if ncp is None:
            return None, None, None, (
                f"target={target!r} requires ncp=<id> (e.g. '1')."
            )
        shell_entry, err = _build_shell_entry(None, ncp, None)
        if err:
            return None, None, None, err
        return shell_entry, meta["dir"], meta["primary"], None

    return None, None, None, (
        f"internal: unknown shell_context {meta['shell']!r}."
    )


def _split_trace_subpath(
    name: str, base_dir: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Fold the directory part of a subdir-relative ``name`` (as listed by
    the recursive ncp listing, e.g. ``datapath/wb_agent.bfd``) into
    ``base_dir``. Returns ``(base_dir, basename, error)``. Every path
    component must match the trace-name whitelist and must not be ``.`` /
    ``..`` — the name can only ever descend below ``base_dir``.
    """
    parts = name.strip().split("/")
    for p in parts:
        if p in ("", ".", "..") or not _TRACE_COMPONENT_RE.fullmatch(p):
            return None, None, (
                "name may include subdirectories relative to the trace root "
                "(e.g. 'datapath/wb_agent.bfd'); each path component must "
                "match [A-Za-z0-9._:-]+ and must not be '.' or '..'. "
                "Use list_traces to discover valid names."
            )
    return "/".join([base_dir] + parts[:-1]), parts[-1], None


def _build_trace_list(
    component: Optional[str],
    include_rotated: bool,
    max_entries: int,
    base_dir: str,
    recursive: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """Build the ``ls | awk | sort | grep | head`` pipeline for list_traces.

    Output format per line (after the pipeline):
    ``<YYYY-MM-DD HH:MM:SS +ZZZZ> <size> <name>`` — newest-first. The mtime
    is rendered in **device-local** time (no ``TZ=UTC`` prefix) and carries
    the offset so the timestamp matches what's printed inside the trace
    lines themselves and what's encoded into rotated ``.gz`` filenames.

    With ``recursive=True`` (non-preset ncp targets, issue #81) the lead
    stage is a ``find`` over ``base_dir`` instead of ``ls``, so per-subsystem
    subdirectories are walked and ``<name>`` becomes a base_dir-relative
    path like ``datapath/wb_agent.bfd`` — feed it back to ``get_trace``
    as-is.
    """
    if component is not None:
        if not isinstance(component, str) or not component.strip():
            return None, "component must be a non-empty string when provided."
        if not _TRACE_COMPONENT_RE.fullmatch(component.strip()):
            return None, "component must match [A-Za-z0-9._:-]+."
    if not isinstance(max_entries, int) or isinstance(max_entries, bool):
        return None, "max_entries must be an integer."
    if not (1 <= max_entries <= _TRACES_LIST_ABS_MAX):
        return None, f"max_entries must be in [1, {_TRACES_LIST_ABS_MAX}]."

    # Time-style emits three whitespace-separated tokens (date / time / tz),
    # so the size is in $5 (unchanged), the timestamp spans $6+$7+$8, and
    # the filename slides to $9. Everything after the awk hand-off is
    # whole-line text again, so the include / exclude greps below don't
    # care about column positions.
    if recursive:
        # find's %TS carries fractional seconds; the awk stage trims the
        # time token to HH:MM:SS so the line shape matches the ls variant.
        parts: List[str] = [
            f"find {shlex.quote(base_dir)}/ -mindepth 1 -maxdepth 2 -type f "
            "-printf '%TY-%Tm-%Td %TH:%TM:%TS %Tz %s %P\\n'",
            "awk '{ $2 = substr($2, 1, 8); print }'",
            "sort -r",
        ]
    else:
        parts = [
            f"ls -laL --time-style='+%Y-%m-%d %H:%M:%S %z' "
            f"{shlex.quote(base_dir)}/",
            "awk '$1 ~ /^-/ {printf \"%s %s %s %s %s\\n\", $6, $7, $8, $5, $9}'",
            "sort -r",
        ]
    if not include_rotated:
        parts.append("grep -v -F -- '.gz'")
    if component is not None:
        parts.append(f"grep -F -- {shlex.quote(component.strip())}")
    parts.append(f"head -n {max_entries}")
    return " | ".join(parts), None


_TRACE_LEVEL_NAMES = ("ERROR", "WARNING", "INFO", "DEBUG")


def _level_grep_pattern(level: str) -> str:
    """Translate a ``level`` enum value into the grep -E regex that picks
    the matching trace lines. Trace lines look like::

        2026-05-10T14:35:26.123 +03:00 [WARNING   ] [zebra_rnh.c:420:...] ...

    so the level token is bracketed and right-padded with spaces. Match
    ``\\[<LEVEL> *\\]`` to catch every padding width without false-positive
    bleed into a message body that happens to contain the word.
    """
    return f"\\[{level} *\\]"


def _validate_trace_filters(
    tail_lines: Optional[int],
    grep: Optional[str],
    grep_exclude: Optional[str],
    level: Optional[str],
) -> Optional[str]:
    """Shared input validation for the get_trace filter parameters."""
    if tail_lines is not None:
        if not isinstance(tail_lines, int) or isinstance(tail_lines, bool):
            return "tail_lines must be an integer or null."
        if not (1 <= tail_lines <= _TRACES_TAIL_MAX):
            return f"tail_lines must be in [1, {_TRACES_TAIL_MAX}]."
    if grep is not None:
        err = validate_grep_pattern(grep, "grep")
        if err:
            return err
    if grep_exclude is not None:
        err = validate_grep_pattern(grep_exclude, "grep_exclude")
        if err:
            return err
    if level is not None and level not in _TRACE_LEVEL_NAMES:
        return (
            "level must be one of "
            f"{', '.join(_TRACE_LEVEL_NAMES)} (case-sensitive)."
        )
    return None


def _build_line_filters(
    since_local: Optional[str],
    until_local: Optional[str],
    grep: Optional[str],
    grep_exclude: Optional[str],
    grep_ignore_case: bool,
    level: Optional[str],
) -> List[str]:
    """Build the per-line filter pipeline stages applied AFTER the
    ``cat`` / ``zcat`` reader. Order: time-window awk → level grep →
    user grep → user grep_exclude. Returns a list of shell-safe
    pipeline segments (no leading pipe).
    """
    parts: List[str] = []

    if since_local is not None or until_local is not None:
        awk_prog = (
            '{ t = substr($1, 1, 19); '
            'if ((since=="" || t>=since) && (until=="" || t<=until)) print }'
        )
        parts.append(
            f"awk -v since={shlex.quote(since_local or '')} "
            f"-v until={shlex.quote(until_local or '')} "
            f"{shlex.quote(awk_prog)}"
        )

    if level is not None:
        parts.append(
            f"grep -E -- {shlex.quote(_level_grep_pattern(level))}"
        )

    grep_flags_i = " -i" if grep_ignore_case else ""
    if grep is not None:
        parts.append(f"grep -E{grep_flags_i} -- {shlex.quote(grep)}")
    if grep_exclude is not None:
        parts.append(f"grep -v -E{grep_flags_i} -- {shlex.quote(grep_exclude)}")

    return parts


def _build_trace_read_single(
    name: str,
    tail_lines: Optional[int],
    since_local: Optional[str],
    until_local: Optional[str],
    grep: Optional[str],
    grep_exclude: Optional[str],
    grep_ignore_case: bool,
    level: Optional[str],
    count_only: bool,
    base_dir: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Single-file read pipeline (``live_only=True`` or an explicit
    ``*.gz`` ``name``).

    Auto-picks ``cat`` vs ``zcat`` based on the ``.gz`` suffix, applies
    the shared line-level filters (time / level / user grep), and either
    tails to ``tail_lines`` and caps at ``_TRACES_MAX_BYTES`` bytes
    (default), or — when ``count_only=True`` — emits a single
    ``<filename> <count>`` summary line instead of the trace content.
    """
    if not isinstance(name, str) or not name.strip():
        return None, "name must be a non-empty string."
    n = name.strip()
    if not _TRACE_NAME_RE.fullmatch(n):
        return None, (
            "name must match [A-Za-z0-9._:-]+ (no slashes, no '..'). "
            "Use list_traces to discover valid filenames."
        )

    err = _validate_trace_filters(tail_lines, grep, grep_exclude, level)
    if err:
        return None, err

    full_path = f"{base_dir}/{n}"
    qp = shlex.quote(full_path)
    reader = "zcat" if n.endswith(".gz") else "cat"

    line_filters = _build_line_filters(
        since_local, until_local, grep, grep_exclude, grep_ignore_case, level,
    )

    if count_only:
        # Emit one ``<basename> <count>`` line. wc -l is the natural fit
        # for "matches per archive"; quoted name keeps the basename
        # intact even when it carries colons (rotated files use them).
        inner = " | ".join([f"{reader} {qp}"] + line_filters + ["wc -l"])
        return f"printf '%s ' {shlex.quote(n)}; {inner}", None

    parts: List[str] = [f"{reader} {qp}"] + line_filters
    if tail_lines is not None:
        parts.append(f"tail -n {tail_lines}")
    parts.append(f"head -c {_TRACES_MAX_BYTES}")
    return " | ".join(parts), None


def _build_trace_read_multi(
    name: str,
    tail_lines: Optional[int],
    since_local: Optional[str],
    until_local: Optional[str],
    grep: Optional[str],
    grep_exclude: Optional[str],
    grep_ignore_case: bool,
    level: Optional[str],
    count_only: bool,
    base_dir: str,
) -> Tuple[Optional[str], Optional[str]]:
    """All-archives read pipeline. Concatenates every existing file
    matching ``<base_dir>/<name>`` (live) and ``<base_dir>/<name>-*.gz``
    (rotated) in chronological order — rotated archives oldest-first
    (their suffix is the rotation timestamp and sorts naturally), then
    the live file last. Each ``.gz`` is ``zcat``-decoded inline.

    The same line-level filters apply across every file, then a single
    ``tail -n`` + ``head -c`` clamp the response. With ``count_only=True``
    each file is reported on its own ``<basename> <count>`` line so the
    caller can triage which archive carries the volume before pulling
    content.
    """
    if not isinstance(name, str) or not name.strip():
        return None, "name must be a non-empty string."
    n = name.strip()
    if not _TRACE_NAME_RE.fullmatch(n):
        return None, (
            "name must match [A-Za-z0-9._:-]+ (no slashes, no '..'). "
            "Use list_traces to discover valid filenames."
        )
    if n.endswith(".gz"):
        return None, (
            "all-archives mode requires a base name (e.g. 'bgpd_traces'); "
            "to read one specific rotated archive, set live_only=true and "
            "pass its full <name>-<ts>.gz filename."
        )

    err = _validate_trace_filters(tail_lines, grep, grep_exclude, level)
    if err:
        return None, err

    qb = shlex.quote(base_dir)
    qn = shlex.quote(n)
    line_filters = _build_line_filters(
        since_local, until_local, grep, grep_exclude, grep_ignore_case, level,
    )
    inner_pipe = " | ".join(line_filters)

    # Enumerate rotated archives via ``find`` so the empty-glob case is
    # handled cleanly (an unmatched bash glob would expand to itself and
    # cause spurious 'No such file' errors). Sort by basename — the
    # filename suffix is the rotation timestamp in ``YYYYMMDD_HH:MM:SS``
    # form and sorts lexicographically into chronological order. The
    # live file (no suffix) is appended last because it carries the
    # newest content.
    #
    # When ``since_local`` is set we additionally skip whole archives
    # whose suffix is < since: that suffix IS the rotation time, i.e.
    # the timestamp on the file's last line, so anything older means
    # 100% of the archive's content predates the window and ``zcat``-ing
    # it would just be device-CPU waste. ``until`` cannot be safely
    # short-circuited the same way — an archive's first-line timestamp
    # is approximately the previous rotation's suffix, which the
    # filename doesn't encode. (Could be added by reading the first
    # line of each archive, but that's extra round-trips for a marginal
    # win.)
    suffix_filter = ""
    if since_local is not None:
        # since_local format is ``YYYY-MM-DDTHH:MM:SS``; archive suffix
        # is ``YYYYMMDD_HH:MM:SS``. Parse the suffix in awk and rebuild
        # an ISO-8601 string so the lex-compare against since_local is
        # apples-to-apples.
        prefix = n + "-"
        suffix_filter = (
            " | awk -v base="
            + shlex.quote(prefix)
            + " -v since="
            + shlex.quote(since_local)
            + " '{ ts = substr($0, length(base)+1, 17); "
            "iso = substr(ts,1,4)\"-\"substr(ts,5,2)\"-\"substr(ts,7,2)"
            "\"T\"substr(ts,10,8); "
            "if (iso >= since) print }'"
        )

    archive_lister = (
        f"find {qb} -maxdepth 1 -type f -name {shlex.quote(n + '-*.gz')} "
        f"-printf '%f\\n' 2>/dev/null | sort{suffix_filter}"
    )

    if count_only:
        # Per-archive counts. Each ``<filename> <count>`` line is at
        # most ~120 bytes, so the cumulative output is bounded by the
        # number of archives — no head -c cap needed for safety.
        archive_loop = (
            f"{archive_lister} | while IFS= read -r f; do "
            f"  printf '%s ' \"$f\"; "
            f"  zcat -- {qb}/\"$f\""
            + (f" | {inner_pipe}" if inner_pipe else "")
            + " | wc -l; "
            f"done"
        )
        live_block = (
            f"if [ -f {qb}/{qn} ]; then "
            f"  printf '%s ' {qn}; "
            f"  cat -- {qb}/{qn}"
            + (f" | {inner_pipe}" if inner_pipe else "")
            + " | wc -l; "
            f"fi"
        )
        return f"{{ {archive_loop}; {live_block}; }}", None

    archive_loop = (
        f"{archive_lister} | while IFS= read -r f; do "
        f"  zcat -- {qb}/\"$f\"; "
        f"done"
    )
    live_block = f"[ -f {qb}/{qn} ] && cat -- {qb}/{qn}"
    concat_block = f"{{ {archive_loop}; {live_block}; }}"

    parts: List[str] = [concat_block]
    parts.extend(line_filters)
    if tail_lines is not None:
        parts.append(f"tail -n {tail_lines}")
    parts.append(f"head -c {_TRACES_MAX_BYTES}")
    return " | ".join(parts), None


@requires(CAP_LOGS)
def list_traces(
    target: Optional[Literal["bgp", "isis", "zebra", "fibmgr", "wb_agent"]] = None,
    component: Optional[str] = None,
    include_rotated: bool = True,
    max_entries: int = _TRACES_LIST_MAX_ENTRIES,
    ncc: Optional[str] = None,
    ncp: Optional[str] = None,
    container: Optional[str] = None,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """List routing-engine / NCP trace logs (files) on the device — e.g.
    ``bgpd_traces``, ``isisd_traces``, ``zebra_traces``, ``fibmgrd_traces``,
    ``wb_agent_traces`` and their rotated ``*.gz`` siblings. Prefer this over
    ``show file ncc <id> traces list``.

    The quickest way to call it is with a ``target`` preset; the preset
    fixes the shell entry and the on-disk directory so you don't have to
    know the DNOS layout.

    Preset mapping (``target`` → shell entry + directory):

        bgp       run start shell              /core/traces/routing_engine/
        isis      run start shell              /core/traces/routing_engine/
        zebra     run start shell              /core/traces/routing_engine/
        fibmgr    run start shell              /core/traces/routing_engine/
        wb_agent  run start shell ncp <ncp>    /core/traces/datapath/

    The four NCC presets run on the active NCC by default; pass ``ncc=``
    to pin a specific NCC. ``wb_agent`` requires ``ncp=<id>``.

    Examples:

        list_traces(target="bgp", device="cl")
        list_traces(target="wb_agent", device="cl", ncp="1")
        list_traces(device="cl", component="rib-manager")  # no preset
        list_traces(device="cl", ncp="7")  # all NCP trace dirs, recursive

    When no preset is given, ``list_traces`` uses the free-form
    ``ncc`` / ``ncp`` / ``container`` shell-entry args. NCC-side contexts
    (default / ``ncc`` / ``container``) list the default
    ``/core/traces/routing_engine/`` directory. ``ncp`` contexts have no
    routing_engine dir — their traces live in per-subsystem dirs under
    ``/core/traces/`` (``datapath``, ``dnos-agent``, ``gi-agent``,
    ``gi-manager``, ``node-manager``, ...) — so the listing walks
    ``/core/traces/`` recursively and emits relative paths like
    ``datapath/wb_agent.bfd``; feed those to ``get_trace`` as-is.

    The tool runs (inside ``run start shell ...``):

        ls -laL --time-style='+%Y-%m-%d %H:%M:%S %z' <base_dir>/
          | awk '$1 ~ /^-/ {print $6, $7, $8, $5, $9}'
          | sort -r
          [| grep -v -F -- '.gz']
          [| grep -F -- <component>]
          | head -n <max_entries>

    (non-preset ``ncp`` swaps the ``ls | awk`` lead for
    ``find /core/traces/ -mindepth 1 -maxdepth 2 -type f -printf ...``
    over the subsystem dirs; the line shape is identical.)

    Each stdout line is
    ``<YYYY-MM-DD HH:MM:SS +ZZZZ> <size-bytes> <filename>``, sorted
    newest-first. The mtime is in **device-local time** (carries the
    ``+ZZZZ`` offset) so it matches the timestamps printed inside the
    trace lines and the ``YYYYMMDD_HH:MM:SS`` timestamps encoded into
    rotated ``.gz`` filenames — no mental TZ conversion needed.

    Typical flow: call ``list_traces`` to find the exact filename, then
    ``get_trace(name=...)`` to read it.

    Args:
        target: Preset for a well-known log — one of ``"bgp"``,
            ``"isis"``, ``"zebra"``, ``"fibmgr"``, ``"wb_agent"``. Fixes
            the shell entry and base directory. Leave ``None`` to use
            raw ``ncc`` / ``ncp`` / ``container``.
        component: Extra fixed-string substring filter on filenames
            (``grep -F``, case-sensitive). e.g. ``"bgpd"`` matches
            ``bgpd_traces`` and ``bgpd_traces-20260420_22:07:56.gz``.
        include_rotated: If ``False``, hide rotated ``*.gz`` files
            (default ``True``).
        max_entries: Cap the number of rows returned after filtering
            (default 200, max 5000).
        ncc: Select NCC: ``"0"``, ``"1"``, or ``"active"``. Rejected for
            ``target="wb_agent"``. Mutually exclusive with ``ncp``.
        ncp: Select NCP: ``"0"``..``"191"`` or ``"bfd-master"``.
            REQUIRED for ``target="wb_agent"``; rejected for NCC-side
            presets. Mutually exclusive with ``ncc`` and ``container``.
        container: Enter a named container on the selected NCC (e.g.
            ``"routing-engine"``, ``"node-manager"``). Ignored / rejected
            when ``target`` is set.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot); also used to authenticate
            the ``run start shell`` challenge.
        timeout: Per-step timeout seconds.
    """
    shell_entry, base_dir, _primary, err = _resolve_trace_target(
        target, ncc, ncp, container,
    )
    if err:
        return error_response(
            err, device=device, host=host,
            next_action=LIST_TRACES_NEXT_ACTION,
        )
    linux_cmd, err = _build_trace_list(
        component, include_rotated, max_entries, base_dir,
        recursive=(target is None and ncp is not None),
    )
    if err:
        return error_response(
            err, device=device, host=host,
            next_action=LIST_TRACES_NEXT_ACTION,
        )
    response = run_linux_on_device(
        "list_traces", device, host, user, password,
        linux_cmd, timeout, LIST_TRACES_NEXT_ACTION,
        shell_entry=shell_entry,
    )
    # A failed listing (e.g. the trace dir doesn't exist in this context)
    # surfaces as ls/find stderr in the transcript, which the DNOS error
    # patterns don't catch — promote it to a real error so the exit code
    # is non-zero (issue #81).
    if response.get("status") == "ok":
        bad = [
            ln.strip() for ln in (response.get("stdout") or "").splitlines()
            if _TRACES_LIST_ERR_RE.match(ln.strip())
        ]
        if bad:
            response["status"] = "error"
            response.setdefault("errors", []).extend(bad[-5:])
            response.setdefault("next_actions", []).append(
                LIST_TRACES_NEXT_ACTION,
            )
    return response


@requires(CAP_LOGS)
def get_trace(
    target: Optional[Literal["bgp", "isis", "zebra", "fibmgr", "wb_agent"]] = None,
    name: Optional[str] = None,
    tail_lines: Optional[int] = _TRACES_TAIL_DEFAULT,
    since: Optional[str] = None,
    until: Optional[str] = None,
    grep: Optional[str] = None,
    grep_exclude: Optional[str] = None,
    grep_ignore_case: bool = False,
    level: Optional[Literal["ERROR", "WARNING", "INFO", "DEBUG"]] = None,
    live_only: bool = False,
    count_only: bool = False,
    ncc: Optional[str] = None,
    ncp: Optional[str] = None,
    container: Optional[str] = None,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Read one or more routing-engine / NCP trace logs (bgp, isis, zebra,
    fibmgr, wb_agent, ...) with UTC time-window + regex grep + level + tail
    filters. Prefer this over ``show file ncc <id> traces <name>``.

    The quickest way to call it is with a ``target`` preset; the preset
    fixes the shell entry, the on-disk directory, AND the default
    base filename (the current, non-rotated trace file).

    Preset mapping (``target`` → shell entry + default base file):

        bgp       run start shell              /core/traces/routing_engine/bgpd_traces
        isis      run start shell              /core/traces/routing_engine/isisd_traces
        zebra     run start shell              /core/traces/routing_engine/zebra_traces
        fibmgr    run start shell              /core/traces/routing_engine/fibmgrd_traces
        wb_agent  run start shell ncp <ncp>    /core/traces/datapath/wb_agent

    **Default mode reads every archive.** Busy daemons (``bgpd_traces``,
    ``rib-manager_traces``) rotate quickly — anything older than ~1 minute
    is already in a ``<base>-<YYYYMMDD_HH:MM:SS>.gz`` archive next to the
    live file. By default this tool concatenates every existing
    ``<base>-*.gz`` (oldest-first, ``zcat``-decoded) followed by the
    live ``<base>`` and runs the shared filter pipeline across the lot.
    Set ``live_only=True`` to read just the live file (cheap when you
    only care about right-now), or pass ``name="<base>-<ts>.gz"``
    (surfaced by ``list_traces``) to read one specific archive.

    Examples:

        # Default: all bgp archives + live, last 20 matching ERROR lines.
        get_trace(target="bgp", device="cl", level="ERROR", tail_lines=20)

        # Just the live file (skip archive scan).
        get_trace(target="bgp", device="cl", live_only=True, tail_lines=20)

        # One specific rotated archive.
        get_trace(target="bgp", device="cl",
                  name="bgpd_traces-20260420_22:07:56.gz", tail_lines=50)

        # NCP-side trace; same all-archives default.
        get_trace(target="wb_agent", device="cl", ncp="1", tail_lines=20)

        # No preset; explicit base name.
        get_trace(device="cl", name="rib-manager_traces", tail_lines=20)

        # NCP-side, no preset: subdir-relative name from list_traces.
        get_trace(device="cl", ncp="7", name="datapath/wb_agent.bfd")

        # Triage: per-archive match counts, no content. Useful before
        # pulling 90 KB of repeated WARNINGs.
        get_trace(target="bgp", device="cl", grep="leaked", count_only=True)

    Pipeline (default all-archives, inside ``run start shell ...``):

        { find <dir> -maxdepth 1 -type f -name '<base>-*.gz' -printf '%f\\n'
            | sort
            | while read f; do zcat -- <dir>/"$f"; done;
          [ -f <dir>/<base> ] && cat -- <dir>/<base>;
        }
          [| awk -v since=<local> -v until=<local> '{ t = substr($1,1,19);
               if ((since=="" || t>=since) && (until=="" || t<=until)) print }']
          [| grep -E -- '\\[<LEVEL> *\\]']
          [| grep -E [-i] -- <grep>]
          [| grep -v -E [-i] -- <grep_exclude>]
          [| tail -n <tail_lines>]
          | head -c 500000

    With ``live_only=True`` (or an explicit ``*.gz`` ``name``) the lead is
    just ``(cat|zcat) <dir>/<name>`` and the rest of the filters are
    identical. ``zcat`` is auto-selected for names ending in ``.gz``.

    **Regex grep.** ``grep`` / ``grep_exclude`` are extended regular
    expressions (``grep -E``) so ``|`` (alternation), ``.`` (any char),
    ``^`` / ``$`` (anchors), ``[...]`` (class) etc. all work. Escape
    them with ``\\`` if you mean the literal character. This is a
    breaking change from older builds where the filters were
    fixed-string (``grep -F``).

    **Level filter.** Trace lines look like ``... [WARNING   ] [...]``,
    so ``level="ERROR"`` injects an extra ``grep -E -- '\\[ERROR *\\]'``
    stage. Compose freely with ``grep`` (both must match a line for it
    to come through).

    **count_only mode.** Skips content entirely; emits one
    ``<filename> <count>`` line per file (just one in single-file
    mode, one per archive in default mode). Use to triage how many
    matches each archive carries before deciding what to pull.

    UTC time bounds — ``since`` / ``until`` are ISO-8601 UTC
    (``YYYY-MM-DDTHH:MM:SS[.sss]Z``) or relative (``30s`` / ``10m`` / ``2h``
    / ``1d``). Trace logs print device-LOCAL timestamps (e.g.
    ``2026-04-20T22:07:56.304+03:00``), so the MCP probes the device's TZ
    offset once per device (``date +%z``, cached in memory) and converts
    your UTC bounds to device-local seconds-precision before filtering.
    You always pass UTC. ``since`` ALSO short-circuits whole archives:
    in all-archives mode, any ``<base>-<ts>.gz`` whose suffix-encoded
    rotation time is < ``since`` is skipped before ``zcat`` ever runs
    (the suffix IS the file's last-line time, so the whole file is
    older than the window). ``until`` only prunes at the line level —
    an archive's first-line timestamp isn't encoded in the filename
    and we don't open files just to peek at it.

    The 500 KB hard cap guarantees a bounded response (content modes
    only — ``count_only`` output is naturally small). If the output is
    anywhere near the cap a ``Output truncated at 500000 bytes`` warning
    is added — narrow with ``live_only`` / ``tail_lines`` / ``since`` /
    ``until`` / ``grep`` / ``level``.

    Args:
        target: Preset for a well-known log — one of ``"bgp"``,
            ``"isis"``, ``"zebra"``, ``"fibmgr"``, ``"wb_agent"``. Fixes
            the shell entry, base directory, and default filename.
        name: Basename within the resolved directory. REQUIRED when
            ``target`` is not set; optional (defaults to the preset's
            current file) when ``target`` is set. Must match
            ``[A-Za-z0-9._:-]+`` per path component — a subdir-relative
            path as listed by the recursive ncp listing (e.g.
            ``datapath/wb_agent.bfd``) is accepted; ``..`` is not. Pass a
            ``<base>-<ts>.gz`` to read just that one archive (engages
            single-file mode automatically).
        tail_lines: Keep the last N matching lines after other filters
            (default 500, max 50000, ``None`` = unlimited up to byte cap).
            Ignored in ``count_only`` mode.
        since: Lower time bound, inclusive. UTC.
        until: Upper time bound, inclusive. UTC.
        grep: Regex include filter (``grep -E``). Pass ``|``-separated
            alternatives to match multiple keywords in one call.
        grep_exclude: Regex exclude filter (``grep -v -E``).
        grep_ignore_case: Apply ``-i`` to both grep filters.
        level: Severity filter — ``"ERROR"`` / ``"WARNING"`` / ``"INFO"``
            / ``"DEBUG"``. Adds ``grep -E '\\[<LEVEL> *\\]'`` to the
            pipeline; case-sensitive (matches the bracketed token DNOS
            traces emit).
        live_only: When ``True``, read ONLY the live (uncompressed) file
            and skip rotated ``.gz`` archives. Default ``False`` reads
            every archive plus the live file. Auto-engaged when
            ``name`` ends in ``.gz``.
        count_only: When ``True``, emit one ``<filename> <count>`` line
            per file instead of trace content. Default ``False``.
        ncc: Select NCC: ``"0"``, ``"1"``, or ``"active"``. Rejected for
            ``target="wb_agent"``. Mutually exclusive with ``ncp``.
        ncp: Select NCP: ``"0"``..``"191"`` or ``"bfd-master"``.
            REQUIRED for ``target="wb_agent"``; rejected for NCC-side
            presets. Mutually exclusive with ``ncc`` and ``container``.
        container: Enter a named container on the selected NCC. Ignored /
            rejected when ``target`` is set.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot); also used to authenticate
            the ``run start shell`` challenge.
        timeout: Per-step timeout seconds.
    """
    shell_entry, base_dir, primary, err = _resolve_trace_target(
        target, ncc, ncp, container,
    )
    if err:
        return error_response(
            err, device=device, host=host,
            next_action=GET_TRACE_NEXT_ACTION,
        )

    if name is None:
        if primary is None:
            return error_response(
                "name is required when target is not set.",
                device=device, host=host,
                next_action=GET_TRACE_NEXT_ACTION,
            )
        name = primary

    # The recursive ncp listing emits base_dir-relative paths like
    # ``datapath/wb_agent.bfd`` (issue #81) — fold the directory part into
    # base_dir so the single/multi readers keep working on a bare basename.
    if "/" in name:
        base_dir, name, err = _split_trace_subpath(name, base_dir)
        if err:
            return error_response(
                err, device=device, host=host,
                next_action=GET_TRACE_NEXT_ACTION,
            )

    since_local: Optional[str] = None
    until_local: Optional[str] = None
    if since is not None or until is not None:
        offset, terr = _get_device_tz_offset_min(
            device, host, user, password, timeout,
        )
        if terr is not None or offset is None:
            return error_response(
                terr or "could not determine device timezone.",
                device=device, host=host,
                next_action=GET_TRACE_NEXT_ACTION,
            )
        if since is not None:
            since_local = _convert_utc_bound_to_device_local(
                since, offset_minutes=offset, upper=False,
            )
            if since_local is None:
                return error_response(
                    "since must be ISO-8601 UTC "
                    "('YYYY-MM-DDTHH:MM:SS[.sss]Z') or relative "
                    "('30s' / '10m' / '2h' / '1d').",
                    device=device, host=host,
                    next_action=GET_TRACE_NEXT_ACTION,
                )
        if until is not None:
            until_local = _convert_utc_bound_to_device_local(
                until, offset_minutes=offset, upper=True,
            )
            if until_local is None:
                return error_response(
                    "until must be ISO-8601 UTC "
                    "('YYYY-MM-DDTHH:MM:SS[.sss]Z') or relative "
                    "('30s' / '10m' / '2h' / '1d').",
                    device=device, host=host,
                    next_action=GET_TRACE_NEXT_ACTION,
                )

    # Single-file mode is the right fit when:
    #   - the caller asked for it explicitly (live_only=True), or
    #   - the caller named a specific rotated archive (.gz suffix) — the
    #     all-archives glob would just rediscover the same file.
    use_single_file = live_only or name.endswith(".gz")
    if use_single_file:
        linux_cmd, err = _build_trace_read_single(
            name, tail_lines, since_local, until_local,
            grep, grep_exclude, grep_ignore_case, level, count_only,
            base_dir,
        )
    else:
        linux_cmd, err = _build_trace_read_multi(
            name, tail_lines, since_local, until_local,
            grep, grep_exclude, grep_ignore_case, level, count_only,
            base_dir,
        )
    if err:
        return error_response(
            err, device=device, host=host,
            next_action=GET_TRACE_NEXT_ACTION,
        )

    response = run_linux_on_device(
        "get_trace", device, host, user, password,
        linux_cmd, timeout, GET_TRACE_NEXT_ACTION,
        shell_entry=shell_entry,
    )
    if not count_only:
        stdout = response.get("stdout") or ""
        if len(stdout.encode("utf-8", errors="replace")) >= _TRACES_MAX_BYTES:
            response.setdefault("warnings", []).append(
                f"Output truncated at {_TRACES_MAX_BYTES} bytes; "
                "narrow with live_only / tail_lines / since / until / "
                "grep / level."
            )
    return response


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(list_traces)
    mcp.tool()(get_trace)
