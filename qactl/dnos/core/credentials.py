"""DNOS device credentials, resolved from the user's setup — never baked in.

``qactl`` runs on a user's own machine, so it must not ship lab secrets
in source. Every value here is resolved at import time (one fresh process
per CLI invocation) via :mod:`qactl.core.config`:

    env var  >  config file ([auth])  >  built-in default

- ``DEFAULT_USER`` / ``DEFAULT_PASSWORD`` — the single account used on
  SSH / NETCONF / gNMI / RESTCONF. Defaults to the public
  ``dnroot`` / ``dnroot`` vendor default so the common lab case needs
  no setup. Override with ``QACTL_USER`` / ``QACTL_PASSWORD`` or the
  ``[auth]`` table.
- ``SSH_KEY`` — optional private-key path (``QACTL_SSH_KEY`` or
  ``[auth].ssh_key``). When set it is offered to SSH / NETCONF / dnftp
  in addition to (or instead of) a password.

There is no separate NETCONF account or auth-failure fallback: every
protocol surface authenticates with the one ``DEFAULT_USER`` /
``DEFAULT_PASSWORD`` pair (plus ``SSH_KEY`` when configured).

Devices can override that global account per box or per vendor — see
:func:`resolve_device_credentials`. Per-box creds work for hosts that
aren't registered yet too (``device add`` probes, ``--host``
overrides). Vendor boxes (cisco /
juniper / arista) don't speak the DNOS ``[auth]`` account, so their
creds come from ``[devices."<name>"]`` in the config file (written by
``qactl setup --device``) or ``<VENDOR>_USER`` / ``<VENDOR>_PASSWORD``
env vars.

Back-compat aliases ``DNROOT_USER`` / ``DNROOT_PASSWORD`` are kept so
any caller that imported the old names keeps compiling.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from qactl.dnos.core.config import resolve


DEFAULT_USER: str = resolve("QACTL_USER", "auth", "user", "dnroot")  # type: ignore[assignment]
DEFAULT_PASSWORD: str = resolve("QACTL_PASSWORD", "auth", "password", "dnroot")  # type: ignore[assignment]

# Optional private key offered to every paramiko / ncclient connect.
SSH_KEY: Optional[str] = resolve("QACTL_SSH_KEY", "auth", "ssh_key", None, expanduser=True)

# Back-compat aliases.
DNROOT_USER = DEFAULT_USER
DNROOT_PASSWORD = DEFAULT_PASSWORD


# Per-vendor env credentials for non-DNOS registry devices. DNOS boxes
# stay on the global account above (no DNOS_* env — that's QACTL_*).
VENDOR_ENV: dict = {
    "arista": ("ARISTA_USER", "ARISTA_PASSWORD"),
    "cisco": ("CISCO_USER", "CISCO_PASSWORD"),
    "juniper": ("JUNIPER_USER", "JUNIPER_PASSWORD"),
}


def resolve_device_credentials(
    device: Optional[str], user: str, password: str,
    host: Optional[str] = None,
) -> Tuple[str, str]:
    """Effective SSH creds for a device call, resolved per field:

        explicit flag (anything != the global default)
        > per-device   [devices."<canonical>"] user/password
        > per-vendor   <VENDOR>_USER / <VENDOR>_PASSWORD env
        > global       DEFAULT_USER / DEFAULT_PASSWORD

    An explicit ``--password`` always passes both fields through
    untouched (tool signatures pass DEFAULT_USER / DEFAULT_PASSWORD when
    no flag was given). An explicit ``--user`` with a *default* password
    still consults the ``[devices."<name>"]`` store: the stored password
    is borrowed when it belongs to that same account (stored ``user``
    matches or is absent) — never cross-wired to a different one (#79).

    The lookup is keyed on ``device`` first, then ``host``, and needs no
    registry entry: pre-stored creds (``setup --device``) must work for
    hosts that aren't registered yet, e.g. the ``device add`` probe and
    ``--host`` overrides (#79). Per-vendor env creds still require a
    registry entry (that's where the vendor is recorded).
    """
    if password != DEFAULT_PASSWORD:
        return user, password
    candidates = [c for c in (device, host) if c]
    if not candidates:
        return user, password
    # Lazy imports: keep module import light and cycle-free.
    from qactl.dnos.core import config as _config
    from qactl.dnos.core import devices as _devices

    per_device: dict = {}
    for candidate in candidates:
        canonical = _devices.resolve_canonical(candidate) or candidate
        per_device = _config.device_config(canonical)
        if not per_device and canonical != candidate:
            per_device = _config.device_config(candidate)
        if per_device:
            break

    if user != DEFAULT_USER:
        stored_user = per_device.get("user")
        stored_password = per_device.get("password")
        if stored_password is not None and stored_user in (None, "", user):
            return user, stored_password
        return user, password

    entry = None
    for candidate in candidates:
        entry = _devices.get_device_entry(candidate)
        if isinstance(entry, dict):
            break
    vendor = (
        (entry.get("vendor") or "").strip().lower()
        if isinstance(entry, dict) else ""
    )
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
