"""Log-read MCP tools (CLI accounting / NETCONF accounting / system events).

Three tools that all share the same underlying pipeline shape:

    <path-resolve preamble; sets $P or errors out>
    ; awk -v since=... -v until=... '{...$<ts_field>...}' "$P"   # or: cat -- "$P"
      | grep -F [-i] -- <grep>
      | grep -v -F [-i] -- <grep_exclude>
      | tail -n <tail_lines>
      | head -c 500000

Each tool just supplies its own candidate list of on-disk paths and
which whitespace-separated awk field carries the ISO-8601 timestamp.
Everything else (time-window normalization, grep validation, byte cap)
is common — that's what ``_build_log_read`` and ``_run_log_tool`` are
for. The pipeline is built server-side via ``shlex.quote`` so user
input never escapes the shell.
"""

from __future__ import annotations

import shlex
from typing import Any, Dict, List, Optional, Sequence, Tuple

from qactl.dnctl.cli.core.envelope import error_response
from qactl.dnctl.cli.core.errors import (
    GET_ACCT_NEXT_ACTION,
    GET_NETCONF_ACCT_NEXT_ACTION,
    GET_SYSTEM_EVENTS_NEXT_ACTION,
)
from qactl.dnctl.cli.core.log_filters import normalize_accounting_ts, validate_grep_pattern
from qactl.dnctl.cli.core.session import DEFAULT_CMD_TIMEOUT, DEFAULT_PASSWORD, DEFAULT_USER
from qactl.dnctl.cli.core.shell_exec import run_linux_on_device
from qactl.dnctl.cli.vendors import CAP_LOGS, requires


_ACCOUNTING_MAX_BYTES = 500_000
_ACCOUNTING_TAIL_DEFAULT = 500
_ACCOUNTING_TAIL_MAX = 50_000

# Stable marker the path-resolve preamble prints when no candidate log
# file exists. It goes to the shell's stderr (merged into the PTY output),
# so the run looks "successful" with empty stdout unless we detect it —
# otherwise an agent reads "no log file" as "no activity".
_LOG_NOT_FOUND_MARKER = "log file not found; tried:"


# On-disk log paths are candidate lists — tried in order at runtime, first
# existing wins. This lets us follow DNOS layout changes across versions
# without client-visible breakage. Primary entries are the paths observed
# on current DNOS (lab: cl, build 26.3 — accounting + netconf accounting +
# system-events all moved into the ``northbound-services`` container, and
# show up on the default ``run start shell`` (routing_engine) as the
# host-side bind mount under ``/core/logs/northbound_services/``); the
# ``/var/log/dn/routing_engine/...`` entries match build 26.2 (lab:
# ariel-cl), and ``/var/log/dn/accounting/...`` is the older still
# layout. Keeping the default shell entry across all builds means we
# don't pay the cost of a second password challenge to enter the nb
# container — the bind mount is visible from where we already are.
_CLI_ACCOUNTING_PATHS: Sequence[str] = (
    "/core/logs/northbound_services/accounting.log",
    "/var/log/dn/routing_engine/accounting.log",
    "/core/logs/routing_engine/accounting.log",
    "/var/log/dn/accounting/accounting.log",
)
_NETCONF_ACCOUNTING_PATHS: Sequence[str] = (
    "/core/logs/northbound_services/accounting_netconf.log",
    "/var/log/dn/routing_engine/accounting_netconf.log",
    "/core/logs/routing_engine/accounting_netconf.log",
    "/var/log/dn/accounting/accounting_netconf.log",
)
_SYSTEM_EVENTS_PATHS: Sequence[str] = (
    "/core/logs/northbound_services/system-events.log",
    "/var/log/dn/system-events.log",
)


