"""Vendor plugin registry + per-device resolution.

Maps a vendor name to its :class:`~dnctl.cli.vendors.base.VendorPlugin`
and resolves the plugin (and its :class:`Dialect`) for a registry device
by reading the ``vendor`` field off the canonical device map. A device
with no recorded vendor — or a ``--host`` call with no registry entry —
resolves to DNOS, so legacy behaviour is unchanged.
"""

from __future__ import annotations

from typing import Dict, Optional

from qactl.dnctl.cli.vendors import arista as _arista
from qactl.dnctl.cli.vendors import cisco as _cisco
from qactl.dnctl.cli.vendors import dnos as _dnos
from qactl.dnctl.cli.vendors import juniper as _juniper
from qactl.dnctl.cli.vendors.base import Dialect, VendorPlugin

DEFAULT_VENDOR = "dnos"

_PLUGINS: Dict[str, VendorPlugin] = {
    "dnos": _dnos.PLUGIN,
    "cisco": _cisco.PLUGIN,
    "juniper": _juniper.PLUGIN,
    "arista": _arista.PLUGIN,
}


def supported_vendors() -> tuple:
    """Names of the vendors with a registered plugin."""
    return tuple(_PLUGINS.keys())


def get_plugin(vendor: Optional[str]) -> VendorPlugin:
    """Return the plugin for ``vendor`` (case-insensitive), DNOS as fallback.

    An unknown / empty vendor falls back to DNOS so the transport path
    never loses a dialect — the device map is the source of truth and a
    missing ``vendor`` field predates multi-vendor support (all such
    devices are DNOS).
    """
    if not vendor or not isinstance(vendor, str):
        return _PLUGINS[DEFAULT_VENDOR]
    return _PLUGINS.get(vendor.strip().lower(), _PLUGINS[DEFAULT_VENDOR])


def vendor_for_device(
    device: Optional[str], host: Optional[str] = None
) -> str:
    """Resolve the vendor name for a ``-d <device>`` (or ``--host``) call.

    Reads the ``vendor`` field off the canonical device entry. A
    host-only call (no registry device) or an entry without a vendor
    resolves to DNOS.
    """
    if not device:
        return DEFAULT_VENDOR
    from qactl.dnctl.core import devices as _dn_devices

    entry = _dn_devices.get_device_entry(device) or {}
    vendor = entry.get("vendor") if isinstance(entry, dict) else None
    return (vendor or DEFAULT_VENDOR).strip().lower()


def plugin_for_device(
    device: Optional[str], host: Optional[str] = None
) -> VendorPlugin:
    """The :class:`VendorPlugin` for a device (DNOS for unknown/host-only)."""
    return get_plugin(vendor_for_device(device, host))


def dialect_for_device(
    device: Optional[str], host: Optional[str] = None
) -> Dialect:
    """The SSH :class:`Dialect` for a device (DNOS for unknown/host-only)."""
    return plugin_for_device(device, host).dialect
