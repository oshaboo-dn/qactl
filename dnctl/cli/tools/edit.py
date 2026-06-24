"""Configure-mode MCP tools — DESTRUCTIVE.

Four tools that all push DNOS through ``configure`` → ... → ``commit``:

- ``edit_config`` — apply a list of configure-mode statements with
  optional commit annotation via ``commit and-exit``.
- ``edit_config_check`` — same input shape as ``edit_config`` but uses
  ``commit check`` + ``rollback 0`` so the running config is never
  touched. Pre-flight dry-run for ``edit_config``.
- ``load_override_factory_default`` — factory-reset the running config.
- ``rollback_config`` — revert to a prior commit id (1..49).

DNOS' candidate configuration is shared across SSH sessions, so all
four serialise per-device through ``dnctl.cli.core.locks.device_lock`` —
otherwise overlapping calls would stomp each other's staged statements.

The actual commit pipeline (channel open, statement send, commit parse,
rollback-on-failure) is owned by ``dnctl.cli.core.configure_commit``; this
module just builds the right command sequences and shapes the standard
response envelope.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from dnctl.cli.core.commit_sequence import parse_commit_output
from dnctl.cli.core.configure_commit import build_configure_commit_steps, drive_configure_commit
from dnctl.cli.core.edit_helpers import (
    abort_shared_candidate,
    build_edit_config_commands,
    detect_rejected_statements,
    validate_edit_log,
    validate_edit_statements,
)
from dnctl.cli.core.envelope import error_response, make_response
from dnctl.cli.core.errors import (
    COMMIT_CONFLICT_NEXT_ACTION,
    EDIT_CONFIG_NEXT_ACTION,
    FACTORY_DEFAULT_NEXT_ACTION,
    detect_error,
)
from dnctl.cli.core.locks import device_lock
from dnctl.cli.core.logging import log_request
from dnctl.cli.core.registry import transport_registry
from dnctl.cli.core.session import DEFAULT_PASSWORD, DEFAULT_USER
from dnctl.cli.vendors import CAP_CONFIGURE, CAP_FACTORY_DEFAULT, requires


# Junos-style rollback IDs are 0..49; DNOS follows the same convention.
# ``rollback 0`` discards the candidate (already exposed via edit_config's
# cleanup path), so this tool accepts 1..49 — a real prior-commit revert.
_ROLLBACK_ID_MIN = 1
_ROLLBACK_ID_MAX = 49


def _commit_conflict_error(commit: Any) -> str:
    """One-line explanation of a stale-candidate ``commit_conflict``.

    Names the conflicting committer / time when DNOS reported them so the
    operator can see who raced the commit.
    """
    who = ""
    if commit.user:
        who = f" by {commit.user}"
        if commit.timestamp:
            who += f" at {commit.timestamp}"
    return (
        f"Commit conflict: another session committed{who} while this "
        "candidate was open, so it is out-of-sync. The rebase prompt was "
        "answered 'abort' — nothing was applied."
    )


def _finalize_apply_commit(
    *,
    tool_name: str,
    inv: Any,
    timeout: int,
    request: Dict[str, Any],
    response: Dict[str, Any],
    next_action: str,
) -> Dict[str, Any]:
    """Fold a ``configure ; <body> ; commit`` outcome into ``response``.

    Shared by ``load_override_factory_default`` / ``rollback_config`` —
    both are destructive applies whose success must be proven by the
    commit verdict, not assumed. Without this they returned the default
    ``status: ok`` even on a timeout or a failed commit (false success).
    """
    if not inv.hit_prompt:
        response["status"] = "timeout"
        response["errors"].append(
            f"Timed out waiting for CLI prompt after {timeout}s."
        )
        response["next_actions"].append(next_action)
        log_request(tool_name, request, response)
        return response

    is_err, err_lines = detect_error(inv.output)
    commit = parse_commit_output(inv.output)
    response["commit"] = {
        "status": commit.status,
        "user": commit.user,
        "timestamp": commit.timestamp,
    }
    if commit.status == "ok":
        if is_err:
            response["warnings"].append(
                "commit reported success but error-looking lines were "
                "detected in stdout; review manually."
            )
            response["warnings"].extend(err_lines[-3:])
        log_request(tool_name, request, response)
        return response

    response["status"] = "error"
    if commit.status == "commit_conflict":
        response["errors"].append(_commit_conflict_error(commit))
        response["errors"].extend(commit.error_lines or [])
        response["next_actions"].append(COMMIT_CONFLICT_NEXT_ACTION)
        log_request(tool_name, request, response)
        return response
    if commit.status == "no_change":
        response["errors"].append(
            "Commit reported no changes — the operation may not have "
            "altered the running config, or a step before commit failed."
        )
    elif commit.status == "check_ok":
        response["errors"].append(
            "expected an applied commit but DNOS reported only a "
            "commit-check result — review stdout and retry."
        )
    else:
        response["errors"].extend(commit.error_lines or [])
    if is_err:
        response["errors"].extend(err_lines[-3:])
    response["next_actions"].append(next_action)
    log_request(tool_name, request, response)
    return response


@requires(CAP_CONFIGURE)
def edit_config(
    statements: List[str],
    device: Optional[str] = None,
    log: Optional[str] = None,
    abort_on_failure: bool = True,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Apply a list of configure-mode statements via ``commit and-exit``.

    Statements are bare DNOS paths to assert (e.g. ``protocols bgp
    neighbor 1.1.1.1 peer-as 65001``); prefix a path with ``no`` to
    delete it (e.g. ``no interfaces ge100-0/0/1.5``). Asserts and ``no``
    lines mix freely in one call — they all land in a single atomic
    commit, so a "delete X then add Y" change set is one operation, not
    two. **Do NOT use Junos-style ``set`` prefixes** — DNOS uses bare
    paths; ``set`` is a separate narrow operational verb in configure
    mode and ``set protocols ...`` will fail.

    Use AFTER the discovery chain — ``cmd_search(scope='configure')`` →
    ``cmd_help`` → ``cli_config_crawler`` — has pinned the exact syntax for
    each statement. This tool does no syntax discovery of its own; it just
    runs what you give it, atomically, on one ephemeral channel.

    For a pre-flight ``commit check`` that does NOT touch the running
    config, call :func:`edit_config_check` with the same arguments.

    Flow on one channel:

        1. ``configure``
        2. each entry of ``statements`` (in order, one per step)
        3. ``commit and-exit`` + ``log "<log>"`` if ``log`` was given —
           this atomically commits and leaves configure mode, so the
           channel is on firm ground at teardown.

    If another session committed while this candidate was open, DNOS
    interrupts the live commit with a rebase prompt ("...out of sync. What
    would you like to do (commit, merge-only, abort)?"). We answer
    ``abort`` rather than silently merging onto someone else's change, so
    nothing is applied and the call returns ``commit.status ==
    "commit_conflict"`` (``status: error``) telling you to re-run — a fresh
    transaction rebases onto the new running config.

    DNOS's candidate configuration is **shared across sessions**. On a
    ``commit and-exit`` failure the channel closes with the candidate
    still holding our statements; with ``abort_on_failure=True`` the tool
    then opens a second fresh channel and runs ``configure ; rollback 0``
    to wipe it. Set ``abort_on_failure=False`` only if you want to inspect
    the failed candidate from another session.

    Args:
        statements: Ordered list of configure-mode command lines, written
            as DNOS path-style statements. Each must be a single line (no
            embedded newlines / control chars) and at most ~1000 chars; up
            to 200 per call.

            **DNOS uses bare paths, NOT Junos-style ``set`` prefixes.**
            Write e.g. ``protocols bgp neighbor 1.1.1.1 peer-as 65001`` —
            do NOT prepend ``set``. In DNOS configure mode ``set`` is a
            separate top-level verb (``set alarm`` / ``set clock`` /
            ``set cli-terminal-length`` / ...) and ``set protocols ...``
            will fail with a syntax error. If you're translating from a
            Junos snippet, strip the leading ``set`` from every line.

            To remove an existing config subtree, prefix the path with
            ``no`` instead — e.g. ``no interfaces ge100-0/0/1.5`` deletes
            that subinterface. ``no`` accepts the same paths as the bare
            assert form; use ``cli_config_crawler`` with a ``no ...``
            prefix to enumerate what is deletable at a given node.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        log: Optional commit annotation. When provided it's appended as
            ``log "<log>"`` to the commit command — DNOS's equivalent of a
            commit comment, visible in ``show config commits``. Must not
            contain double quotes or control characters; ≤200 chars.
        abort_on_failure: When the commit errors, run a second
            ``configure ; rollback 0`` on a fresh channel to clear the
            shared candidate. Default ``True``.
        host: Raw hostname/IP (alternative to device).
        user: SSH username on the device (default dnroot).
        password: SSH password on the device (default dnroot).
        timeout: Per-step timeout seconds.
    """
    err = validate_edit_statements(statements)
    if err:
        return error_response(
            err, device=device, host=host,
            next_action=EDIT_CONFIG_NEXT_ACTION,
        )
    log_norm, err = validate_edit_log(log)
    if err:
        return error_response(
            err, device=device, host=host,
            next_action=EDIT_CONFIG_NEXT_ACTION,
        )

    steps, commit_line, full_command = build_edit_config_commands(
        list(statements), log_norm, deploy=True,
    )

    request = {
        "device": device, "host": host, "user": user,
        "statements": list(statements), "log": log_norm,
        "abort_on_failure": abort_on_failure,
    }

    response = make_response(
        device=device, host=host, command=full_command,
        deploy=True, commit_line=commit_line,
    )

    # Share the per-device mutex with backup_device / restore_device /
    # create_techsupport so only one config-touching op runs per device at
    # a time. DNOS's candidate is shared across sessions; overlapping
    # edits would stomp each other's staged statements.
    device_key = device or host or ""
    lock = device_lock(device_key)
    with lock:
        result = drive_configure_commit(
            transport_registry, tool_name="edit_config",
            device=device, host=host, user=user, password=password,
            timeout=timeout, steps=steps, command=full_command,
            request=request, response=response,
            capture_all=False,
        )
        if result is None:
            return response

        if not result.hit_prompt:
            response["status"] = "timeout"
            response["errors"].append(
                f"Timed out waiting for CLI prompt after {timeout}s."
            )
            response["next_actions"].append(EDIT_CONFIG_NEXT_ACTION)
            if abort_on_failure:
                abort_err = abort_shared_candidate(
                    device, host, user, password, timeout,
                )
                if abort_err:
                    response["warnings"].append(
                        f"candidate-abort cleanup failed: {abort_err}"
                    )
                else:
                    response["warnings"].append(
                        "candidate-abort cleanup: ran 'configure ; abort' "
                        "to clear the shared candidate."
                    )
            log_request("edit_config", request, response)
            return response

        is_err, err_lines = detect_error(result.output)
        commit = parse_commit_output(result.output)
        response["commit"] = {
            "status": commit.status,
            "user": commit.user,
            "timestamp": commit.timestamp,
        }

        if commit.status == "ok":
            # DNOS commits whatever parsed and still reports success even
            # when individual statements were rejected (e.g. a top-level
            # create parsed inside a stale context left by a preceding
            # `no ...` delete). Those per-statement errors are invisible to
            # parse_commit_output, so scan each statement step: any rejection
            # means the running config is partial — fail loudly.
            rejected = detect_rejected_statements(result.steps)
            if rejected:
                response["status"] = "error"
                response["errors"].append(
                    f"commit reported success but {len(rejected)} of "
                    f"{len(statements)} statement(s) were rejected by the "
                    "device and silently dropped; the running config is "
                    "partial. Re-run each rejected statement on its own (a "
                    "preceding `no ...` delete can leave a stale parse "
                    "context that poisons the statements after it)."
                )
                for stmt, lines in rejected:
                    response["errors"].append(f"rejected statement: {stmt}")
                    response["errors"].extend(lines[-2:])
                response["next_actions"].append(EDIT_CONFIG_NEXT_ACTION)
                log_request("edit_config", request, response)
                return response
            if is_err:
                response["warnings"].append(
                    "commit reported success but error-looking lines were "
                    "detected in stdout; review manually."
                )
                response["warnings"].extend(err_lines[-3:])
            log_request("edit_config", request, response)
            return response

        response["status"] = "error"
        next_action = EDIT_CONFIG_NEXT_ACTION
        if commit.status == "commit_conflict":
            response["errors"].append(_commit_conflict_error(commit))
            response["errors"].extend(commit.error_lines or [])
            next_action = COMMIT_CONFLICT_NEXT_ACTION
        elif commit.status == "no_change":
            response["errors"].append(
                "Commit reported no changes — statements may already match "
                "the running config, or the configure steps before commit "
                "failed."
            )
        elif commit.status == "check_ok":
            # Should not happen — edit_config builds a 'commit and-exit'
            # sequence. If DNOS reports only a check verdict, something
            # rewrote the command on the wire.
            response["errors"].append(
                "edit_config requested 'commit and-exit' but DNOS reported "
                "only a commit-check result — review stdout and retry. "
                "For dry-run validation, call edit_config_check instead."
            )
        else:
            response["errors"].extend(commit.error_lines or [])
        if is_err and commit.status != "commit_conflict":
            response["errors"].extend(err_lines[-3:])
        response["next_actions"].append(next_action)

        if abort_on_failure:
            abort_err = abort_shared_candidate(
                device, host, user, password, timeout,
            )
            if abort_err:
                response["warnings"].append(
                    f"candidate-abort cleanup failed: {abort_err}"
                )
            else:
                response["warnings"].append(
                    "candidate-abort cleanup: ran 'configure ; abort' to "
                    "clear the shared candidate after the failed commit."
                )

        log_request("edit_config", request, response)
        return response


