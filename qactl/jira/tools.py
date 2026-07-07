"""Jira tool layer: pure functions that return the qactl envelope dict.

This is the shared "compute a result envelope" layer that both fronts
call: the CLI ([qactl jira ...]) and the stdio MCP server. No printing,
no argparse, no TTY prompts live here. Destructive tools take a
``confirm: bool = False`` parameter and return a ``confirmation_required``
envelope until it is set true -- that is the MCP-side gate (the CLI front
applies its own ``--yes`` / TTY gate before calling with ``confirm=True``).

Credentials still resolve from the environment (``ATLASSIAN_*``); the
optional per-call overrides exist so the CLI's ``--email`` / ``--token`` /
``--base-url`` flags keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from qactl.core.creds import CredentialError
from qactl.core.envelope import error_envelope, ok_envelope
from qactl.jira.client import JiraClient, JiraError


def _client(
    kind: str, *, timeout: float = 30.0,
    email: Optional[str] = None, token: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Tuple[Optional[JiraClient], Optional[dict]]:
    try:
        client = JiraClient.from_env(
            timeout=timeout, email=email, api_token=token, base_url=base_url,
        )
        return client, None
    except CredentialError as e:
        return None, error_envelope(str(e), kind=kind, status="bad_argument")


def _jira_err(e: JiraError, *, kind: str) -> dict:
    return error_envelope(
        f"Jira REST {e.method} -> HTTP {e.status_code}: {e.body[:300]}",
        kind=kind,
        next_actions=[
            "Check the issue key / id and that the token can see this issue; "
            "401/403 means auth or permissions, 404 means it's missing."
        ],
        result={"http_status": e.status_code, "http_body": e.body[:1000]},
    )


def _run(
    kind: str, fn: Callable[[JiraClient], dict], *,
    timeout: float = 30.0, email: Optional[str] = None,
    token: Optional[str] = None, base_url: Optional[str] = None,
) -> dict:
    client, err = _client(kind, timeout=timeout, email=email, token=token, base_url=base_url)
    if err is not None:
        return err
    try:
        return fn(client)
    except JiraError as e:
        return _jira_err(e, kind=kind)
    except Exception as e:  # noqa: BLE001
        return error_envelope(f"{kind} failed: {e}", kind=kind)


def _needs_confirm(kind: str, action: str) -> dict:
    return error_envelope(
        f"Refusing destructive operation without confirm=true: {action}",
        kind=kind, status="confirmation_required",
        next_actions=["Re-call with confirm=true to proceed."],
    )


# ---- read tools ----------------------------------------------------------

def jira_whoami(
    *, timeout: float = 30.0, email: Optional[str] = None,
    token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve the configured Atlassian token to its Jira user."""
    def fn(c: JiraClient) -> dict:
        me = c.myself()
        return ok_envelope(kind="jira_whoami", result={
            "account_id": me.get("accountId"),
            "email": me.get("emailAddress"),
            "display_name": me.get("displayName"),
            "active": me.get("active"),
            "time_zone": me.get("timeZone"),
        })
    return _run("jira_whoami", fn, timeout=timeout, email=email, token=token, base_url=base_url)


