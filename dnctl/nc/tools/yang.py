"""YANG-related tools.

Two tools are exposed:

- ``netconf_yang_library`` — list modules advertised by the device's
  ``ietf-yang-library`` (RFC 8525 / 7895).
- ``netconf_get_schema`` — fetch one ``.yang`` source via ``<get-schema>``
  (RFC 6022). The full source is returned inline; pass ``--out-file`` to
  also write it to a path.

Together these are the primary on-demand path to schema knowledge:
discover module names with ``nc yang-library``, pull the source for the
ones you need with ``nc schema``, then hand-author XML for ``nc get`` /
``nc edit`` against the shapes you read. No cache, no preflight
validation — the device is the final authority on payload correctness.

``netconf_refresh_yang`` (bulk pre-fetch into ``yangs/<build>/<sub_build>/``)
is intentionally **not registered**. The code is preserved for a future
``fs-netconf-yangs`` cache — see ``TODO-fs-netconf-yangs.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from dnctl.nc.core.device_log import _begin, _log_action, _log_event
from dnctl.nc.core.netconf_rpc import get_schema_source, get_yang_library
from dnctl.nc.core.results import _base_result, _error_result
from dnctl.nc.core.session import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    ROOT_DIR,
    _connect_device,
    _session_id,
)

from dnctl.nc.yang._yang_io import YANGS_DIR as _YANGS_DIR
from dnctl.nc.yang.send import resolve_build_and_bootstrap as _yang_resolve_build


def netconf_yang_library(
    host: Optional[str] = None,
    device: Optional[str] = None,
    name_contains: Optional[str] = None,
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    no_verify: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """List YANG modules advertised by the device (ietf-yang-library).

    Step 1 of the agent's schema workflow. Returns one entry per module
    with ``name``, ``revision``, ``namespace``, ``conformance_type``,
    and nested ``submodules``. The ``namespace`` is the value the agent
    must put on the ``xmlns="..."`` attribute when wrapping that module's
    elements inside ``<drivenets-top>...</drivenets-top>``.

    ``name_contains`` filters modules whose name contains the given
    substring (case-sensitive). Use it to scope discovery — e.g.
    ``name_contains="dn-bgp"`` to find BGP-related modules,
    ``name_contains="dn-system"`` for system, ``name_contains="dn-if"``
    for interfaces.

    Typical follow-up: ``netconf_get_schema(identifier=<module-name>)``
    on a hit, then read the ``container`` / ``list`` / ``leaf`` shapes
    in the source to build XML for ``netconf_get`` / ``netconf_edit``.

    Example::

        netconf_yang_library(device="sa", name_contains="dn-system")
        # -> {modules: [{name: "dn-system",
        #                namespace: "http://drivenets.com/ns/yang/dn-system",
        #                revision: "2025-12-31", ...}]}
    """
    sid = _session_id()
    try:
        with _connect_device(host, device, port, user, password, no_verify, timeout) as cr:
            log_path = _begin(cr, sid, "yang-library", device=device)
            modules = get_yang_library(cr.mgr)
            if name_contains:
                modules = [m for m in modules if name_contains in (m.get("name") or "")]
            _log_action(
                log_path, "action", action="yang-library",
                modules=len(modules), filter=name_contains or "",
            )
            _log_event(log_path, sid, "end", status="ok")
            return _base_result(
                "yang-library", cr, sid,
                {
                    "status": "ok",
                    "modules_count": len(modules),
                    "modules": modules,
                },
            )
    except Exception as e:
        return _error_result("yang-library", sid, e)


def netconf_get_schema(
    identifier: str,
    host: Optional[str] = None,
    device: Optional[str] = None,
    version: str = "",
    out_file: Optional[str] = None,
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    no_verify: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Fetch a YANG module source from the device via <get-schema> (RFC 6022).

    Step 2 of the agent's schema workflow. Returns the literal ``.yang``
    source text in ``source``. Read the ``container`` / ``list`` /
    ``leaf`` / ``leaf-list`` definitions and any ``augment "/dn-top:..."``
    statement to derive the XML shape, and pair the module's own
    ``namespace`` (from ``netconf_yang_library``) with each top-level
    element you emit.

    Args:
        identifier: Module name as listed by ``netconf_yang_library``
            (e.g. ``dn-bgp``, ``dn-interfaces``, ``dn-system``,
            ``ietf-netconf``).
        version: Optional ``YYYY-MM-DD`` revision; omit to take the
            server default (almost always what you want).
        out_file: Optional path to also write the full ``.yang`` text to.

    Response keys to inspect:
        ``source`` (full text), ``source_truncated`` (always False —
        retained for envelope stability), ``source_total_chars``,
        ``out_file`` (path written when ``out_file`` was given, else
        None), ``warnings``.

    Example::

        netconf_get_schema(device="sa", identifier="dn-system")
        # source contains:
        #   augment "/dn-top:drivenets-top" {
        #     uses system-top;   // -> container system { container config-items { ... } }
        #   }
        # ⇒ XML for netconf_get / netconf_edit:
        #   <system xmlns="http://drivenets.com/ns/yang/dn-system">
        #     <config-items>
        #       <system-info><description>...</description></system-info>
        #     </config-items>
        #   </system>

    To chase imports / submodules referenced by the source, call this
    tool again with each ``identifier``. There is no local cache — every
    call hits the device.
    """
    sid = _session_id()
    if not identifier:
        return _error_result(
            "get-schema", sid,
            ValueError("Provide identifier= (YANG module name)."),
        )

    warnings: List[str] = []
    try:
        with _connect_device(host, device, port, user, password, no_verify, timeout) as cr:
            log_path = _begin(cr, sid, "get-schema", device=device)
            source = get_schema_source(cr.mgr, identifier=identifier, version=version)
            _log_action(
                log_path, "action", action="get-schema",
                identifier=identifier, version=version or "",
                bytes=len(source), result="ok",
            )

            spill_path: Optional[str] = None
            if out_file:
                output_path = Path(out_file) if Path(out_file).is_absolute() else ROOT_DIR / out_file
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(source, encoding="utf-8")
                spill_path = str(output_path)

            _log_event(log_path, sid, "end", status="ok")
            return _base_result(
                "get-schema", cr, sid,
                {
                    "status": "ok",
                    "identifier": identifier,
                    "version": version,
                    "out_file": spill_path,
                    "warnings": warnings,
                    "source": source,
                    "source_truncated": False,
                    "source_total_chars": len(source),
                },
            )
    except Exception as e:
        return _error_result("get-schema", sid, e)


