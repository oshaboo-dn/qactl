"""Portable on-disk path resolution for ``qactl``.

The four MCP servers each anchored their writable state (device map,
backups metadata, per-device logs, RESTCONF endpoints, rendered
templates, spilled large reads) to *their own folder inside the
``dnos-mcps`` monorepo*. A standalone, pip-installed ``qactl`` has no
such anchor, so every one of those anchors is rerouted here.

Two roots, both overridable by env so the tool stays portable across
clones / hosts / users (see the repo-portability rule):

* **state dir** — writable runtime state. ``$QACTL_STATE_DIR`` or
  ``~/.local/state/qactl``. Holds the device map (unless overridden),
  RESTCONF endpoints, per-protocol logs, backup indexes, spill files.
* **bundled data** — read-only defaults shipped inside the package
  (``qactl/data``); currently just a seed copy of the device map.

The device map is special: it is the one piece of state a user most
often wants to *share* with their existing ``dnos-mcps`` checkout, so
it has its own override ``$QACTL_DEVICES`` pointing at an explicit file
(e.g. ``.../dnos-mcps/devices/devices_mgmt0.json``).
"""

from __future__ import annotations

import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent.parent  # the ``qactl`` package
DATA_DIR = PACKAGE_DIR / "data"


def state_dir(sub: str | None = None) -> Path:
    """Writable runtime state root (optionally a named subdir).

    Honours ``$QACTL_STATE_DIR``; defaults to ``~/.local/state/qactl``.
    Does not create anything — use :func:`ensure_state_dir` for that.
    """
    base_env = os.environ.get("QACTL_STATE_DIR")
    base = Path(base_env).expanduser() if base_env else Path.home() / ".local" / "state" / "qactl"
    return base / sub if sub else base


def ensure_state_dir(sub: str | None = None) -> Path:
    """Like :func:`state_dir` but ``mkdir -p`` the result first."""
    p = state_dir(sub)
    p.mkdir(parents=True, exist_ok=True)
    return p


def device_map_path() -> str:
    """Absolute path to the canonical device map JSON.

    Resolution order:

    1. ``$QACTL_DEVICES`` — explicit file (point this at your existing
       ``dnos-mcps/devices/devices_mgmt0.json`` to share one map).
    2. ``<state_dir>/devices_mgmt0.json`` — seeded on first use from the
       bundled default if it doesn't exist yet.
    """
    explicit = os.environ.get("QACTL_DEVICES")
    if explicit:
        return str(Path(explicit).expanduser())

    p = state_dir() / "devices_mgmt0.json"
    if not p.exists():
        seed = DATA_DIR / "devices_mgmt0.json"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if seed.exists():
                p.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    return str(p)