def _build_log_read(
    paths: Sequence[str],
    tail_lines: Optional[int],
    since: Optional[str],
    until: Optional[str],
    grep: Optional[str],
    grep_exclude: Optional[str],
    grep_ignore_case: bool,
    ts_field: int = 1,
) -> Tuple[Optional[str], Optional[str]]:
    """Build the Linux one-liner run inside ``run start shell`` for a log-read
    tool (CLI accounting, NETCONF accounting, system-events). Returns
    ``(command, None)`` on success or ``(None, error_message)`` on invalid
    input.

    ``paths`` is a candidate list of absolute on-disk paths, tried in order
    at execution time — the first one that exists on the active NCC is
    used. Accepting multiple candidates lets us follow DNOS layout changes
    across versions (e.g. the old ``/var/log/dn/accounting/`` vs the newer
    ``/var/log/dn/routing_engine/`` location for accounting logs). If no
    candidate exists the one-liner prints a clear ``ERR: ... not found``
    line and exits non-zero.

    ``ts_field`` selects which whitespace-separated awk field carries the
    ISO-8601 timestamp used for ``since``/``until`` filtering. Defaults to
    1 (accounting logs — timestamp is the first field); system-events.log
    prepends a syslog facility token so it uses ``ts_field=2``.

    Pipeline shape (every stage conditional except the final hard cap):

        <path-resolve preamble, sets $P or errors out>
        ; awk -v since=... -v until=... '{...$<ts_field>...}' -- "$P"
           # or: cat -- "$P"
          | grep -F [-i] -- <grep>
          | grep -v -F [-i] -- <grep_exclude>
          | tail -n <tail_lines>
          | head -c 500000
    """
    if not paths:
        return None, "paths must contain at least one candidate path."
    if not isinstance(ts_field, int) or isinstance(ts_field, bool) or ts_field < 1:
        return None, "ts_field must be a positive integer."

    if tail_lines is not None:
        if not isinstance(tail_lines, int) or isinstance(tail_lines, bool):
            return None, "tail_lines must be an integer or null."
        if not (1 <= tail_lines <= _ACCOUNTING_TAIL_MAX):
            return None, (
                f"tail_lines must be in [1, {_ACCOUNTING_TAIL_MAX}]."
            )

    since_norm: Optional[str] = None
    until_norm: Optional[str] = None
    if since is not None:
        since_norm = normalize_accounting_ts(since, upper=False)
        if since_norm is None:
            return None, (
                "since must be ISO-8601 UTC "
                "('YYYY-MM-DDTHH:MM:SS[.sss]Z') or relative "
                "('30s' / '10m' / '2h' / '1d')."
            )
    if until is not None:
        until_norm = normalize_accounting_ts(until, upper=True)
        if until_norm is None:
            return None, (
                "until must be ISO-8601 UTC "
                "('YYYY-MM-DDTHH:MM:SS[.sss]Z') or relative "
                "('30s' / '10m' / '2h' / '1d')."
            )

    if grep is not None:
        err = validate_grep_pattern(grep, "grep")
        if err:
            return None, err
    if grep_exclude is not None:
        err = validate_grep_pattern(grep_exclude, "grep_exclude")
        if err:
            return None, err

    # Shell preamble: pick the first existing candidate path into $P, or fail
    # loudly. ``echo "$P"`` into stdout on success is deliberately omitted —
    # we don't want the filename polluting the tool's output body.
    quoted_paths = " ".join(shlex.quote(p) for p in paths)
    joined_paths_for_msg = ", ".join(paths)
    preamble = (
        f"P=; for f in {quoted_paths}; do "
        f'[ -f "$f" ] && P=$f && break; '
        f"done; "
        f'[ -z "$P" ] && {{ '
        f"echo {shlex.quote('ERR: log file not found; tried: ' + joined_paths_for_msg)} >&2; "
        f"exit 2; "
        f"}}"
    )

    parts: List[str] = []

    if since_norm is not None or until_norm is not None:
        awk_prog = (
            '{ if ((since=="" || $' + str(ts_field) + '>=since) && '
            '(until=="" || $' + str(ts_field) + '<=until)) print }'
        )
        # Note: no ``--`` separator before "$P" — gawk (which ships on DNOS)
        # does not honour POSIX's ``--`` end-of-options and would treat it
        # as a literal input filename, causing:
        #   awk: fatal: cannot open file `--' for reading
        # All candidate paths start with ``/`` so there's no option-arg
        # ambiguity to guard against.
        parts.append(
            f"awk -v since={shlex.quote(since_norm or '')} "
            f"-v until={shlex.quote(until_norm or '')} "
            f'{shlex.quote(awk_prog)} "$P"'
        )
    else:
        parts.append('cat -- "$P"')

    grep_flags = "-F" + (" -i" if grep_ignore_case else "")
    if grep is not None:
        parts.append(f"grep {grep_flags} -- {shlex.quote(grep)}")
    if grep_exclude is not None:
        parts.append(f"grep -v {grep_flags} -- {shlex.quote(grep_exclude)}")

    if tail_lines is not None:
        parts.append(f"tail -n {tail_lines}")

    parts.append(f"head -c {_ACCOUNTING_MAX_BYTES}")
    return preamble + "; " + " | ".join(parts), None


