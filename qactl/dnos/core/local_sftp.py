"""Self-hosted SFTP target for device ``request file upload|download``.

The cli backup flow has the *device* run DNOS' ``request file upload config
... protocol sftp`` and ``request file download ...`` — i.e. the device is
the SSH/SFTP client and the artefact host is the server. Historically that
host was the shared external ``dnftp`` box (see :mod:`qactl.core.dnftp`).

For routine config backups that is overkill: a saved config is a few KB of
text, and coupling it to dnftp means a backup fails outright when dnftp
creds aren't set even though the device + SSH creds are fine. So backups
now point the device at **this host instead** — the machine running
``qactl`` — over its own sshd. dnftp stays reserved for the big artefacts
(tech-support tarballs).

This module is the single source for the *self* target the device dials
back into. Everything is resolved at runtime so the same code works on any
user's machine (this is a locally-run, per-user tool):

- :data:`LOCAL_SFTP_HOST` — what the device should connect to. Defaults to
  ``socket.getfqdn()`` so the device's resolver can find us; override with
  ``QACTL_LOCAL_SFTP_HOST`` / ``[local].host`` when the auto-detected name
  isn't reachable from the lab (e.g. pin an IP).
- :data:`LOCAL_SFTP_USER` — the account the device logs into us as.
  Defaults to the local OS user (``getpass.getuser()``).
- :data:`LOCAL_SFTP_PASSWORD` — that account's password, fed to the device
  at the SFTP password prompt (mirrors how ``DNFTP_PASSWORD`` is fed for
  dnftp). No safe built-in default — must be supplied via
  ``QACTL_LOCAL_SFTP_PASSWORD`` / ``[local].password``.
- :data:`LOCAL_SFTP_VRF` — the VRF the device uses to reach us. Defaults to
  ``mgmt0`` (the lab management VRF).

Unlike :mod:`qactl.core.dnftp` there is no MCP-side SFTP context manager
here: the artefacts land on our own filesystem, so listing / reading /
verifying is plain local file I/O (see :mod:`qactl.cli.core.backup_store`).
"""

from __future__ import annotations

import getpass
import socket
from dataclasses import dataclass
from typing import Optional

from qactl.dnos.core.config import resolve, resolved_source


def _default_host() -> str:
    """Best-effort fully-qualified name for this host.

    ``socket.getfqdn()`` falls back to the short hostname (and ultimately
    ``localhost``) when no FQDN resolves; that's fine as a *default* —
    users on a network where it isn't reachable from the device pin an
    explicit value via ``QACTL_LOCAL_SFTP_HOST`` / ``[local].host``.
    """
    return socket.getfqdn() or socket.gethostname()


LOCAL_SFTP_HOST: str = resolve(
    "QACTL_LOCAL_SFTP_HOST", "local", "host", _default_host(),
)  # type: ignore[assignment]
LOCAL_SFTP_USER: str = resolve(
    "QACTL_LOCAL_SFTP_USER", "local", "user", getpass.getuser(),
)  # type: ignore[assignment]
LOCAL_SFTP_PASSWORD: Optional[str] = resolve(
    "QACTL_LOCAL_SFTP_PASSWORD", "local", "password", None,
)
LOCAL_SFTP_VRF: str = resolve(
    "QACTL_LOCAL_SFTP_VRF", "local", "vrf", "mgmt0",
)  # type: ignore[assignment]
LOCAL_SFTP_PORT: str = resolve(
    "QACTL_LOCAL_SFTP_PORT", "local", "port", "22",
)  # type: ignore[assignment]


# Default timeout (seconds) for the TCP reachability probe in
# :func:`probe_endpoint`. Short on purpose: a self-check should fail fast
# when the local sshd isn't listening rather than hang the CLI.
_PROBE_TIMEOUT = 3.0


@dataclass(frozen=True)
class LocalSftpSettings:
    """The resolved ``[local]`` SFTP target the device dials back into.

    Resolved *fresh* from env / config at call time (not the import-time
    module constants) so a self-check reflects whatever ``qactl setup``
    just wrote. ``password`` is ``None`` when unconfigured — the one value
    with no safe built-in default.
    """

    host: str
    host_source: str
    user: str
    vrf: str
    port: int
    password: Optional[str]

    @property
    def password_set(self) -> bool:
        return bool(self.password)


def resolve_local_sftp() -> LocalSftpSettings:
    """Resolve the ``[local]`` SFTP settings fresh from env / config.

    Mirrors the import-time constants but re-reads on every call so the
    ``--check-local-sftp`` self-check sees the current config (the module
    constants are frozen at first import).
    """
    host = resolve("QACTL_LOCAL_SFTP_HOST", "local", "host", _default_host())
    user = resolve("QACTL_LOCAL_SFTP_USER", "local", "user", getpass.getuser())
    vrf = resolve("QACTL_LOCAL_SFTP_VRF", "local", "vrf", "mgmt0")
    port_str = resolve("QACTL_LOCAL_SFTP_PORT", "local", "port", "22")
    password = resolve("QACTL_LOCAL_SFTP_PASSWORD", "local", "password", None)
    try:
        port = int(port_str or "22")
    except (TypeError, ValueError):
        port = 22
    return LocalSftpSettings(
        host=host or _default_host(),
        host_source=resolved_source(
            "QACTL_LOCAL_SFTP_HOST", "local", "host", _default_host(),
        ),
        user=user or getpass.getuser(),
        vrf=vrf or "mgmt0",
        port=port,
        password=password,
    )


def probe_endpoint(
    host: str, port: int = 22, timeout: float = _PROBE_TIMEOUT,
) -> tuple[bool, str]:
    """Best-effort TCP probe of ``host:port`` from *this* host.

    Confirms an sshd/SFTP server is accepting connections at the resolved
    local endpoint — the precondition for the device's ``request file
    download ... protocol sftp`` to ever connect. This runs from the agent
    host (where the server is meant to live), so it validates "is the
    server up" but NOT "can the lab device route to me in the VRF"; the
    latter still needs a device-side ``ping`` (see the next_action text).

    Returns ``(reachable, detail)`` — never raises, so callers can fold the
    detail straight into a report.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"connected to {host}:{port}"
    except OSError as exc:
        return False, f"cannot connect to {host}:{port}: {exc}"


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
            "host to push/pull config backups — set QACTL_LOCAL_SFTP_PASSWORD "
            "(or [local].password in the config) to this account's password; "
            "run `qactl setup` to write the config, then "
            "`qactl setup --check-local-sftp` to verify the endpoint."
        )
    return LOCAL_SFTP_PASSWORD


__all__ = [
    "LOCAL_SFTP_HOST",
    "LOCAL_SFTP_USER",
    "LOCAL_SFTP_PASSWORD",
    "LOCAL_SFTP_VRF",
    "LOCAL_SFTP_PORT",
    "LocalSftpSettings",
    "LocalSftpNotConfigured",
    "resolve_local_sftp",
    "probe_endpoint",
    "require_password",
]
