"""Local backup file store for cli config snapshots.

A cli backup is a single saved DNOS config (plain text, a few KB). The
device produces it with ``save <filename>`` and then pushes it to **this
host** with ``request file upload config <filename> <user>@<host>:<path>
protocol sftp`` — i.e. the device SFTPs the file into the directory tree
this module owns, on the machine running ``dnctl``. Listing, reading,
verifying, and deleting are plain local file I/O; restore points the
device's ``request file download`` back at the same local path. The
shared external ``dnftp`` host is reserved for the large device-pushed
tech-support tarballs (see :mod:`dnctl.cli.core.ts_store`).

The *self* target the device dials back into (host / user / password /
VRF) is resolved at runtime by :mod:`dnctl.core.local_sftp` so the same
code works on any user's machine.

Layout
------

The on-disk tree is **always rooted at the device alias**::

    <BACKUP_DIR>/<device>/                              # device root
    ├── <device>__<UTC>__<desc>.md                      # bucket=None
    └── <bucket>/                                       # optional sub-bucket
        └── <device>__<UTC>__<desc>.md

- ``device`` (mandatory) is the cli alias (``cl``, ``sa``, …) and is the
  **implicit top level** — every helper derives the device folder from
  it; callers never pass the device as a bucket. This makes "show me
  everything for `cl`" a single ``scandir`` and gives each device a
  stable home for retention / pruning policies.
- ``bucket`` (optional, default ``None``) is a one-level sub-folder
  *under* the device root, used to group captures by purpose
  (``"nightly"`` for the scheduled job, ``"bug-1234-repro"`` for ad-hoc
  investigation captures, …). ``bucket=None`` lands the file directly
  in the device root.

Bucket names are sanitised to ``[A-Za-z0-9._-]{1,60}``, may not contain
``__`` (reserved as the filename separator), may not contain ``/`` (one
level only), and must not match the canonical filename shape (so a
bucket can't be confused for a backup file).

Filename convention::

    <device>__<YYYYMMDD-HHMMSS>[__<desc>].md

- ``device`` prefix is **redundant with the folder** — but kept on
  purpose: ``restore_device`` uses the in-filename device prefix as a
  belt-and-suspenders safety check (refuses to apply ``cl__...md`` to
  ``device="sa"`` even if someone manually moved the file into
  ``BACKUP_DIR/sa/``).
- ``YYYYMMDD-HHMMSS`` is UTC, generated at backup time.
- ``description`` is optional, sanitised to ``[A-Za-z0-9._-]{1,40}``.
  Missing description drops the trailing ``__<description>`` segment.
- ``.md`` suffix is cosmetic — DNOS treats the filename as opaque, but the
  extension makes editors / IDEs render the saved config nicely.

DNOS ``save`` writes a plain text file with whatever name we hand it, and
``load override <name>`` accepts the same name verbatim, so the suffix
travels through both upload and restore unchanged.
"""

from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from qactl.dnctl.core.paths import state_dir


# --------------------------------------------------------------------------
# Backup-store layout. Backups land on the LOCAL filesystem under the
# state dir (honouring $DNCTL_STATE_DIR). ``BACKUP_HOST`` is a display
# sentinel kept so the list-envelope shape is unchanged from the old
# dnftp-backed store.
# --------------------------------------------------------------------------

BACKUP_HOST = "local"


def _root() -> str:
    """Local backup root: ``<state_dir>/backups/cli``."""
    return str(state_dir("backups") / "cli")


# Convenience snapshot used by the list envelope for display; the path
# helpers below call _root() dynamically so $DNCTL_STATE_DIR overrides
# still take effect at call time.
BACKUP_DIR = _root()


# --------------------------------------------------------------------------
# Filename grammar
# --------------------------------------------------------------------------

