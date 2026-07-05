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

Registry devices can override that global account per box or per
vendor — see :func:`resolve_device_credentials`. Vendor boxes (cisco /
juniper / arista) don't speak the DNOS ``[auth]`` account, so their
creds come from ``[devices."<name>"]`` in the config file (written by
``dnctl setup --device``) or ``<VENDOR>_USER`` / ``<VENDOR>_PASSWORD``
env vars.

Back-compat aliases ``DNROOT_USER`` / ``DNROOT_PASSWORD`` are kept so
any caller that imported the old names keeps compiling.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from dnctl.core.config import resolve


DEFAULT_USER: str = resolve("DNCTL_USER", "auth", "user", "dnroot")  # type: ignore[assignment]
DEFAULT_PASSWORD: str = resolve("DNCTL_PASSWORD", "auth", "password", "dnroot")  # type: ignore[assignment]

# Optional private key offered to every paramiko / ncclient connect.
SSH_KEY: Optional[str] = resolve("DNCTL_SSH_KEY", "auth", "ssh_key", None, expanduser=True)

# Back-compat aliases.
DNROOT_USER = DEFAULT_USER
DNROOT_PASSWORD = DEFAULT_PASSWORD


# Per-vendor env credentials for non-DNOS registry devices. DNOS boxes
# stay on the global account above (no DNOS_* env — that's DNCTL_*).
VENDOR_ENV: dict = {
    "arista": ("ARISTA_USER", "ARISTA_PASSWORD"),
    "cisco": ("CISCO_USER", "CISCO_PASSWORD"),
    "juniper": ("JUNIPER_USER", "JUNIPER_PASSWORD"),
}


def resolve_device_credentials(
    device: Optional[str], user: str, password: str
) -> Tuple[str, str]:
    """Effective SSH creds for a registry-device call, resolved per field:

        explicit flag (anything != the global default)
        > per-device   [devices."<canonical>"] user/password
        > per-vendor   <VENDOR>_USER / <VENDOR>_PASSWORD env
        > global       DEFAULT_USER / DEFAULT_PASSWORD

    Explicit ``--user`` / ``--password`` always pass through untouched:
    layering only kicks in when the caller left *both* at the global
    default (tool signatures pass DEFAULT_USER / DEFAULT_PASSWORD when no
    flag was given). Host-only calls and unknown devices pass through —
    there is no registry entry to key the lookup on.
    """
    if not device or user != DEFAULT_USER or password != DEFAULT_PASSWORD:
        return user, password
    # Lazy imports: keep module import light and cycle-free.
    from dnctl.core import config as _config
    from dnctl.core import devices as _devices

    entry = _devices.get_device_entry(device)
    if not isinstance(entry, dict):
        return user, password
    canonical = _devices.resolve_canonical(device) or device
    per_device = _config.device_config(canonical)
    if not per_device and canonical != device:
        per_device = _config.device_config(device)

    vendor = (entry.get("vendor") or "").strip().lower()
    env_user_key, env_password_key = VENDOR_ENV.get(vendor, (None, None))
    env_user = os.environ.get(env_user_key) if env_user_key else None
    env_password = os.environ.get(env_password_key) if env_password_key else None

    eff_user = per_device.get("user") or env_user or user
    eff_password = per_device.get("password")
    if eff_password is None:
        # Empty-string env passwords are legal (e.g. arista lab default).
        eff_password = env_password if env_password is not None else password
    return eff_user, eff_password


__all__ = [
    "DEFAULT_USER",
    "DEFAULT_PASSWORD",
    "SSH_KEY",
    "DNROOT_USER",
    "DNROOT_PASSWORD",
    "VENDOR_ENV",
    "resolve_device_credentials",
]