def _run_log_tool(
    tool_name: str,
    paths: Sequence[str],
    next_action: str,
    tail_lines: Optional[int],
    since: Optional[str],
    until: Optional[str],
    grep: Optional[str],
    grep_exclude: Optional[str],
    grep_ignore_case: bool,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    timeout: int,
    ts_field: int = 1,
) -> Dict[str, Any]:
    linux_cmd, err = _build_log_read(
        paths, tail_lines, since, until, grep, grep_exclude, grep_ignore_case,
        ts_field=ts_field,
    )
    if err:
        return error_response(
            err, device=device, host=host, next_action=next_action,
        )
    response = run_linux_on_device(
        tool_name, device, host, user, password,
        linux_cmd, timeout, next_action,
    )
    # No candidate path existed on the box: the preamble printed the
    # not-found marker and exited 2. detect_error doesn't know that shape,
    # so the envelope would otherwise stay "ok" with empty stdout — an
    # agent reads that as "no activity" rather than "couldn't read the
    # log". Escalate to an error.
    if response.get("status") == "ok" and _LOG_NOT_FOUND_MARKER in (response.get("stdout") or ""):
        response["status"] = "error"
        response.setdefault("errors", []).append(
            "log file not found on the device — none of the candidate "
            "paths exist (the on-disk layout may have shifted)."
        )
        response.setdefault("next_actions", []).append(next_action)
    # head -c caps raw output at _ACCOUNTING_MAX_BYTES. send_shell_exec strips
    # the echo + trailing shell prompt from stdout, so the body byte count we
    # see here is at most ~N bytes off the cap. If we're anywhere near the
    # cap, warn the caller so they narrow the filters rather than inferring
    # truth from a truncated tail.
    stdout = response.get("stdout") or ""
    if len(stdout.encode("utf-8", errors="replace")) >= _ACCOUNTING_MAX_BYTES:
        response.setdefault("warnings", []).append(
            f"Output truncated at {_ACCOUNTING_MAX_BYTES} bytes; "
            "narrow with tail_lines / since / until / grep."
        )
    return response


