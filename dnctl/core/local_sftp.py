"""Self-hosted SFTP target for device ``request file upload|download``.

The cli backup flow has the *device* run DNOS' ``request file upload config
... protocol sftp`` and ``request file download ...`` — i.e. the device is
the SSH/SFTP client and the artefact host is the server. Historically that
host was the shared external ``dnftp`` box (see :mod:`dnctl.core.dnftp`).

For routine config backups that is overkill: a saved config is a few KB of
text, and coupling it to dnftp means a backup fails outright when dnftp
creds aren't set even though the device + SSH creds are fine. So backups
now point the device at **this host instead** — the machine running
``dnctl`` — over its own sshd. dnftp stays reserved for the big artefacts
(tech-support tarballs).

This module is the single source for the *self* target the device dials
back into. Everything is resolved at runtime so the same code works on any
user's machine (this is a locally-run, per-user tool):

- :data:`LOCAL_SFTP_HOST` — what the device should connect to. Defaults to
  ``socket.getfqdn()`` so the device's resolver can find us; override with
  ``DNCTL_LOCAL_SFTP_HOST`` / ``[local].host`` when the auto-detected name
  isn't reachable from the lab (e.g. pin an IP).
- :data:`LOCAL_SFTP_USER` — the account the device logs into us as.
  Defaults to the local OS user (``getpass.getuser()``).
- :data:`LOCAL_SFTP_PASSWORD` — that account's password, fed to the device
  at the SFTP password prompt (mirrors how ``DNFTP_PASSWORD`` is fed for
  dnftp). No safe built-in default — must be supplied via
  ``DNCTL_LOCAL_SFTP_PASSWORD`` / ``[local].password``.
- :data:`LOCAL_SFTP_VRF` — the VRF the device uses to reach us. Defaults to
  ``mgmt0`` (the lab management VRF).

Unlike :mod:`dnctl.core.dnftp` there is no MCP-side SFTP context manager
here: the artefacts land on our own filesystem, so listing / reading /
verifying is plain local file I/O (see :mod:`dnctl.cli.core.backup_store`).
"""

from __future__ import annotations

import getpass
import socket
from typing import Optional

from dnctl.core.config import resolve


def _default_host() -> str:
    """Best-effort fully-qualified name for this host.

    ``socket.getfqdn()`` falls back to the short hostname (and ultimately
    ``localhost``) when no FQDN resolves; that's fine as a *default* —
    users on a network where it isn't reachable from the device pin an
    explicit value via ``DNCTL_LOCAL_SFTP_HOST`` / ``[local].host``.
    """
    return socket.getfqdn() or socket.gethostname()


LOCAL_SFTP_HOST: str = resolve(
    "DNCTL_LOCAL_SFTP_HOST", "local", "host", _default_host(),
)  # type: ignore[assignment]
LOCAL_SFTP_USER: str = resolve(
    "DNCTL_LOCAL_SFTP_USER", "local", "user", getpass.getuser(),
)  # type: ignore[assignment]
LOCAL_SFTP_PASSWORD: Optional[str] = resolve(
    "DNCTL_LOCAL_SFTP_PASSWORD", "local", "password", None,
)
LOCAL_SFTP_VRF: str = resolve(
    "DNCTL_LOCAL_SFTP_VRF", "local", "vrf", "mgmt0",
)  # type: ignore[assignment]


class LocalSftpNotConfigured(RuntimeError):
    """Raised when a local-SFTP backup is attempted without a password set."""


def require_password() -> str:
    """Return :data:`LOCAL_SFTP_PASSWORD` or raise :class:`LocalSftpNotConfigured`.

    The device authenticates to our sshd with this password at the prompt
    (same shape as the dnftp flow). Without it the device upload/download
    would hang on the prompt, so callers gate on this first and surface a
    clear, actionable error instead.
    """
    if not LOCAL_SFTP_PASSWORD:
        raise LocalSftpNotConfigured(
            f"No local SFTP password configured for "
            f"{LOCAL_SFTP_USER}@{LOCAL_SFTP_HOST}. The device logs into this "
            "host to push/pull config backups — set DNCTL_LOCAL_SFTP_PASSWORD "
            "(or [local].password in the config) to this account's password; "
            "run `dnctl setup` to write the config."
        )
    return LOCAL_SFTP_PASSWORD


__all__ = [
    "LOCAL_SFTP_HOST",
    "LOCAL_SFTP_USER",
    "LOCAL_SFTP_PASSWORD",
    "LOCAL_SFTP_VRF",
    "LocalSftpNotConfigured",
    "require_password",
]
