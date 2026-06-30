"""DNOS CLI discovery tools.

Seven tools that let an agent learn DNOS grammar before running anything
destructive:

- ``cmd_search`` — keyword search in any CLI tree.
- ``cmd_help`` — full help for a specific command line.
- ``show`` / ``show_config`` — execute operational / configuration reads.
- ``show_system`` — quick topology + version snapshot (call first for any
  system / restart task).
- ``cli_crawler`` / ``cli_config_crawler`` — walk the operational and
  configure-mode CLI trees one level at a time by appending ``?``.

None of these mutate device state (the ``?`` crawlers cancel the typed
prefix with Ctrl-U before any newline is sent), so they're the natural
first stop in any agent flow.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

import re

from dnctl.cli.core.envelope import error_response, make_response
from dnctl.cli.core.errors import (
    CMD_HELP_NEXT_ACTION,
    SHOW_CONFIG_INCOMPLETE_NEXT_ACTION,
    SHOW_CONFIG_NEXT_ACTION,
    SHOW_NEXT_ACTION,
    is_incomplete_command,
)
from dnctl.cli.core.runner import _run_on_device
from dnctl.cli.core.session import DEFAULT_CMD_TIMEOUT, DEFAULT_PASSWORD, DEFAULT_USER
from dnctl.cli.vendors import (
    CAP_DISCOVERY,
    CAP_SHOW,
    CAP_SHOW_CONFIG,
    CAP_SYSTEM,
    requires,
)
from dnctl.core.cli_probe import detect_system_mode, parse_gi_inventory
from dnctl.cli.core.validation import _quote_list, _validate_quoted, _validate_show_command


# Heuristic for "this looks like an MCP tool name, not an on-device DNOS
# command". DNOS CLI verbs are space-separated lowercase words
# (``show bgp summary``, ``request system restart``); MCP tool names are
# snake_case Python identifiers (``get_trace``, ``list_traces``,
# ``cmd_search``, …) and never contain spaces. Anything that is one
# whitespace-free token AND carries an ``_`` matches — the underscore
# is the only character DNOS doesn't use in CLI verbs.
_MCP_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")

_CMD_HELP_MCP_TOOL_HINT = (
    "This name has underscores like a tool name, not an on-device DNOS "
    "command — and `qactl cli help` only knows the on-device CLI. For "
    "qactl's own subcommands run `qactl cli --help` (or `qactl cli "
    "<command> --help`). `qactl cli help` / `qactl cli search` are for "
    "discovering DNOS CLI grammar (show / show config / request / run / "
    "configure / clear / set / unset / cmd)."
)

# DNOS prints this marker (e.g. ``* A partial match is found ...``) when
# ``cmd help`` cannot resolve the exact line and falls back to the nearest
# documented ancestor doc. The fallback still exits 0 / status ok, so it is
# easy to mistake for real leaf-level help. We surface it as ``partial_match``
# plus a warning so callers can detect the fallback programmatically.
_CMD_HELP_PARTIAL_MATCH_MARKER = "a partial match is found"

_CMD_HELP_PARTIAL_MATCH_WARNING = (
    "DNOS returned an ANCESTOR doc, not leaf help for this exact line — "
    "`cmd help` only resolves the canonical command string with "
    "`<placeholder>` tokens intact (exactly as `qactl cli search` emits "
    "it). A concrete/instantiated path (real AS, IP, ...) falls back to the "
    "nearest documented ancestor. Re-run with the canonical form, e.g. "
    "`configure protocols bgp <bgp> neighbor <neighbor> ...`."
)


_CMD_SEARCH_NEXT_ACTIONS = {
    "show": SHOW_NEXT_ACTION,
    "show_config": SHOW_CONFIG_NEXT_ACTION,
    "configure": CMD_HELP_NEXT_ACTION,
    "clear": CMD_HELP_NEXT_ACTION,
    "request": CMD_HELP_NEXT_ACTION,
    "run": CMD_HELP_NEXT_ACTION,
    "set": CMD_HELP_NEXT_ACTION,
    "unset": CMD_HELP_NEXT_ACTION,
    "all-commands": CMD_HELP_NEXT_ACTION,
}


@requires(CAP_DISCOVERY)
def cmd_search(
    scope: Literal[
        "show",
        "show_config",
        "configure",
        "clear",
        "request",
        "run",
        "set",
        "unset",
        "all-commands",
    ],
    words: List[str],
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Search one of the DNOS command trees for commands matching keywords.

    This is the FIRST tool to call when you need to discover the syntax of
    a DNOS command. Behind the scenes the MCP runs:

        cmd search <scope> | include "w1" | include "w2" | ...

    (``show_config`` is passed to DNOS as the quoted literal
    ``"show config"``; every other scope is passed verbatim. ``cmd search``
    is a plain substring match on the full command string, so each scope
    above is a top-level CLI keyword that bounds the family — pick the
    narrowest one that fits.)

    Scope → pair-wise execution tool:

      - ``show``        → pick a candidate, then run it via ``show``.
      - ``show_config`` → pick a candidate, then run it via ``show_config``.
      - ``configure``   → configure-mode syntax; use with ``cmd_help`` /
                          ``cli_config_crawler`` / ``edit_config``.
      - ``clear``       → operational clear-state commands (counters, ARP,
                          BGP/ISIS sessions, ...). Pair with ``cmd_help``
                          to confirm syntax before running on the device.
      - ``request``     → ``request system ...`` / ``request file ...`` /
                          ``request security ...`` family. Pair with
                          ``cmd_help`` and the dedicated ``request_*``
                          tools where they exist.
      - ``run``         → ``run ping`` / ``run ssh`` / ``run packet-capture``
                          / ``run start shell`` family.
      - ``set`` /
        ``unset``       → operational toggles (``set cli-no-confirm``,
                          ``set clock``, ``set logging``, ...). Both
                          families are small (~10 children) — ``cmd_help``
                          or ``cli_crawler(path='set')`` may be faster
                          than a keyword search.
      - ``all-commands``→ search every CLI tree at once. Returns a
                          dense list (≈12k commands on a typical CL-16
                          build), so always narrow with ``words=[...]``.
                          Requires DNOS build with the SW-262755 fix
                          (the older ``cmd search all`` form was a
                          substring-only match and is NOT what this
                          scope sends).

    Feed any candidate to ``cmd_help`` for full grammar detail.

    Args:
        scope: Which CLI tree to search.
        words: Single-word keywords, AND-matched against syntax.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-command timeout seconds.
    """
    if scope not in _CMD_SEARCH_NEXT_ACTIONS:
        choices = ", ".join(_CMD_SEARCH_NEXT_ACTIONS)
        return error_response(
            f"invalid SCOPE {scope!r} (choose one of: {choices})",
            device=device, host=host,
        )
    chain = _quote_list(words or [])
    if chain is None:
        return error_response(
            "Each word must be a non-empty string without double quotes.",
            device=device, host=host,
        )
    cli_scope = '"show config"' if scope == "show_config" else scope
    command = f"cmd search {cli_scope}{chain}"
    return _run_on_device(
        "cmd_search", device, host, user, password,
        command, timeout, _CMD_SEARCH_NEXT_ACTIONS[scope],
    )


