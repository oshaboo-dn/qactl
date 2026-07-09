"""Vendor plugin model: capabilities + SSH CLI dialect.

qactl's cli-mcp grew up speaking only DNOS. To reach Cisco / Juniper
boxes too, the per-vendor differences are isolated behind a small
plugin:

- :class:`Dialect` carries the bits the transport layer needs to drive a
  fresh SSH channel for a given vendor — the prompt regexes (detection +
  trailing-prompt trim) and the command(s) that disable terminal
  pagination. The DNOS defaults live in ``qactl.cli.core.shell``; a
  plugin overrides them.
- :class:`VendorPlugin` bundles the dialect with the vendor's
  capability set (which tool families it supports) and its error
  detector (the regexes that mean "the device rejected this command").

The registry in :mod:`qactl.cli.vendors` maps a vendor name
(``"dnos"`` / ``"cisco"`` / ``"juniper"``) to its plugin, and resolves
the plugin for a registry device by reading the ``vendor`` field off the
canonical device map.

Capability tokens are coarse tool *families* (one per user-facing
command group), so gating is "does vendor X support family Y?" rather
than enumerating every command. DNOS supports everything; Cisco /
Juniper are read-only ``show`` for now (issue: multi-vendor shows, no
edit-config).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, FrozenSet, List, Optional, Pattern, Tuple


# ---------------------------------------------------------------------------
# Capability tokens — coarse tool families a vendor may or may not support.
# ---------------------------------------------------------------------------

CAP_SHOW = "show"                  # `show <...>` operational reads
CAP_SHOW_CONFIG = "show_config"    # `show config` / running-config reads
CAP_SYSTEM = "system"              # `show system` summary
CAP_INTERFACES = "interfaces"      # structured interface views
CAP_DISCOVERY = "discovery"        # search / help / crawl grammar walkers
CAP_PING = "ping"                  # `run ping` style reachability
CAP_LOGS = "logs"                  # log / trace / accounting readers
CAP_SHELL = "shell"                # Linux / NCM shell passthrough
CAP_RAW = "raw"                    # raw CLI line passthrough
CAP_CLEAR = "clear"                # operational `clear ...`
CAP_CONFIGURE = "configure"        # candidate edit + commit
CAP_BACKUP = "backup"              # config backup / restore
CAP_TECHSUPPORT = "techsupport"    # tech-support bundle generation
CAP_TARLOAD = "tarload"            # image tar-load / pre-check
CAP_RESTART = "restart"            # restart / switchover / kill
CAP_FACTORY_DEFAULT = "factory_default"

# Every capability the tool surface knows about. DNOS supports all of
# these; a new tool family should add its token here and to the DNOS
# plugin so the gate keeps treating DNOS as fully-featured.
ALL_CAPABILITIES: Tuple[str, ...] = (
    CAP_SHOW,
    CAP_SHOW_CONFIG,
    CAP_SYSTEM,
    CAP_INTERFACES,
    CAP_DISCOVERY,
    CAP_PING,
    CAP_LOGS,
    CAP_SHELL,
    CAP_RAW,
    CAP_CLEAR,
    CAP_CONFIGURE,
    CAP_BACKUP,
    CAP_TECHSUPPORT,
    CAP_TARLOAD,
    CAP_RESTART,
    CAP_FACTORY_DEFAULT,
)


@dataclass(frozen=True)
class Dialect:
    """Per-vendor knobs the SSH transport layer needs for a fresh channel.

    ``prompt_re`` matches the device's prompt at the very end of a buffer
    (must expose a named group ``prompt``); ``tail_re`` matches a single
    trailing prompt line for output trimming. ``page_off`` is the ordered
    list of init commands that disable output pagination (so a long
    ``show`` doesn't stall on a ``--More--`` pager). ``strip_paren_context``
    drops a trailing ``(...)`` block from the detected prompt (DNOS
    repaints a timestamp/context there; Cisco config mode shows
    ``(config)``) so prompt matching survives the context changing
    between reads.
    """

    name: str
    prompt_re: Pattern
    tail_re: Pattern
    page_off: Tuple[str, ...]
    strip_paren_context: bool = True


@dataclass(frozen=True)
class VendorPlugin:
    """A vendor's capability set + SSH dialect + error detector."""

    name: str
    capabilities: FrozenSet[str]
    dialect: Dialect
    # Patterns that, matched against a line of command output, mean the
    # device rejected the command (unknown/invalid/ambiguous/...).
    error_patterns: Tuple[Pattern, ...] = field(default_factory=tuple)

    def supports(self, capability: str) -> bool:
        """True when this vendor supports the given tool-family capability."""
        return capability in self.capabilities

    def detect_error(self, output: str) -> Tuple[bool, List[str]]:
        """Return ``(is_error, matched_lines)`` for a command's output.

        Mirrors :func:`qactl.cli.core.errors.detect_error` but uses this
        vendor's own patterns. A vendor with no patterns never reports an
        error from output (the transport-level timeout still applies).
        """
        if not output:
            return False, []
        hits: List[str] = []
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            for rx in self.error_patterns:
                if rx.search(stripped):
                    hits.append(stripped)
                    break
        return bool(hits), hits


def compile_patterns(*patterns: str) -> Tuple[Pattern, ...]:
    """Compile a list of regex strings (convenience for plugin modules)."""
    return tuple(re.compile(p) for p in patterns)
