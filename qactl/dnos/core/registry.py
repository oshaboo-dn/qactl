"""Device registry — the single shared alias → mgmt-IP / SN map.

This is the ``core/registry.py`` of the qactl architecture: one device
map shared by every subcommand group (``cli`` / ``nc`` / ``gnmi`` /
``rc``). The actual read/write implementation is lifted verbatim from
the monorepo's ``dn_common.devices`` and lives next door in
:mod:`qactl.core.devices`; this module is the stable public façade the
front-end imports.
"""

from __future__ import annotations

from qactl.dnos.core.devices import (
    add_alias,
    default_device_map_path,
    get_aliases,
    get_device_entry,
    list_device_aliases,
    load_device_map,
    remove_alias,
    remove_device,
    resolve_canonical,
    resolve_mgmt0,
    update_device,
)

__all__ = [
    "add_alias",
    "default_device_map_path",
    "get_aliases",
    "get_device_entry",
    "list_device_aliases",
    "load_device_map",
    "remove_alias",
    "remove_device",
    "resolve_canonical",
    "resolve_mgmt0",
    "update_device",
]