# Device aliases live in the canonical devices_mgmt0.json map and are
# alnum + ``._-``. We validate the filename against the same shape.
# Using non-greedy for device so the ``__`` separator splits cleanly
# even when the alias contains a single ``_`` (some lab chassis bake the
# SN into their System Name with single underscores, e.g.
# ``Slava_1_WK31C8V10001BP2``).
_FILENAME_RE = re.compile(
    r"^(?P<device>[A-Za-z0-9][A-Za-z0-9._-]*?)"
    r"__(?P<ts>\d{8}-\d{6})"
    r"(?:__(?P<description>[A-Za-z0-9][A-Za-z0-9._-]*))?"
    r"\.md$"
)

_DESCRIPTION_CLEAN_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DESCRIPTION_MAX = 40

# Device-name and bucket-name shapes come from dnctl.core.validators —
# the same patterns are used by any cli tool that lands files under a
# bucket. Local aliases keep the constants visible to existing call
# sites that read them.
from qactl.dnctl.core.validators import (
    BUCKET_NAME_MAX as _BUCKET_MAX,
    DEVICE_NAME_MAX as _DEVICE_MAX,
    validate_bucket_name as _validate_bucket_name,
    validate_device_name as _validate_device_name,
)


@dataclass(frozen=True)
class BackupFile:
    """Parsed view of one backup on the local disk.

    ``path`` is the absolute local path of the saved config under the
    backup root. ``bucket`` is ``None`` for files in the device root,
    or the sub-directory name for files in a sub-bucket.
    """

    filename: str
    device: str
    timestamp_utc: str              # "YYYY-MM-DDTHH:MM:SSZ"
    description: Optional[str]
    bucket: Optional[str]
    size_bytes: int
    path: str


