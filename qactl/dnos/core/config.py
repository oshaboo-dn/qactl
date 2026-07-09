"""User configuration for ``qactl`` — credentials, keys, dnftp, and the
local SFTP target for config backups.

``qactl`` is a standalone tool installed on a user's own machine, so it
must not ship lab secrets baked into source. Anything sensitive comes
from the user at setup time via one of two channels:

* **env vars** — ``QACTL_USER`` / ``QACTL_PASSWORD`` / ``QACTL_SSH_KEY`` /
  ``QACTL_DNFTP_*``.
* **config file** — TOML at ``$QACTL_CONFIG`` or
  ``~/.config/qactl/config.toml`` (written by ``qactl setup``, mode 0600).

Resolution order, highest priority first:

    explicit CLI flag  >  env var  >  config file  >  built-in default

This module only owns the env-var / config-file layers and exposes them
to :mod:`qactl.core.credentials` and :mod:`qactl.core.dnftp`. The flag
layer lives at the call sites (``--user`` / ``--password``); the
built-in defaults are the non-secret values those modules declare.

Config file shape::

    [auth]
    user = "dnroot"
    password = "..."          # optional; omit to use a key or the default
    ssh_key = "~/.ssh/id_ed25519"

    [dnftp]
    host = "dnftp"
    user = "dn"
    password = "..."
    vrf = "mgmt0"

    [local]                   # device uploads config backups to this host
    host = "myhost.example"   # optional; defaults to socket.getfqdn()
    user = "me"               # optional; defaults to the OS user
    password = "..."          # this account's password, fed to the device
    vrf = "mgmt0"

    [devices."jun-rt02"]      # per-device SSH creds (vendor boxes etc.);
    user = "labuser"          # written by ``qactl setup --device jun-rt02``
    password = "..."          # keyed by the canonical registry alias
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    import tomli as _toml  # type: ignore[no-redef]


def config_path() -> Path:
    """Resolved config-file path. ``$QACTL_CONFIG`` or ``~/.config/qactl/config.toml``."""
    env = os.environ.get("QACTL_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "qactl" / "config.toml"


@lru_cache(maxsize=1)
def load_config() -> Dict[str, Any]:
    """Parse the config file once per process. Missing / unreadable → ``{}``."""
    p = config_path()
    try:
        with p.open("rb") as fh:
            return _toml.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, _toml.TOMLDecodeError):
        return {}


def config_value(section: str, key: str) -> Optional[str]:
    """Look up ``[section] key`` from the config file, or ``None``."""
    sec = load_config().get(section)
    if isinstance(sec, dict):
        val = sec.get(key)
        if val is not None:
            return str(val)
    return None


def device_config(name: str) -> Dict[str, str]:
    """Per-device overrides from the ``[devices."<name>"]`` table.

    Returns ``{}`` when the table (or the device) is absent. Values are
    stringified; key interpretation (``user`` / ``password`` today) is
    owned by :mod:`qactl.core.credentials`.
    """
    devices = load_config().get("devices")
    if isinstance(devices, dict):
        entry = devices.get(name)
        if isinstance(entry, dict):
            return {k: str(v) for k, v in entry.items() if v is not None}
    return {}


def resolve(
    env_key: str,
    section: str,
    key: str,
    default: Optional[str] = None,
    *,
    expanduser: bool = False,
) -> Optional[str]:
    """Resolve one setting: env var > config file > default.

    Returns ``None`` if no layer supplies a value and ``default`` is
    ``None`` (used for secrets that have no safe built-in fallback).
    """
    val = os.environ.get(env_key)
    if val is None:
        val = config_value(section, key)
    if val is None:
        val = default
    if val is not None and expanduser:
        val = str(Path(val).expanduser())
    return val


def resolved_source(env_key: str, section: str, key: str, default: Optional[str]) -> str:
    """Where would :func:`resolve` get this value from? For ``qactl setup --show``."""
    if os.environ.get(env_key) is not None:
        return f"env:{env_key}"
    if config_value(section, key) is not None:
        return f"config:[{section}].{key}"
    if default is not None:
        return "default"
    return "unset"