def _resolve_build(
    *,
    device: Optional[str],
    host: Optional[str],
    force: bool = False,
) -> Dict[str, Any]:
    """Probe DNOS build for this target and ensure the YANG cache is populated."""
    info = _yang_resolve_build(device=device, host=host, force=force)
    if not info.get("build") or not info.get("sub_build"):
        raise RuntimeError(
            f"could not resolve YANG build for device={device!r} host={host!r}: {info!r}"
        )
    return info


def netconf_refresh_yang(
    device: Optional[str] = None,
    host: Optional[str] = None,
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    no_verify: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Probe the device's DNOS build and bulk-cache its YANG modules.

    NOT REGISTERED as an MCP tool — kept here so the bulk-fetch path
    (``yang/bootstrap.py``, ``yang/send.py``) stays exercised and
    ready. See ``TODO-fs-netconf-yangs.md`` for the plan to revive this
    once a sibling ``fs-netconf-yangs`` filesystem MCP exists.

    Behaviour, when called directly from Python:

    1. Opens a short NETCONF session and probes the DNOS version to
       get ``build`` / ``sub_build``.
    2. Reads ``ietf-yang-library`` and fetches every advertised module
       (and submodules) via ``<get-schema>`` into
       ``yangs/<build>/<sub_build>/`` (plus a ``_metadata.json``
       marker). Idempotent: existing files are left untouched.
    3. Closes the session. Result is cached per-device for the
       process lifetime.
    """
    sid = _session_id()
    try:
        resolved = _resolve_build(device=device, host=host, force=True)
    except Exception as e:
        return _error_result("refresh-yang", sid, e)

    build = resolved["build"]
    sub_build = resolved["sub_build"]
    store = _YANGS_DIR / build / sub_build

    return {
        "action": "refresh-yang",
        "status": "ok",
        "sid": sid,
        "build": build,
        "sub_build": sub_build,
        "path": str(store),
        "yangs_root": str(_YANGS_DIR),
        "yangs_relpath": f"{build}/{sub_build}",
        "filesystem_mcp": "fs-netconf-yangs",
        "bootstrap": resolved.get("bootstrap", {}),
        "hint": (
            "Read .yang sources directly from the 'fs-netconf-yangs' filesystem "
            f"MCP under '{build}/{sub_build}/'. If a module is missing there, "
            "call netconf_refresh_yang(device=...) again."
        ),
    }


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance.

    Only the on-demand discovery pair is exposed. ``netconf_refresh_yang``
    stays unregistered — see this module's docstring and
    ``TODO-fs-netconf-yangs.md``.
    """
    mcp.tool()(netconf_yang_library)
    mcp.tool()(netconf_get_schema)
