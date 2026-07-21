"""``qactl jira ...`` — Jira Cloud watchers, attachments, comments, transitions.

Thin argparse front: every handler resolves args, applies the ``--yes`` /
TTY confirm gate for destructive ops, then calls the shared envelope layer
in :mod:`qactl.jira.tools` (the same functions the stdio MCP server
exposes) and prints the result via :func:`qactl.core.output.emit`.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict

from qactl.core.common import confirm_or_exit, resolve_timeout
from qactl.core.envelope import error_envelope
from qactl.core.output import emit, read_payload
from qactl.jira import tools


def _creds(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "timeout": resolve_timeout(args, 30.0),
        "email": getattr(args, "email", None),
        "token": getattr(args, "token", None),
        "base_url": getattr(args, "base_url", None),
    }


# ---- handlers ------------------------------------------------------------

def _whoami(args):
    return emit(tools.jira_whoami(**_creds(args)), as_json=args.json)


def _status(args):
    keys = args.issue_key
    if len(keys) == 1:
        return emit(tools.jira_status(keys[0], **_creds(args)), as_json=args.json)
    return emit(tools.jira_status_bulk(keys, **_creds(args)), as_json=args.json)


def _watchers_list(args):
    return emit(tools.jira_list_watchers(args.issue_key, **_creds(args)), as_json=args.json)


def _watchers_add(args):
    return emit(tools.jira_add_watcher(args.issue_key, args.account_id, **_creds(args)),
                as_json=args.json)


def _watchers_remove(args):
    rc = confirm_or_exit(args, kind="jira_remove_watcher",
                         action=f"Remove watcher {args.account_id} from {args.issue_key}.")
    if rc is not None:
        return rc
    return emit(tools.jira_remove_watcher(args.issue_key, args.account_id,
                                          confirm=True, **_creds(args)), as_json=args.json)


def _attachments_list(args):
    return emit(tools.jira_list_attachments(args.issue_key, **_creds(args)), as_json=args.json)


def _attachments_upload(args):
    return emit(tools.jira_upload_attachment(args.issue_key, args.file, name=args.name,
                                             **_creds(args)), as_json=args.json)


def _attachments_delete(args):
    rc = confirm_or_exit(args, kind="jira_delete_attachment",
                         action=f"Delete attachment {args.attachment_id} (no undo).")
    if rc is not None:
        return rc
    return emit(tools.jira_delete_attachment(args.attachment_id, confirm=True, **_creds(args)),
                as_json=args.json)


def _comment_add(args):
    try:
        text = read_payload(args.text, args.text_file)
    except Exception as e:  # noqa: BLE001
        return emit(error_envelope(f"cannot read --text-file: {e}",
                                   kind="jira_add_comment", status="bad_argument"),
                    as_json=args.json)
    return emit(tools.jira_add_comment(args.issue_key, text or "", **_creds(args)),
                as_json=args.json)


def _comment_delete(args):
    rc = confirm_or_exit(args, kind="jira_delete_comment",
                         action=f"Delete comment {args.comment_id} on {args.issue_key} (no undo).")
    if rc is not None:
        return rc
    return emit(tools.jira_delete_comment(args.issue_key, args.comment_id,
                                          confirm=True, **_creds(args)), as_json=args.json)


def _transitions_list(args):
    return emit(tools.jira_list_transitions(args.issue_key, **_creds(args)), as_json=args.json)


def _transitions_do(args):
    rc = confirm_or_exit(args, kind="jira_transition_issue",
                         action=f"Transition {args.issue_key} via transition id {args.transition_id}.")
    if rc is not None:
        return rc
    return emit(tools.jira_transition_issue(args.issue_key, args.transition_id,
                                            confirm=True, **_creds(args)), as_json=args.json)


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

    p = leaf("status", help="issue status + summary + assignee + story points; several keys = one bulk "
                            "envelope (falls back to JSM service-desk on 404)")
    p.add_argument("issue_key", nargs="+")
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

    c = sub.add_parser("comment", help="comment add / delete")
    csub = c.add_subparsers(dest="subcmd", required=True)
    p = csub.add_parser("add", parents=[parent], help="post a plain-text comment (ADF)")
    _add_cred_flags(p)
    p.add_argument("issue_key")
    p.add_argument("--text", default=None, help="comment body (plain text); '-' reads stdin")
    p.add_argument("--text-file", default=None, dest="text_file",
                   help="read the comment body from a file (wins over --text)")
    p.set_defaults(func=_comment_add)
    p = csub.add_parser("delete", parents=[parent], help="(--yes)"); _add_cred_flags(p)
    p.add_argument("issue_key"); p.add_argument("comment_id"); p.set_defaults(func=_comment_delete)

    t = sub.add_parser("transitions", help="workflow transition list / do")
    tsub = t.add_subparsers(dest="subcmd", required=True)
    p = tsub.add_parser("list", parents=[parent]); _add_cred_flags(p)
    p.add_argument("issue_key"); p.set_defaults(func=_transitions_list)
    p = tsub.add_parser("do", parents=[parent], help="(--yes; validated against live list)")
    _add_cred_flags(p)
    p.add_argument("issue_key"); p.add_argument("transition_id"); p.set_defaults(func=_transitions_do)