@requires(CAP_LOGS)
def get_accounting(
    tail_lines: Optional[int] = _ACCOUNTING_TAIL_DEFAULT,
    since: Optional[str] = None,
    until: Optional[str] = None,
    grep: Optional[str] = None,
    grep_exclude: Optional[str] = None,
    grep_ignore_case: bool = False,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Read the DNOS **CLI action audit log** from the active NCC — one
    line per CLI command executed on the box (plus the surrounding login /
    logout / service-auth events).

    Use this tool to answer questions like *"what commands did user X run
    in the last 2 hours?"*, *"what was done on the box around time T?"*,
    or *"has anyone touched config N recently?"*. It is **not** the place
    to look for subsystem state changes (link down/up, HA transitions,
    tech-support completion, NCF state changes) — those live in
    ``get_system_events``. For NETCONF RPC activity use
    ``get_netconf_accounting``.

    On disk the log lives at
    ``/core/logs/northbound_services/accounting.log`` on current DNOS
    (DNOS 26.3+, where the CLI/accounting subsystem moved into the
    ``northbound-services`` container; the path is the host-side bind
    mount, visible from the default ``run start shell``). Older builds
    are checked too: ``/var/log/dn/routing_engine/accounting.log`` (DNOS
    26.2) and ``/var/log/dn/accounting/accounting.log`` (legacy). The
    tool probes the candidates in that order and uses the first one
    that exists.

    Enters ``run start shell`` (same mechanism as ``kill_9_ncc_process``),
    runs a read-only ``awk | grep | tail | head -c`` pipeline on the
    resolved path, and returns the filtered content in ``stdout``.

    Each log line is tab-separated and starts with an ISO-8601 timestamp,
    e.g.::

        2026-04-15T07:01:57.597Z  user=dnroot  time=1776236517.5977128 \
task_id=1162  service=ssh  cmd=show system

    Filter order (all optional, all composed server-side):

      1. ``since`` / ``until`` — awk lex-compare on field 1.
      2. ``grep`` / ``grep_exclude`` — fixed-string (``grep -F``) include /
         exclude, optionally case-insensitive.
      3. ``tail_lines`` — keep only the last N matching lines (default 500).
      4. Hard 500 KB cap via ``head -c``. If tripped, a ``warnings`` entry
         tells the caller to narrow the filters.

    ``tail_lines`` must be in ``[1, 50000]`` or ``None`` for unlimited (still
    bounded by the 500 KB byte cap).

    ``since`` / ``until`` accept either:

      - absolute ISO-8601 UTC, ``YYYY-MM-DDTHH:MM:SS[.sss]Z``
      - relative, ``<n>{s|m|h|d}`` (e.g. ``10m`` = 10 minutes ago, computed
        server-side against ``datetime.now(UTC)``).

    Values are interpreted as UTC. Seconds-precision bounds are padded with
    ``.000`` (since) / ``.999`` (until) so sub-second log entries aren't
    accidentally dropped. Note: on some DNOS builds the accounting log has
    drifted from trailing-``Z`` UTC to trailing-``+HH:MM`` device-local
    timestamps mid-file; the lex-compare is approximate near that
    boundary. Widen the window if you need certainty.

    Args:
        tail_lines: Keep the last N lines after other filters
            (default 500, max 50000, ``None`` = unlimited up to byte cap).
        since: Lower time bound, inclusive. ISO-8601 UTC or relative.
        until: Upper time bound, inclusive. ISO-8601 UTC or relative.
        grep: Fixed-string include filter (``grep -F``).
        grep_exclude: Fixed-string exclude filter (``grep -v -F``).
        grep_ignore_case: Apply ``-i`` to both grep filters.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot); also used to authenticate
            the ``run start shell`` challenge.
        timeout: Per-step timeout seconds.
    """
    return _run_log_tool(
        "get_accounting",
        _CLI_ACCOUNTING_PATHS,
        GET_ACCT_NEXT_ACTION,
        tail_lines, since, until,
        grep, grep_exclude, grep_ignore_case,
        device, host, user, password, timeout,
    )


@requires(CAP_LOGS)
def get_netconf_accounting(
    tail_lines: Optional[int] = _ACCOUNTING_TAIL_DEFAULT,
    since: Optional[str] = None,
    until: Optional[str] = None,
    grep: Optional[str] = None,
    grep_exclude: Optional[str] = None,
    grep_ignore_case: bool = False,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Read the DNOS **NETCONF action audit log** from the active NCC —
    one line per NETCONF RPC (get / get-config / edit-config / commit /
    discard / close-session / ...) handled by the device.

    Use this tool to audit *what NETCONF clients did*: which session,
    which source IP, which user, which RPC, success vs failure. For CLI
    activity use ``get_accounting``; for subsystem state changes use
    ``get_system_events``.

    On disk the log lives at
    ``/core/logs/northbound_services/accounting_netconf.log`` on current
    DNOS (DNOS 26.3+, host-side bind mount of the ``northbound-services``
    container, visible from the default ``run start shell``). Older
    builds are checked too:
    ``/var/log/dn/routing_engine/accounting_netconf.log`` (DNOS 26.2)
    and ``/var/log/dn/accounting/accounting_netconf.log`` (legacy). The
    tool probes the candidates in that order and uses the first one
    that exists.

    Identical mechanism, pipeline, and arguments to ``get_accounting`` —
    same tab-separated ISO-8601-first-field format, same 500 KB cap.

    See ``get_accounting`` for full argument semantics.

    Args:
        tail_lines: Keep the last N lines after other filters
            (default 500, max 50000, ``None`` = unlimited up to byte cap).
        since: Lower time bound, inclusive. ISO-8601 UTC or relative.
        until: Upper time bound, inclusive. ISO-8601 UTC or relative.
        grep: Fixed-string include filter (``grep -F``).
        grep_exclude: Fixed-string exclude filter (``grep -v -F``).
        grep_ignore_case: Apply ``-i`` to both grep filters.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot); also used to authenticate
            the ``run start shell`` challenge.
        timeout: Per-step timeout seconds.
    """
    return _run_log_tool(
        "get_netconf_accounting",
        _NETCONF_ACCOUNTING_PATHS,
        GET_NETCONF_ACCT_NEXT_ACTION,
        tail_lines, since, until,
        grep, grep_exclude, grep_ignore_case,
        device, host, user, password, timeout,
    )


