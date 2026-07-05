"""Candidate-datastore lifecycle tools.

- ``netconf_apply`` — multi-payload edit-config + commit driver. Reads
  payload files from ``operations/<action>/`` (resolved by
  :func:`dnctl.nc.core.session._resolve_operation_file`).
- ``netconf_rollback`` — NETCONF ``<rollback>`` by index, optional commit.
- ``netconf_discard_changes`` — clear the candidate datastore.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from dnctl.nc.core.change_ops import _change_operation, _try_commit
from dnctl.nc.core.device_log import _begin, _log_action, _log_event
from dnctl.nc.core.netconf_rpc import discard_changes, require_candidate, rollback
from dnctl.nc.core.results import _base_result, _error_result
from dnctl.nc.core.session import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    _connect_device,
    _session_id,
)


def netconf_apply(
    payload_files: List[str],
    operation_type: str = "edit",
    host: Optional[str] = None,
    device: Optional[str] = None,
    target: str = "candidate",
    validate_after: bool = False,
    commit_if_changed: bool = True,
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    no_verify: bool = True,
    verify_mgmt0: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Apply multiple XML payload files via edit-config + commit.

    Useful for batch-loading a pre-prepared set of payloads from disk
    (e.g. a full config restore from a saved backup). For one-off changes
    prefer ``netconf_edit(xml=...)``.
    """
    if operation_type not in {"edit", "delete"}:
        raise ValueError("operation_type must be one of: edit, delete")
    return _change_operation(
        action=operation_type,
        host=host,
        device=device,
        payload_files=payload_files,
        target=target,
        validate_after=validate_after,
        commit_if_changed=commit_if_changed,
        verify_subtree=None,
        port=port,
        user=user,
        password=password,
        no_verify=no_verify,
        verify_mgmt0=verify_mgmt0,
        timeout=timeout,
    )


def netconf_rollback(
    index: int,
    host: Optional[str] = None,
    device: Optional[str] = None,
    commit_after: bool = True,
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    no_verify: bool = True,
    verify_mgmt0: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Load a previous commit into candidate by rollback index, then optionally commit."""
    sid = _session_id()
    try:
        with _connect_device(host, device, port, user, password, no_verify, timeout, verify_mgmt0) as cr:
            log_path = _begin(cr, sid, "rollback", device=device)

            m = cr.mgr
            require_candidate(m)

            rollback_xml = rollback(m, index)
            _log_action(log_path, "action", action="rollback", index=index, result="ok")

            commit_status, commit_xml = _try_commit(m, log_path, enabled=commit_after)

            _log_event(log_path, sid, "end", status="ok")
            return _base_result(
                "rollback", cr, sid,
                {
                    "status": "ok",
                    "rollback_index": index,
                    "rollback_xml": rollback_xml,
                    "commit_status": commit_status,
                    "commit_xml": commit_xml,
                },
            )
    except Exception as e:
        return _error_result("rollback", sid, e)


def netconf_discard_changes(
    host: Optional[str] = None,
    device: Optional[str] = None,
    port: int = DEFAULT_PORT,
    user: Optional[str] = None,
    password: Optional[str] = None,
    no_verify: bool = True,
    verify_mgmt0: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Discard pending candidate changes (safety net after failed edits)."""
    sid = _session_id()
    try:
        with _connect_device(host, device, port, user, password, no_verify, timeout, verify_mgmt0) as cr:
            log_path = _begin(cr, sid, "discard-changes", device=device)
            discard_xml = discard_changes(cr.mgr)
            _log_action(log_path, "action", action="discard-changes", result="ok")
            _log_event(log_path, sid, "end", status="ok")
            return _base_result(
                "discard-changes", cr, sid,
                {"status": "ok", "discard_xml": discard_xml},
            )
    except Exception as e:
        return _error_result("discard-changes", sid, e)


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(netconf_apply)
    mcp.tool()(netconf_rollback)
    mcp.tool()(netconf_discard_changes)
