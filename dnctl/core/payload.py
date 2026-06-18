"""Resolve an XML / JSON / config body from stdin, a file, or inline.

Every mutating command that takes a payload accepts it three ways, in
priority order:

1. ``--file PATH`` — read the file.
2. positional ``-`` — read all of stdin (``cat x.xml | dnctl nc get sa -``).
3. positional inline string — used verbatim.

This keeps the CLI pipe-friendly for agents while still allowing quick
inline one-liners.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional


class PayloadError(ValueError):
    """Raised when no usable payload could be resolved."""


def resolve_body(
    positional: Optional[str],
    file: Optional[str] = None,
    *,
    required: bool = True,
) -> Optional[str]:
    """Return the payload string from ``--file`` / stdin (``-``) / inline.

    With ``required=False`` returns ``None`` when nothing was supplied.
    """
    if file:
        return Path(file).expanduser().read_text(encoding="utf-8")
    if positional == "-":
        return sys.stdin.read()
    if positional is not None:
        return positional
    if required:
        raise PayloadError(
            "no payload provided: pass it inline, via --file PATH, "
            "or as '-' to read stdin"
        )
    return None
