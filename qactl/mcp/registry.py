"""Group -> MCP tool surface map, and the registration plumbing.

The merged repo has one shared tool layer. Each surface's tools already
ship a ``register(mcp)`` function (native ``qactl.{jira,confluence,jenkins}
.tools``; vendored ``dnctl.*.tools`` and ``ixia_tools`` kept theirs from
their MCP days). This module re-exposes those over a stdio FastMCP server,
applying:

  * the **surface map** -- which tools each group exposes over MCP. A tool
    is kept **CLI-only** only when it is *interactive* (e.g. the one-time
    ``setup`` credential flow) or *destructive without a confirm gate*.
    The only tools left in that bucket are the long image-staging /
    deploy ops (``request_system_tar_load`` / ``scale_deploy``), which
    mutate the box and don't yet take a ``confirm=true`` argument. Device
    + NETCONF backup/restore ARE exposed -- backups are non-destructive
    and the restores already gate execution behind ``confirm=true``
    (``confirm=false`` returns a dry-run). Fire-and-forget kickoffs
    (tech-support) and cheap read-only / job-poll calls also stay on MCP:
    their artifacts land on remote dnftp, never in the local agent
    context. The CLI-only set lives in :data:`CLI_ONLY` and is skipped
    here.
  * an optional per-tool **wrap** (the JSONL request logger).

Registration goes through :class:`_Selector`, a thin proxy around the real
FastMCP instance that intercepts ``mcp.tool()`` so it can drop CLI-only
tools and wrap the rest -- letting us reuse every existing ``register()``
unchanged.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Callable, Dict, List, Optional, Set


# --- group sets ------------------------------------------------------------

NATIVE_GROUPS = ("jira", "confluence", "jenkins", "arista")
DNCTL_GROUPS = ("cli", "nc", "gnmi", "rc")
IXIA_GROUPS = ("ixia",)
ALL_GROUPS = NATIVE_GROUPS + DNCTL_GROUPS + IXIA_GROUPS

_DNCTL_PKG: Dict[str, str] = {
    "cli": "qactl.dnctl.cli.tools",
    "nc": "qactl.dnctl.nc.tools",
    "gnmi": "qactl.dnctl.gnmi.tools",
    "rc": "qactl.dnctl.rc.tools",
}


# --- CLI-only surface (skipped on MCP) -------------------------------------
# These keep a CLI front but are NOT exposed over MCP. The bar for hiding a
# tool: it is either *interactive* (no clean one-shot mapping) or it
# *writes a large config to the device* in a long, destructive way. "Output
# lands on remote dnftp" is NOT a reason to hide a tool — that data never
# touches the local agent context, so a fire-and-forget kickoff or a cheap
# read-only / job-poll call is perfectly MCP-shaped. Keyed by group, valued
# by tool function name.
CLI_ONLY: Dict[str, Set[str]] = {
    "cli": {
        # scale config deploy (heavy, destructive, not yet confirm-gated).
        # request_system_tar_load USED to live here too, but it grew a
        # confirm=true gate (fire-and-forget kickoff returns immediately)
        # so it's now MCP-shaped and exposed.
        "scale_deploy",
    },
}


class _Selector:
    """Proxy around a FastMCP instance used during ``register(mcp)``.

    Intercepts ``mcp.tool(...)`` so each registered tool can be (a) skipped
    when CLI-only and (b) wrapped (request logging) before it reaches the
    real FastMCP. Records what it registered/skipped for diagnostics.
    """

    def __init__(self, mcp, *, skip: Set[str] = frozenset(),
                 wrap: Optional[Callable] = None) -> None:
        self._mcp = mcp
        self._skip = set(skip)
        self._wrap = wrap
        self.registered: List[str] = []
        self.skipped: List[str] = []

    def tool(self, *args, **kwargs):
        deco = self._mcp.tool(*args, **kwargs)

        def apply(fn):
            name = getattr(fn, "__name__", "?")
            if name in self._skip:
                self.skipped.append(name)
                return fn
            self.registered.append(name)
            wrapped = self._wrap(fn) if self._wrap else fn
            return deco(wrapped)

        return apply


def _register_package(pkg_name: str, sel: _Selector) -> None:
    """Call ``register(sel)`` on every submodule of ``pkg_name`` that has one."""
    pkg = importlib.import_module(pkg_name)
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"{pkg_name}.{mod_info.name}")
        reg = getattr(module, "register", None)
        if callable(reg):
            reg(sel)


def register_group(group: str, mcp, *, wrap: Optional[Callable] = None) -> List[str]:
    """Register ``group``'s MCP tools onto ``mcp``; return the tool names."""
    if group in NATIVE_GROUPS:
        module = importlib.import_module(f"qactl.{group}.tools")
        sel = _Selector(mcp, wrap=wrap)
        module.register(sel)
        return sel.registered
    if group in DNCTL_GROUPS:
        sel = _Selector(mcp, skip=CLI_ONLY.get(group, set()), wrap=wrap)
        _register_package(_DNCTL_PKG[group], sel)
        return sel.registered
    if group in IXIA_GROUPS:
        sel = _Selector(mcp, wrap=wrap)
        _register_package("ixia_tools", sel)
        return sel.registered
    raise ValueError(f"unknown MCP group {group!r}; choose from {', '.join(ALL_GROUPS)}")


class _DummyMCP:
    """A no-op MCP used to enumerate a group's tools without FastMCP."""

    def tool(self, *args, **kwargs):
        def deco(fn):
            return fn
        return deco


def list_group_tools(group: str) -> List[str]:
    """Return the MCP tool names a group would expose (no FastMCP needed)."""
    return sorted(register_group(group, _DummyMCP()))
