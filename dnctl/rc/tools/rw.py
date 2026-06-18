"""Data-tree read + write tools.

Read:
* ``restconf_get(device, segments, ...)`` — single targeted GET.
* ``restconf_get_url(endpoint, url)`` — escape hatch for arbitrary URLs.
* ``restconf_enumerate_keys(device, list_segments)`` — return the list
  of keys that exist under a YANG list (helper for building per-element
  GETs without a manual probe).

Write (RFC 8040 §4):
* ``restconf_put(device, segments, payload, ...)``    — replace resource
* ``restconf_patch(device, segments, payload, ...)``  — merge resource
* ``restconf_post(device, segments, payload, ...)``   — create child
* ``restconf_delete(device, segments, ...)``          — remove resource

Writes are gated by ``confirm=False`` (default). Without ``confirm=True``
the tool returns the URL + body it *would* send and ``status="dry_run"``.
This mirrors the safety convention used by ``cli-mcp`` for any
device-modifying operation.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence

from dnctl.rc.core.envelope import error_envelope, make_envelope
from dnctl.rc.core.registry import find_mount, get_endpoint
from dnctl.rc.core.session import request as http_request
from dnctl.rc.core.uri import build_data_url


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _normalize_segments(segments: Sequence[Any]) -> List[Any]:
    """Convert agent-friendly segment notation to the internal form.

    Accepts:
      - list of strings, mixing plain containers and ``"list=key"`` shorthand
      - list of strings + tuples ``("list", key)`` / ``("list", [k1, k2])``
      - a single slash-separated string ``"a/b/list=1/c"``
    """
    out: List[Any] = []
    if isinstance(segments, str):
        raw = [s for s in segments.split("/") if s]
    else:
        raw = list(segments)
    for s in raw:
        if isinstance(s, tuple):
            out.append(s)
            continue
        if isinstance(s, str) and "=" in s:
            list_name, _, key = s.partition("=")
            if "," in key:
                out.append((list_name, key.split(",")))
            else:
                out.append((list_name, key))
        else:
            out.append(s)
    return out


def _resolve_endpoint(
    device: Optional[str],
    endpoint: Optional[str],
    mount_name: Optional[str],
    kind: str,
) -> Dict[str, Any]:
    """Return ``{ok, endpoint, mount_name, ep_cfg}`` or an error envelope."""
    if device:
        ep_alias, mount, _mcfg = find_mount(device)
        if endpoint and ep_alias and ep_alias != endpoint:
            return {
                "ok": False,
                "env": error_envelope(
                    f"device '{device}' is mounted on '{ep_alias}', not '{endpoint}'",
                    kind=kind, device=device, endpoint=endpoint,
                ),
            }
        endpoint = endpoint or ep_alias
        mount_name = mount_name or mount
    if not endpoint:
        return {
            "ok": False,
            "env": error_envelope(
                "either 'device' or 'endpoint' must be provided",
                kind=kind, device=device, endpoint=endpoint,
            ),
        }
    ep = get_endpoint(endpoint)
    if not ep:
        return {
            "ok": False,
            "env": error_envelope(
                f"unknown endpoint '{endpoint}'", kind=kind,
                device=device, endpoint=endpoint,
            ),
        }
    return {"ok": True, "endpoint": endpoint, "mount_name": mount_name, "ep_cfg": ep}


def _surface_odl_hints(env: Dict[str, Any], raw_text: str, eff_style: str) -> None:
    """Translate common ODL 4xx error patterns into actionable hints."""
    if "module does not exist" in raw_text:
        env["next_actions"].append(
            "the first path segment must use the YANG MODULE name, not the "
            "container name (e.g. 'dn-top:drivenets-top', not "
            "'drivenets-top:drivenets-top'). Module quirks are configured "
            "per endpoint in restconf_endpoints.json."
        )
    if "was not found in parent data node" in raw_text and eff_style == "rfc8040":
        env["next_actions"].append(
            "ODL on this lab rejects RFC-8040 keyed-list syntax; retry with "
            "style='legacy' (path uses /list/key instead of /list=key)."
        )


def _do_write(
    *,
    method: str,
    kind: str,
    device: Optional[str],
    segments: Any,
    endpoint: Optional[str],
    mount_name: Optional[str],
    payload: Any,
    style: Optional[str],
    timeout: float,
    confirm: bool,
) -> Dict[str, Any]:
    """Shared implementation of PUT / PATCH / POST / DELETE."""
    if segments is None:
        return error_envelope("segments is required", kind=kind,
                              device=device, endpoint=endpoint)

    r = _resolve_endpoint(device, endpoint, mount_name, kind)
    if not r["ok"]:
        return r["env"]
    endpoint = r["endpoint"]
    mount_name = r["mount_name"]
    ep = r["ep_cfg"]

    base = ep["base_url"]
    auth = ep.get("auth") or {}
    eff_style = style or ep.get("uri_style", "legacy")
    quirks = ep.get("module_name_quirks") or {}

    norm = _normalize_segments(segments)
    # PATCH/PUT/POST/DELETE on data tree always use the config datastore.
    url = build_data_url(
        base_url=base, mount_name=mount_name, yang_segments=norm,
        datastore="config", style=eff_style, module_quirks=quirks,
    )

    env = make_envelope(
        kind=kind, device=device, endpoint=endpoint, base_url=base,
        request={
            "method": method, "device": device, "mount_name": mount_name,
            "segments": norm, "style": eff_style, "url": url,
            "payload": payload,
        },
    )

    if not confirm:
        env["status"] = "dry_run"
        env["next_actions"].append(
            f"this is a DRY RUN — no traffic was sent. Re-run with confirm=True "
            f"to actually {method} {url}"
        )
        return env

    body_json = payload if not (
        isinstance(payload, str) and payload.lstrip().startswith("<")
    ) else None
    body_xml = payload if (
        isinstance(payload, str) and payload.lstrip().startswith("<")
    ) else None

    t0 = time.monotonic()
    sc, _h, body, _el = http_request(
        method=method, url=url,
        user=auth.get("user"), password=auth.get("password"),
        verify=False, timeout=timeout,
        json_body=body_json, xml_body=body_xml,
    )
    raw_text = body.decode("utf-8", errors="replace")
    env["result"] = {
        "http_status": sc,
        "elapsed_ms": round((time.monotonic() - t0) * 1000),
        "url": url,
        "response": raw_text[:1000] if raw_text else "",
    }
    # 200/201/204 are success on RFC 8040 + ODL.
    if sc not in (200, 201, 204):
        env["status"] = "error"
        env["errors"].append(f"HTTP {sc}: {raw_text[:400]}")
        _surface_odl_hints(env, raw_text, eff_style)
    return env


# --------------------------------------------------------------------------
# read tools
# --------------------------------------------------------------------------


def restconf_get(
    device: Optional[str] = None,
    segments: Any = None,
    endpoint: Optional[str] = None,
    mount_name: Optional[str] = None,
    datastore: str = "operational",
    style: Optional[str] = None,
    accept: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Run a RESTCONF GET against ``device`` (or directly an endpoint)."""
    if segments is None:
        return error_envelope("segments is required", kind="get",
                              device=device, endpoint=endpoint)

    r = _resolve_endpoint(device, endpoint, mount_name, "get")
    if not r["ok"]:
        return r["env"]
    endpoint = r["endpoint"]
    mount_name = r["mount_name"]
    ep = r["ep_cfg"]

    base = ep["base_url"]
    auth = ep.get("auth") or {}
    eff_style = style or ep.get("uri_style", "legacy")
    quirks = ep.get("module_name_quirks") or {}

    norm = _normalize_segments(segments)
    url = build_data_url(
        base_url=base, mount_name=mount_name, yang_segments=norm,
        datastore=datastore, style=eff_style, module_quirks=quirks,
    )

    env = make_envelope(
        kind="get", device=device, endpoint=endpoint, base_url=base,
        request={
            "device": device, "mount_name": mount_name,
            "segments": norm, "datastore": datastore,
            "style": eff_style, "url": url,
        },
    )

    headers: Dict[str, str] = {}
    if accept:
        headers["Accept"] = accept

    t0 = time.monotonic()
    sc, resp_h, body, _el = http_request(
        method="GET", url=url,
        user=auth.get("user"), password=auth.get("password"),
        verify=False, timeout=timeout,
        extra_headers=headers,
    )
    elapsed_ms = round((time.monotonic() - t0) * 1000)

    raw_text = body.decode("utf-8", errors="replace")
    parsed: Any = None
    if resp_h.get("content-type", "").lower().startswith(
        ("application/json", "application/yang-data+json")
    ):
        try:
            parsed = json.loads(raw_text)
        except Exception:
            parsed = None

    env["result"] = {
        "http_status": sc,
        "elapsed_ms": elapsed_ms,
        "bytes": len(body),
        "url": url,
        "json": parsed,
        "text": raw_text if parsed is None else None,
    }
    if sc != 200:
        env["status"] = "error"
        env["errors"].append(f"HTTP {sc}: {raw_text[:400]}")
        _surface_odl_hints(env, raw_text, eff_style)
    return env


