"""Single canonical device-map I/O for every MCP in the monorepo.

The map lives at ``<repo-root>/devices/devices_mgmt0.json`` and has the
shape::

    {
      "generated_at": "2026-04-19T15:43:53Z",
      "devices": {
        "<alias>": {
          "mgmt0": "<ip>",
          "expected_role": "SA"|"CL",          # required for netconf-mcp
          "expected_sns": ["<sn>", ...],       # SSH host candidates
          "system_id": "<uuid>",               # optional, dual-NCC tiebreaker
          "aliases": ["<nickname>", ...]       # optional secondary names
        }, ...
      }
    }

The map key is the **canonical** alias (always the chassis's configured
``System Name``). The optional per-entry ``aliases`` list holds
**secondary** names that resolve to the same device — so ``-d spine-a``
can reach the box registered as ``sa``. Secondary aliases never shadow a
canonical key: direct key lookups always win.

This module is **schema-agnostic about per-device fields** beyond
``mgmt0`` — every other field (``expected_role``, ``expected_sns``,
``system_id``) is owned and interpreted by the caller (typically
``cli-mcp/dnctl.cli.tools/devices.py`` for writes, ``netconf-mcp/dnctl.nc.core/session.py``
for reads). Adding a new field doesn't require touching this module.

Reader API (the common case)::

    from dnctl.core.devices import resolve_mgmt0, get_device_entry, load_device_map

    ip = resolve_mgmt0("sa")           # -> "100.64.11.4" or None
    e  = get_device_entry("sa")        # -> {"mgmt0": ..., "expected_role": ..., ...}
    full = load_device_map()           # -> entire dict, useful for ``*_list_devices``

Writer API (only the device-registry tools should call these)::

    from dnctl.core.devices import update_device

    update_device("kira", mgmt0="100.64.11.64", expected_role="SA")

Every mutator holds a cross-process exclusive ``flock`` (on a sidecar
``<map>.lock``) for the whole read-modify-write, and writes via a temp
file + ``os.replace`` so a crash mid-write can never truncate the map
(a reader either sees the old file or the new one, never a half-written
one). On non-POSIX hosts the flock degrades to a no-op (the atomic
rename still holds).
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

try:
    import fcntl  # POSIX-only; used for the cross-process map lock.
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


def default_device_map_path() -> str:
    """Absolute path to the canonical device map.

    Resolved portably (``$DNCTL_DEVICES`` or ``<state_dir>/devices_mgmt0.json``,
    seeded from the bundled default) — see :mod:`dnctl.core.paths`.
    """
    from dnctl.core import paths
    return paths.device_map_path()


def _resolve_path(path: Optional[str]) -> str:
    return path or default_device_map_path()


def load_device_map(path: Optional[str] = None) -> Dict[str, Any]:
    """Load the full device-map dict. Returns ``{"devices": {}}`` when missing.

    Never raises on a missing or malformed file — the caller can
    distinguish ``empty`` from ``corrupt`` only by re-reading. We
    favour graceful degradation here because losing the map shouldn't
    crash a tool that doesn't strictly need it.
    """
    p = _resolve_path(path)
    if not os.path.exists(p):
        return {"devices": {}}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"devices": {}}
    if not isinstance(data, dict):
        return {"devices": {}}
    if not isinstance(data.get("devices"), dict):
        data["devices"] = {}
    return data


def list_device_aliases(path: Optional[str] = None) -> List[str]:
    """Sorted list of all known **canonical** device aliases.

    Secondary aliases are intentionally excluded — use
    :func:`get_aliases` for a device's nicknames or
    :func:`resolve_canonical` to map a nickname back to its canonical
    key.
    """
    return sorted((load_device_map(path).get("devices") or {}).keys())


def _entry_aliases(entry: Any) -> List[str]:
    """Clean ``aliases`` list off a device entry (``[]`` when absent)."""
    if not isinstance(entry, dict):
        return []
    raw = entry.get("aliases")
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, str) and a]


def resolve_canonical(device: str, path: Optional[str] = None) -> Optional[str]:
    """Map ``device`` (canonical key OR secondary alias) to its canonical key.

    A direct canonical-key hit always wins, so a secondary alias can
    never shadow a real device. Returns ``None`` when ``device`` matches
    neither a canonical key nor any entry's ``aliases`` list.
    """
    if not isinstance(device, str) or not device:
        return None
    devices = load_device_map(path).get("devices") or {}
    if device in devices:
        return device
    for key, entry in devices.items():
        if device in _entry_aliases(entry):
            return key
    return None


def get_aliases(device: str, path: Optional[str] = None) -> List[str]:
    """Sorted secondary aliases for ``device`` (canonical key or alias)."""
    canonical = resolve_canonical(device, path)
    if canonical is None:
        return []
    entry = (load_device_map(path).get("devices") or {}).get(canonical)
    return sorted(_entry_aliases(entry))


def get_device_entry(
    device: str, path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Per-device entry dict, or ``None`` if the name isn't known.

    Resolves ``device`` as a canonical key first, then falls back to a
    secondary alias, so both ``-d sa`` and ``-d spine-a`` return the same
    entry.
    """
    devices = load_device_map(path).get("devices") or {}
    entry = devices.get(device)
    if isinstance(entry, dict):
        return entry
    for candidate in devices.values():
        if device in _entry_aliases(candidate):
            return candidate if isinstance(candidate, dict) else None
    return None


