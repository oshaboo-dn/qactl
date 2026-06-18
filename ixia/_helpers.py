from __future__ import annotations

from fnmatch import fnmatch
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Safe type conversions
# ---------------------------------------------------------------------------

def safe_float(val: Any, default: float = 0.0) -> float:
    """Convert any value to float, handling None, 'N/A', empty strings."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val: Any, default: int = 0) -> int:
    """Convert any value to int, handling float strings like '12500.000'."""
    if val is None:
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Raw REST helpers (bypass RestPy scope leaks)
# ---------------------------------------------------------------------------

def raw_read(ixn: Any, href: str) -> Any:
    """Read a raw REST href via ixn._connection._read(). Returns dict or list."""
    return ixn._connection._read(href)


def mv_values(ixn: Any, href: str, take: Optional[int] = None) -> Any:
    """Resolve a multivalue href to its actual value list.

    Returns single string if one value, list if multiple, None if empty.
    """
    if not href:
        return None
    url = f"{href}?skip=0&take={take}" if take is not None else href
    try:
        data = raw_read(ixn, url)
    except Exception:
        return None
    if isinstance(data, list):
        vals = data
    elif isinstance(data, dict):
        vals = data.get("values", [])
    else:
        return None
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    return vals


def href_mv(ixn: Any, obj: dict, key: str) -> Any:
    """Extract a multivalue from a dict key that contains an href reference.

    The value at obj[key] may be a string href or a dict with an 'href' key.
    """
    ref = obj.get(key)
    if not ref:
        return None
    if isinstance(ref, dict):
        ref = ref.get("href", "")
    if not ref or not isinstance(ref, str):
        return None
    return mv_values(ixn, ref)


# ---------------------------------------------------------------------------
# Multivalue read helper (RestPy objects)
# ---------------------------------------------------------------------------

def read_multivalue(mv_obj: Any, ixn: Any, take: int = 50) -> Any:
    """Read a RestPy multivalue object.

    Tries .Values first, falls back to raw REST.
    Returns unwrapped Python value (str if single, list if multiple, None if empty).
    """
    if mv_obj is None:
        return None
    try:
        vals = mv_obj.Values
        if vals is not None:
            return vals[0] if len(vals) == 1 else list(vals)
    except Exception:
        pass

    href = getattr(mv_obj, "href", None) or getattr(mv_obj, "Href", None)
    if href:
        return mv_values(ixn, href, take=take)
    return None


# ---------------------------------------------------------------------------
# Glob/fnmatch helper
# ---------------------------------------------------------------------------

def match_names(names: list[str], pattern: str) -> list[str]:
    """Filter names by glob pattern (fnmatch). Supports * and ? wildcards."""
    return [n for n in names if fnmatch(n, pattern)]
