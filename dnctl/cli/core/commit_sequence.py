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


@dataclass
class CommitResult:
    """Outcome of parsing a ``commit`` stdout."""

    status: str                      # "ok" | "check_ok" | "no_change" | "error"
    user: Optional[str] = None       # from "Commit succeeded by X"
    timestamp: Optional[str] = None  # raw timestamp substring
    error_lines: Optional[List[str]] = None


def parse_commit_output(output: str) -> CommitResult:
    """Classify ``output`` as a success / check-success / no-op / failure.

    Success wins over failure if both shapes appear (possible when older
    warnings got carried along, but the last line is the decisive one).
    No-change is reported as its own status so the tool can flag it as a
    warning without failing the whole operation. ``check_ok`` is the
    ``commit check`` (dry-run) counterpart of ``ok`` — the candidate
    validated but was intentionally NOT applied.
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
