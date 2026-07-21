"""Generic input validators shared by tool modules.

Pure functions: no I/O, no device contact. They return ``None`` on
success or a human-readable error string the tool can wrap into the
standard error envelope.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple


_PING_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:/\-]+$")


def _validate_quoted(command: str) -> Optional[str]:
    if '"' in command:
        return 'Command must not contain double quotes; pass the bare command.'
    return None


def _quote_list(words: List[str]) -> Optional[str]:
    """Validate the list of match words and render a "| include ..." chain."""
    if not words:
        return ""
    parts = []
    for w in words:
        if not isinstance(w, str) or not w.strip():
            return None
        if '"' in w:
            return None
        parts.append(f'| include "{w.strip()}"')
    return " " + " ".join(parts)


def _validate_show_command(
    command: str, *, want_config: bool,
) -> Tuple[str, Optional[str]]:
    """Validate and normalize a full ``show [config] ...`` command.

    Returns ``(normalized_command, error)``. On success ``error`` is None
    and ``normalized_command`` has its internal whitespace collapsed; on
    failure ``error`` explains the problem and ``normalized_command`` is
    the caller's original (stripped) input, useful for echoing back.

    The caller tells us which flavor it wants via ``want_config``:

    - ``want_config=False`` → tool is ``show``. Command must start with
      ``show`` and the next token (if any) must not be ``config``; we
      explicitly redirect ``show config ...`` to ``show_config``.
    - ``want_config=True`` → tool is ``show_config``. Command must start
      with ``show config``.

    Matching is case-insensitive but we preserve the caller's casing in
    the normalized output, because DNOS identifiers (interface names,
    VRFs, …) are case-sensitive even when the leading verb isn't.
    """
    raw = (command or "").strip()
    if not raw:
        hint = "show config" if want_config else "show"
        scope_arg = "show_config" if want_config else "show"
        return raw, (
            f"command must be non-empty; pass the full '{hint} ...' "
            f"command as discovered by `qactl cli search {scope_arg} "
            f"<keywords>` or `qactl cli crawl`."
        )
    tokens = raw.split()
    lowered = [t.lower() for t in tokens]
    if lowered[0] != "show":
        return raw, (
            "command must start with 'show' — pass the full command as "
            "discovered by `qactl cli search show <keywords>` "
            "(e.g. 'show bgp summary')."
        )
    is_show_config = len(lowered) >= 2 and lowered[1] == "config"
    if want_config and not is_show_config:
        return raw, (
            "show-config requires a 'show config ...' command; for "
            "operational 'show ...' commands use `qactl cli show` instead."
        )
    if not want_config and is_show_config:
        return raw, (
            "show does not accept 'show config ...' commands; route "
            "configuration reads through `qactl cli show-config`."
        )
    # Bare `show config` is a legitimate DNOS command (dumps the full
    # running config); bare `show` is not — it needs a subcommand.
    if not want_config and len(tokens) < 2:
        return raw, (
            "command must include a subcommand after 'show' — pass the "
            "full command as discovered by `qactl cli search show "
            "<keywords>` (e.g. 'show bgp summary')."
        )
    return " ".join(tokens), None


def _validate_run_command(command: str) -> Tuple[str, Optional[str]]:
    """Validate and normalize a full operational ``run ...`` command.

    Returns ``(normalized_command, error)``. On success ``error`` is None
    and ``normalized_command`` has its internal whitespace collapsed; on
    failure ``error`` explains the problem and ``normalized_command`` is
    the caller's original (stripped) input, useful for echoing back.

    ``run`` is DNOS's operational-command escape (usable from either CLI
    mode). This passthrough carries the run-scope operational / diagnostic
    commands the structured tools don't model — ``traceroute`` and its
    ``traceroute mpls isis|bgp-car`` variants, ``monitor``, ... — so their
    transcript can be captured. Two families are refused and redirected to
    their dedicated, ``--yes``-gated tools, so the passthrough never
    becomes an ungated write path:

    - ``run start shell ...`` → ``qactl cli shell`` (Linux passthrough)
    - ``run request ...``     → ``qactl cli raw '<line>' --yes``

    Leading-verb matching is case-insensitive but the caller's casing is
    preserved in the normalized output, because DNOS identifiers
    (prefixes, VRFs, ISIS instance names, ...) are case-sensitive even
    when the leading verb isn't.
    """
    raw = (command or "").strip()
    if not raw:
        return raw, (
            "command must be non-empty; pass the full 'run ...' command "
            "(e.g. 'run traceroute 10.0.0.1', "
            "'run traceroute mpls isis <prefix>')."
        )
    tokens = raw.split()
    lowered = [t.lower() for t in tokens]
    if lowered[0] != "run":
        return raw, (
            "command must start with 'run' — this passthrough issues "
            "operational run-scope commands (e.g. 'run traceroute "
            "10.0.0.1', 'run traceroute mpls isis <prefix>'). For "
            "operational 'show ...' use `qactl cli show` instead."
        )
    if len(tokens) < 2:
        return raw, (
            "command must include a subcommand after 'run' — pass the "
            "full command (e.g. 'run traceroute 10.0.0.1', "
            "'run traceroute mpls bgp-car <prefix>')."
        )
    if lowered[1] == "start" and len(lowered) >= 3 and lowered[2] == "shell":
        return raw, (
            "'run start shell ...' is the Linux shell passthrough — use "
            "`qactl cli shell '<cmd>' ...` instead; it keeps the "
            "destructive --yes gate on write commands."
        )
    if lowered[1] == "request":
        return raw, (
            "'run request ...' can mutate the device — route it through "
            "`qactl cli raw '<line>' --yes` (or the purpose-built "
            "restart / tar-load tools) so the destructive gate applies."
        )
    return " ".join(tokens), None


def _validate_clear_command(command: str) -> Tuple[str, Optional[str]]:
    """Validate and normalize a full ``clear ...`` command.

    Returns ``(normalized_command, error)``. On success ``error`` is None
    and ``normalized_command`` has its internal whitespace collapsed; on
    failure ``error`` explains the problem and ``normalized_command`` is
    the caller's original (stripped) input, useful for echoing back.

    The leading verb match is case-insensitive but we preserve the
    caller's casing in the normalized output, because DNOS identifiers
    (interface names, VRFs, neighbor IPs, …) are case-sensitive even
    when ``clear`` isn't.

    Bare ``clear`` is rejected: every DNOS ``clear`` action requires at
    least one subcommand (``clear arp``, ``clear bgp neighbor ...``, …).
    """
    raw = (command or "").strip()
    if not raw:
        return raw, (
            "command must be non-empty; pass the full 'clear ...' command "
            "as discovered by `qactl cli crawl 'clear ...'` "
            "(e.g. 'clear arp', 'clear bgp neighbor 1.2.3.4', "
            "'clear evpn mac-table')."
        )
    tokens = raw.split()
    if tokens[0].lower() != "clear":
        return raw, (
            "command must start with 'clear' — pass the full command as "
            "discovered by `qactl cli crawl 'clear ...'` "
            "(e.g. 'clear arp', 'clear bgp neighbor 1.2.3.4')."
        )
    if len(tokens) < 2:
        return raw, (
            "command must include a subcommand after 'clear' — pass the "
            "full command as discovered by `qactl cli crawl 'clear ...'` "
            "(e.g. 'clear arp', 'clear evpn mac-table')."
        )
    return " ".join(tokens), None


def _validate_token(name: str, value: str) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return f"{name} must be a non-empty string."
    if not _PING_TOKEN_RE.match(value.strip()):
        return f"{name} must match [A-Za-z0-9._:/-]+ (got {value!r})."
    return None


def _int_in(name: str, val: Any, lo: int, hi: int) -> Optional[str]:
    if not isinstance(val, int) or isinstance(val, bool) or not (lo <= val <= hi):
        return f"{name} must be int in [{lo}, {hi}]."
    return None


def _num_in(name: str, val: Any, lo: float, hi: float) -> Optional[str]:
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not (lo <= float(val) <= hi):
        return f"{name} must be number in [{lo}, {hi}]."
    return None
