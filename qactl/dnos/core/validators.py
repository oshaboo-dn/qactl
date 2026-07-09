"""Shared input validators for safe-token / bucket-name / device-name patterns.

Three layers:

- :func:`validate_safe_token` — generic engine. Validates that a string
  is a "safe filesystem-ish token": starts with alnum, contains only
  ``[A-Za-z0-9._-]``, no ``__`` (reserved separator), no path
  separators, within a length budget.

- :func:`validate_device_name` and :func:`validate_bucket_name` — thin
  wrappers around the engine with the per-use-case length budget and
  error label baked in. cli-mcp's backup-store and cert-store tools
  both call these instead of redefining the regex.

- :func:`default_date_bucket` — today's UTC date as ``YYYY-MM-DD``.
  Both backup_store and cert_store use this as the default bucket
  name when the caller omits ``bucket=``.

Per-use-case extras (e.g. backup_store rejecting bucket names that
parse as canonical backup filenames) stay in the per-use-case file —
this module only owns the shared base contract.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional


# Common charset across cli-mcp stores: alnum + ``._-``, must start
# with alnum so a name doesn't begin with a sneaky ``-`` or ``.`` that
# could trip a CLI flag parser. Length is enforced separately so the
# same regex works for 40-char devices and 60-char buckets.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_safe_token(
    value: object,
    *,
    label: str = "value",
    max_len: int,
    allow_dunders: bool = False,
    allow_path_separators: bool = False,
) -> Optional[str]:
    """Generic safe-token validator. Returns an error string or ``None``.

    Args:
        value: The candidate string. Anything not ``str`` (or empty)
            fails up front with a fixed message.
        label: How to refer to the field in the returned error
            (e.g. ``"device"``, ``"bucket"``, ``"name"``).
        max_len: Maximum length, INCLUSIVE.
        allow_dunders: ``False`` (default) rejects ``__`` anywhere in
            the value. cli-mcp uses ``__`` as a filename separator
            (e.g. ``<device>__<ts>.tar.gz``), so bucket / device /
            cert names must NOT contain it.
        allow_path_separators: ``False`` (default) rejects ``/`` and
            ``\\``. The returned token typically lands as a filename
            or single directory level.
    """
    if not isinstance(value, str) or not value:
        return f"{label} must be a non-empty string."
    if len(value) > max_len:
        return f"{label} too long (> {max_len} chars)."
    if not _TOKEN_RE.match(value):
        return (
            f"{label} must start with alnum and contain only "
            f"[A-Za-z0-9._-]."
        )
    if not allow_dunders and "__" in value:
        return f"{label} must not contain '__' (reserved as field separator)."
    if not allow_path_separators and ("/" in value or "\\" in value):
        return f"{label} must not contain path separators."
    return None


# Tighter budgets match what cli-mcp's stores already document.
DEVICE_NAME_MAX = 40
BUCKET_NAME_MAX = 60


def validate_device_name(device: object) -> Optional[str]:
    """Device-name shape (alnum + ``._-``, ≤40 chars, no ``__``)."""
    return validate_safe_token(device, label="device name", max_len=DEVICE_NAME_MAX)


def validate_bucket_name(bucket: Optional[object]) -> Optional[str]:
    """Bucket-name shape (alnum + ``._-``, ≤60 chars, no ``__``, no ``/``).

    ``None`` is always valid — it means "default / no bucket". Callers
    that need a special "root only" sentinel like the literal string
    ``"-"`` handle that separately.
    """
    if bucket is None:
        return None
    return validate_safe_token(bucket, label="bucket", max_len=BUCKET_NAME_MAX)


def default_date_bucket() -> str:
    """Today's UTC date as ``YYYY-MM-DD``. Used as the implicit bucket."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


__all__ = [
    "validate_safe_token",
    "validate_device_name",
    "validate_bucket_name",
    "default_date_bucket",
    "DEVICE_NAME_MAX",
    "BUCKET_NAME_MAX",
]
