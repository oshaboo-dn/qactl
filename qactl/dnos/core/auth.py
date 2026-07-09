"""Credential / auth resolution shared by every subcommand group.

This is the ``core/auth.py`` of the qactl architecture. The canonical
lab credential pair is lifted verbatim from the monorepo's
``dn_common.credentials`` (next door in
:mod:`qactl.core.credentials`); this module is the stable façade plus a
small helper for resolving the effective ``(user, password)`` from the
global CLI flags.
"""

from __future__ import annotations

from typing import Optional, Tuple

from qactl.dnos.core.credentials import (
    DEFAULT_PASSWORD,
    DEFAULT_USER,
)

__all__ = [
    "DEFAULT_USER",
    "DEFAULT_PASSWORD",
    "resolve",
]


def resolve(
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> Tuple[str, str]:
    """Resolve the effective ``(user, password)``.

    Falls back to the canonical lab account (``dnroot`` / ``dnroot``)
    when a flag is unset — matching what every MCP tool did by default.
    """
    return (
        user if user is not None else DEFAULT_USER,
        password if password is not None else DEFAULT_PASSWORD,
    )
