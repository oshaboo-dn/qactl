"""Re-export shim: ``dnctl.cli.core.dnftp`` now points at ``dnctl.core.dnftp``.

The dnftp infra moved to the monorepo's shared library. All tool /
store call sites still import from ``dnctl.cli.core.dnftp`` to avoid a
disruptive rename across cert_store / ts_store / backup_store /
backup.py / certificate.py / techsupport.py — this thin re-export
keeps those imports working while routing them at the shared module.

Net effect: one source of truth for ``DNFTP_HOST`` / ``DNFTP_USER`` /
``DNFTP_PASSWORD`` / ``DNFTP_VRF`` / ``dnftp_sftp`` /
``build_upload_command`` / ``build_download_command``, and the cli-mcp
namespace stays familiar to anyone reading existing code.
"""

from dnctl.core.dnftp import (
    DNFTP_HOST,
    DNFTP_PASSWORD,
    DNFTP_SSH_KEY,
    DNFTP_USER,
    DNFTP_VRF,
    DnftpNotConfigured,
    build_download_command,
    build_upload_command,
    dnftp_sftp,
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