@requires(CAP_LOGS)
def get_system_events(
    tail_lines: Optional[int] = _ACCOUNTING_TAIL_DEFAULT,
    since: Optional[str] = None,
    until: Optional[str] = None,
    grep: Optional[str] = None,
    grep_exclude: Optional[str] = None,
    grep_ignore_case: bool = False,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Read the DNOS **system-events log** from the active NCC —
    subsystem / service-level state changes emitted by the platform
    itself, not a record of what users did.

    Use this tool to answer questions like *"did the tech-support bundle
    finish generating?"*, *"is there a link-down on any control
    interface?"*, *"did an NCF/NCC change state recently?"*, *"was there
    an SSH login in the last hour?"*. For CLI commands executed on the
    box use ``get_accounting``; for NETCONF RPC activity use
    ``get_netconf_accounting``.

    On disk the log lives at
    ``/core/logs/northbound_services/system-events.log`` on current DNOS
    (DNOS 26.3+, where ``system-events`` moved into the
    ``northbound-services`` container; the path is the host-side bind
    mount, visible from the default ``run start shell``). Older builds
    keep it at ``/var/log/dn/system-events.log``; the tool probes both
    and uses the first that exists. Each line is space-separated and
    looks like::

        local7.warning 2026-04-14T18:43:04.189Z OHADZS-CL System - - - \
NCF_STATE_CHANGE_DISCONNECTED:NCF 0 state has changed from versioning \
to disconnected

    — i.e. ``<syslog-facility> <timestamp> <host> <subsystem> - - - \
<EVENT_CODE>:<human message>``. The timestamp is field **2**, so the
    ``since`` / ``until`` awk filter compares against that column. A
    common workflow is:

      1. ``get_system_events(device='cl', since='2h', grep='TECH_SUPPORT')``
         to see recent tech-support lifecycle events.
      2. Narrow with ``grep`` on a specific ``EVENT_CODE:`` or subsystem
         name (``Interfaces``, ``Platform``, ``Management``, ``System``).

    Identical mechanism, pipeline, and arguments to ``get_accounting`` —
    same ``since`` / ``until`` grammar, same ``grep`` / ``grep_exclude``,
    same ``tail_lines`` cap, same 500 KB byte cap. Note: on some DNOS
    builds the timestamp in this file drifts from trailing-``Z`` UTC to
    trailing-``+HH:MM`` device-local mid-file; the lex-compare is
    approximate near that boundary. Widen the window if you need
    certainty.

    See ``get_accounting`` for full argument semantics.

    Args:
        tail_lines: Keep the last N lines after other filters
            (default 500, max 50000, ``None`` = unlimited up to byte cap).
        since: Lower time bound, inclusive. ISO-8601 UTC or relative.
        until: Upper time bound, inclusive. ISO-8601 UTC or relative.
        grep: Fixed-string include filter (``grep -F``).
        grep_exclude: Fixed-string exclude filter (``grep -v -F``).
        grep_ignore_case: Apply ``-i`` to both grep filters.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot); also used to authenticate
            the ``run start shell`` challenge.
        timeout: Per-step timeout seconds.
    """
    return _run_log_tool(
        "get_system_events",
        _SYSTEM_EVENTS_PATHS,
        GET_SYSTEM_EVENTS_NEXT_ACTION,
        tail_lines, since, until,
        grep, grep_exclude, grep_ignore_case,
        device, host, user, password, timeout,
        ts_field=2,
    )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(get_accounting)
    mcp.tool()(get_netconf_accounting)
    mcp.tool()(get_system_events)
