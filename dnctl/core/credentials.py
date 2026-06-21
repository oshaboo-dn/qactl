"""DNOS device credentials, resolved from the user's setup — never baked in.

``dnctl`` runs on a user's own machine, so it must not ship lab secrets
in source. Every value here is resolved at import time (one fresh process
per CLI invocation) via :mod:`dnctl.core.config`:

    env var  >  config file ([auth])  >  built-in default

- ``DEFAULT_USER`` / ``DEFAULT_PASSWORD`` — the single account used on
  SSH / NETCONF / gNMI / RESTCONF. Defaults to the public
  ``dnroot`` / ``dnroot`` vendor default so the common lab case needs
  no setup. Override with ``DNCTL_USER`` / ``DNCTL_PASSWORD`` or the
  ``[auth]`` table.
- ``SSH_KEY`` — optional private-key path (``DNCTL_SSH_KEY`` or
  ``[auth].ssh_key``). When set it is offered to SSH / NETCONF / dnftp
  in addition to (or instead of) a password.

There is no separate NETCONF account or auth-failure fallback: every
protocol surface authenticates with the one ``DEFAULT_USER`` /
``DEFAULT_PASSWORD`` pair (plus ``SSH_KEY`` when configured).

Back-compat aliases ``DNROOT_USER`` / ``DNROOT_PASSWORD`` are kept so
any caller that imported the old names keeps compiling.
"""

from __future__ import annotations

from typing import Optional

from dnctl.core.config import resolve


DEFAULT_USER: str = resolve("DNCTL_USER", "auth", "user", "dnroot")  # type: ignore[assignment]
DEFAULT_PASSWORD: str = resolve("DNCTL_PASSWORD", "auth", "password", "dnroot")  # type: ignore[assignment]

# Optional private key offered to every paramiko / ncclient connect.
SSH_KEY: Optional[str] = resolve("DNCTL_SSH_KEY", "auth", "ssh_key", None, expanduser=True)

# Back-compat aliases.
DNROOT_USER = DEFAULT_USER
DNROOT_PASSWORD = DEFAULT_PASSWORD


__all__ = [
    "DEFAULT_USER",
    "DEFAULT_PASSWORD",
    "SSH_KEY",
    "DNROOT_USER",
    "DNROOT_PASSWORD",
]
