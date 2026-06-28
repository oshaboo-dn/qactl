"""Raw IxNetwork REST escape hatch.

For IxNetwork objects the bundled ``ixia`` wrapper doesn't cover
(``bgpVrf``, ``bgpImportRouteTargetList``, ``bgpIPv4EvpnEvi``, exotic
prefix-pool attributes, …) it is way more efficient to issue the REST
call directly than to write a dedicated MCP tool per attribute. This
module exposes two functions:

- ``ixia_rest_get(path)`` — GET / OPTIONS on any IxNetwork REST path,
  no mutation. Safe, no confirm gate.
- ``ixia_rest_patch(path, body, confirm)`` — POST / PATCH / DELETE
  against any IxNetwork REST path. Destructive; requires
  ``confirm=True``.

Both go through the same RestPy connection the wrapper already uses
(``ixn._connection``) so session / auth / logging work the same way.
Paths are always **relative to the session root** (no hostname).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ixia.models import IxiaError

from ixia_core.envelope import make_envelope, error_envelope
from ixia_core.session import (
    DEFAULT_PORT, DEFAULT_USER,
    get_session, write_lock, session_id_of,
)


_VALID_GET = {"GET", "OPTIONS"}
_VALID_WRITE = {"POST", "PATCH", "PUT", "DELETE"}


def _ixnetwork_root(s) -> Optional[str]:
    """Best-effort ``/api/v1/sessions/<id>/ixnetwork`` prefix for ``s``.

    Read off the connected handle's ``ixn.href`` so relative paths can
    be resolved against the real session root rather than the bare
    server root (which RestPy otherwise rewrites to a local
    ``/api/v1/sessions/<id>/ixnetwork/.../apibrowser`` HTML page —
    the inconsistency called out in #49).
    """
    try:
        href = s.ixn.href
    except Exception:
        return None
    if not isinstance(href, str) or not href:
        return None
    return "/" + href.strip("/")


def _normalise_path(path: str, root: Optional[str] = None) -> str:
    """Resolve a REST path to one rooted at the IxNetwork server.

    Three shapes are accepted:

    - absolute API paths (``/api/v1/sessions/...``) pass through as-is;
    - relative paths (``topology/1/deviceGroup/1``) are joined onto the
      session's ``ixnetwork`` root when ``root`` is known, so they hit
      the live session instead of the bare server root;
    - any other leading-slash path is left rooted at the server.
    """
    if not path:
        raise ValueError("path must be non-empty")
    p = path.strip()
    if p.startswith("/api/") or p.startswith("api/"):
        return "/" + p.lstrip("/")
    if p.startswith("/"):
        return p
    if root:
        return root.rstrip("/") + "/" + p.lstrip("/")
    return "/" + p.lstrip("/")


def _trim_body(body: Any, max_chars: int = 40_000) -> Any:
    """Keep response bodies reasonable in the envelope — large lists can
    blow out MCP streaming. Caller can re-query for full data if
    needed."""
    s = json.dumps(body) if not isinstance(body, str) else body
    if len(s) <= max_chars:
        return body
    return {
        "_truncated": True,
        "_size_chars": len(s),
        "_preview_chars": max_chars,
        "_preview": s[:max_chars],
    }


def ixia_rest_get(
    host: str,
    path: str,
    method: str = "GET",
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> Dict[str, Any]:
    """Read any IxNetwork REST endpoint.

    Args:
        path: IxNetwork REST path, e.g.
            ``/api/v1/sessions/1/ixnetwork/topology/1/deviceGroup/1/ethernet/1/ipv4/1/bgpIpv4Peer/1``.
            Paths not starting with ``/`` get a leading slash added.
        method: Either ``GET`` (default, returns object payload) or
            ``OPTIONS`` (returns the schema — attributes, children,
            enum values). Use OPTIONS to discover object shape before
            building it.

    Returns envelope with ``result = <response body>``. Large bodies
    (>40k chars) are truncated to keep the envelope manageable.
    """
    request = {"host": host, "port": port, "user": user,
               "path": path, "method": method.upper()}
    if method.upper() not in _VALID_GET:
        return error_envelope(
            f"method must be one of {sorted(_VALID_GET)} — use "
            "ixia_rest_patch for writes.",
            kind="rest_get", host=host, port=port,
            status="bad_argument",
        )

    if not path:
        return error_envelope(
            "path must be non-empty", kind="rest_get",
            host=host, port=port, status="bad_argument",
        )

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="rest_get",
            host=host, port=port, status="connect_error",
        )

    try:
        p = _normalise_path(path, _ixnetwork_root(s))
    except ValueError as e:
        return error_envelope(
            str(e), kind="rest_get", host=host, port=port,
            status="bad_argument",
        )

    env = make_envelope(
        kind="rest_get", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        conn = s.ixn._connection
        if method.upper() == "GET":
            body = conn._read(p)
        else:
            # RestPy's ``_execute`` is POST-only (``_execute(url,
            # payload)``); calling it for OPTIONS raised the
            # TypeError reported in #49. ``_send_recv`` is the generic
            # verb dispatcher and the one ``_read`` itself delegates to.
            body = conn._send_recv("OPTIONS", p)
        env["result"] = _trim_body(body)
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def ixia_rest_patch(
    host: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    method: str = "PATCH",
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Write against any IxNetwork REST endpoint.

    Args:
        path: IxNetwork REST path (e.g.
            ``/api/v1/sessions/1/ixnetwork/topology/1/deviceGroup/1/ethernet/1/ipv4/1/bgpIpv4Peer/1``).
        body: JSON payload. ``None`` is valid for DELETE. POST
            typically needs ``{}`` to create a default child.
        method: One of ``POST``, ``PATCH``, ``PUT``, ``DELETE``.
            ``PATCH`` default because most writes to existing objects
            are patches.
        confirm: Must be ``True``. Session state is mutated — no undo
            unless you reload a saved `.ixncfg`.

    Returns envelope with ``result = <response body>`` when the
    IxNetwork server returns one (POST typically returns the new
    object's href / id; PATCH may return the updated object). Takes
    the per-session write lock.
    """
    request = {
        "host": host, "port": port, "user": user,
        "path": path, "method": method.upper(),
        "body": body, "confirm": confirm,
    }
    m = method.upper()
    if m not in _VALID_WRITE:
        return error_envelope(
            f"method must be one of {sorted(_VALID_WRITE)} for write ops.",
            kind="rest_patch", host=host, port=port,
            status="bad_argument",
        )
    if confirm is not True:
        return error_envelope(
            "Destructive REST call — re-call with confirm=True after "
            "reviewing method / path / body.",
            kind="rest_patch", host=host, port=port,
            status="confirmation_required",
            next_actions=["Re-invoke with confirm=True to proceed."],
        )

    if not path:
        return error_envelope(
            "path must be non-empty", kind="rest_patch",
            host=host, port=port, status="bad_argument",
        )

    try:
        s = get_session(host=host, port=port, user=user)
    except IxiaError as e:
        return error_envelope(
            f"{type(e).__name__}: {e}", kind="rest_patch",
            host=host, port=port, status="connect_error",
        )

    try:
        p = _normalise_path(path, _ixnetwork_root(s))
    except ValueError as e:
        return error_envelope(
            str(e), kind="rest_patch", host=host, port=port,
            status="bad_argument",
        )

    env = make_envelope(
        kind="rest_patch", host=host, port=port,
        session_id=session_id_of(s), request=request,
    )
    try:
        conn = s.ixn._connection
        payload = body if body is not None else {}
        with write_lock(host, port, user):
            if m == "POST":
                result = conn._create(p, payload)
            elif m == "PATCH":
                result = conn._update(p, payload)
            elif m == "PUT":
                # RestPy's connection doesn't expose a PUT helper
                # directly; fall back to _execute.
                result = conn._execute("PUT", p, None, payload)
            else:  # DELETE
                result = conn._delete(p)
        env["result"] = _trim_body(result) if result is not None else {"ok": True}
        return env
    except Exception as e:
        env["status"] = "error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        return env


def register(mcp) -> None:
    mcp.tool()(ixia_rest_get)
    mcp.tool()(ixia_rest_patch)
