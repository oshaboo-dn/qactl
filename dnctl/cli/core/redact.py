"""Defensive secret-redaction helpers used by SFTP-touching tools.

DNOS *should* never echo a password back at us, but the
``request file upload`` / ``request file download`` flow briefly accepts
the SFTP password on the channel — and any future shape change in DNOS
output could leak it into a transcript we're about to log or return to
the caller. These helpers strip a known secret out of strings (and out
of multi-step transcripts captured by ``dnctl.cli.core.session``) before the
data leaves the trust boundary.

Both functions are no-ops when ``secret`` is empty.
"""

from __future__ import annotations

from typing import Iterable, List

from dnctl.cli.core.session import StepCapture


def scrub_password(text: str, secret: str) -> str:
    """Return ``text`` with every literal occurrence of ``secret`` replaced
    by ``"***"``."""
    if not secret:
        return text
    return text.replace(secret, "***")


def scrub_steps(steps: Iterable[StepCapture], secret: str) -> List[StepCapture]:
    """Return a copy of ``steps`` with ``secret`` redacted from every
    per-step ``output``."""
    if not secret:
        return list(steps)
    return [
        StepCapture(
            s.command, s.head_prompt_line,
            s.output.replace(secret, "***"),
            s.tail_prompt, s.hit_prompt,
        )
        for s in steps
    ]
