"""Shared result-dict builders for tool responses."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .session import ConnectResult, _utc_now


def _base_result(
    action: str, cr: ConnectResult, session_id: str, extra: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "action": action,
        "host": cr.host,
        "port": cr.port,
        "user": cr.user,
        "session_id": session_id,
        "timestamp": _utc_now(),
    }
    if cr.device:
        result["device"] = cr.device
    if cr.serial_numbers:
        result["serial_numbers"] = cr.serial_numbers
    if cr.sn_verified:
        result["sn_verified"] = True
    if cr.mgmt0_verified:
        result["mgmt0_verified"] = True
    if cr.mgmt0_warnings:
        result["mgmt0_warnings"] = list(cr.mgmt0_warnings)
    if extra:
        result.update(extra)
    return result


def _error_result(action: str, session_id: str, error: Exception) -> Dict[str, Any]:
    """Build result dict when the tool failed before returning (no ConnectResult)."""
    return {
        "action": action,
        "status": "error",
        "error": str(error),
        "session_id": session_id,
        "timestamp": _utc_now(),
    }