@requires(CAP_CONFIGURE)
def edit_config_check(
    statements: List[str],
    device: Optional[str] = None,
    log: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Validate configure-mode statements via DNOS ``commit check`` (dry-run).

    Same input shape as :func:`edit_config`, but the running config is
    never modified. Use this to pre-flight a candidate before committing
    it live with :func:`edit_config`.

    Flow on one channel:

        1. ``configure``
        2. each entry of ``statements`` (in order, one per step)
        3. ``commit check`` + ``log "<log>"`` if ``log`` was given +
           ``no-warning`` — validates the candidate without applying it.
           ``no-warning`` auto-accepts DNOS's "another user committed
           first — commit / merge-only / abort?" prompt (the SSH channel
           would otherwise hang on it).
        4. ``rollback 0`` — drops the staged statements from the shared
           candidate so the next operator doesn't inherit them. A dry-run
           really is a dry-run.

    Success returns ``commit.status == "check_ok"``. There is no
    ``abort_on_failure`` knob — the trailing ``rollback 0`` is itself the
    candidate cleanup, and it runs whether the check passed or failed.

    Args:
        statements: Same constraints as :func:`edit_config`.
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        log: Optional commit annotation; same constraints as
            :func:`edit_config`.
        host: Raw hostname/IP (alternative to device).
        user: SSH username on the device (default dnroot).
        password: SSH password on the device (default dnroot).
        timeout: Per-step timeout seconds.
    """
    err = validate_edit_statements(statements)
    if err:
        return error_response(
            err, device=device, host=host,
            next_action=EDIT_CONFIG_NEXT_ACTION,
        )
    log_norm, err = validate_edit_log(log)
    if err:
        return error_response(
            err, device=device, host=host,
            next_action=EDIT_CONFIG_NEXT_ACTION,
        )

    steps, commit_line, full_command = build_edit_config_commands(
        list(statements), log_norm, deploy=False,
    )

    request = {
        "device": device, "host": host, "user": user,
        "statements": list(statements), "log": log_norm,
    }

    response = make_response(
        device=device, host=host, command=full_command,
        deploy=False, commit_line=commit_line,
    )

    device_key = device or host or ""
    lock = device_lock(device_key)
    with lock:
        result = drive_configure_commit(
            transport_registry, tool_name="edit_config_check",
            device=device, host=host, user=user, password=password,
            timeout=timeout, steps=steps, command=full_command,
            request=request, response=response,
            # Dry run ends on 'rollback 0' (after the commit check); we
            # need the full transcript to parse the check verdict.
            capture_all=True,
        )
        if result is None:
            return response

        if not result.hit_prompt:
            response["status"] = "timeout"
            response["errors"].append(
                f"Timed out waiting for CLI prompt after {timeout}s."
            )
            response["next_actions"].append(EDIT_CONFIG_NEXT_ACTION)
            log_request("edit_config_check", request, response)
            return response

        is_err, err_lines = detect_error(result.output)
        commit = parse_commit_output(result.output)
        response["commit"] = {
            "status": commit.status,
            "user": commit.user,
            "timestamp": commit.timestamp,
        }

        if commit.status == "check_ok":
            # Same trap as edit_config: `commit check` validates the parts
            # that parsed and reports success even when statements were
            # rejected mid-sequence. Surface the specific rejected
            # statements as a hard error so the dry-run actually warns the
            # operator the batch won't apply cleanly.
            rejected = detect_rejected_statements(result.steps)
            if rejected:
                response["status"] = "error"
                response["errors"].append(
                    f"commit check passed but {len(rejected)} of "
                    f"{len(statements)} statement(s) were rejected by the "
                    "device parser and would be silently dropped on apply. "
                    "Split the batch so a `no ...` delete can't leave a "
                    "stale parse context for the statements after it."
                )
                for stmt, lines in rejected:
                    response["errors"].append(f"rejected statement: {stmt}")
                    response["errors"].extend(lines[-2:])
                response["next_actions"].append(EDIT_CONFIG_NEXT_ACTION)
                log_request("edit_config_check", request, response)
                return response
            if is_err:
                response["warnings"].append(
                    "commit check reported success but error-looking "
                    "lines were detected in stdout; review manually."
                )
                response["warnings"].extend(err_lines[-3:])
            log_request("edit_config_check", request, response)
            return response

        response["status"] = "error"
        if commit.status == "no_change":
            response["errors"].append(
                "commit check reported no changes — statements may "
                "already match the running config, or the configure steps "
                "before commit failed."
            )
        elif commit.status == "ok":
            # Should not happen on a well-formed 'commit check' sequence,
            # but flag loudly: a real commit may have landed.
            response["errors"].append(
                "edit_config_check requested 'commit check' but DNOS "
                "reported an applied commit — review stdout and confirm "
                "running config is intact."
            )
        else:
            response["errors"].extend(commit.error_lines or [])
        if is_err:
            response["errors"].extend(err_lines[-3:])
        response["next_actions"].append(EDIT_CONFIG_NEXT_ACTION)

        log_request("edit_config_check", request, response)
        return response


@requires(CAP_FACTORY_DEFAULT)
def load_override_factory_default(
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Reset the device's running config to factory-default (DESTRUCTIVE).

    Short alias in conversation: ``lofd`` ("load override factory-default").

    Runs on one ephemeral channel:

        configure ; load override factory-default ; commit

    Returns DNOS' raw stdout. No dry-run, no commit parsing, no automatic
    backup — take a ``backup_device`` snapshot first if you need a
    rollback target.

    Args:
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username on the device (default dnroot).
        password: SSH password on the device (default dnroot).
        timeout: Per-command timeout seconds.
    """
    steps, command = build_configure_commit_steps(
        body_statements=["load override factory-default"],
    )
    request = {"device": device, "host": host, "user": user}
    response = make_response(device=device, host=host, command=command)

    # Shared candidate is per-device global; serialise with the other
    # config-touching tools so a concurrent edit can't stomp our staged
    # factory-default load.
    device_key = device or host or ""
    with device_lock(device_key):
        inv = drive_configure_commit(
            transport_registry, tool_name="load_override_factory_default",
            device=device, host=host, user=user, password=password,
            timeout=timeout, steps=steps, command=command,
            request=request, response=response,
        )
        if inv is None:
            return response
        return _finalize_apply_commit(
            tool_name="load_override_factory_default",
            inv=inv, timeout=timeout, request=request, response=response,
            next_action=FACTORY_DEFAULT_NEXT_ACTION,
        )


