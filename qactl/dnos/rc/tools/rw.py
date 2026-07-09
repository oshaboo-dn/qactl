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

from qactl.dnos.rc.core.envelope import error_envelope, make_envelope
from qactl.dnos.rc.core.registry import find_mount, get_endpoint
from qactl.dnos.rc.core.session import request as http_request
from qactl.dnos.rc.core.uri import build_data_url


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

# Legacy (bierman02/draft02) ODL only speaks the pre-RFC-8040 dot-form
# media types; the RFC 8040 dash form gets HTTP 415 (issue #80).
LEGACY_DATA_MEDIA = "application/yang.data+json"
LEGACY_PATCH_MEDIA = "application/yang.patch+json"
LEGACY_ACCEPT = "application/yang.data+json, application/json;q=0.9"
# yang-patch responses are a distinct media type; advertising the data
# form makes bierman02 refuse with HTTP 406 (issue #80 follow-up).
LEGACY_PATCH_STATUS_ACCEPT = "application/yang.patch-status+json"


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


def _yang_patch_scalar(v: Any) -> Any:
    """Shape a leaf value for the bierman02 yang-patch parser.

    Trial-verified quirk (issue #80): bare JSON numbers/booleans are
    rejected — every scalar must arrive string-quoted.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return v


def _plain_merge_to_yang_patch(norm_segments: List[Any], payload: Any):
    """Convert a plain-merge PATCH payload into a YANG-PATCH document.

    bierman02 ODL rejects plain-merge PATCH outright (415); the working
    form (issue #80) is ``application/yang.patch+json`` where every
    ``target`` segment is module-prefixed, each edit touches one leaf,
    and the leaf value is wrapped ``{"leaf": "<string>"}``.

    Accepts the same wrapper-object payload ``restconf_put`` takes
    (``{"mod:container": {...leaves/containers...}}``). If the request
    URL already ends at the wrapper resource the PATCH is issued against
    its parent, mirroring the proven manual session.

    Returns ``(patch_segments, yang_patch_doc)`` or ``None`` when the
    payload doesn't fit the convertible shape.
    """
    if not isinstance(payload, dict) or len(payload) != 1 or not norm_segments:
        return None
    [(wrapper, inner)] = payload.items()
    if not isinstance(inner, dict) or not inner:
        return None

    prefix, _, local = str(wrapper).rpartition(":")
    if not prefix:
        # inherit the module prefix from the nearest prefixed URL segment
        for seg in reversed(norm_segments):
            name = seg[0] if isinstance(seg, tuple) else str(seg)
            p = name.rpartition(":")[0]
            if p:
                prefix = p
                break
    if not prefix:
        return None

    # If the URL ends at the wrapper resource itself, PATCH its parent —
    # yang-patch targets are relative to the request URI resource.
    last = norm_segments[-1]
    last_name = last[0] if isinstance(last, tuple) else str(last)
    if (
        not isinstance(last, tuple)
        and last_name.rpartition(":")[2] == local
        and len(norm_segments) > 1
    ):
        patch_segments = list(norm_segments[:-1])
    else:
        patch_segments = list(norm_segments)

    edits: List[Dict[str, Any]] = []

    def _walk(path: str, node: Dict[str, Any], pfx: str) -> None:
        for k, v in node.items():
            p, _, loc = str(k).rpartition(":")
            cur = p or pfx
            step = f"{path}/{cur}:{loc}"
            if isinstance(v, dict):
                _walk(step, v, cur)
            else:
                edits.append({
                    "edit-id": f"e{len(edits) + 1}",
                    "operation": "merge",
                    "target": step,
                    "value": {loc: _yang_patch_scalar(v)},
                })

    _walk(f"/{prefix}:{local}", inner, prefix)
    if not edits:
        return None
    doc = {
        "ietf-yang-patch:yang-patch": {"patch-id": "qactl-merge", "edit": edits},
    }
    return patch_segments, doc


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

    body_json = payload if not (
        isinstance(payload, str) and payload.lstrip().startswith("<")
    ) else None
    body_xml = payload if (
        isinstance(payload, str) and payload.lstrip().startswith("<")
    ) else None

    # Legacy (bierman02) ODL media-type handling — issue #80. The RFC 8040
    # dash-form Content-Type gets 415; plain-merge PATCH is unsupported and
    # must go out as a YANG-PATCH against the parent resource.
    legacy_odl = ep.get("kind") == "odl" and eff_style == "legacy"
    write_headers: Dict[str, str] = {}
    send_segments = norm
    if legacy_odl and body_json is not None:
        write_headers["Accept"] = LEGACY_ACCEPT
        if method == "PATCH":
            write_headers["Content-Type"] = LEGACY_PATCH_MEDIA
            write_headers["Accept"] = LEGACY_PATCH_STATUS_ACCEPT
            if not (
                isinstance(body_json, dict)
                and "ietf-yang-patch:yang-patch" in body_json
            ):
                conv = _plain_merge_to_yang_patch(norm, body_json)
                if conv is None:
                    env = error_envelope(
                        "legacy ODL PATCH needs a YANG-PATCH body and this "
                        "payload can't be converted automatically — pass a "
                        "single wrapper object ({\"module:container\": "
                        "{...leaves...}}) or a full ietf-yang-patch:yang-patch "
                        "document",
                        kind=kind, device=device, endpoint=endpoint,
                    )
                    return env
                send_segments, body_json = conv
        else:
            write_headers["Content-Type"] = LEGACY_DATA_MEDIA

    # PATCH/PUT/POST/DELETE on data tree always use the config datastore.
    url = build_data_url(
        base_url=base, mount_name=mount_name, yang_segments=send_segments,
        datastore="config", style=eff_style, module_quirks=quirks,
    )

    request_info = {
        "method": method, "device": device, "mount_name": mount_name,
        "segments": norm, "style": eff_style, "url": url,
        "payload": payload,
    }
    if write_headers.get("Content-Type"):
        request_info["content_type"] = write_headers["Content-Type"]
    if body_json is not payload and body_json is not None:
        request_info["yang_patch"] = body_json

    env = make_envelope(
        kind=kind, device=device, endpoint=endpoint, base_url=base,
        request=request_info,
    )

    if not confirm:
        env["status"] = "dry_run"
        env["next_actions"].append(
            f"this is a DRY RUN — no traffic was sent. Re-run with confirm=True "
            f"to actually {method} {url}"
        )
        return env

    t0 = time.monotonic()
    sc, _h, body, _el = http_request(
        method=method, url=url,
        user=auth.get("user"), password=auth.get("password"),
        verify=False, timeout=timeout,
        json_body=body_json, xml_body=body_xml,
        extra_headers=write_headers or None,
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
        if sc == 415:
            env["next_actions"].append(
                "HTTP 415 = unsupported media type. bierman02/legacy ODL "
                "expects application/yang.data+json (dot form) for writes "
                "and a YANG-PATCH (application/yang.patch+json) for PATCH; "
                "these are applied automatically when the endpoint has "
                "kind=odl + uri_style=legacy — check the endpoint config "
                "and any explicit --style override."
            )
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
