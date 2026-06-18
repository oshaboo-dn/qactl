"""DNOS device credentials, resolved from the user's setup — never baked in.

``dnctl`` runs on a user's own machine, so it must not ship lab secrets
in source. Every value here is resolved at import time (one fresh process
per CLI invocation) via :mod:`dnctl.core.config`:

    env var  >  config file ([auth]/[netconf])  >  built-in default

- ``DEFAULT_USER`` / ``DEFAULT_PASSWORD`` — the standard account tried
  first on SSH / NETCONF / gNMI / RESTCONF. Defaults to the public
  ``dnroot`` / ``dnroot`` vendor default so the common lab case needs
  no setup. Override with ``DNCTL_USER`` / ``DNCTL_PASSWORD`` or the
  ``[auth]`` table.
- ``SSH_KEY`` — optional private-key path (``DNCTL_SSH_KEY`` or
  ``[auth].ssh_key``). When set it is offered to SSH / NETCONF / dnftp
  in addition to (or instead of) a password.
- ``NETCONF_USER`` / ``NETCONF_PASSWORD`` — the dedicated NETCONF
  account some builds gate the YANG / gNMI surface to. Used as the
  ``PROTOCOL_FALLBACK`` retried on auth failure. **No built-in
  password** — supply ``DNCTL_NETCONF_PASSWORD`` or ``[netconf].password``
  to enable the fallback; otherwise it is inert.

``PROTOCOL_FALLBACK`` is the ``(user, password)`` tuple that
netconf-mcp / gnmi-mcp / restconf-mcp retry against; ``password`` is
``None`` when the user hasn't configured it.

Back-compat aliases ``DNROOT_USER`` / ``DNROOT_PASSWORD`` are kept so
any caller that imported the old names keeps compiling.
"""

from __future__ import annotations

from typing import Optional, Tuple

from dnctl.core.config import resolve


DEFAULT_USER: str = resolve("DNCTL_USER", "auth", "user", "dnroot")  # type: ignore[assignment]
DEFAULT_PASSWORD: str = resolve("DNCTL_PASSWORD", "auth", "password", "dnroot")  # type: ignore[assignment]

# Optional private key offered to every paramiko / ncclient connect.
SSH_KEY: Optional[str] = resolve("DNCTL_SSH_KEY", "auth", "ssh_key", None, expanduser=True)

NETCONF_USER: str = resolve("DNCTL_NETCONF_USER", "netconf", "user", "netconf")  # type: ignore[assignment]
# No safe default for a password — only enabled when the user sets it.
NETCONF_PASSWORD: Optional[str] = resolve("DNCTL_NETCONF_PASSWORD", "netconf", "password", None)

# Auth-failure fallback for protocol-API surfaces. ``password`` may be
# ``None`` (fallback not configured); call sites must treat that as
# "no fallback available".
PROTOCOL_FALLBACK: Tuple[str, Optional[str]] = (NETCONF_USER, NETCONF_PASSWORD)

# Back-compat aliases.
DNROOT_USER = DEFAULT_USER
DNROOT_PASSWORD = DEFAULT_PASSWORD


__all__ = [
    "DEFAULT_USER",
    "DEFAULT_PASSWORD",
    "SSH_KEY",
    "NETCONF_USER",
    "NETCONF_PASSWORD",
    "PROTOCOL_FALLBACK",
    "DNROOT_USER",
    "DNROOT_PASSWORD",
]
