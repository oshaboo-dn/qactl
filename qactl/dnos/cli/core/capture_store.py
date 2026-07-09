"""Local landing store for ``qactl cli capture`` pcaps.

A capture pcap is produced on a DNOS device (control-plane tcpdump or the
datapath wbox-cli engine) and pushed to **this host** with an in-shell
``scp`` to the local-sftp endpoint — the same machine / account
``cli backup`` dials back into (see :mod:`qactl.core.local_sftp`). This
module owns the directory tree the device SFTPs into and the plain local
file I/O used to verify / locate the landed pcap afterwards.

Layout, rooted at the device alias (mirrors
:mod:`qactl.cli.core.backup_store`)::

    <state_dir>/captures/cli/<device>/<prefix>_<device>_<UTCish>.pcap

The device folder is auto-created before the upload — the sftp-server
won't create parents on demand, so a missing dir would surface as an
opaque ``Failure`` from the device-side scp.
"""

from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass
from typing import Optional

from qactl.dnos.core.paths import state_dir
from qactl.dnos.core.validators import validate_device_name

# pcap filename shape produced by :func:`capture_helpers.make_pcap_name`
# (``<prefix>_<device>_<YYYYmmdd_HHMMSS>.pcap``). Kept permissive but
# anchored + ``.pcap``-suffixed so a traversal / odd name can't slip
# through into a path join.
_PCAP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.pcap$")


def _root() -> str:
    """Local capture root: ``<state_dir>/captures/cli`` (honours $QACTL_STATE_DIR)."""
    return str(state_dir("captures") / "cli")


@dataclass(frozen=True)
class CaptureFile:
    """Parsed view of one landed pcap on the local disk."""

    filename: str
    device: str
    size_bytes: int
    path: str


def validate_device(device: str) -> Optional[str]:
    """Return an error string if ``device`` is not a legal folder segment."""
    return validate_device_name(device)


def validate_name(filename: str) -> Optional[str]:
    """Return an error string if ``filename`` is not a safe pcap name."""
    if not isinstance(filename, str) or not _PCAP_NAME_RE.match(filename):
        return f"{filename!r} is not a canonical <...>.pcap capture name."
    return None


def _device_dir(device: str) -> str:
    return posixpath.join(_root(), device)


def _file_path(filename: str, device: str) -> str:
    return posixpath.join(_device_dir(device), filename)


def remote_path(filename: str, *, device: str) -> str:
    """Absolute local path the device scp's the pcap to (and we stat after).

    Pass the ``<user>@<host>:`` prefix from :mod:`qactl.core.local_sftp`;
    this is the ``<path>`` half.
    """
    return _file_path(filename, device)


def ensure_dir(*, device: str) -> None:
    """Create ``<root>/<device>/`` locally so the device-side scp can land.

    Idempotent; raises :class:`ValueError` on an invalid device name.
    """
    err = validate_device(device)
    if err:
        raise ValueError(err)
    os.makedirs(_device_dir(device), exist_ok=True)


def stat_pcap(filename: str, *, device: str) -> Optional[CaptureFile]:
    """Local-stat a landed pcap. ``None`` if absent or the name/device is bad."""
    if validate_name(filename) is not None:
        return None
    if validate_device(device) is not None:
        return None
    try:
        size = os.stat(_file_path(filename, device)).st_size
    except OSError:
        return None
    return CaptureFile(
        filename=filename,
        device=device,
        size_bytes=int(size),
        path=_file_path(filename, device),
    )


__all__ = [
    "CaptureFile",
    "validate_device",
    "validate_name",
    "remote_path",
    "ensure_dir",
    "stat_pcap",
]
