"""Read-surface gNMI tools: get, get-many, enumerate-keys.

The agent supplies the gNMI xpath itself (no path templating server-side).
We pace per-device to dodge DNOS' "Rate limit exceeded!" guard, normalise
the pygnmi response into the standard envelope, and surface the most
common DNOS-side errors (missing keys, unsupported datatype, message size)
with actionable ``next_actions``.

Important DNOS gNMI quirks (measured on cl + sa):

- **Keyed paths only.** ``/…/ncps/ncp/…`` is rejected with
  ``Path does not exist: /drivenets-top/system/ncps/ncp``. The agent must
  use ``ncp[ncp-id=N]`` syntax. Use ``gnmi_enumerate_keys`` to discover
  which keys exist.
- **datatype=ALL only.** STATE / OPERATIONAL / CONFIG all rejected.
- **encoding json or proto.** No ``json_ietf``.
- **4 MiB grpc default is too small.** session.py pins
  ``grpc.max_receive_message_length=32 MiB``.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from qactl.dnctl.gnmi.core import rate_limiter
from qactl.dnctl.gnmi.core.envelope import error_envelope, make_envelope
from qactl.dnctl.gnmi.core.session import (
    DEFAULT_TIMEOUT_S,
    VALID_TLS_MODES,
    open_client,
)


_VALID_ENCODINGS = ("json", "proto")
_VALID_DATATYPES = ("all", "config", "state", "operational")  # server only honours "all"

# Heuristic: bare list-element name without [key=val] in the path. Matches
# segments like ``ncp/`` or ``ncp/something`` where ncp is followed by a
# slash and not by ``[``. Used only to enrich error messages — the server
# is the authority on path validity.
_BARE_LIST_RE = re.compile(r"/(ncp|ncf|ncc|ncm|interface|vrf|neighbor|disk)(?:/|$)")


def _classify_grpc_error(msg: str) -> Optional[str]:
    """Map a DNOS gRPC error string to a single actionable hint."""
    m = msg.lower()
    if "path does not exist" in m and ("/ncp" in m or "/ncf" in m or "/ncc" in m or "/ncm" in m):
        return (
            "DNOS gNMI requires keyed list paths. Add the list key in "
            "the form `[ncp-id=N]` / `[ncf-id=N]` / `[ncc-id=N]`. Use "
            "gnmi_enumerate_keys to discover which keys exist."
        )
    if "rate limit exceeded" in m:
        return (
            "DNOS gNMI rate limiter tripped. Wait a few seconds and "
            "retry; this tool already paces 3 s per device by default."
        )
    if "data_type" in m and "not supported" in m:
        return "Use datatype='all' — the only datatype this server accepts."
    if "no valid requests in the session" in m or (
        "invalid_argument" in m and "subscri" in m
    ):
        return (
            "DNOS rejected the Subscribe request itself (the SubscriptionList "
            "was not accepted), not a transport error. Common causes on DNOS: "
            "(1) a keyless list path — use OpenConfig keyed wildcards like "
            "`/interfaces/interface[name=*]/state/oper-status`, not a bare "
            "subtree; (2) a SAMPLE interval outside 5s–1h (the device floor "
            "is 5s); (3) ON_CHANGE on a path not in the device's on-change "
            "registry (interface oper-status/admin-state, transceivers, "
            "PSU/fan/temp, LACP are registered; BGP neighbor state is NOT — "
            "use the syslog event source for BGP). Verify the path with "
            "`gnmi get` first."
        )
    if "received message larger than max" in m:
        return (
            "Response exceeded the grpc receive cap. Narrow the path; "
            "this tool already raised the cap to 32 MiB."
        )
    return None


def _do_get(
    *,
    paths: List[str],
    encoding: str,
    datatype: str,
    device: Optional[str],
    host: Optional[str],
    port: Optional[int],
    user: Optional[str],
    password: Optional[str],
    tls_mode: str,
    cert_file: Optional[str],
    key_file: Optional[str],
    ca_file: Optional[str],
    verify_mgmt0: bool = True,
) -> Dict[str, Any]:
    """Internal: issue ONE gNMI Get for one or more paths and return a
    normalised dict ``{result, error, slept_s, latency_ms}``.

    The caller wraps this in the standard envelope.
    """
    out: Dict[str, Any] = {"result": None, "error": None,
                           "slept_s": 0.0, "latency_ms": 0}
    try:
        client, resolved, _ = open_client(
            device=device, host=host, port=port,
            user=user, password=password,
            tls_mode=tls_mode,
            cert_file=cert_file, key_file=key_file, ca_file=ca_file,
        verify_mgmt0=verify_mgmt0,
        )
    except Exception as e:
        out["error"] = ("connect_error", f"resolve/setup failed: {e}")
        return out

    out["resolved_host"] = resolved.host
    out["resolved_port"] = resolved.port
    out["resolved_device"] = resolved.device
    out["mgmt0_warnings"] = list(resolved.warnings)
    out["slept_s"] = rate_limiter.gate(
        resolved.device, resolved.host, resolved.port,
    )

    t0 = time.time()
    try:
        with client as gc:
            r = gc.get(path=paths, encoding=encoding, datatype=datatype)
        out["latency_ms"] = int((time.time() - t0) * 1000)
        out["result"] = r
        return out
    except Exception as e:
        out["latency_ms"] = int((time.time() - t0) * 1000)
        out["error"] = ("error", f"{type(e).__name__}: {str(e)[:300]}")
        return out


def gnmi_get(
    path: str,
    device: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    tls_mode: str = "insecure",
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    ca_file: Optional[str] = None,
    verify_mgmt0: bool = True,
    encoding: str = "json",
    datatype: str = "all",
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Single-path gNMI Get.

    ``path`` is a slash-separated xpath without YANG namespaces. List
    segments require keys: ``ncps/ncp[ncp-id=1]/...``. The function does
    NOT validate the path before sending — DNOS is the final authority.

    Defaults match what DNOS actually accepts:

    - ``encoding="json"`` (this server doesn't advertise ``json_ietf``).
    - ``datatype="all"`` (this server rejects STATE / OPERATIONAL / CONFIG).

    Auto-paces: every call waits up to 3 s since the last Get against the
    same device, so an agent can fire ``gnmi_get`` repeatedly without
    tripping DNOS' rate limiter.
    """
    if encoding not in _VALID_ENCODINGS:
        return error_envelope(
            f"encoding must be one of {_VALID_ENCODINGS}",
            kind="get", device=device, host=host, port=port, tls_mode=tls_mode,
        )
    if datatype not in _VALID_DATATYPES:
        return error_envelope(
            f"datatype must be one of {_VALID_DATATYPES}",
            kind="get", device=device, host=host, port=port, tls_mode=tls_mode,
        )
    if tls_mode not in VALID_TLS_MODES:
        return error_envelope(
            f"tls_mode must be one of {VALID_TLS_MODES}",
            kind="get", device=device, host=host, port=port, tls_mode=tls_mode,
        )
    if not isinstance(path, str) or not path.strip():
        return error_envelope(
            "path must be a non-empty xpath string",
            kind="get", device=device, host=host, port=port, tls_mode=tls_mode,
        )

    request = {
        "device": device, "host": host, "port": port, "user": user,
        "tls_mode": tls_mode, "path": path,
        "encoding": encoding, "datatype": datatype,
        "timeout_s": timeout_s,
    }

    res = _do_get(
        paths=[path], encoding=encoding, datatype=datatype,
        device=device, host=host, port=port,
        user=user, password=password,
        tls_mode=tls_mode,
        cert_file=cert_file, key_file=key_file, ca_file=ca_file,
        verify_mgmt0=verify_mgmt0,
    )

    env = make_envelope(
        kind="get",
        device=res.get("resolved_device") or device,
        host=res.get("resolved_host") or host,
        port=res.get("resolved_port") or port,
        tls_mode=tls_mode, request=request,
    )
    env["warnings"].extend(res.get("mgmt0_warnings") or [])
    if res["slept_s"] > 0:
        env["warnings"].append(
            f"paced {res['slept_s']:.2f}s before sending to keep "
            f"DNOS rate limiter happy"
        )

    if res["error"]:
        kind, msg = res["error"]
        env["status"] = kind
        env["errors"].append(msg)
        hint = _classify_grpc_error(msg)
        if hint:
            env["next_actions"].append(hint)
        return env

    env["result"] = {
        "latency_ms": res["latency_ms"],
        "notification_count": len(res["result"].get("notification", []) or []),
        "update_count": sum(
            len(n.get("update", []) or [])
            for n in (res["result"].get("notification") or [])
        ),
        "notification": res["result"].get("notification", []),
    }
    return env


