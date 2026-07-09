"""HTTP session helper for RESTCONF speakers.

Single thin wrapper around ``httpx.Client`` that carries:

* basic auth (controller credentials for ODL, device-local credentials
  for native RESTCONF in the future);
* RESTCONF-correct media types (``application/yang-data+json`` first,
  ``application/json`` as ODL-friendly fallback);
* a sensible read timeout (the underlying NETCONF GET on the device can
  take a few seconds when the subtree is large).

Sessions are intentionally **not pooled** across calls: one request per
tool invocation keeps the per-call HTTP error / timeout boundaries clean
and matches the rest of the MCP family. If we ever see latency cost from
this we can add an LRU cache here without changing tool code.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import httpx


DEFAULT_TIMEOUT = 30.0  # seconds — generous enough for ODL→NETCONF round trips
DEFAULT_HEADERS = {
    # Order matters: ODL accepts both, but plain application/json is what we
    # actually exercised in SW-252550 testing. Servers that prefer
    # yang-data+json will still honour it via Accept negotiation.
    "Accept": "application/yang-data+json, application/json;q=0.9",
}


def make_client(
    *,
    base_url: str,
    user: Optional[str] = None,
    password: Optional[str] = None,
    verify: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    extra_headers: Optional[Dict[str, str]] = None,
) -> httpx.Client:
    """Build an httpx client pre-configured for a RESTCONF endpoint."""
    headers = dict(DEFAULT_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    auth: Optional[httpx.BasicAuth] = None
    if user is not None:
        auth = httpx.BasicAuth(user, password or "")
    return httpx.Client(
        base_url=base_url,
        auth=auth,
        headers=headers,
        timeout=timeout,
        verify=verify,
        follow_redirects=False,
    )


def request(
    *,
    method: str,
    url: str,
    user: Optional[str] = None,
    password: Optional[str] = None,
    verify: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    json_body: Any = None,
    xml_body: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict[str, str], bytes, float]:
    """One-shot HTTP call, suitable for any RESTCONF tool.

    Returns ``(status_code, headers, raw_body, elapsed_seconds)``. Never
    raises — transport / timeout errors come back as ``status_code=0`` and
    a synthetic ``X-RESTCONF-MCP-Error`` header so tool code can render a
    uniform error envelope.
    """
    headers = dict(DEFAULT_HEADERS)
    if extra_headers:
        headers.update(extra_headers)

    auth: Optional[httpx.BasicAuth] = None
    if user is not None:
        auth = httpx.BasicAuth(user, password or "")

    body: Any = None
    if xml_body is not None:
        body = xml_body.encode("utf-8")
        headers.setdefault("Content-Type", "application/xml")
    elif json_body is not None:
        import json as _json
        body = _json.dumps(json_body).encode("utf-8")
        # Callers can force a different media type via extra_headers —
        # legacy (bierman02) ODL only accepts the pre-RFC-8040 dot form.
        headers.setdefault("Content-Type", "application/yang-data+json")

    import time
    t0 = time.monotonic()
    try:
        with httpx.Client(verify=verify, timeout=timeout, follow_redirects=False) as c:
            resp = c.request(method, url, auth=auth, headers=headers, content=body)
            elapsed = time.monotonic() - t0
            return resp.status_code, dict(resp.headers), resp.content, elapsed
    except httpx.HTTPError as e:
        elapsed = time.monotonic() - t0
        return 0, {"X-RESTCONF-MCP-Error": type(e).__name__ + ": " + str(e)[:300]}, b"", elapsed
