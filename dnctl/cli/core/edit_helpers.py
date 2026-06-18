"""Configure-mode editing helpers shared by the edit-config and template
deployment tool families.

Three classes of helpers live here:

1. **Input validators** for the ``edit_config`` argument shape —
   :func:`validate_edit_log`, :func:`validate_edit_statements`. Pure
   functions, no I/O. They reject inputs that would either break out of
   the DNOS ``log "<msg>"`` quoting or feed control characters into a
   configure-mode statement.

2. **Command-sequence builder** —
   :func:`build_edit_config_commands` turns a validated ``statements``
   list (plus optional ``log`` annotation and a ``deploy`` flag) into
   the ``(steps, commit_line, joined_command)`` tuple every edit-style
   tool feeds to :func:`dnctl.cli.core.configure_commit.drive_configure_commit`.

3. **Candidate-cleanup channel** — :func:`abort_shared_candidate` opens
   a fresh SSH channel and runs ``configure ; rollback 0`` to clear the
   DNOS shared candidate after a failed ``commit and-exit``. DNOS keeps
   the candidate across sessions, so leaving it dirty would leak our
   half-applied changes to the next operator.

Used by ``dnctl.cli.tools/edit.py`` (``edit_config``) and
``dnctl.cli.tools/templates.py`` (``deploy_template`` / ``deploy_scale``).
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

from dnctl.cli.core.configure_commit import build_configure_commit_steps
from dnctl.cli.core.registry import transport_registry
from dnctl.cli.core.session import run_sequence


_EDIT_CONFIG_LOG_MAX = 200
_EDIT_CONFIG_STMT_MAX = 1000
_EDIT_CONFIG_STMTS_MAX = 200
# Control chars (NUL + C0 minus TAB) and anything that would break out of the
# ``log "<msg>"`` quoting on DNOS.
_EDIT_CONFIG_BAD_CHAR_RE = re.compile(r"[\x00-\x08\x0a-\x1f]")


def validate_edit_log(log: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Normalise ``log`` and return ``(normalised, error)``.

    ``log=None`` is allowed (no annotation). When provided, we reject
    characters that would break the ``log "<msg>"`` DNOS quoting or make
    the command span lines — double quotes and any control character.
    """
    if log is None:
        return None, None
    if not isinstance(log, str):
        return None, "log must be a string or null."
    stripped = log.strip()
    if not stripped:
        return None, "log must be non-empty when provided."
    if len(stripped) > _EDIT_CONFIG_LOG_MAX:
        return None, f"log must be at most {_EDIT_CONFIG_LOG_MAX} characters."
    if '"' in stripped:
        return None, "log must not contain double quotes."
    if _EDIT_CONFIG_BAD_CHAR_RE.search(stripped):
        return None, "log must not contain newline / control characters."
    return stripped, None


def validate_edit_statements(
    statements: Any,
    max_statements: int = _EDIT_CONFIG_STMTS_MAX,
) -> Optional[str]:
    """Reject malformed statement lists. Returns ``None`` on success.

    ``max_statements`` defaults to the conservative edit_config ceiling;
    the scale-deploy path raises it (the config is built to a local file
    and pushed in one commit, so the small per-call cap doesn't apply).
    """
    if not isinstance(statements, list) or not statements:
        return "statements must be a non-empty list of strings."
    if len(statements) > max_statements:
        return f"statements length must be at most {max_statements}."
    for i, s in enumerate(statements):
        if not isinstance(s, str) or not s.strip():
            return f"statements[{i}] must be a non-empty string."
        if _EDIT_CONFIG_BAD_CHAR_RE.search(s):
            return (
                f"statements[{i}] must not contain newline / control characters."
            )
        if len(s) > _EDIT_CONFIG_STMT_MAX:
            return (
                f"statements[{i}] must be at most {_EDIT_CONFIG_STMT_MAX} chars."
            )
    return None


def build_edit_config_commands(
    statements: List[str],
    log_norm: Optional[str],
    deploy: bool,
) -> Tuple[List[Tuple[str, Optional[str]]], str, str]:
    """Return (steps, commit_line, joined_command) for the edit_config flow.

    The commit-line is returned separately so the envelope can surface it
    on its own — DNOS operators recognise the commit shape at a glance.
    The joined command string is what ``response["command"]`` advertises.
    """
    log_suffix = f' log "{log_norm}"' if log_norm else ""
    if deploy:
        commit_line = f"commit and-exit{log_suffix}"
    else:
        # commit check is a dry run; no-warning keeps us out of the
        # interactive "another user committed" prompt that would hang us.
        commit_line = f"commit check{log_suffix} no-warning"

    # Dry run: drop the staged statements from the shared candidate.
    # DNOS spelling is ``rollback 0`` (JunOS-style); there is no
    # standalone ``abort`` command in configure mode.
    trailing = [("rollback 0", None)] if not deploy else []
    steps, joined = build_configure_commit_steps(
        body_statements=[s.strip() for s in statements],
        commit_line=commit_line,
        trailing_commands=trailing,
    )
    return steps, commit_line, joined


def abort_shared_candidate(
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    timeout: float,
) -> Optional[str]:
    """Open a fresh channel and run ``configure ; rollback 0`` to clear the
    candidate.

    Returns ``None`` on success or an error string. Used as the cleanup
    step after a failed ``commit and-exit`` — the original channel
    closed with the candidate still carrying our statements, and DNOS's
    candidate is shared across sessions, so the next operator would see
    our leftover. ``rollback 0`` replaces the candidate with the current
    running config, effectively discarding every uncommitted change.
    """
    try:
        run_sequence(
            transport_registry,
            device=device, host=host, user=user, password=password,
            commands=["configure", "rollback 0"],
            timeout=timeout,
        )
    except Exception as exc:
        return str(exc)
    return None
