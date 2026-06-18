"""Resolve DNOS build for a device and fetch its raw YANG modules.

Thin bridge between the :mod:`yang` package and ``dnctl.nc.core``: probes
DNOS version over an open NETCONF session, runs
:func:`yang.bootstrap.ensure_yang` on the same session to pull raw
``.yang`` sources, and caches the result per-device for the process
lifetime.

Nothing on the core NETCONF tool paths (``show`` / ``edit`` / ``delete``)
calls this automatically anymore -- it's invoked on demand by
``netconf_refresh_yang``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from dnctl.nc.core import netconf_rpc, session

from . import bootstrap


_BUILD_CACHE: Dict[str, Dict[str, Any]] = {}


def resolve_build_and_bootstrap(
    *,
    device: Optional[str] = None,
    host: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Return ``{build, sub_build, bootstrap, version_info}`` for a target.

    Opens a NETCONF session, probes the DNOS version, runs
    :func:`yang.bootstrap.ensure_yang` on the same session, closes.
    Cached per device/host. ``force=True`` re-probes and re-fetches.
    """
    key = device or host or ""

    if not force and key and key in _BUILD_CACHE:
        return _BUILD_CACHE[key]

    cr = session.connect(device=device, host=host)
    try:
        version_info = netconf_rpc.get_dnos_version(cr.mgr)
        resolved_build = str(version_info.get("build") or "").strip()
        resolved_sub = str(version_info.get("sub_build") or "").strip()
        if not resolved_build or not resolved_sub:
            raise RuntimeError(
                f"could not resolve DNOS build from device probe: {version_info!r}"
            )

        bootstrap_result = bootstrap.ensure_yang(
            resolved_build, resolved_sub, cr.mgr, force=force,
        )
    finally:
        try:
            cr.mgr.close_session()
        except Exception:
            pass

    out: Dict[str, Any] = {
        "build": resolved_build,
        "sub_build": resolved_sub,
        "bootstrap": bootstrap_result,
        "version_info": version_info,
    }
    if key:
        _BUILD_CACHE[key] = out
    return out