def resolve_mgmt0(device: str, path: Optional[str] = None) -> Optional[str]:
    """Return the ``mgmt0`` IP for ``device`` or ``None`` if unknown.

    Accepts a canonical key or a secondary alias. Tolerates both
    ``{"mgmt0": "..."}`` and the legacy plain-string entry shape
    (``"<alias>": "<ip>"``) some older snapshots may have.
    """
    devices = load_device_map(path).get("devices") or {}
    entry = devices.get(device)
    if entry is None:
        canonical = resolve_canonical(device, path)
        if canonical is not None:
            entry = devices.get(canonical)
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, dict):
        ip = entry.get("mgmt0")
        if isinstance(ip, str) and ip.strip():
            return ip.strip()
    return None


def _bump_generated_at(data: Dict[str, Any]) -> None:
    data["generated_at"] = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        .replace("+00:00", "Z")
    )


def _write_map(path: str, data: Dict[str, Any]) -> None:
    """Atomically replace the map file (temp write + ``os.replace``).

    A crash between truncate and write would otherwise leave a
    half-written / empty JSON that ``load_device_map`` reads as an empty
    registry. Writing a sibling temp file and renaming it into place
    makes the swap atomic on POSIX.
    """
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".devices_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _map_lock(path: str) -> Iterator[None]:
    """Hold a cross-process exclusive lock for one read-modify-write.

    Serialises concurrent writers (e.g. two MCPs on the same host) so a
    later writer always sees the earlier writer's committed state instead
    of clobbering it. No-op where ``fcntl`` is unavailable.
    """
    if fcntl is None:  # pragma: no cover - non-POSIX fallback
        yield
        return
    lock_path = os.path.abspath(path) + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def update_device(
    device: str, path: Optional[str] = None, **fields: Any
) -> None:
    """Read-modify-write a single device entry.

    Any kwarg is set on the entry verbatim (overwriting an existing
    value of the same key). To remove a field, set it to ``None`` and
    the caller filters that out — we don't second-guess the schema.

    Side effect: bumps ``generated_at`` to current UTC. Creates the
    parent directory if missing.
    """
    if not isinstance(device, str) or not device:
        raise ValueError("device must be a non-empty string")
    p = _resolve_path(path)
    with _map_lock(p):
        data = load_device_map(p)
        entry = data["devices"].get(device)
        if not isinstance(entry, dict):
            entry = {}
        entry.update(fields)
        data["devices"][device] = entry
        _bump_generated_at(data)
        _write_map(p, data)


def add_alias(
    alias: str, canonical: str, path: Optional[str] = None
) -> bool:
    """Attach secondary ``alias`` to an existing ``canonical`` device.

    The canonical name keeps being the device's primary key (the
    chassis ``System Name``); ``alias`` becomes an extra name that
    resolves to the same entry. Returns ``True`` if the alias was newly
    added, ``False`` if it was already present on that device.

    Raises ``ValueError`` when:

    - ``canonical`` is not a registered device,
    - ``alias`` collides with an existing canonical device key (a
      secondary alias must never shadow a real device), or
    - ``alias`` is already a secondary alias of a *different* device.
    """
    if not isinstance(alias, str) or not alias:
        raise ValueError("alias must be a non-empty string")
    if not isinstance(canonical, str) or not canonical:
        raise ValueError("canonical must be a non-empty string")

    p = _resolve_path(path)
    with _map_lock(p):
        data = load_device_map(p)
        devices = data.get("devices") or {}
        data["devices"] = devices

        entry = devices.get(canonical)
        if not isinstance(entry, dict):
            raise ValueError(f"device '{canonical}' is not registered")
        if alias == canonical:
            raise ValueError("alias must differ from the canonical name")
        if alias in devices:
            raise ValueError(
                f"'{alias}' is already a canonical device name; a secondary "
                f"alias must not shadow a registered device"
            )
        for key, other in devices.items():
            if key != canonical and alias in _entry_aliases(other):
                raise ValueError(
                    f"alias '{alias}' is already assigned to device '{key}'"
                )

        aliases = _entry_aliases(entry)
        if alias in aliases:
            return False
        aliases.append(alias)
        entry["aliases"] = sorted(aliases)
        devices[canonical] = entry
        _bump_generated_at(data)
        _write_map(p, data)
        return True


