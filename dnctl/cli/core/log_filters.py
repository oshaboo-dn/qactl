"""Generic log-filter helpers shared by the log-read and trace tools.

Two pure functions:

- :func:`normalize_accounting_ts` — turn a user-supplied timestamp
  (absolute ISO-8601 UTC or relative ``30s`` / ``10m`` / ``2h`` / ``1d``)
  into a fully-shaped ISO-8601 string. Sub-second precision is padded so
  lex-compare against device log lines is correct at boundaries.
- :func:`validate_grep_pattern` — reject NUL / newline / ASCII control
  characters in user-supplied grep patterns. ``shlex.quote`` does the
  shell escaping; this just keeps weird control bytes out of the pipe.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional


# ISO-8601 UTC with optional millisecond fraction: 2026-04-20T22:17:09[.597]Z
_ISO_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$"
)
_REL_TS_RE = re.compile(r"^(\d+)([smhd])$")
_REL_UNIT_SECS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Reject NUL + ASCII control chars (C0 minus TAB) in grep patterns.
_GREP_BAD_RE = re.compile(r"[\x00-\x08\x0a-\x1f]")


def normalize_accounting_ts(value: str, *, upper: bool) -> Optional[str]:
    """Turn a user-supplied timestamp into a fully-shaped ISO-8601 UTC string.

    Accepts either absolute ISO (``YYYY-MM-DDTHH:MM:SS[.sss]Z``) or relative
    (``30s`` / ``10m`` / ``2h`` / ``1d``). Relative values anchor to *now* in
    UTC. Seconds-precision inputs are padded with ``.000`` (lower bound) or
    ``.999`` (upper bound) so lex-compare against log lines like
    ``2026-04-20T22:17:09.597Z`` doesn't drop / keep the wrong sub-second
    entries. Returns ``None`` on invalid input.
    """
    s = (value or "").strip()
    if not s:
        return None
    m_rel = _REL_TS_RE.match(s)
    if m_rel:
        n = int(m_rel.group(1))
        delta = timedelta(seconds=n * _REL_UNIT_SECS[m_rel.group(2)])
        ref = datetime.now(timezone.utc) - delta
        ms = ref.microsecond // 1000
        return ref.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"
    m_iso = _ISO_TS_RE.match(s)
    if m_iso:
        if m_iso.group(1):
            return s
        pad = ".999" if upper else ".000"
        return s[:-1] + pad + "Z"
    return None


def validate_grep_pattern(value: str, field: str) -> Optional[str]:
    """Return an error string if the grep pattern carries control bytes."""
    if not value:
        return f"{field} must be a non-empty string when provided."
    if _GREP_BAD_RE.search(value):
        return (
            f"{field} must not contain NUL / newline / ASCII control characters."
        )
    return None
