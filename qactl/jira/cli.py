"""``qactl jira ...`` — Jira Cloud watchers, attachments, comments, transitions.

Fills the same gaps the local atlassian-mcp filled (watcher CRUD,
attachment upload/delete, comment delete, workflow transitions) plus a
quick ``status`` read — but as shell subcommands with ``--json``, real
exit codes, and a ``--yes`` gate on the destructive ones.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, Tuple

from qactl.core.common import confirm_or_exit, resolve_timeout
from qactl.core.creds import CredentialError
from qactl.core.envelope import error_envelope, ok_envelope
from qactl.core.output import emit
from qactl.jira.client import JiraClient, JiraError


def _client(args: argparse.Namespace, *, kind: str) -> Tuple[Optional[JiraClient], Optional[dict]]:
    try:
        client = JiraClient.from_env(
            timeout=resolve_timeout(args, 30.0),
            email=getattr(args, "email", None),
            api_token=getattr(args, "token", None),
            base_url=getattr(args, "base_url", None),
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


def _run(args, *, kind, fn):
    """Build a client, run ``fn(client)``, and emit the resulting envelope."""
    client, err = _client(args, kind=kind)
    if err is not None:
        return emit(err, as_json=args.json)
    try:
        env = fn(client)
    except JiraError as e:
        env = _jira_err(e, kind=kind)
    except Exception as e:  # noqa: BLE001
        env = error_envelope(f"{kind} failed: {e}", kind=kind)
    return emit(env, as_json=args.json)


# ---- handlers ------------------------------------------------------------

def _whoami(args):
    def fn(c):
        me = c.myself()
        return ok_envelope(kind="jira_whoami", result={
            "account_id": me.get("accountId"),
            "email": me.get("emailAddress"),
            "display_name": me.get("displayName"),
            "active": me.get("active"),
            "time_zone": me.get("timeZone"),
        })
    return _run(args, kind="jira_whoami", fn=fn)


def _status(args):
    return _run(args, kind="jira_status",
                fn=lambda c: ok_envelope(kind="jira_status",
                                         result=c.get_issue_status(args.issue_key)))


def _watchers_list(args):
    def fn(c):
        data = c.list_watchers(args.issue_key)
        return ok_envelope(kind="jira_list_watchers", result={
            "issue_key": args.issue_key,
            "is_watching": bool(data.get("isWatching")),
            "watch_count": int(data.get("watchCount") or 0),
            "watchers": list(data.get("watchers") or []),
        })
    return _run(args, kind="jira_list_watchers", fn=fn)


def _watchers_add(args):
    def fn(c):
        code = c.add_watcher(args.issue_key, args.account_id)
        return ok_envelope(kind="jira_add_watcher", result={
            "issue_key": args.issue_key, "account_id": args.account_id,
            "http_status": code,
        })
    return _run(args, kind="jira_add_watcher", fn=fn)


def _watchers_remove(args):
    rc = confirm_or_exit(args, kind="jira_remove_watcher",
                         action=f"Remove watcher {args.account_id} from {args.issue_key}.")
    if rc is not None:
        return rc

    def fn(c):
        code = c.remove_watcher(args.issue_key, args.account_id)
        return ok_envelope(kind="jira_remove_watcher", result={
            "issue_key": args.issue_key, "account_id": args.account_id,
            "http_status": code,
        })
    return _run(args, kind="jira_remove_watcher", fn=fn)


def _attachments_list(args):
    def fn(c):
        raw = c.list_attachments(args.issue_key)
        summary = [{
            "id": a.get("id"),
            "filename": a.get("filename"),
            "size": a.get("size"),
            "mime_type": a.get("mimeType"),
            "created": a.get("created"),
            "author_display_name": (a.get("author") or {}).get("displayName"),
            "content_url": a.get("content"),
        } for a in raw]
        return ok_envelope(kind="jira_list_attachments", result={
            "issue_key": args.issue_key, "count": len(summary),
            "attachments": summary,
        })
    return _run(args, kind="jira_list_attachments", fn=fn)


def _attachments_upload(args):
    path = Path(args.file)
    if not path.is_file():
        return emit(error_envelope(f"not a file: {path}", kind="jira_upload_attachment",
                                   status="bad_argument"), as_json=args.json)

    def fn(c):
        created = c.upload_attachment(args.issue_key, path, name=args.name)
        return ok_envelope(kind="jira_upload_attachment", result={
            "issue_key": args.issue_key,
            "attachments": [{
                "id": a.get("id"), "filename": a.get("filename"),
                "size": a.get("size"), "content_url": a.get("content"),
            } for a in created],
        })
    return _run(args, kind="jira_upload_attachment", fn=fn)


def _attachments_delete(args):
    rc = confirm_or_exit(args, kind="jira_delete_attachment",
                         action=f"Delete attachment {args.attachment_id} (no undo).")
    if rc is not None:
        return rc

    def fn(c):
        code = c.delete_attachment(args.attachment_id)
        return ok_envelope(kind="jira_delete_attachment",
                           result={"attachment_id": args.attachment_id, "http_status": code})
    return _run(args, kind="jira_delete_attachment", fn=fn)


def _comment_delete(args):
    rc = confirm_or_exit(args, kind="jira_delete_comment",
                         action=f"Delete comment {args.comment_id} on {args.issue_key} (no undo).")
    if rc is not None:
        return rc

    def fn(c):
        code = c.delete_comment(args.issue_key, args.comment_id)
        return ok_envelope(kind="jira_delete_comment", result={
            "issue_key": args.issue_key, "comment_id": args.comment_id,
            "http_status": code,
        })
    return _run(args, kind="jira_delete_comment", fn=fn)


def _transitions_list(args):
    def fn(c):
        ts = c.list_transitions(args.issue_key)
        return ok_envelope(kind="jira_list_transitions", result={
            "issue_key": args.issue_key,
            "transitions": [{
                "id": t.get("id"), "name": t.get("name"),
                "to_status": (t.get("to") or {}).get("name"),
                "has_screen": t.get("hasScreen"),
            } for t in ts],
        })
    return _run(args, kind="jira_list_transitions", fn=fn)


def _transitions_do(args):
    rc = confirm_or_exit(args, kind="jira_transition_issue",
                         action=f"Transition {args.issue_key} via transition id {args.transition_id}.")
    if rc is not None:
        return rc

    def fn(c):
        available = c.list_transitions(args.issue_key)
        match = next((t for t in available if str(t.get("id")) == str(args.transition_id)), None)
        if match is None:
            legal = ", ".join(
                f"{t.get('id')}={t.get('name')!r}->{(t.get('to') or {}).get('name')!r}"
                for t in available
            ) or "(none — terminal status or no permission)"
            return error_envelope(
                f"transition_id={args.transition_id!r} is not valid for "
                f"{args.issue_key} right now. Valid: [{legal}].",
                kind="jira_transition_issue", status="bad_argument",
                next_actions=["qactl jira transitions list <issue> to see valid ids."],
                result={"issue_key": args.issue_key, "available_transitions": available},
            )
        c.transition_issue(args.issue_key, str(args.transition_id))
        to = match.get("to") or {}
        return ok_envelope(kind="jira_transition_issue", result={
            "issue_key": args.issue_key,
            "transition": {"id": match.get("id"), "name": match.get("name")},
            "to_status": to.get("name"),
        })
    return _run(args, kind="jira_transition_issue", fn=fn)


# ---- registration --------------------------------------------------------

def _add_cred_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("atlassian credentials (default: environment)")
    g.add_argument("--email", default=None, help="override $ATLASSIAN_EMAIL")
    g.add_argument("--token", default=None, help="override $ATLASSIAN_API_TOKEN")
    g.add_argument("--base-url", default=None, dest="base_url",
                   help="override $ATLASSIAN_BASE_URL")


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("jira", help="Jira Cloud (watchers, attachments, comments, transitions)")
    sub = grp.add_subparsers(dest="cmd", required=True)

    def leaf(name, **kw):
        p = sub.add_parser(name, parents=[parent], **kw)
        _add_cred_flags(p)
        return p

    leaf("whoami", help="resolve the token to a Jira user").set_defaults(func=_whoami)

    p = leaf("status", help="issue status + summary")
    p.add_argument("issue_key")
    p.set_defaults(func=_status)

    w = sub.add_parser("watchers", help="watcher list / add / remove")
    ws = w.add_subparsers(dest="subcmd", required=True)
    p = ws.add_parser("list", parents=[parent]); _add_cred_flags(p)
    p.add_argument("issue_key"); p.set_defaults(func=_watchers_list)
    p = ws.add_parser("add", parents=[parent]); _add_cred_flags(p)
    p.add_argument("issue_key"); p.add_argument("account_id"); p.set_defaults(func=_watchers_add)
    p = ws.add_parser("remove", parents=[parent], help="(--yes)"); _add_cred_flags(p)
    p.add_argument("issue_key"); p.add_argument("account_id"); p.set_defaults(func=_watchers_remove)

    a = sub.add_parser("attachments", help="attachment list / upload / delete")
    asub = a.add_subparsers(dest="subcmd", required=True)
    p = asub.add_parser("list", parents=[parent]); _add_cred_flags(p)
    p.add_argument("issue_key"); p.set_defaults(func=_attachments_list)
    p = asub.add_parser("upload", parents=[parent]); _add_cred_flags(p)
    p.add_argument("issue_key"); p.add_argument("file")
    p.add_argument("--name", default=None, help="override stored filename")
    p.set_defaults(func=_attachments_upload)
    p = asub.add_parser("delete", parents=[parent], help="(--yes)"); _add_cred_flags(p)
    p.add_argument("attachment_id"); p.set_defaults(func=_attachments_delete)

    c = sub.add_parser("comment", help="comment delete")
    csub = c.add_subparsers(dest="subcmd", required=True)
    p = csub.add_parser("delete", parents=[parent], help="(--yes)"); _add_cred_flags(p)
    p.add_argument("issue_key"); p.add_argument("comment_id"); p.set_defaults(func=_comment_delete)

    t = sub.add_parser("transitions", help="workflow transition list / do")
    tsub = t.add_subparsers(dest="subcmd", required=True)
    p = tsub.add_parser("list", parents=[parent]); _add_cred_flags(p)
    p.add_argument("issue_key"); p.set_defaults(func=_transitions_list)
    p = tsub.add_parser("do", parents=[parent], help="(--yes; validated against live list)")
    _add_cred_flags(p)
    p.add_argument("issue_key"); p.add_argument("transition_id"); p.set_defaults(func=_transitions_do)