def remove_alias(alias: str, path: Optional[str] = None) -> Optional[str]:
    """Detach a secondary ``alias`` from whichever device owns it.

    Returns the canonical name it was attached to, or ``None`` if no
    device had that secondary alias. Never removes a canonical device —
    only the secondary name.
    """
    if not isinstance(alias, str) or not alias:
        raise ValueError("alias must be a non-empty string")
    p = _resolve_path(path)
    with _map_lock(p):
        data = load_device_map(p)
        devices = data.get("devices") or {}
        for key, entry in devices.items():
            aliases = _entry_aliases(entry)
            if alias in aliases:
                remaining = [a for a in aliases if a != alias]
                if remaining:
                    entry["aliases"] = remaining
                else:
                    entry.pop("aliases", None)
                devices[key] = entry
                data["devices"] = devices
                _bump_generated_at(data)
                _write_map(p, data)
                return key
        return None


def rename_device(
    old: str,
    new: str,
    keep_old_as_alias: bool = True,
    path: Optional[str] = None,
) -> List[str]:
    """Rename a canonical device key in place, preserving its entry.

    The whole entry (``mgmt0`` / ``expected_role`` / ``expected_sns`` /
    ``system_id`` / ``aliases``) moves from ``old`` to ``new`` with no
    re-probe — use this when a chassis's ``System Name`` changed and the
    registry key needs to catch up without dropping creds/history.

    ``old`` must be a **canonical** key (not a secondary alias). When
    ``keep_old_as_alias`` is true the old name is retained as a secondary
    alias so ``-d <old>`` keeps resolving to the same box.

    Returns the renamed entry's secondary-alias list. Raises
    ``ValueError`` when:

    - ``old`` / ``new`` are empty or equal,
    - ``old`` is not a registered canonical device,
    - ``new`` is already a canonical device key (would collide), or
    - ``new`` is a secondary alias of a *different* device.
    """
    if not isinstance(old, str) or not old:
        raise ValueError("old must be a non-empty string")
    if not isinstance(new, str) or not new:
        raise ValueError("new must be a non-empty string")
    if old == new:
        raise ValueError("new must differ from old")

    p = _resolve_path(path)
    with _map_lock(p):
        data = load_device_map(p)
        devices = data.get("devices") or {}
        data["devices"] = devices

        entry = devices.get(old)
        if not isinstance(entry, dict):
            # Help the caller who passed a secondary alias by mistake.
            canonical = resolve_canonical(old, p)
            if canonical and canonical != old:
                raise ValueError(
                    f"'{old}' is a secondary alias of '{canonical}', not a "
                    f"canonical device; rename '{canonical}' instead"
                )
            raise ValueError(f"device '{old}' is not registered")
        if new in devices:
            raise ValueError(
                f"'{new}' is already a canonical device name; remove it first "
                f"or pick a different name"
            )
        for key, other in devices.items():
            if key != old and new in _entry_aliases(other):
                raise ValueError(
                    f"'{new}' is already a secondary alias of device '{key}'"
                )

        # ``new`` may currently be a secondary alias of ``old`` itself — drop
        # it from the alias list since it's becoming the canonical key.
        aliases = [a for a in _entry_aliases(entry) if a != new]
        if keep_old_as_alias and old not in aliases:
            aliases.append(old)
        if aliases:
            entry["aliases"] = sorted(aliases)
        else:
            entry.pop("aliases", None)

        devices.pop(old)
        devices[new] = entry
        _bump_generated_at(data)
        _write_map(p, data)
        return sorted(_entry_aliases(entry))


def remove_device(device: str, path: Optional[str] = None) -> bool:
    """Drop the alias entirely. Returns True if it existed.

    No-op (returns False) when the alias is not registered. Bumps
    ``generated_at`` only when something actually changed.
    """
    if not isinstance(device, str) or not device:
        raise ValueError("device must be a non-empty string")
    p = _resolve_path(path)
    with _map_lock(p):
        data = load_device_map(p)
        devices = data.get("devices") or {}
        if device not in devices:
            return False
        devices.pop(device)
        data["devices"] = devices
        _bump_generated_at(data)
        _write_map(p, data)
        return True


__all__ = [
    "default_device_map_path",
    "load_device_map",
    "list_device_aliases",
    "get_device_entry",
    "resolve_mgmt0",
    "resolve_canonical",
    "get_aliases",
    "add_alias",
    "remove_alias",
    "update_device",
    "rename_device",
    "remove_device",
]
