"""Shared SFTP infra for the external ``dnftp`` artefact host.

Single source for:

- The deployment-pinned host / user / password / VRF constants used by
  every tool that runs DNOS' ``request file upload|download dn@dnftp:...``.
- :func:`dnftp_sftp`, the short-lived SFTP context manager the MCPs use
  for verification / list / delete.
- :func:`build_upload_command` and :func:`build_download_command`, which
  render the exact ``request file upload|download dn@dnftp:<path>
  protocol sftp vrf <vrf>`` string DNOS expects.

This module replaces three previous copies of the same code:

- ``cli-mcp/dnctl.cli.core/dnftp.py`` (verbatim, moved here).
- ``netconf-mcp/dnctl.nc.core/backup_store.py`` constants + private ``_sftp``
  context manager (now imports from here).
- The ad-hoc constants in ``cli-mcp/dnctl.cli.core/ts_store.py`` /
  ``cli-mcp/dnctl.cli.core/cert_store.py`` (already routed through
  ``cli-mcp/dnctl.cli.core/dnftp.py``, which is now a re-export shim).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

import paramiko

from qactl.dnctl.core.config import resolve

# Host / account for the external artefact server. Only the password is
# a secret with no built-in default — supply ``DNCTL_DNFTP_PASSWORD`` (or
# ``[dnftp].password`` in the config, or an SSH key via ``DNCTL_SSH_KEY``)
# before any dnftp-backed operation (techsupport / tar-load).
DNFTP_HOST: str = resolve("DNCTL_DNFTP_HOST", "dnftp", "host", "dnftp")  # type: ignore[assignment]
DNFTP_USER: str = resolve("DNCTL_DNFTP_USER", "dnftp", "user", "dn")  # type: ignore[assignment]
DNFTP_PASSWORD: Optional[str] = resolve("DNCTL_DNFTP_PASSWORD", "dnftp", "password", None)
DNFTP_VRF: str = resolve("DNCTL_DNFTP_VRF", "dnftp", "vrf", "mgmt0")  # type: ignore[assignment]
DNFTP_SSH_KEY: Optional[str] = resolve("DNCTL_SSH_KEY", "auth", "ssh_key", None, expanduser=True)


class DnftpNotConfigured(RuntimeError):
    """Raised when a dnftp operation is attempted without any auth configured."""

# 30 s for connection establishment (TCP + auth + banner). Bulk
# put()/get() transfers use Paramiko's transfer logic and are NOT
# bounded by this. cli-mcp historically used 15 s, netconf-mcp 60 s;
# 30 s is a no-regression compromise. Override per-call via
# `dnftp_sftp(timeout_s=...)` if a slower link calls for it.
_DEFAULT_SFTP_TIMEOUT_S = 30


@contextmanager
def dnftp_sftp(timeout_s: int = _DEFAULT_SFTP_TIMEOUT_S) -> Iterator[paramiko.SFTPClient]:
    """Open a short-lived SFTP session to :data:`DNFTP_HOST` and tear it down.

    AutoAddPolicy mirrors what the device-SSH pool does — host keys for
    ``dnftp`` are stable and we accept first-seen.
    """
    if not DNFTP_PASSWORD and not DNFTP_SSH_KEY:
        raise DnftpNotConfigured(
            f"No dnftp credentials configured for {DNFTP_USER}@{DNFTP_HOST}. "
            "Set DNCTL_DNFTP_PASSWORD (or [dnftp].password in the config, or an "
            "SSH key via DNCTL_SSH_KEY) — run `dnctl setup` to write the config."
        )
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        DNFTP_HOST,
        username=DNFTP_USER,
        password=DNFTP_PASSWORD,
        key_filename=DNFTP_SSH_KEY,
        timeout=timeout_s,
        banner_timeout=timeout_s,
        auth_timeout=timeout_s,
        allow_agent=bool(DNFTP_SSH_KEY),
        look_for_keys=bool(DNFTP_SSH_KEY),
    )
    try:
        sftp = client.open_sftp()
        try:
            yield sftp
        finally:
            sftp.close()
    finally:
        client.close()


def build_upload_command(
    *,
    kind: str,
    local_name: str,
    remote_path: str,
    vrf: str = DNFTP_VRF,
    user: str = DNFTP_USER,
    host: str = DNFTP_HOST,
) -> str:
    """Render ``request file upload <kind> <local> <remote-uri> protocol sftp vrf <vrf>``.

    ``kind`` is the DNOS file-class token (``config`` for backups,
    ``tech-support`` for tech-support tarballs, ``certificate`` /
    ``key`` for cert material). ``remote_path`` is the absolute POSIX
    path on the target host; we stitch the ``<user>@<host>:`` prefix.

    ``user`` / ``host`` default to the dnftp account so existing call
    sites are unchanged; the local-backup flow passes this host's own
    user/FQDN (see :mod:`dnctl.core.local_sftp`) to make the device
    upload to us instead of dnftp.
    """
    remote_uri = f"{user}@{host}:{remote_path}"
    return (
        f"request file upload {kind} {local_name} {remote_uri} "
        f"protocol sftp vrf {vrf}"
    )


def build_download_command(
    *,
    kind: str,
    local_name: str,
    remote_path: str,
    vrf: str = DNFTP_VRF,
    user: str = DNFTP_USER,
    host: str = DNFTP_HOST,
) -> str:
    """Render ``request file download <remote-uri> <kind> <local> protocol sftp vrf <vrf>``.

    ``user`` / ``host`` default to the dnftp account; the local-backup
    flow passes this host's own user/FQDN so the device downloads from
    us instead of dnftp.
    """
    remote_uri = f"{user}@{host}:{remote_path}"
    return (
        f"request file download {remote_uri} {kind} {local_name} "
        f"protocol sftp vrf {vrf}"
    )


__all__ = [
    "DNFTP_HOST",
    "DNFTP_USER",
    "DNFTP_PASSWORD",
    "DNFTP_VRF",
    "DNFTP_SSH_KEY",
    "DnftpNotConfigured",
    "dnftp_sftp",
    "build_upload_command",
    "build_download_command",
]
