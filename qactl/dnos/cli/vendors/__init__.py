"""Vendor plugin layer for cli-mcp (dnos / cisco / juniper).

Public surface:

- capability tokens + :class:`Dialect` / :class:`VendorPlugin` (``base``)
- the registry + resolvers: :func:`get_plugin`, :func:`vendor_for_device`,
  :func:`plugin_for_device`, :func:`dialect_for_device` (``registry``)
- the tool capability gate: :func:`requires`, :func:`unsupported_response`
  (``gate``)
"""

from __future__ import annotations

from qactl.dnos.cli.vendors.base import (
    ALL_CAPABILITIES,
    CAP_BACKUP,
    CAP_CLEAR,
    CAP_CONFIGURE,
    CAP_DISCOVERY,
    CAP_FACTORY_DEFAULT,
    CAP_INTERFACES,
    CAP_LOGS,
    CAP_PING,
    CAP_RAW,
    CAP_RESTART,
    CAP_RUN,
    CAP_SHELL,
    CAP_SHOW,
    CAP_SHOW_CONFIG,
    CAP_SYSTEM,
    CAP_TARLOAD,
    CAP_TECHSUPPORT,
    Dialect,
    VendorPlugin,
)
from qactl.dnos.cli.vendors.gate import requires, unsupported_response
from qactl.dnos.cli.vendors.registry import (
    DEFAULT_VENDOR,
    dialect_for_device,
    get_plugin,
    plugin_for_device,
    supported_vendors,
    vendor_for_device,
)

__all__ = [
    "ALL_CAPABILITIES",
    "CAP_BACKUP",
    "CAP_CLEAR",
    "CAP_CONFIGURE",
    "CAP_DISCOVERY",
    "CAP_FACTORY_DEFAULT",
    "CAP_INTERFACES",
    "CAP_LOGS",
    "CAP_PING",
    "CAP_RAW",
    "CAP_RESTART",
    "CAP_RUN",
    "CAP_SHELL",
    "CAP_SHOW",
    "CAP_SHOW_CONFIG",
    "CAP_SYSTEM",
    "CAP_TARLOAD",
    "CAP_TECHSUPPORT",
    "Dialect",
    "VendorPlugin",
    "DEFAULT_VENDOR",
    "dialect_for_device",
    "get_plugin",
    "plugin_for_device",
    "supported_vendors",
    "vendor_for_device",
    "requires",
    "unsupported_response",
]