@requires(CAP_DISCOVERY)
def cmd_help(
    command: str,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Fetch CLI help for any full DNOS command (show / show config / request / run / ...).

    ``command`` MUST be the **canonical command string with ``<placeholder>``
    tokens intact** — exactly as ``cmd_search`` emits it. Keep argument slots
    as ``<bgp>`` / ``<neighbor>`` / ``<hold_time>``; do NOT instantiate them
    with concrete values (real AS numbers, IPs, names). Example that resolves
    leaf-level help::

        configure protocols bgp <bgp> neighbor <neighbor> bfd strict-mode \\
            hold-time <hold_time>

    **A concrete/instantiated path silently falls back.** Passing
    ``configure protocols bgp 100001 neighbor 1.1.1.2 bfd strict-mode
    hold-time`` does NOT error — DNOS returns the nearest documented ANCESTOR
    doc (here, the generic ``protocols bgp`` doc) with a small ``* A partial
    match is found ...`` line, still at ``status: ok`` / exit 0. That looks
    like a successful help response, so it's easy to wrongly conclude "this
    command has no help". When this happens the envelope sets
    ``partial_match: true`` and adds a warning — re-run with the canonical
    ``<placeholder>`` form from ``cmd_search``.

    ``command`` must be a real DNOS command — one of:

      - A line returned by ``cmd_search`` (preferred — guaranteed to
        exist in this build's grammar, with placeholders intact).
      - A line returned by ``cli_crawler`` / ``cli_config_crawler``.
      - A command you've actually seen DNOS accept on this device.

    **Made-up / guessed commands won't work.** DNOS's ``cmd help`` does
    a literal grammar lookup; an invented line like
    ``show bgp neighbors detail`` (when the real verb is
    ``show bgp neighbor``) returns "No additional information" or an
    error — no fuzzy matching, no spell correction. Always discover
    via ``cmd_search`` first, then feed the candidate verbatim here.

    The MCP wraps the command in double quotes and runs:

        cmd help "<command>"

    This works for every DNOS command tree, not just ``show`` — e.g.
    ``cmd_help(command="request system restart")`` enumerates the accepted
    arguments of the restart command.

    **Note: this is for on-device DNOS CLI only.** ``cmd_help`` cannot
    document MCP tools (``get_trace``, ``list_traces``, ``cmd_search``,
    ``edit_config``, …). Callers passing a snake_case identifier
    (Python-style name, never produced by DNOS) get a short-circuit
    pointer to the tool's JSON descriptor instead of a wasted SSH
    round-trip.

    Args:
        command: Canonical DNOS command line to look up (no outer quotes),
            with ``<placeholder>`` tokens intact — typically a line returned
            by ``cmd_search``. A concrete/instantiated path falls back to an
            ancestor doc (``partial_match: true``); made-up commands return
            "No additional information". There is no fuzzy match.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-command timeout seconds.
    """
    err = _validate_quoted(command)
    if err:
        return error_response(
            err, device=device, host=host, next_action=CMD_HELP_NEXT_ACTION,
        )

    # Short-circuit MCP-tool lookups: bouncing them off the device just
    # produces "No additional information" with no actionable hint.
    cleaned = (command or "").strip()
    first_token = cleaned.split()[0] if cleaned else ""
    if first_token and _MCP_TOOL_NAME_RE.fullmatch(first_token):
        return make_response(
            status="ok",
            device=device, host=host or "",
            command=cleaned,
            stdout="",
            warnings=[_CMD_HELP_MCP_TOOL_HINT],
            next_actions=[
                "If you meant a DNOS CLI command, retype it with spaces "
                "(e.g. 'show bgp summary', not 'show_bgp_summary') and "
                "discover the correct grammar via "
                "`qactl cli search <scope> <keywords>` (scope: show | "
                "show_config | configure | clear | request | run | set | "
                "unset).",
            ],
        )

    wrapped = f'cmd help "{cleaned}"'
    response = _run_on_device(
        "cmd_help", device, host, user, password,
        wrapped, timeout, CMD_HELP_NEXT_ACTION,
    )

    # Flag the silent fall-back to an ancestor doc (see module constants).
    # Only meaningful on an otherwise-successful response.
    if response.get("status") == "ok":
        if _CMD_HELP_PARTIAL_MATCH_MARKER in (response.get("stdout") or "").lower():
            response["partial_match"] = True
            response.setdefault("warnings", []).append(_CMD_HELP_PARTIAL_MATCH_WARNING)
            response.setdefault("next_actions", []).append(CMD_HELP_NEXT_ACTION)
    return response


@requires(CAP_SHOW)
def show(
    command: str,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Run an operational ``show`` command on the device.

    Pass the full command verbatim, exactly as emitted by the discovery
    tools (``cmd_search(scope='show')``, ``cli_crawler``). Example:
      - command="show bgp summary"  -> runs ``show bgp summary``

    Configuration reads (``show config ...``) must go through
    ``show_config`` instead — this tool rejects them with a pointer.

    To enumerate child options of a subtree, use ``cli_crawler`` instead of
    embedding ``?`` in this tool.

    On DNOS errors ("% Unknown command", "Invalid input", etc.) the result is
    returned with status="error" and ``next_actions`` telling the caller to
    use ``cmd_search(scope='show')`` to find the correct syntax.

    Args:
        command: Full operational command, must start with ``show`` (e.g.
            ``show bgp summary``).
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-command timeout seconds.
    """
    full, err = _validate_show_command(command, want_config=False)
    if err:
        return error_response(
            err, device=device, host=host, command=(command or "").strip(),
            next_action=SHOW_NEXT_ACTION,
        )
    return _run_on_device(
        "show", device, host, user, password,
        full, timeout, SHOW_NEXT_ACTION,
    )


def _parent_show_config_command(full: str) -> Optional[str]:
    """Return ``show config <…one-token-shorter…>``, or None if the
    fallback would land somewhere unsafe.

    Used by :func:`show_config` to recover from DNOS "Incomplete command"
    by re-running the parent — pick the missing identifier off the
    parent's output and re-call. Rules:

      - Pipes (``|``) disable fallback. The caller already crafted a
        filter; rewriting it is too risky.
      - Need at least 4 tokens (``show config <X> <Y>...``) so the
        parent is at least ``show config <X>``. Falling back into bare
        ``show config`` would dump the entire device config, which can
        be many MB — never worth it as a recovery step.
    """
    cmd = (full or "").strip()
    if "|" in cmd:
        return None
    parts = cmd.split()
    if len(parts) < 4:
        return None
    return " ".join(parts[:-1])


@requires(CAP_SHOW_CONFIG)
def show_config(
    command: str,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Run ``show config ...`` on the device to read configuration.

    Pass the full command verbatim, exactly as emitted by the discovery
    tools (``cmd_search(scope='show_config')``, ``cli_config_crawler``). Example:
      - command="show config protocols bgp 100001"  -> runs ``show config protocols bgp 100001``

    Operational reads (``show ...`` without ``config``) must go through
    ``show`` instead — this tool rejects them with a pointer.

    To enumerate child options of a subtree, use ``cli_config_crawler``
    instead of embedding ``?`` in this tool.

    **Auto-fallback on "Incomplete command".** Many DNOS config
    containers are keyed by an identifier the device picks at runtime
    (BGP by AS-number, ISIS by instance name, VRFs by name, BGP
    neighbor by address, ...). A bare path like ``show config protocols
    bgp`` errors with ``Incomplete command`` because BGP needs the
    AS-number. Rather than make you re-discover that token from
    scratch, this tool transparently re-runs the **parent** path
    (``show config protocols`` in this example) and returns its output
    — every configured child is listed inline with its identifier, so
    you can pick the right one and re-call ``show_config`` with it
    appended.

    When the fallback fires, the response envelope makes the substitution
    visible:

      - ``command`` is rewritten to the parent that actually ran.
      - ``original_command`` carries what you asked for.
      - ``warnings[0]`` describes the substitution and tells you to pick
        the identifier from the output and re-call.
      - ``status`` reflects the parent run (``ok`` if the parent
        succeeded, ``error`` if even the parent failed).

    The fallback is skipped (and you get the original Incomplete error
    plus a recipe-style ``next_actions`` hint) when the command already
    contains a pipe or is too shallow to step up safely (would land on
    bare ``show config``, which dumps the entire config tree).

    Other DNOS show-config pipes that help narrow output once you know
    the path: ``| include``, ``| exclude``, ``| find``, ``| flatten``,
    ``| count``, ``| display-inherited``, ``| display-xml``, ``| tail``.

    On DNOS errors that are not ``Incomplete command`` the result is
    returned with status="error" and ``next_actions`` telling the
    caller to use ``cmd_search(scope='show_config')``.

    Args:
        command: Full show-config command, must start with ``show config``
            (e.g. ``show config protocols bgp 100001``).
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-command timeout seconds.
    """
    full, err = _validate_show_command(command, want_config=True)
    if err:
        return error_response(
            err, device=device, host=host, command=(command or "").strip(),
            next_action=SHOW_CONFIG_NEXT_ACTION,
        )
    response = _run_on_device(
        "show_config", device, host, user, password,
        full, timeout, SHOW_CONFIG_NEXT_ACTION,
    )
    # Happy path or non-Incomplete error: nothing to recover from.
    if response.get("status") != "error":
        return response
    if not is_incomplete_command(response.get("errors", []) or []):
        return response

    # Incomplete command: the agent's path is structurally right, it
    # just lacks one required identifier (BGP AS-number, ISIS instance
    # name, VRF name, ...). Re-run the parent transparently so the
    # caller gets every configured child listed inline in one round
    # trip, instead of having to construct + parse a `| flatten
    # | include` pipe themselves.
    parent = _parent_show_config_command(full)
    if parent is None:
        # Pipe present, or too shallow to fall back safely — keep the
        # targeted recipe-style hint and let the agent decide.
        response["next_actions"] = [SHOW_CONFIG_INCOMPLETE_NEXT_ACTION]
        return response

    parent_response = _run_on_device(
        "show_config", device, host, user, password,
        parent, timeout, SHOW_CONFIG_NEXT_ACTION,
    )
    parent_response["original_command"] = full
    fallback_note = (
        f"Original command {full!r} returned 'Incomplete command' "
        f"(DNOS expects a required identifier at this level); ran "
        f"parent {parent!r} instead. Pick the identifier you want "
        f"from the output and re-call show_config with it appended."
    )
    parent_response["warnings"] = [fallback_note] + list(
        parent_response.get("warnings") or []
    )
    return parent_response


@requires(CAP_SYSTEM)
def show_system(
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Run ``show system`` — **call this FIRST for any system / topology task**.

    Call this FIRST whenever the request mentions: node IDs, NCC / NCP /
    NCM / NCF, active vs standby, container names, process names, DNOS
    version, system uptime, or hardware inventory. One cheap call returns
    everything the other system-level tools need, so downstream tools
    don't have to run their own discovery rounds.

    The output feeds these arguments directly:

      - ``node_id`` for ``request_system_restart_nce`` /
        ``request_system_container_restart`` /
        ``request_system_process_restart`` (NCC / NCP / NCM / NCF inventory
        with IDs).
      - ``container_name`` for ``request_system_container_restart`` /
        ``request_system_process_restart`` (container list per node).
      - ``process_name`` for ``request_system_process_restart`` (process
        table per container).
      - Active vs standby NCC (``kill_9_ncc_process`` / ``get_accounting``
        / ``get_netconf_accounting`` / ``get_system_events`` all target
        the active NCC).

    Prefer this over walking ``cli_crawler(path='request system restart
    ...')`` when all you need is topology — ``show_system`` is a single
    call, ``cli_crawler`` typically needs 3–5 round-trips to cover the
    same ground.

    On DNOS errors the result is returned with status="error" and
    ``next_actions`` pointing at ``cmd_search(scope='show')`` for syntax discovery.

    Args:
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-command timeout seconds.

    Response adds:
        - ``mode``: ``"operational"`` when the device is running DNOS,
          ``"gi"`` when it's sitting in the golden-image / installer
          environment (after a ``delete to GI`` + redeploy, before DNOS is
          up), or ``"unknown"`` when neither schema is recognised. Both
          schemas print ``System status: running``, so consumers MUST read
          ``mode`` — not the ``running`` line — to decide whether
          operational DNOS is actually up.
        - ``gi_inventory`` (GI mode only): per-node rows parsed from the
          GI-mode inventory table (``status`` / ``baseos_version`` /
          ``gi_version`` / ``onie_version`` / ...).
    """
    response = _run_on_device(
        "show_system", device, host, user, password,
        "show system", timeout, SHOW_NEXT_ACTION,
    )
    if response.get("status") in {"ok", "warning"}:
        _annotate_system_mode(response)
    return response


def _annotate_system_mode(response: Dict[str, Any]) -> None:
    """Tag a ``show_system`` envelope with ``mode`` and, in GI mode, inventory.

    Keeps ``status`` untouched (the command itself succeeded) but flags GI
    mode loudly via ``mode`` + a warning + a next action, so neither a
    human nor an agent mistakes the bare ``System status: running`` for
    "operational DNOS is up".
    """
    stdout = response.get("stdout") or ""
    mode = detect_system_mode(stdout)
    response["mode"] = mode
    if mode == "gi":
        inventory = parse_gi_inventory(stdout)
        if inventory:
            response["gi_inventory"] = inventory
        response.setdefault("warnings", []).append(
            "Device is in GI mode (golden-image installer environment), NOT "
            "running operational DNOS — `System status: running` here reflects "
            "the installer, not DNOS. Operational-only tools (get_gitcommit, "
            "show config, ...) will fail/time out until DNOS is up."
        )
        response.setdefault("next_actions", []).append(
            "Wait for the redeploy to finish (DNOS to come up) before running "
            "operational commands; re-run show_system and confirm mode is "
            "'operational'."
        )


@requires(CAP_DISCOVERY)
def cli_crawler(
    path: str = "",
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Enumerate the DNOS CLI tree by appending ``?`` to a partial command.

    Pure discovery: this tool sends ``<path> ?`` WITHOUT a trailing newline,
    captures the context-help block DNOS prints, and then clears the
    buffered line with Ctrl-U so the base command is never submitted.
    That makes it safe even for leaf-complete destructive commands such
    as ``request system restart``.

    **Crawl iteratively** — one call is almost never enough.
    A single `?` only shows the direct children of the current prefix.
    To understand a command's full grammar you must walk down the tree:
    for each child that is not ``<CR>`` (terminal) and not an obvious
    free-text placeholder like ``<container_name>``, call this tool again
    with that child appended to the path. Keep going until every branch
    you care about ends in ``<CR>`` or a placeholder. Typical depth for a
    ``request`` / ``show config`` command is 3–5 levels.

    Legend for the output lines you will see:
      - ``<CR>``                  → branch is terminal; the current path
                                    is a complete executable command.
      - ``<foo_name>`` / ``<0-N>``→ the next token is a free-text or
                                    numeric argument; recurse only if you
                                    have a concrete value to test.
      - any other word           → a keyword child; recurse into it.

    Examples (one step per call — chain them to go deeper):
      - path=""                              -> top-level options
      - path="show bgp"                      -> children of ``show bgp``
      - path="request system restart"        -> children of restart
      - path="request system restart ncc"    -> next-level children (ids)
      - path="request system restart ncc 0"  -> next-level (``<CR>`` / ``warm``)

    Args:
        path: The partial command to expand. Empty string lists top-level
              options. Do not include a trailing ``?`` yourself.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-command timeout seconds.
    """
    prefix = (path or "").strip().rstrip("?").strip()
    return _run_on_device(
        "cli_crawler", device, host, user, password,
        prefix, timeout, CMD_HELP_NEXT_ACTION,
        mode="help",
    )


@requires(CAP_DISCOVERY)
def cli_config_crawler(
    path: str = "",
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Enumerate the DNOS CONFIGURE-mode CLI tree by appending ``?`` to a path.

    Same safety model and iterative walk as :func:`cli_crawler`, but the
    channel is first pushed into ``configure`` mode so the children you
    see are the configuration grammar — top-level containers like
    ``protocols`` / ``interfaces`` / ``system`` / ``routing-options``,
    plus action verbs like ``commit`` / ``rollback`` / ``no`` / ``show``
    — rather than the operational one.

    Nothing is ever submitted: the ``?`` trigger is sent WITHOUT a newline,
    the buffered prefix is wiped with Ctrl-U, and the session leaves
    configure mode via ``end`` before the channel closes — so the shared
    candidate is never touched.

    Use this to:
      - Drill into configuration subtrees (e.g. ``path="protocols bgp
        neighbor"``) when ``cmd_help`` / ``cmd_search(scope='configure')``
        are not enough. **Crawl bare paths — DNOS does NOT use Junos
        ``set`` prefixes.** ``set`` exists in configure mode but only as
        a narrow operational verb (``set alarm`` / ``set clock`` /
        ``set cli-terminal-length`` / ...); crawling ``set protocols``
        will return nothing useful.
      - Enumerate the accepted variants of ``commit`` (``commit check`` /
        ``commit confirmed`` / ``commit comment`` / ...) on this build.

    Crawl iteratively — one call shows only the direct children of the
    current prefix. See :func:`cli_crawler` for the legend on ``<CR>`` /
    ``<name>`` / ``<0-N>`` output lines and the recommended walk strategy.

    Args:
        path: The partial configure-mode command to expand. Empty string
            lists the top-level configure children. Do not include a
            trailing ``?`` yourself.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-command timeout seconds.
    """
    prefix = (path or "").strip().rstrip("?").strip()
    return _run_on_device(
        "cli_config_crawler", device, host, user, password,
        prefix, timeout, CMD_HELP_NEXT_ACTION,
        mode="config_help",
    )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    # cmd_search exposes the per-family scopes (show / show_config /
    # configure / clear / request / run / set / unset) plus the combined
    # ``all-commands`` scope. ``all-commands`` requires a DNOS build with
    # the SW-262755 fix (verified on DNOS 26.2.0 build 398_dev / ariel-cl);
    # older builds only have the broken ``cmd search all`` substring form.
    mcp.tool()(cmd_search)
    mcp.tool()(cmd_help)
    mcp.tool()(show)
    mcp.tool()(show_config)
    mcp.tool()(show_system)
    mcp.tool()(cli_crawler)
    mcp.tool()(cli_config_crawler)
