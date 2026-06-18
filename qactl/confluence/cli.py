"""``qactl confluence ...`` — post comments (with optional file attach),
list a page's comments/attachments, and delete content by id.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, Tuple

from qactl.core.common import confirm_or_exit, resolve_timeout
from qactl.core.creds import CredentialError
from qactl.core.envelope import error_envelope, ok_envelope
from qactl.core.output import emit
from qactl.confluence.client import ConfluenceClient, ConfluenceError


def _client(args, *, kind) -> Tuple[Optional[ConfluenceClient], Optional[dict]]:
    try:
        return ConfluenceClient.from_env(
            timeout=resolve_timeout(args, 60.0),
            email=getattr(args, "email", None),
            api_token=getattr(args, "token", None),
            base_url=getattr(args, "base_url", None),
        ), None
    except CredentialError as e:
        return None, error_envelope(str(e), kind=kind, status="bad_argument")


def _run(args, *, kind, fn):
    client, err = _client(args, kind=kind)
    if err is not None:
        return emit(err, as_json=args.json)
    try:
        env = fn(client)
    except ConfluenceError as e:
        env = error_envelope(
            f"Confluence REST {e.method} -> HTTP {e.status_code}: {e.body[:300]}",
            kind=kind,
            next_actions=["Check the page/content id and that the token can edit this space."],
            result={"http_status": e.status_code, "http_body": e.body[:1000]},
        )
    except Exception as e:  # noqa: BLE001
        env = error_envelope(f"{kind} failed: {e}", kind=kind)
    return emit(env, as_json=args.json)


def _list(args):
    def fn(c):
        return ok_envelope(kind="confluence_list", result={
            "page_id": args.page_id,
            "attachments": c.list_attachments(args.page_id),
            "comments": c.list_comments(args.page_id),
        })
    return _run(args, kind="confluence_list", fn=fn)


def _comment(args):
    if not args.text and not args.attach:
        return emit(error_envelope(
            "nothing to post: provide --text and/or --attach",
            kind="confluence_comment", status="bad_argument"), as_json=args.json)

    def fn(c):
        attach_name = None
        attached = None
        if args.attach:
            path = Path(args.attach)
            if not path.is_file():
                return error_envelope(f"not a file: {path}", kind="confluence_comment",
                                      status="bad_argument")
            att = c.upload_attachment(args.page_id, path)
            attach_name = att["title"]
            attached = att
        body = c.build_comment_body(args.text, attach_name)
        comment_id = c.post_comment(args.page_id, body)
        return ok_envelope(kind="confluence_comment", result={
            "page_id": args.page_id, "comment_id": comment_id, "attachment": attached,
        })
    return _run(args, kind="confluence_comment", fn=fn)


def _delete(args):
    rc = confirm_or_exit(args, kind="confluence_delete",
                         action=f"Delete Confluence content {args.content_id} (comment or attachment, no undo).")
    if rc is not None:
        return rc

    def fn(c):
        code = c.delete_content(args.content_id)
        return ok_envelope(kind="confluence_delete",
                           result={"content_id": args.content_id, "http_status": code})
    return _run(args, kind="confluence_delete", fn=fn)


def _add_cred_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("atlassian credentials (default: environment)")
    g.add_argument("--email", default=None, help="override $ATLASSIAN_EMAIL")
    g.add_argument("--token", default=None, help="override $ATLASSIAN_API_TOKEN")
    g.add_argument("--base-url", default=None, dest="base_url",
                   help="override $ATLASSIAN_BASE_URL")


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser("confluence", help="Confluence Cloud (comments, attachments)")
    sub = grp.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("comment", parents=[parent],
                       help="post a comment (with optional --attach file)")
    _add_cred_flags(p)
    p.add_argument("page_id")
    p.add_argument("--text", default=None, help="comment body (plain text)")
    p.add_argument("--attach", default=None, help="file to attach to the page and embed")
    p.set_defaults(func=_comment)

    p = sub.add_parser("list", parents=[parent], help="list a page's comments + attachments")
    _add_cred_flags(p)
    p.add_argument("page_id")
    p.set_defaults(func=_list)

    p = sub.add_parser("delete", parents=[parent], help="delete a comment or attachment by id (--yes)")
    _add_cred_flags(p)
    p.add_argument("content_id")
    p.set_defaults(func=_delete)
