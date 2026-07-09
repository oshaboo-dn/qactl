"""On-disk state for the event collector (``qactl cli monitor tick``).

The collector is a **bounded one-shot**: each ``tick`` reads new device
events, alerts on them, and exits — so the "what have I already seen?"
memory must survive between separate CLI invocations. That state lives
here as a single small JSON file under the CLI state dir (same home as
:mod:`dnctl.cli.core.job_store`):

    {
      "version": 1,
      "devices": {
        "<canonical>": {
          "cursor": "<iso-8601 ts of newest event handled>",
          "seen":   ["<fingerprint>", ...]   # bounded ring of recent ids
        }
      }
    }

``cursor`` lets the next tick ask the device only for events ``since`` the
last one handled; ``seen`` dedupes across the deliberate lookback overlap
(so a line straddling two windows alerts once). It stores no secrets and
never touches the device — purely a local cache. Writes are atomic
(temp + rename) and best-effort; a lost state file just means the next
tick re-reads its lookback window (dedupe still mostly holds via cursor).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List, Optional

from qactl.dnctl.core import paths as _paths

_FILENAME = "events-spool.json"
_SUBDIR = "monitor"
# Keep at most this many recent fingerprints per device — enough to cover a
# lookback window's worth of overlap without growing unbounded.
_SEEN_CAP = 2000

# Serialize read-modify-write within a process; cross-process races just
# fall back to last-writer-wins on the atomic rename, which is acceptable
# for a dedupe cache.
_LOCK = threading.Lock()


def _path() -> str:
    return os.path.join(str(_paths.state_dir("cli")), _SUBDIR, _FILENAME)


def load(path: Optional[str] = None) -> Dict[str, Any]:
    """Load the spool, returning a well-formed empty state on any error."""
    p = path or _path()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    devs = data.get("devices")
    if not isinstance(devs, dict):
        data["devices"] = {}
    return data


def save(state: Dict[str, Any], path: Optional[str] = None) -> None:
    """Persist ``state`` atomically. Best-effort (errors swallowed)."""
    p = path or _path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError:
        pass


def _dev(state: Dict[str, Any], device: str) -> Dict[str, Any]:
    devs = state.setdefault("devices", {})
    entry = devs.get(device)
    if not isinstance(entry, dict):
        entry = {"cursor": None, "seen": []}
        devs[device] = entry
    if not isinstance(entry.get("seen"), list):
        entry["seen"] = []
    return entry


def get_cursor(state: Dict[str, Any], device: str) -> Optional[str]:
    """Return the newest event timestamp handled for ``device``, or None."""
    entry = state.get("devices", {}).get(device)
    if isinstance(entry, dict):
        cur = entry.get("cursor")
        return cur if isinstance(cur, str) and cur else None
    return None


def get_links(state: Dict[str, Any], device: str) -> Optional[Dict[str, str]]:
    """Return the last interface oper-status snapshot for ``device``.

    ``None`` means "no baseline yet" — the gNMI link source treats that as
    "establish a baseline this tick, don't alert".
    """
    entry = state.get("devices", {}).get(device)
    if isinstance(entry, dict):
        links = entry.get("links")
        if isinstance(links, dict):
            return {str(k): str(v) for k, v in links.items()}
    return None


def set_links(state: Dict[str, Any], device: str, links: Dict[str, str]) -> None:
    """Store the current interface oper-status snapshot for ``device``."""
    entry = _dev(state, device)
    entry["links"] = {str(k): str(v) for k, v in links.items()}


def is_new(state: Dict[str, Any], device: str, fingerprint: str) -> bool:
    """True if ``fingerprint`` has not been seen for ``device`` yet."""
    entry = state.get("devices", {}).get(device)
    if not isinstance(entry, dict):
        return True
    return fingerprint not in (entry.get("seen") or [])


def record(
    state: Dict[str, Any],
    device: str,
    fingerprints: List[str],
    cursor: Optional[str],
) -> None:
    """Mark ``fingerprints`` seen for ``device`` and advance its cursor.

    The cursor only ever moves forward (lexicographic compare on the
    ISO-8601 timestamp). The seen-list is capped to the most recent
    :data:`_SEEN_CAP` ids.
    """
    entry = _dev(state, device)
    seen: List[str] = entry["seen"]
    have = set(seen)
    for fp in fingerprints:
        if fp not in have:
            seen.append(fp)
            have.add(fp)
    if len(seen) > _SEEN_CAP:
        entry["seen"] = seen[-_SEEN_CAP:]
    if cursor and (not entry.get("cursor") or cursor > entry["cursor"]):
        entry["cursor"] = cursor


def reset(state: Dict[str, Any], device: Optional[str] = None) -> None:
    """Clear collector memory for ``device`` (or all devices if None)."""
    if device is None:
        state["devices"] = {}
    else:
        state.get("devices", {}).pop(device, None)


__all__ = [
    "load", "save", "get_cursor", "get_links", "set_links",
    "is_new", "record", "reset", "_LOCK", "_SEEN_CAP",
]
