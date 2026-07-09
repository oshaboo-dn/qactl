"""YANG cache bootstrap: fetch raw ``.yang`` modules from a device.

Minimal: probes ``ietf-yang-library``, pulls every advertised module (plus
submodules) via ``<get-schema>`` (RFC 6022), and writes them under
``yangs/<build>/<sub_build>/`` along with a provenance marker
``_metadata.json``. No derived files, no YANG parsing -- the MCP no longer
generates XML and the agent works directly off raw ``.yang`` sources.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from ncclient import manager as _nc_manager
from ncclient.operations import RPCError

from qactl.dnos.nc.core.netconf_rpc import (  # type: ignore[import-untyped]
    get_schema_source,
    get_yang_library,
)

from ._yang_io import yang_store_path


# Only marker file that must exist for the cache to be considered complete.
_REQUIRED_FILES: Tuple[str, ...] = ("_metadata.json",)


def cache_ready(build: str, sub_build: str) -> bool:
    """True if the bootstrap marker is on disk for this build."""
    store = yang_store_path(build, sub_build)
    if not store.is_dir():
        return False
    return all((store / name).exists() for name in _REQUIRED_FILES)


def ensure_yang(
    build: str,
    sub_build: str,
    mgr: _nc_manager.Manager,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Ensure ``yangs/<build>/<sub_build>/`` has raw ``.yang`` sources.

    Fast path: ``_metadata.json`` exists -> ``{"status": "cached"}``.
    Otherwise fetch every module advertised by ``ietf-yang-library`` via
    ``<get-schema>`` and write the marker.

    Never raises; per-module errors are collected in the returned dict.
    """
    store = yang_store_path(build, sub_build)
    store.mkdir(parents=True, exist_ok=True)

    if not force and cache_ready(build, sub_build):
        return {
            "status": "cached",
            "build": build,
            "sub_build": sub_build,
            "path": str(store),
        }

    try:
        fetch = _fetch_schemas(mgr, build, sub_build, force=force)
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "build": build,
            "sub_build": sub_build,
            "path": str(store),
            "errors": [f"fetch_schemas: {e}"],
        }

    return {
        "status": "built",
        "build": build,
        "sub_build": sub_build,
        "path": str(store),
        "fetch": fetch,
    }


def _fetch_schemas(
    mgr: _nc_manager.Manager,
    build: str,
    sub_build: str,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Pull every module advertised by ietf-yang-library into the store."""
    store = yang_store_path(build, sub_build)
    store.mkdir(parents=True, exist_ok=True)

    modules = get_yang_library(mgr)
    fetched = 0
    skipped = 0
    errors: List[Dict[str, str]] = []
    start = time.monotonic()

    for mod in modules:
        name = mod["name"]
        revision = mod["revision"]
        filepath = store / f"{name}.yang"

        if not force and filepath.exists():
            skipped += 1
        else:
            try:
                source = get_schema_source(mgr, name, revision)
                filepath.write_text(source, encoding="utf-8")
                fetched += 1
            except (RPCError, Exception) as e:  # noqa: BLE001
                errors.append({
                    "module": name, "revision": revision, "error": str(e),
                })

        for sub in mod.get("submodules", []) or []:
            sub_name = sub["name"]
            sub_rev = sub.get("revision", "")
            sub_path = store / f"{sub_name}.yang"
            if not force and sub_path.exists():
                skipped += 1
                continue
            try:
                source = get_schema_source(mgr, sub_name, sub_rev)
                sub_path.write_text(source, encoding="utf-8")
                fetched += 1
            except (RPCError, Exception) as e:  # noqa: BLE001
                errors.append({
                    "module": sub_name, "revision": sub_rev, "error": str(e),
                })

    elapsed = time.monotonic() - start

    metadata = {
        "build": build,
        "sub_build": sub_build,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "module_count": fetched + skipped,
        "fetched": fetched,
        "skipped": skipped,
        "errors_count": len(errors),
        "errors": errors[:20],
        "elapsed_seconds": round(elapsed, 1),
        "yang_library_modules": len(modules),
    }
    (store / "_metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return {
        "fetched": fetched,
        "skipped": skipped,
        "errors_count": len(errors),
        "elapsed_seconds": round(elapsed, 1),
    }
