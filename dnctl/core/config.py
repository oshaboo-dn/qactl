"""User configuration for ``dnctl`` — credentials, keys, and dnftp.

``dnctl`` is a standalone tool installed on a user's own machine, so it
must not ship lab secrets baked into source. Anything sensitive comes
from the user at setup time via one of two channels:

* **env vars** — ``DNCTL_USER`` / ``DNCTL_PASSWORD`` / ``DNCTL_SSH_KEY`` /
  ``DNCTL_DNFTP_*``.
* **config file** — TOML at ``$DNCTL_CONFIG`` or
  ``~/.config/dnctl/config.toml`` (written by ``dnctl setup``, mode 0600).

Resolution order, highest priority first:

    explicit CLI flag  >  env var  >  config file  >  built-in default

This module only owns the env-var / config-file layers and exposes them
to :mod:`dnctl.core.credentials` and :mod:`dnctl.core.dnftp`. The flag
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
    """Resolved config-file path. ``$DNCTL_CONFIG`` or ``~/.config/dnctl/config.toml``."""
    env = os.environ.get("DNCTL_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "dnctl" / "config.toml"


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
    """Where would :func:`resolve` get this value from? For ``dnctl setup --show``."""
    if os.environ.get(env_key) is not None:
        return f"env:{env_key}"
    if config_value(section, key) is not None:
        return f"config:[{section}].{key}"
    if default is not None:
        return "default"
    return "unset"