def jira_status(
    issue_key: str, *, timeout: float = 30.0, email: Optional[str] = None,
    token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return an issue's status, summary and assignee."""
    return _run("jira_status",
                lambda c: ok_envelope(kind="jira_status", result=c.get_issue_status(issue_key)),
                timeout=timeout, email=email, token=token, base_url=base_url)


def jira_status_bulk(
    issue_keys: Sequence[str], *, timeout: float = 30.0, email: Optional[str] = None,
    token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Status + assignee for many issue keys in one envelope (one GET per key).

    Per-key error tolerance: a failing key never fails the batch — it lands
    in ``result.errors`` and the envelope is ``warning`` (exit 0) as long as
    at least one key resolved. Only all-keys-failed is an error.
    """
    kind = "jira_status_bulk"
    keys = list(dict.fromkeys(issue_keys))
    if not keys:
        return error_envelope("no issue keys given", kind=kind, status="bad_argument")

    client, err = _client(kind, timeout=timeout, email=email, token=token, base_url=base_url)
    if err is not None:
        return err

    issues: Dict[str, Any] = {}
    failures: Dict[str, str] = {}
    for key in keys:
        try:
            issues[key] = client.get_issue_status(key)
        except JiraError as e:
            failures[key] = f"HTTP {e.status_code}: {e.body[:200]}"
        except Exception as e:  # noqa: BLE001
            failures[key] = str(e)

    result = {
        "requested": len(keys), "resolved": len(issues), "failed": len(failures),
        "issues": issues, "errors": failures,
    }
    if not issues:
        env = error_envelope(
            f"all {len(keys)} issue key(s) failed", kind=kind, result=result,
            next_actions=[
                "Check the issue keys and that the token can see them; "
                "401/403 means auth or permissions, 404 means missing."
            ],
        )
        env["errors"].extend(f"{k}: {v}" for k, v in failures.items())
        return env
    return ok_envelope(kind=kind, result=result,
                       warnings=[f"{k}: {v}" for k, v in failures.items()])


def jira_list_watchers(
    issue_key: str, *, timeout: float = 30.0, email: Optional[str] = None,
    token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """List the watchers on an issue."""
    def fn(c: JiraClient) -> dict:
        data = c.list_watchers(issue_key)
        return ok_envelope(kind="jira_list_watchers", result={
            "issue_key": issue_key,
            "is_watching": bool(data.get("isWatching")),
            "watch_count": int(data.get("watchCount") or 0),
            "watchers": list(data.get("watchers") or []),
        })
    return _run("jira_list_watchers", fn, timeout=timeout, email=email, token=token, base_url=base_url)


def jira_list_attachments(
    issue_key: str, *, timeout: float = 30.0, email: Optional[str] = None,
    token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """List an issue's attachments (id, filename, size, author)."""
    def fn(c: JiraClient) -> dict:
        raw = c.list_attachments(issue_key)
        summary = [{
            "id": a.get("id"), "filename": a.get("filename"), "size": a.get("size"),
            "mime_type": a.get("mimeType"), "created": a.get("created"),
            "author_display_name": (a.get("author") or {}).get("displayName"),
            "content_url": a.get("content"),
        } for a in raw]
        return ok_envelope(kind="jira_list_attachments", result={
            "issue_key": issue_key, "count": len(summary), "attachments": summary,
        })
    return _run("jira_list_attachments", fn, timeout=timeout, email=email, token=token, base_url=base_url)


def jira_list_transitions(
    issue_key: str, *, timeout: float = 30.0, email: Optional[str] = None,
    token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """List the workflow transitions available on an issue right now."""
    def fn(c: JiraClient) -> dict:
        ts = c.list_transitions(issue_key)
        return ok_envelope(kind="jira_list_transitions", result={
            "issue_key": issue_key,
            "transitions": [{
                "id": t.get("id"), "name": t.get("name"),
                "to_status": (t.get("to") or {}).get("name"),
                "has_screen": t.get("hasScreen"),
            } for t in ts],
        })
    return _run("jira_list_transitions", fn, timeout=timeout, email=email, token=token, base_url=base_url)


# ---- write / destructive tools -------------------------------------------

def jira_add_watcher(
    issue_key: str, account_id: str, *, timeout: float = 30.0,
    email: Optional[str] = None, token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Add a watcher to an issue."""
    def fn(c: JiraClient) -> dict:
        code = c.add_watcher(issue_key, account_id)
        return ok_envelope(kind="jira_add_watcher", result={
            "issue_key": issue_key, "account_id": account_id, "http_status": code,
        })
    return _run("jira_add_watcher", fn, timeout=timeout, email=email, token=token, base_url=base_url)


def jira_upload_attachment(
    issue_key: str, file: str, name: Optional[str] = None, *, timeout: float = 30.0,
    email: Optional[str] = None, token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload a local file as an attachment on an issue."""
    path = Path(file)
    if not path.is_file():
        return error_envelope(f"not a file: {path}", kind="jira_upload_attachment",
                              status="bad_argument")

    def fn(c: JiraClient) -> dict:
        created = c.upload_attachment(issue_key, path, name=name)
        return ok_envelope(kind="jira_upload_attachment", result={
            "issue_key": issue_key,
            "attachments": [{
                "id": a.get("id"), "filename": a.get("filename"),
                "size": a.get("size"), "content_url": a.get("content"),
            } for a in created],
        })
    return _run("jira_upload_attachment", fn, timeout=timeout, email=email, token=token, base_url=base_url)


def jira_remove_watcher(
    issue_key: str, account_id: str, *, confirm: bool = False, timeout: float = 30.0,
    email: Optional[str] = None, token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Remove a watcher from an issue (destructive; needs confirm=true)."""
    if not confirm:
        return _needs_confirm("jira_remove_watcher",
                              f"Remove watcher {account_id} from {issue_key}.")

    def fn(c: JiraClient) -> dict:
        code = c.remove_watcher(issue_key, account_id)
        return ok_envelope(kind="jira_remove_watcher", result={
            "issue_key": issue_key, "account_id": account_id, "http_status": code,
        })
    return _run("jira_remove_watcher", fn, timeout=timeout, email=email, token=token, base_url=base_url)


def jira_delete_attachment(
    attachment_id: str, *, confirm: bool = False, timeout: float = 30.0,
    email: Optional[str] = None, token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete an attachment by id (destructive; needs confirm=true)."""
    if not confirm:
        return _needs_confirm("jira_delete_attachment",
                              f"Delete attachment {attachment_id} (no undo).")

    def fn(c: JiraClient) -> dict:
        code = c.delete_attachment(attachment_id)
        return ok_envelope(kind="jira_delete_attachment",
                           result={"attachment_id": attachment_id, "http_status": code})
    return _run("jira_delete_attachment", fn, timeout=timeout, email=email, token=token, base_url=base_url)


def jira_delete_comment(
    issue_key: str, comment_id: str, *, confirm: bool = False, timeout: float = 30.0,
    email: Optional[str] = None, token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete a comment on an issue (destructive; needs confirm=true)."""
    if not confirm:
        return _needs_confirm("jira_delete_comment",
                              f"Delete comment {comment_id} on {issue_key} (no undo).")

    def fn(c: JiraClient) -> dict:
        code = c.delete_comment(issue_key, comment_id)
        return ok_envelope(kind="jira_delete_comment", result={
            "issue_key": issue_key, "comment_id": comment_id, "http_status": code,
        })
    return _run("jira_delete_comment", fn, timeout=timeout, email=email, token=token, base_url=base_url)


def jira_transition_issue(
    issue_key: str, transition_id: str, *, confirm: bool = False, timeout: float = 30.0,
    email: Optional[str] = None, token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply a workflow transition, validated against the live list (needs confirm=true)."""
    if not confirm:
        return _needs_confirm("jira_transition_issue",
                              f"Transition {issue_key} via transition id {transition_id}.")

    def fn(c: JiraClient) -> dict:
        available = c.list_transitions(issue_key)
        match = next((t for t in available if str(t.get("id")) == str(transition_id)), None)
        if match is None:
            legal = ", ".join(
                f"{t.get('id')}={t.get('name')!r}->{(t.get('to') or {}).get('name')!r}"
                for t in available
            ) or "(none -- terminal status or no permission)"
            return error_envelope(
                f"transition_id={transition_id!r} is not valid for "
                f"{issue_key} right now. Valid: [{legal}].",
                kind="jira_transition_issue", status="bad_argument",
                next_actions=["List transitions first to see valid ids."],
                result={"issue_key": issue_key, "available_transitions": available},
            )
        c.transition_issue(issue_key, str(transition_id))
        to = match.get("to") or {}
        return ok_envelope(kind="jira_transition_issue", result={
            "issue_key": issue_key,
            "transition": {"id": match.get("id"), "name": match.get("name")},
            "to_status": to.get("name"),
        })
    return _run("jira_transition_issue", fn, timeout=timeout, email=email, token=token, base_url=base_url)


def register(mcp) -> None:
    """Wire the Jira tools onto a FastMCP (or compatible) instance."""
    mcp.tool()(jira_whoami)
    mcp.tool()(jira_status)
    mcp.tool()(jira_list_watchers)
    mcp.tool()(jira_list_attachments)
    mcp.tool()(jira_list_transitions)
    mcp.tool()(jira_add_watcher)
    mcp.tool()(jira_upload_attachment)
    mcp.tool()(jira_remove_watcher)
    mcp.tool()(jira_delete_attachment)
    mcp.tool()(jira_delete_comment)
    mcp.tool()(jira_transition_issue)