def gnmi_get_many(
    paths: List[str],
    device: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    tls_mode: str = "insecure",
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    ca_file: Optional[str] = None,
    verify_mgmt0: bool = True,
    encoding: str = "json",
    datatype: str = "all",
    one_call: bool = False,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Multiple gNMI Gets against one device.

    Two modes, picked by ``one_call``:

    - ``one_call=False`` (default): one Get RPC per path, paced 3 s apart
      to avoid DNOS' rate limiter. Returns a per-path result list. Use
      this when one of the paths might fail and you want the others to
      still come back.
    - ``one_call=True``: pass all paths in a single Get RPC. DNOS returns
      one Notification per path in one response. Faster (no pacing) but
      a single bad path errors the whole RPC. Use only when all paths
      are known good.
    """
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return error_envelope(
            "paths must be a list of xpath strings",
            kind="get_many", device=device, host=host, port=port, tls_mode=tls_mode,
        )
    if not paths:
        return error_envelope(
            "paths must be non-empty",
            kind="get_many", device=device, host=host, port=port, tls_mode=tls_mode,
        )

    request = {
        "device": device, "host": host, "port": port,
        "tls_mode": tls_mode, "paths": list(paths),
        "encoding": encoding, "datatype": datatype, "one_call": one_call,
    }

    if one_call:
        res = _do_get(
            paths=list(paths), encoding=encoding, datatype=datatype,
            device=device, host=host, port=port,
            user=user, password=password,
            tls_mode=tls_mode,
            cert_file=cert_file, key_file=key_file, ca_file=ca_file,
        verify_mgmt0=verify_mgmt0,
        )
        env = make_envelope(
            kind="get_many",
            device=res.get("resolved_device") or device,
            host=res.get("resolved_host") or host,
            port=res.get("resolved_port") or port,
            tls_mode=tls_mode, request=request,
        )
        env["warnings"].extend(res.get("mgmt0_warnings") or [])
        if res["error"]:
            kind, msg = res["error"]
            env["status"] = kind
            env["errors"].append(msg)
            hint = _classify_grpc_error(msg)
            if hint:
                env["next_actions"].append(hint)
            return env
        env["result"] = {
            "latency_ms": res["latency_ms"],
            "paths_count": len(paths),
            "notification": res["result"].get("notification", []),
        }
        return env

    per_path: List[Dict[str, Any]] = []
    total_slept = 0.0
    total_latency = 0
    failures = 0
    mgmt0_warnings: List[str] = []
    for p in paths:
        sub = _do_get(
            paths=[p], encoding=encoding, datatype=datatype,
            device=device, host=host, port=port,
            user=user, password=password,
            tls_mode=tls_mode,
            cert_file=cert_file, key_file=key_file, ca_file=ca_file,
        verify_mgmt0=verify_mgmt0,
        )
        total_slept += sub["slept_s"]
        total_latency += sub["latency_ms"]
        for w in sub.get("mgmt0_warnings") or []:
            if w not in mgmt0_warnings:
                mgmt0_warnings.append(w)
        entry: Dict[str, Any] = {
            "path": p,
            "latency_ms": sub["latency_ms"],
            "slept_s": sub["slept_s"],
        }
        if sub["error"]:
            kind, msg = sub["error"]
            entry["status"] = kind
            entry["error"] = msg
            hint = _classify_grpc_error(msg)
            if hint:
                entry["next_action"] = hint
            failures += 1
        else:
            entry["status"] = "ok"
            entry["notification"] = (sub["result"] or {}).get("notification", [])
        per_path.append(entry)

    env = make_envelope(
        kind="get_many",
        device=device, host=host, port=port,
        tls_mode=tls_mode, request=request,
    )
    env["warnings"].extend(mgmt0_warnings)
    if failures == len(paths):
        env["status"] = "error"
    elif failures > 0:
        env["warnings"].append(f"{failures}/{len(paths)} paths failed; see results[].error")
    env["result"] = {
        "paths_count": len(paths),
        "failures": failures,
        "total_slept_s": round(total_slept, 2),
        "total_latency_ms": total_latency,
        "results": per_path,
    }
    return env


# Heuristics for "this dict field is a list key". Order matters — we
# pick the first matching field per entry. Known DriveNets YANG list
# keys: ncp-id / ncf-id / ncc-id / ncm-id / vrf-name / interface-name /
# name (protocols, neighbors) / location (disks).
def _detect_key_field(entry: Dict[str, Any]) -> Optional[str]:
    keys = list(entry.keys())
    for k in keys:
        if k.endswith("-id"):
            return k
    for k in keys:
        if k.endswith("-name"):
            return k
    for k in keys:
        if k in ("name", "id", "location"):
            return k
    return None


def _walk_immediate_lists(val: Any) -> List[Dict[str, Any]]:
    """Yield list-of-dicts found at the immediate children of ``val``.

    Returns ``[{"list_name": str, "entries": [...], "key_field": str|None}]``.
    Only depth 1 — we don't recurse, since ``gnmi_enumerate_keys`` is
    supposed to surface the parent's direct list children, not every
    nested list buried deep in the subtree.
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(val, dict):
        return out
    for name, child in val.items():
        if (
            isinstance(child, list)
            and child
            and all(isinstance(e, dict) for e in child)
        ):
            key_field = _detect_key_field(child[0])
            entries: List[Dict[str, Any]] = []
            for e in child:
                if key_field and key_field in e:
                    entries.append({"key": key_field, "value": e[key_field]})
            out.append({
                "list_name": name,
                "key_field": key_field,
                "count": len(child),
                "entries": entries,
            })
    return out


def gnmi_enumerate_keys(
    list_path: str,
    device: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    tls_mode: str = "insecure",
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    ca_file: Optional[str] = None,
    verify_mgmt0: bool = True,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Discover the list keys present at a parent path.

    ``list_path`` should be the parent of the keyed list — e.g.
    ``/drivenets-top/system/ncps`` to find which ``ncp-id`` values exist.
    The function Gets the parent subtree, walks the response's ``val``
    dict at depth 1, and reports every list-of-dicts it finds along with
    the detected key field (heuristic: prefer ``*-id``, then ``*-name``,
    then ``name``/``id``/``location``).

    Why this exists: DNOS gNMI requires keyed list paths
    (``ncp[ncp-id=1]``) and rejects bare list segments. The agent has no
    way to learn keys from the protocol surface — Capabilities only
    advertises modules, not instances. So we Get the parent and parse
    the keys out of ``val``.

    Result shape::

        result: {
          list_path: "/drivenets-top/system/ncps",
          lists: [
            { list_name: "ncp",
              key_field: "ncp-id",
              count: 1,
              entries: [{key: "ncp-id", value: 0}] },
            ...
          ],
          # legacy: keys parsed from inline [k=v] in update[].path
          inline_keys: [],
          latency_ms: 212,
        }
    """
    request = {
        "device": device, "host": host, "port": port,
        "tls_mode": tls_mode, "list_path": list_path,
    }

    res = _do_get(
        paths=[list_path], encoding="json", datatype="all",
        device=device, host=host, port=port,
        user=user, password=password,
        tls_mode=tls_mode,
        cert_file=cert_file, key_file=key_file, ca_file=ca_file,
        verify_mgmt0=verify_mgmt0,
    )

    env = make_envelope(
        kind="enumerate_keys",
        device=res.get("resolved_device") or device,
        host=res.get("resolved_host") or host,
        port=res.get("resolved_port") or port,
        tls_mode=tls_mode, request=request,
    )
    env["warnings"].extend(res.get("mgmt0_warnings") or [])

    if res["error"]:
        kind, msg = res["error"]
        env["status"] = kind
        env["errors"].append(msg)
        hint = _classify_grpc_error(msg)
        if hint:
            env["next_actions"].append(hint)
        return env

    lists: List[Dict[str, Any]] = []
    inline_keys: List[Dict[str, str]] = []
    seen_inline = set()

    for n in (res["result"].get("notification") or []):
        for u in n.get("update", []) or []:
            # Method A: parse inline [k=v] from the response path itself.
            p = u.get("path", "")
            for m in re.finditer(r"\[([^=\]]+)=([^\]]+)\]", p):
                k = (m.group(1), m.group(2))
                if k not in seen_inline:
                    seen_inline.add(k)
                    inline_keys.append({"key": m.group(1), "value": m.group(2)})
            # Method B: walk val at depth 1 looking for lists-of-dicts.
            v = u.get("val")
            for entry in _walk_immediate_lists(v):
                lists.append(entry)

    env["result"] = {
        "list_path": list_path,
        "lists": lists,
        "inline_keys": inline_keys,
        "latency_ms": res["latency_ms"],
    }
    return env


def _validate_set_lists(
    update: Optional[List[Any]],
    replace: Optional[List[Any]],
    delete: Optional[List[Any]],
) -> Optional[str]:
    """Validate gnmi_set's three operation lists. Returns None when ok."""
    if not (update or replace or delete):
        return (
            "gnmi_set requires at least one of update / replace / delete; "
            "empty Set RPCs are pointless."
        )
    for label, lst in (("update", update), ("replace", replace)):
        if not lst:
            continue
        if not isinstance(lst, list):
            return f"{label} must be a list of {{path, val}} dicts."
        for i, e in enumerate(lst):
            if not isinstance(e, dict):
                return f"{label}[{i}] must be a dict with keys path, val."
            if not isinstance(e.get("path"), str) or not e["path"].strip():
                return f"{label}[{i}].path must be a non-empty xpath string."
            if "val" not in e:
                return f"{label}[{i}] missing required 'val' key."
    if delete:
        if not isinstance(delete, list):
            return "delete must be a list of xpath strings."
        for i, p in enumerate(delete):
            if not isinstance(p, str) or not p.strip():
                return f"delete[{i}] must be a non-empty xpath string."
    return None


def gnmi_set(
    update: Optional[List[Dict[str, Any]]] = None,
    replace: Optional[List[Dict[str, Any]]] = None,
    delete: Optional[List[str]] = None,
    device: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    tls_mode: str = "insecure",
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    ca_file: Optional[str] = None,
    verify_mgmt0: bool = True,
    encoding: str = "json",
    confirm: bool = False,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Atomic gNMI Set RPC.

    Three operation lists, all optional but at least one non-empty:

    - ``update``: list of ``{"path": "...", "val": <json-serialisable>}``.
      Merges ``val`` into the subtree at ``path`` — leaves outside ``val``
      stay untouched. Use for surgical leaf flips.
    - ``replace``: same shape. Replaces the subtree at ``path`` with
      ``val`` — anything not in ``val`` is removed. Use when you want to
      blow away a list / container and rebuild it.
    - ``delete``: list of xpath strings. Removes each subtree.

    The server applies all three lists in one atomic Set RPC. Unlike
    NETCONF there is **no candidate datastore and no commit-check** —
    success means it's already in the running config; failure means
    nothing changed.

    DNOS rejects unkeyed list paths the same way Get does. Use
    ``gnmi_enumerate_keys`` first to learn the keys, then pass the
    keyed form (``ncp[ncp-id=N]/...``).

    Safety: ``confirm=False`` (default) returns a dry-run envelope with
    the SetRequest that would be sent — **no device traffic**. Pass
    ``confirm=True`` to execute. There is no on-device dry-run because
    gNMI Set has no simulation primitive.
    """
    if encoding not in _VALID_ENCODINGS:
        return error_envelope(
            f"encoding must be one of {_VALID_ENCODINGS}",
            kind="set", device=device, host=host, port=port, tls_mode=tls_mode,
        )
    if tls_mode not in VALID_TLS_MODES:
        return error_envelope(
            f"tls_mode must be one of {VALID_TLS_MODES}",
            kind="set", device=device, host=host, port=port, tls_mode=tls_mode,
        )
    err = _validate_set_lists(update, replace, delete)
    if err:
        return error_envelope(
            err, kind="set", device=device, host=host, port=port, tls_mode=tls_mode,
        )

    # pygnmi.set() expects update / replace as list of (path, value) tuples
    # and delete as list of paths.
    pyg_update = [(e["path"], e["val"]) for e in (update or [])]
    pyg_replace = [(e["path"], e["val"]) for e in (replace or [])]
    pyg_delete = list(delete or [])

    request = {
        "device": device, "host": host, "port": port, "user": user,
        "tls_mode": tls_mode,
        "update": update or [], "replace": replace or [], "delete": pyg_delete,
        "encoding": encoding, "confirm": confirm, "timeout_s": timeout_s,
    }

    # Dry-run path: don't open a client, don't pace, don't send.
    if not confirm:
        env = make_envelope(
            kind="set", device=device, host=host, port=port,
            tls_mode=tls_mode, request=request,
        )
        env["warnings"].append(
            "Dry-run: confirm=False. Re-invoke with confirm=true to "
            "execute. gNMI Set is atomic on the server — there is no "
            "commit-check; on success the change is already in the "
            "running config."
        )
        env["result"] = {
            "would_send": {
                "update": pyg_update,
                "replace": pyg_replace,
                "delete": pyg_delete,
                "encoding": encoding,
            },
            "operation_count": (
                len(pyg_update) + len(pyg_replace) + len(pyg_delete)
            ),
        }
        return env

    # Live path.
    try:
        client, resolved, _ = open_client(
            device=device, host=host, port=port,
            user=user, password=password,
            tls_mode=tls_mode,
            cert_file=cert_file, key_file=key_file, ca_file=ca_file,
        verify_mgmt0=verify_mgmt0,
        )
    except Exception as e:
        return error_envelope(
            f"resolve/setup failed: {e}",
            kind="set", device=device, host=host, port=port,
            tls_mode=tls_mode, status="connect_error",
        )

    env = make_envelope(
        kind="set", device=resolved.device or device,
        host=resolved.host, port=resolved.port,
        tls_mode=tls_mode, request=request,
    )
    env["warnings"].extend(resolved.warnings)
    slept = rate_limiter.gate(resolved.device, resolved.host, resolved.port)
    if slept > 0:
        env["warnings"].append(
            f"paced {slept:.2f}s before sending to keep DNOS rate limiter happy"
        )

    t0 = time.time()
    try:
        with client as gc:
            r = gc.set(
                update=pyg_update or None,
                replace=pyg_replace or None,
                delete=pyg_delete or None,
                encoding=encoding,
            )
        env["result"] = {
            "latency_ms": int((time.time() - t0) * 1000),
            "response": r,
        }
        return env
    except Exception as e:
        msg = str(e).replace("\n", " ")
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {msg[:300]}")
        hint = _classify_grpc_error(msg)
        if hint:
            env["next_actions"].append(hint)
        return env


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(gnmi_get)
    mcp.tool()(gnmi_get_many)
    mcp.tool()(gnmi_enumerate_keys)
    mcp.tool()(gnmi_set)
