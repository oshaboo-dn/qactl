"""Parse the result of a DNOS ``commit`` from its stdout.

DNOS prints one of:

- ``Commit succeeded by <USER> at <D-Mon-YYYY HH:MM:SS UTC>`` on success.
  A rollback-id line usually follows; we don't parse it today because it
  depends on the `show config commits` convention and is not always echoed.
- A line like ``Commit failed: <reason>`` or ``% Error: ...`` on failure.

This helper is intentionally small. Reusable by any future tool that runs
``configure → ... → commit → exit``. The caller is responsible for running
the sequence — we only look at the captured output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


_COMMIT_OK_RE = re.compile(
    r"Commit\s+succeeded(?:\s+by\s+(?P<user>\S+))?"
    r"(?:\s+at\s+(?P<ts>[^\r\n]+?))?\s*$",
    re.MULTILINE | re.IGNORECASE,
)
# ``commit check`` dry-run success. DNOS builds vary a bit on the exact
# phrasing; cover the common spellings. Matched BEFORE the plain-commit
# success regex so a ``check`` result is never misclassified as an apply.
_COMMIT_CHECK_OK_RE = re.compile(
    r"Commit\s+check\s+(?:succeeded|passed|success|ok)",
    re.IGNORECASE,
)
_COMMIT_FAIL_RE = re.compile(
    r"(?:Commit\s+(?:check\s+)?failed|%\s*Error[:\s]|^Error:)[^\r\n]*",
    re.MULTILINE | re.IGNORECASE,
)
_COMMIT_NOCHANGE_RE = re.compile(
    r"(?:no\s+changes\s+to\s+commit|nothing\s+to\s+commit|"
    r"commit\s+action\s+is\s+not\s+applicable)",
    re.IGNORECASE,
)
# A live ``commit`` was interrupted because another session committed first
# and our candidate is stale (the "rebase" prompt). We answer ``abort`` on
# the wire so the channel doesn't hang; this classifies the leftover warning
# so callers can tell "someone raced you, re-run" apart from a broken
# candidate. Either the warning line or the question line is enough to match
# (the question's choices/whitespace may drift across builds).
_COMMIT_CONFLICT_RE = re.compile(
    r"(?:your\s+configuration\s+is\s+out\s+of\s+sync|"
    r"what\s+would\s+you\s+like\s+to\s+do\s*\(\s*commit\s*,\s*"
    r"merge-only\s*,\s*abort\s*\))",
    re.IGNORECASE,
)
# Pull the conflicting committer + timestamp out of the warning line, e.g.
# ``Warning: User 'dnroot' committed at 03-Jul-2025 06:48:02 UTC, your ...``.
_CONFLICT_WHO_RE = re.compile(
    r"User\s+'(?P<user>[^']+)'\s+committed\s+at\s+(?P<ts>[^,\r\n]+)",
    re.IGNORECASE,
)


@dataclass
class CommitResult:
    """Outcome of parsing a ``commit`` stdout."""

    status: str                      # "ok" | "check_ok" | "no_change" | "commit_conflict" | "error"
    user: Optional[str] = None       # from "Commit succeeded by X" / conflicting committer
    timestamp: Optional[str] = None  # raw timestamp substring
    error_lines: Optional[List[str]] = None


def parse_commit_output(output: str) -> CommitResult:
    """Classify ``output`` as success / check-success / no-op / conflict / failure.

    Success wins over failure if both shapes appear (possible when older
    warnings got carried along, but the last line is the decisive one).
    No-change is reported as its own status so the tool can flag it as a
    warning without failing the whole operation. ``check_ok`` is the
    ``commit check`` (dry-run) counterpart of ``ok`` — the candidate
    validated but was intentionally NOT applied. ``commit_conflict`` means
    another session committed first, leaving this candidate stale; we
    answered the rebase prompt with ``abort`` so nothing was applied and
    the caller should re-run to rebase. Conflict is checked AFTER success
    (a future explicit merge that succeeds still reports ``ok``).
    """
    if not output:
        return CommitResult(status="error", error_lines=["(empty output)"])

    # ``commit check`` success has to be tested BEFORE the plain-commit
    # success regex, otherwise a validation result could be misclassified
    # as an applied commit on builds where the phrasings overlap.
    if _COMMIT_CHECK_OK_RE.search(output):
        return CommitResult(status="check_ok")

    # Look for the most recent success marker; DNOS always emits it last.
    ok_matches = list(_COMMIT_OK_RE.finditer(output))
    if ok_matches:
        m = ok_matches[-1]
        return CommitResult(
            status="ok",
            user=(m.group("user") or None),
            timestamp=(m.group("ts") or None),
        )

    # Stale-candidate rebase prompt: no success line landed (we answer
    # ``abort`` on the wire), so report it as its own status with the
    # warning lines so the caller can advise a re-run.
    if _COMMIT_CONFLICT_RE.search(output):
        m = _CONFLICT_WHO_RE.search(output)
        conflict_lines = [
            ln.strip()
            for ln in output.splitlines()
            if _COMMIT_CONFLICT_RE.search(ln)
        ]
        return CommitResult(
            status="commit_conflict",
            user=(m.group("user") if m else None),
            timestamp=(m.group("ts").strip() if m else None),
            error_lines=conflict_lines[-3:] or None,
        )

    if _COMMIT_NOCHANGE_RE.search(output):
        return CommitResult(status="no_change")

    fails = [m.group(0).strip() for m in _COMMIT_FAIL_RE.finditer(output)]
    if fails:
        return CommitResult(status="error", error_lines=fails[-5:])

    # No decisive marker — surface the tail as the error reason so the caller
    # can see what DNOS actually said.
    tail = [ln.strip() for ln in output.splitlines() if ln.strip()][-5:]
    return CommitResult(
        status="error",
        error_lines=tail or ["commit produced no recognisable output"],
    )


__all__ = ["CommitResult", "parse_commit_output"]
