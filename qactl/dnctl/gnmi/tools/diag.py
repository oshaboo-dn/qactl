"""Diagnostic gNMI tools: ping (TCP + Capabilities), full Capabilities."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from qactl.dnctl.gnmi.core.envelope import make_envelope, error_envelope
from qactl.dnctl.gnmi.core.session import (
    DEFAULT_TIMEOUT_S,
    open_client,
    VALID_TLS_MODES,
)


def gnmi_ping(
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
    """Cheap reachability check: open gRPC + run Capabilities.

    Validates the gNMI server is up, accepts our auth, and returns the
    advertised gNMI version + encodings + supported_models count. Use
    before any expensive Get / Set call.

    ``tls_mode`` ∈ ``insecure | skip_verify | verify_ca | mtls`` —
    DriveNets boxes vary build-by-build:

    - ``cl`` is plaintext (``insecure``) on this rig.
    - ``sa`` is TLS with no-SAN server cert (``skip_verify``).

    Try ``insecure`` first; if the call times out, retry ``skip_verify``.
    """
    if tls_mode not in VALID_TLS_MODES:
        return error_envelope(
            f"tls_mode must be one of {VALID_TLS_MODES}",
            kind="ping", device=device, host=host, port=port,
        )

    request = {
        "device": device, "host": host, "port": port,
        "user": user, "tls_mode": tls_mode,
        "cert_file": cert_file, "ca_file": ca_file,
        "key_file": "<set>" if key_file else None,
        "timeout_s": timeout_s,
    }

    try:
        client, resolved, final_user = open_client(
            device=device, host=host, port=port,
            user=user, password=password,
            tls_mode=tls_mode,
            cert_file=cert_file, key_file=key_file, ca_file=ca_file,
            verify_mgmt0=verify_mgmt0,
        )
    except Exception as e:
        return error_envelope(
            f"resolve/setup failed: {e}",
            kind="ping", device=device, host=host, port=port,
            tls_mode=tls_mode, status="connect_error",
        )

    env = make_envelope(
        kind="ping", device=resolved.device or device,
        host=resolved.host, port=resolved.port,
        tls_mode=tls_mode, request=request,
    )
    env["warnings"].extend(resolved.warnings)

    t0 = time.time()
    try:
        with client as gc:
            caps = gc.capabilities()
        elapsed_ms = int((time.time() - t0) * 1000)
        env["result"] = {
            "gnmi_version": caps.get("gnmi_version"),
            "supported_encodings": caps.get("supported_encodings"),
            "supported_models_count": len(caps.get("supported_models", [])),
            "latency_ms": elapsed_ms,
            "user": final_user,
        }
        return env
    except Exception as e:
        env["status"] = "connect_error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        env["next_actions"].append(
            "If this hung / timed out, the server is likely TLS-only. "
            "Retry with tls_mode='skip_verify'."
        )
        return env


def gnmi_capabilities(
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
    name_contains: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Full gNMI Capabilities — version, encodings, all supported_models.

    ``name_contains`` filters the ``supported_models`` list to entries whose
    name matches the substring (case-insensitive). Use it to check whether
    a specific YANG module is advertised before composing a path.
    """
    if tls_mode not in VALID_TLS_MODES:
        return error_envelope(
            f"tls_mode must be one of {VALID_TLS_MODES}",
            kind="capabilities", device=device, host=host, port=port,
        )

    request = {
        "device": device, "host": host, "port": port,
        "user": user, "tls_mode": tls_mode,
        "name_contains": name_contains,
        "timeout_s": timeout_s,
    }

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
            kind="capabilities", device=device, host=host, port=port,
            tls_mode=tls_mode, status="connect_error",
        )

    env = make_envelope(
        kind="capabilities", device=resolved.device or device,
        host=resolved.host, port=resolved.port,
        tls_mode=tls_mode, request=request,
    )
    env["warnings"].extend(resolved.warnings)

    try:
        with client as gc:
            caps = gc.capabilities()
        models: List[Dict[str, Any]] = caps.get("supported_models", []) or []
        if name_contains:
            needle = name_contains.lower()
            models = [m for m in models if needle in (m.get("name", "").lower())]
        env["result"] = {
            "gnmi_version": caps.get("gnmi_version"),
            "supported_encodings": caps.get("supported_encodings"),
            "supported_models_total": len(caps.get("supported_models", [])),
            "supported_models_returned": len(models),
            "supported_models": models,
        }
        return env
    except Exception as e:
        env["status"] = "connect_error"
        env["errors"].append(f"{type(e).__name__}: {str(e)[:240]}")
        env["next_actions"].append(
            "If this timed out, retry with tls_mode='skip_verify'."
        )
        return env


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(gnmi_ping)
    mcp.tool()(gnmi_capabilities)