@requires(CAP_CONFIGURE)
def rollback_config(
    rollback_id: int = 1,
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Revert the running config to a prior commit id (DESTRUCTIVE).

    Short alias in conversation: ``rol`` ("rollback").

    Runs on one ephemeral channel:

        configure ; rollback <N> ; commit

    where ``N`` is ``rollback_id`` (default ``1`` — the most recent prior
    commit). DNOS uses the JunOS-style rollback numbering: ``0`` is the
    current running config (i.e. discard the candidate — not exposed here;
    use ``edit_config``'s cleanup instead), ``1`` is the previous commit,
    ``2`` the one before that, up to ``49``.

    Note: ``rollback`` is a DNOS hidden configure-mode command, so it is
    NOT indexed by ``cmd_search*`` / ``cli_crawler`` discovery. Use
    ``show(command="system commit")`` to list the commit history table
    (columns: Rollback ID, Version, User, Commit time, Commit origin,
    Commit log message) and pick the right id.

    Returns DNOS' raw stdout. No dry-run, no commit parsing, no automatic
    backup — take a ``backup_device`` snapshot first if you need a safety
    net.

    Args:
        rollback_id: Prior-commit id to revert to, 1..49. Default ``1``
            (most recent previous commit).
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username on the device (default dnroot).
        password: SSH password on the device (default dnroot).
        timeout: Per-command timeout seconds.
    """
    if not isinstance(rollback_id, int) or isinstance(rollback_id, bool):
        return error_response(
            "rollback_id must be an integer.",
            device=device, host=host,
        )
    if rollback_id < _ROLLBACK_ID_MIN or rollback_id > _ROLLBACK_ID_MAX:
        return error_response(
            f"rollback_id must be between {_ROLLBACK_ID_MIN} and "
            f"{_ROLLBACK_ID_MAX} (got {rollback_id}). ``rollback 0`` is "
            "candidate-discard and is handled by edit_config, not this tool.",
            device=device, host=host,
        )

    steps, command = build_configure_commit_steps(
        body_statements=[f"rollback {rollback_id}"],
    )
    request = {
        "device": device, "host": host, "user": user,
        "rollback_id": rollback_id,
    }
    response = make_response(device=device, host=host, command=command)

    device_key = device or host or ""
    with device_lock(device_key):
        inv = drive_configure_commit(
            transport_registry, tool_name="rollback_config",
            device=device, host=host, user=user, password=password,
            timeout=timeout, steps=steps, command=command,
            request=request, response=response,
        )
        if inv is None:
            return response
        return _finalize_apply_commit(
            tool_name="rollback_config",
            inv=inv, timeout=timeout, request=request, response=response,
            next_action=EDIT_CONFIG_NEXT_ACTION,
        )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(edit_config)
    mcp.tool()(edit_config_check)
    mcp.tool()(load_override_factory_default)
    mcp.tool()(rollback_config)