def _utc_timestamp() -> str:
    """``YYYYMMDD-HHMMSS`` in UTC, used in backup filenames."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _sanitise_description(description: Optional[str]) -> Optional[str]:
    """Reduce description to the allowed charset, truncate, drop if empty."""
    if description is None:
        return None
    cleaned = _DESCRIPTION_CLEAN_RE.sub("_", description.strip()).strip("._-")
    if not cleaned:
        return None
    return cleaned[:_DESCRIPTION_MAX]


def validate_device(device: str) -> Optional[str]:
    """Return an error string if ``device`` is not a legal filename segment.

    Pass-through to :func:`dnctl.core.validators.validate_device_name`.
    """
    return _validate_device_name(device)


def validate_bucket(bucket: Optional[str]) -> Optional[str]:
    """Return an error string if ``bucket`` is not a legal sub-directory name.

    ``None`` is always valid (means "root"). The shared shape check
    (alnum + ``._-``, ≤60 chars, no ``__``, no ``/``) lives in
    :func:`dnctl.core.validators.validate_bucket_name`. The
    backup-store-specific extra constraint — the bucket must NOT also
    parse as a canonical backup filename, otherwise a file in the
    root could be confused with a sub-bucket — is enforced here.
    """
    err = _validate_bucket_name(bucket)
    if err:
        return err
    if bucket is not None and parse_filename(bucket) is not None:
        return (
            "bucket must not match the canonical backup filename shape "
            "(would be ambiguous with a file in the root bucket)."
        )
    return None


def make_filename(device: str, description: Optional[str] = None) -> str:
    """Build a canonical backup filename for ``device`` (description optional).

    Raises :class:`ValueError` if ``device`` is not a valid filename segment.
    ``description`` is quietly sanitised (illegal chars collapsed to ``_``;
    truncated to 40 chars; empty after sanitisation → dropped).
    """
    err = validate_device(device)
    if err:
        raise ValueError(err)
    ts = _utc_timestamp()
    clean_description = _sanitise_description(description)
    if clean_description:
        return f"{device}__{ts}__{clean_description}.md"
    return f"{device}__{ts}.md"


def parse_filename(filename: str) -> Optional[BackupFile]:
    """Parse a bare filename into :class:`BackupFile` without touching disk.

    Returns ``None`` if the name does not match the canonical shape. The
    returned :class:`BackupFile` has ``size_bytes=0``, ``bucket=None``,
    and an empty ``path`` — none of those are derivable from the bare
    filename alone (they all need the device folder + bucket the file
    actually lives in). Use :func:`stat_backup` for the populated form.
    """
    m = _FILENAME_RE.match(filename)
    if not m:
        return None
    ts_raw = m.group("ts")  # YYYYMMDD-HHMMSS
    iso = (
        f"{ts_raw[0:4]}-{ts_raw[4:6]}-{ts_raw[6:8]}T"
        f"{ts_raw[9:11]}:{ts_raw[11:13]}:{ts_raw[13:15]}Z"
    )
    return BackupFile(
        filename=filename,
        device=m.group("device"),
        timestamp_utc=iso,
        description=m.group("description"),
        bucket=None,
        size_bytes=0,
        path="",
    )


def _device_dir(device: str) -> str:
    """Absolute path of the per-device backup root on the local disk."""
    return posixpath.join(_root(), device)


def _bucket_dir(device: str, bucket: Optional[str]) -> str:
    """Absolute path of the directory holding backups for ``(device, bucket)``.

    ``bucket=None`` returns the device root; non-None returns the
    sub-bucket under it. The device dir is mandatory — there is no
    "loose under BACKUP_DIR" tier in this layout.
    """
    if bucket is None:
        return _device_dir(device)
    return posixpath.join(_device_dir(device), bucket)


def _file_path(filename: str, device: str, bucket: Optional[str]) -> str:
    """Absolute local path of a backup file."""
    return posixpath.join(_bucket_dir(device, bucket), filename)


def remote_path(
    filename: str, *, device: str, bucket: Optional[str] = None,
) -> str:
    """Absolute local path of a backup file.

    This is both the destination the device SFTPs the saved config to
    (the ``<path>`` in the device's ``request file upload ... protocol
    sftp`` command) and the path the store stats / reads afterwards.
    Pass to :func:`dnctl.core.dnftp.build_upload_command` /
    :func:`dnctl.core.dnftp.build_download_command` (with this host's
    ``user`` / ``host`` from :mod:`dnctl.core.local_sftp`).
    """
    return _file_path(filename, device, bucket)


def _build_backupfile(
    filename: str,
    size_bytes: int,
    device: str,
    bucket: Optional[str],
) -> Optional[BackupFile]:
    parsed = parse_filename(filename)
    if parsed is None:
        return None
    return BackupFile(
        filename=parsed.filename,
        device=parsed.device,
        timestamp_utc=parsed.timestamp_utc,
        description=parsed.description,
        bucket=bucket,
        size_bytes=size_bytes,
        path=_file_path(filename, device, bucket),
    )


def stat_backup(
    filename: str, *, device: str, bucket: Optional[str] = None,
) -> Optional[BackupFile]:
    """Parse + local-stat under ``(device, bucket)``.

    Returns ``None`` if the filename is malformed, ``device`` /
    ``bucket`` are invalid, or the file does not exist at the resolved
    local path.
    """
    if parse_filename(filename) is None:
        return None
    if validate_device(device) is not None:
        return None
    if validate_bucket(bucket) is not None:
        return None
    try:
        size = os.stat(_file_path(filename, device, bucket)).st_size
    except OSError:
        return None
    return _build_backupfile(filename, int(size), device, bucket)


def _list_one(device: str, bucket: Optional[str]) -> List[BackupFile]:
    """List canonical backups inside ``BACKUP_DIR/<device>[/<bucket>]``.

    Filters the entries to only those whose in-filename device prefix
    matches ``device`` — guards against a stray ``cl__...md`` somehow
    sitting under ``BACKUP_DIR/sa/`` (manual mv, migration bug, etc.)
    showing up when the caller asked for ``sa``'s history.
    """
    out: List[BackupFile] = []
    try:
        entries = list(os.scandir(_bucket_dir(device, bucket)))
    except OSError:
        return out
    for entry in entries:
        if not entry.is_file():
            continue
        backup = _build_backupfile(
            entry.name, int(entry.stat().st_size), device, bucket,
        )
        if backup is None:
            continue
        if backup.device != device:
            continue
        out.append(backup)
    return out


def _subdirs(path: str) -> List[str]:
    try:
        return [e.name for e in os.scandir(path) if e.is_dir()]
    except OSError:
        return []


def _list_device_buckets(device: str) -> List[str]:
    """Sub-bucket dir names under ``BACKUP_DIR/<device>/``, validator-filtered."""
    out = [
        name for name in _subdirs(_device_dir(device))
        if validate_bucket(name) is None
    ]
    out.sort()
    return out


def _list_devices() -> List[str]:
    """Top-level device dir names under ``BACKUP_DIR/``, validator-filtered."""
    out = [
        name for name in _subdirs(_root())
        if validate_device(name) is None
    ]
    out.sort()
    return out


def list_backups(
    device: Optional[str] = None,
    limit: Optional[int] = 100,
    bucket: Optional[str] = None,
) -> List[BackupFile]:
    """List backups under :data:`BACKUP_DIR`, newest first.

    Walking strategy:

    - ``device="<d>"``, ``bucket=None``: list ``BACKUP_DIR/<d>/`` AND
      every sub-bucket under it.
    - ``device="<d>"``, ``bucket="<b>"``: list ONLY
      ``BACKUP_DIR/<d>/<b>/``. Returns an empty list if it doesn't exist.
    - ``device=None``, ``bucket=None``: walk every device dir under
      ``BACKUP_DIR`` AND every sub-bucket under each device.
    - ``device=None``, ``bucket="<b>"``: walk every device, but only
      look at the ``<b>`` sub-bucket under each.

    Files that don't match the canonical name are silently skipped —
    callers that want to surface them should use :func:`list_orphans`.
    Each returned :class:`BackupFile` has a populated ``path`` and
    ``bucket`` (``None`` if the file was in the device root).
    """
    if device is not None and validate_device(device) is not None:
        return []
    if bucket is not None and validate_bucket(bucket) is not None:
        return []

    out: List[BackupFile] = []
    target_devices = [device] if device is not None else _list_devices()
    for d in target_devices:
        if bucket is not None:
            out.extend(_list_one(d, bucket))
        else:
            out.extend(_list_one(d, None))
            for b in _list_device_buckets(d):
                out.extend(_list_one(d, b))
    # Newest first — the timestamp segment is lex-sortable because of the
    # YYYYMMDD-HHMMSS shape. Tie-break on (device, bucket) so the order is
    # stable across calls.
    out.sort(
        key=lambda b: (b.filename, b.device, b.bucket or ""),
        reverse=True,
    )
    if limit is not None and limit > 0:
        out = out[:limit]
    return out


def list_buckets(device: Optional[str] = None) -> List[str]:
    """Return bucket directory names under ``BACKUP_DIR``.

    - ``device="<d>"``: sub-bucket names under ``BACKUP_DIR/<d>/``, sorted.
    - ``device=None``: top-level device-folder names under
      ``BACKUP_DIR/``, sorted.

    Excludes any entry that isn't a directory or whose name fails the
    relevant validator.
    """
    if device is not None and validate_device(device) is not None:
        return []
    if device is None:
        return _list_devices()
    return _list_device_buckets(device)


def list_orphans() -> List[str]:
    """Files under :data:`BACKUP_DIR` whose names or locations don't fit.

    Walks the full per-device tree and surfaces:

    - Top-level entries under ``BACKUP_DIR/`` that aren't a valid device
      dir (rendered as ``"<name>/"`` for dirs, ``"<name>"`` for files).
    - Files directly under any device dir whose names don't parse.
    - Files inside a sub-bucket whose names don't parse.
    - Sub-bucket entries (under a device dir) that don't parse as a valid
      bucket name (rendered as ``"<device>/<name>/"``).
    """
    out: List[str] = []
    try:
        top_entries = list(os.scandir(_root()))
    except OSError:
        return out
    device_dirs: List[str] = []
    for entry in top_entries:
        if entry.is_dir():
            if validate_device(entry.name) is None:
                device_dirs.append(entry.name)
            else:
                out.append(entry.name + "/")
            continue
        if entry.is_file():
            out.append(entry.name)
    for device in device_dirs:
        try:
            ddir_entries = list(os.scandir(_device_dir(device)))
        except OSError:
            continue
        sub_buckets: List[str] = []
        for entry in ddir_entries:
            if entry.is_dir():
                if validate_bucket(entry.name) is None:
                    sub_buckets.append(entry.name)
                else:
                    out.append(f"{device}/{entry.name}/")
                continue
            if not entry.is_file():
                continue
            if parse_filename(entry.name) is None:
                out.append(f"{device}/{entry.name}")
        for bucket in sub_buckets:
            try:
                sub_entries = list(os.scandir(_bucket_dir(device, bucket)))
            except OSError:
                continue
            for entry in sub_entries:
                if not entry.is_file():
                    continue
                if parse_filename(entry.name) is None:
                    out.append(f"{device}/{bucket}/{entry.name}")
    out.sort()
    return out


def ensure_dir(
    *, device: str, bucket: Optional[str] = None,
) -> None:
    """Make sure ``BACKUP_DIR/<device>[/<bucket>]`` exists locally.

    The device SFTPs into this directory directly — the sftp-server will
    not create parents on demand, so a missing dir manifests as a
    confusing ``Failure`` from DNOS. Call this right before kicking off
    an upload so the very first ``backup_device`` for a new device just
    works. Idempotent; safe to call repeatedly.
    """
    err = validate_device(device)
    if err:
        raise ValueError(err)
    if bucket is not None:
        b_err = validate_bucket(bucket)
        if b_err:
            raise ValueError(b_err)
    os.makedirs(_bucket_dir(device, bucket), exist_ok=True)


def download_bytes(
    filename: str, *, device: str, bucket: Optional[str] = None,
) -> bytes:
    """Read ``filename`` under ``(device, bucket)`` from the local disk."""
    if parse_filename(filename) is None:
        raise ValueError(
            f"refusing to read {filename!r}: not a canonical backup name."
        )
    d_err = validate_device(device)
    if d_err:
        raise ValueError(f"refusing to read with invalid device: {d_err}")
    b_err = validate_bucket(bucket)
    if b_err:
        raise ValueError(f"refusing to read with invalid bucket: {b_err}")
    with open(_file_path(filename, device, bucket), "rb") as fh:
        return fh.read()


def delete_backup(
    filename: str, *, device: str, bucket: Optional[str] = None,
) -> bool:
    """Delete a backup under ``(device, bucket)``. ``True`` if deleted,
    ``False`` if missing.

    Refuses to touch anything whose name doesn't parse, and rejects
    invalid device / bucket — a thin safety net against a caller
    smuggling a path traversal through any of the three.
    """
    if parse_filename(filename) is None:
        raise ValueError(
            f"refusing to delete {filename!r}: not a canonical backup name."
        )
    d_err = validate_device(device)
    if d_err:
        raise ValueError(f"refusing to delete with invalid device: {d_err}")
    b_err = validate_bucket(bucket)
    if b_err:
        raise ValueError(f"refusing to delete with invalid bucket: {b_err}")
    try:
        os.remove(_file_path(filename, device, bucket))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


__all__ = [
    "BACKUP_HOST",
    "BACKUP_DIR",
    "BackupFile",
    "validate_device",
    "validate_bucket",
    "make_filename",
    "parse_filename",
    "stat_backup",
    "list_backups",
    "list_buckets",
    "list_orphans",
    "ensure_dir",
    "remote_path",
    "download_bytes",
    "delete_backup",
]