def restconf_get_url(
    endpoint: str,
    url: str,
    timeout: float = 30.0,
    accept: str = "application/json",
) -> Dict[str, Any]:
    """Escape hatch — GET an arbitrary URL using the endpoint's credentials."""
    ep = get_endpoint(endpoint)
    if not ep:
        return error_envelope(
            f"unknown endpoint '{endpoint}'", kind="get_url", endpoint=endpoint,
        )
    auth = ep.get("auth") or {}
    env = make_envelope(
        kind="get_url", endpoint=endpoint, base_url=ep.get("base_url"),
        request={"url": url, "accept": accept},
    )
    sc, _h, body, _el = http_request(
        method="GET", url=url,
        user=auth.get("user"), password=auth.get("password"),
        verify=False, timeout=timeout,
        extra_headers={"Accept": accept},
    )
    raw = body.decode("utf-8", errors="replace")
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    env["result"] = {
        "http_status": sc, "bytes": len(body),
        "json": parsed, "text": raw if parsed is None else None,
    }
    if sc != 200:
        env["status"] = "error"
        env["errors"].append(f"HTTP {sc}: {raw[:400]}")
    return env


def restconf_enumerate_keys(
    device: Optional[str] = None,
    list_segments: Any = None,
    key_field: Optional[str] = None,
    endpoint: Optional[str] = None,
    mount_name: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Return the set of keys for a YANG list, by GET-ing the parent.

    Example::

        restconf_enumerate_keys(device='cl',
                                list_segments=['drivenets-top','dn-system:system','ncps'],
                                key_field='ncp-id')
        => result.keys = [1, 2]
    """
    if list_segments is None:
        return error_envelope("list_segments is required", kind="enumerate_keys",
                              device=device, endpoint=endpoint)

    env = restconf_get(
        device=device, segments=list_segments, endpoint=endpoint,
        mount_name=mount_name, timeout=timeout,
    )
    if env.get("status") != "ok":
        env["kind"] = "enumerate_keys"
        return env

    j = (env.get("result") or {}).get("json") or {}
    top = next(iter(j.values()), {}) if isinstance(j, dict) and j else {}

    keys: List[Any] = []
    list_name = None
    if isinstance(top, dict):
        for k, v in top.items():
            if isinstance(v, list):
                list_name = k
                if not key_field:
                    if v and isinstance(v[0], dict):
                        # heuristic: first non-container scalar key with `-id` suffix
                        cands = [kk for kk in v[0].keys() if kk.endswith("-id")]
                        key_field = cands[0] if cands else next(iter(v[0].keys()))
                if key_field:
                    keys = [it.get(key_field) for it in v if isinstance(it, dict)]
                break

    out = make_envelope(
        kind="enumerate_keys", device=device,
        endpoint=env.get("endpoint"), base_url=env.get("base_url"),
        request=env.get("request") or {},
    )
    out["result"] = {
        "list_name": list_name,
        "key_field": key_field,
        "keys": keys,
        "count": len(keys),
        "url": (env.get("result") or {}).get("url"),
    }
    if list_name is None:
        out["status"] = "error"
        out["errors"].append(
            "no list found at the given path — pass the parent container of the "
            "YANG list (e.g. .../ncps) and the tool will pick the contained list."
        )
    return out


# --------------------------------------------------------------------------
# write tools — all gated on confirm=True
# --------------------------------------------------------------------------


def restconf_put(
    device: Optional[str] = None,
    segments: Any = None,
    payload: Any = None,
    endpoint: Optional[str] = None,
    mount_name: Optional[str] = None,
    style: Optional[str] = None,
    timeout: float = 30.0,
    confirm: bool = False,
) -> Dict[str, Any]:
    """RFC 8040 PUT — create or **replace** the resource at ``segments``.

    ``payload`` is the JSON object the server expects (usually one
    top-level key matching the leaf module:container). XML strings are
    detected by leading ``<`` and sent as-is with ``Content-Type:
    application/xml``.
    """
    return _do_write(
        method="PUT", kind="put", device=device, segments=segments,
        endpoint=endpoint, mount_name=mount_name, payload=payload,
        style=style, timeout=timeout, confirm=confirm,
    )


def restconf_patch(
    device: Optional[str] = None,
    segments: Any = None,
    payload: Any = None,
    endpoint: Optional[str] = None,
    mount_name: Optional[str] = None,
    style: Optional[str] = None,
    timeout: float = 30.0,
    confirm: bool = False,
) -> Dict[str, Any]:
    """RFC 8040 PATCH — **merge** ``payload`` into the resource.

    Use for partial updates that don't touch fields you didn't include.
    """
    return _do_write(
        method="PATCH", kind="patch", device=device, segments=segments,
        endpoint=endpoint, mount_name=mount_name, payload=payload,
        style=style, timeout=timeout, confirm=confirm,
    )


def restconf_post(
    device: Optional[str] = None,
    segments: Any = None,
    payload: Any = None,
    endpoint: Optional[str] = None,
    mount_name: Optional[str] = None,
    style: Optional[str] = None,
    timeout: float = 30.0,
    confirm: bool = False,
) -> Dict[str, Any]:
    """RFC 8040 POST — **create a child** under the resource at ``segments``.

    Server returns 201 + Location header pointing at the new resource.
    """
    return _do_write(
        method="POST", kind="post", device=device, segments=segments,
        endpoint=endpoint, mount_name=mount_name, payload=payload,
        style=style, timeout=timeout, confirm=confirm,
    )


def restconf_delete(
    device: Optional[str] = None,
    segments: Any = None,
    endpoint: Optional[str] = None,
    mount_name: Optional[str] = None,
    style: Optional[str] = None,
    timeout: float = 30.0,
    confirm: bool = False,
) -> Dict[str, Any]:
    """RFC 8040 DELETE — remove the resource at ``segments``."""
    return _do_write(
        method="DELETE", kind="delete", device=device, segments=segments,
        endpoint=endpoint, mount_name=mount_name, payload=None,
        style=style, timeout=timeout, confirm=confirm,
    )


def register(mcp) -> None:
    mcp.tool()(restconf_get)
    mcp.tool()(restconf_get_url)
    mcp.tool()(restconf_enumerate_keys)
    mcp.tool()(restconf_put)
    mcp.tool()(restconf_patch)
    mcp.tool()(restconf_post)
    mcp.tool()(restconf_delete)
