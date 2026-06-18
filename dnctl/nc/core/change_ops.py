"""Multi-payload change operation."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ncclient.operations import RPCError

from .netconf_rpc import (
    commit, discard_changes, edit_config, get_config, require_candidate,
)
from .xml_payload import extract_payload_for_edit, pretty_xml

from .device_log import _begin, _log_action, _log_event
from .results import _base_result, _error_result
from .session import (
    _connect_device,
    _load_payload,
    _resolve_operation_file,
    _session_id,
)


_NC_BASE_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"
_VALID_OPS = ("merge", "replace", "remove", "delete", "create")


def annotate_operation(xml: str, op: str) -> str:
    """Return ``xml`` with ``nc:operation="op"`` on the topmost element.

    Used by ``edit_from_xml`` to turn an agent-supplied payload into an
    edit-config payload that performs replace/remove/delete instead of
    the default merge. No-op for ``op="merge"``.
    """
    if op == "merge":
        return xml
    if op not in _VALID_OPS:
        raise ValueError(f"op must be one of {_VALID_OPS}, got {op!r}")
    m = re.match(r'\s*<([A-Za-z0-9_\-]+)([^>]*?)(/?)>', xml, re.DOTALL)
    if not m:
        return xml
    tag, attrs, self_close = m.group(1), m.group(2), m.group(3)
    new_attrs = (
        f' xmlns:nc="{_NC_BASE_NS}" nc:operation="{op}"' + attrs
    )
    head = xml[: m.start()]
    tail = xml[m.end():]
    return f"{head}<{tag}{new_attrs}{self_close}>{tail}"


def _try_commit(m, log_path, *, enabled: bool = True) -> tuple:
    """Commit the candidate datastore (if enabled).

    Returns (status, xml) where status is one of:
      - "skipped"   - enabled=False, commit not attempted
      - "committed" - commit succeeded
      - "no-change" - server reported "empty commit" (candidate had no changes)
    Non-empty-commit RPCErrors propagate.
    """
    if not enabled:
        return "skipped", None
    try:
        commit_xml = commit(m)
        _log_action(log_path, "action", action="commit", result="ok")
        return "committed", commit_xml
    except RPCError as e:
        if "empty commit" in str(e).lower():
            _log_action(log_path, "action", action="commit", result="no-change")
            return "no-change", "<ok/> <!-- empty commit: no candidate changes -->"
        raise


def _change_operation(
    *,
    action: str,
    host: Optional[str],
    device: Optional[str],
    payload_files: List[str],
    target: str,
    validate_after: bool,
    commit_if_changed: bool,
    verify_subtree: Optional[str],
    port: int,
    user: Optional[str],
    password: Optional[str],
    no_verify: bool,
    timeout: int,
    **_kwargs: Any,
) -> Dict[str, Any]:
    if target != "candidate" and commit_if_changed:
        raise ValueError("commit_if_changed requires target=candidate")

    sid = _session_id()
    resolved_files = [_resolve_operation_file(p, category=action) for p in payload_files]
    payloads = [_load_payload(p) for p in resolved_files]
    operations: List[Dict[str, Any]] = []
    verification: Optional[Dict[str, Any]] = None

    try:
        with _connect_device(host, device, port, user, password, no_verify, timeout) as cr:
            log_path = _begin(cr, sid, action, device=device)

            m = cr.mgr

            if commit_if_changed:
                require_candidate(m)

            for file_path, payload in zip(resolved_files, payloads):
                edit_result = edit_config(m, config_xml=payload, target=target)
                _log_action(log_path, "action", action="edit-config", file=str(file_path), target=target, result="ok")
                operations.append({"file": str(file_path), "edit_result_xml": edit_result})

            validate_xml = None
            if validate_after:
                validate_xml = m.validate(source=target).xml
                _log_action(log_path, "action", action="validate", source=target, result="ok")

            commit_status, commit_xml = _try_commit(m, log_path, enabled=commit_if_changed)

            if verify_subtree:
                readback_xml = get_config(m, source="running", subtree=verify_subtree)
                verification = {
                    "verify_subtree": verify_subtree,
                    "readback_xml": pretty_xml(readback_xml),
                    "ok": True,
                }
                _log_action(log_path, "action", action="verify", source="running")

            _log_event(log_path, sid, "end", status="ok")
            return _base_result(
                action=action,
                cr=cr,
                session_id=sid,
                extra={
                    "status": "ok",
                    "target": target,
                    "operations": operations,
                    "validate_xml": validate_xml,
                    "commit_status": commit_status,
                    "commit_xml": commit_xml,
                    "verification": verification,
                },
            )
    except Exception as e:
        return _error_result(action, sid, e)


def edit_from_xml(
    *,
    host: Optional[str],
    device: Optional[str],
    xml: str,
    op: str = "merge",
    comment: Optional[str] = None,
    port: int,
    user: Optional[str],
    password: Optional[str],
    no_verify: bool,
    timeout: int,
    xml_source: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomic stage -> commit flow for a single agent-supplied XML payload.

    The agent provides the full config XML; this helper:

    1. Applies the ``op`` attribute to the top element (no-op for merge).
    2. Connects, ``require_candidate``, ``edit-config target=candidate``.
    3. ``commit`` -- DNOS validates implicitly. If commit raises, discard
       the candidate and return ``status="commit_error"`` with the
       device's error messages.
    4. On success return ``status="ok"``.

    ``xml_source`` is a free-form tag recorded in the audit log; defaults
    to ``"inline"`` when the payload was provided directly in the tool call.
    """
    sid = _session_id()
    action_label = "delete" if op == "remove" else "edit"
    applied_xml = annotate_operation(xml, op)
    payload = extract_payload_for_edit(applied_xml)
    source_tag = xml_source or "inline"
    payload_bytes = len(payload.encode("utf-8"))

    try:
        with _connect_device(
            host, device, port, user, password, no_verify, timeout,
        ) as cr:
            log_path = _begin(cr, sid, action_label, device=device)
            m = cr.mgr
            require_candidate(m)

            try:
                edit_result = edit_config(m, config_xml=payload, target="candidate")
                _log_action(
                    log_path, "action", action="edit-config",
                    target="candidate", op=op, result="ok",
                    source=source_tag, bytes=payload_bytes,
                )
            except RPCError as e:
                try:
                    discard_changes(m)
                except Exception:  # noqa: BLE001
                    pass
                _log_action(
                    log_path, "action", action="edit-config",
                    target="candidate", op=op, result="error",
                    source=source_tag, bytes=payload_bytes,
                )
                _log_event(log_path, sid, "end", status="error")
                return _base_result(
                    action_label, cr, sid,
                    {
                        "status": "edit_error",
                        "op": op,
                        "device_error": str(e),
                        "applied_xml": applied_xml,
                    },
                )

            try:
                commit_xml = commit(m)
                _log_action(log_path, "action", action="commit", result="ok")
            except RPCError as e:
                try:
                    discard_changes(m)
                    _log_action(
                        log_path, "action", action="discard-changes",
                        reason="commit_failed", result="ok",
                    )
                except Exception:  # noqa: BLE001
                    pass
                _log_event(log_path, sid, "end", status="error")
                return _base_result(
                    action_label, cr, sid,
                    {
                        "status": "commit_error",
                        "op": op,
                        "device_error": str(e),
                        "applied_xml": applied_xml,
                        "edit_result_xml": edit_result,
                    },
                )

            _log_event(log_path, sid, "end", status="ok")
            return _base_result(
                action_label, cr, sid,
                {
                    "status": "ok",
                    "op": op,
                    "comment": comment,
                    "applied_xml": applied_xml,
                    "edit_result_xml": edit_result,
                    "commit_xml": commit_xml,
                },
            )
    except Exception as e:  # noqa: BLE001
        return _error_result(action_label, sid, e)
