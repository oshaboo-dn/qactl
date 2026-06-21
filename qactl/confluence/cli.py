"""``qactl confluence ...`` — post comments (with optional file attach),
list a page's comments/attachments, and delete content by id.

Thin argparse front over :mod:`qactl.confluence.tools` (the same envelope
layer the stdio MCP server exposes). The CLI resolves the comment body
from inline / ``--text-file`` / stdin before handing it off.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict

from qactl.core.common import confirm_or_exit, resolve_timeout
from qactl.core.envelope import error_envelope
from qactl.core.output import emit, read_payload
from qactl.confluence import tools


def _creds(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "timeout": resolve_timeout(args, 60.0),
        "email": getattr(args, "email", None),
        "token": getattr(args, "token", None),
        "base_url": getattr(args, "base_url", None),
    }


def _list(args):
    return emit(tools.confluence_list(args.page_id, **_creds(args)), as_json=args.json)


def _comment(args):
    try:
        text = read_payload(args.text, args.text_file)
    except OSError as e:
        return emit(error_envelope(f"cannot read --text-file: {e}",
                                   kind="confluence_comment", status="bad_argument"),
                    as_json=args.json)
    return emit(tools.confluence_comment(args.page_id, text=text, attach=args.attach,
                                         **_creds(args)), as_json=args.json)


def _delete(args):
    rc = confirm_or_exit(args, kind="confluence_delete",
                         action=f"Delete Confluence content {args.content_id} (comment or attachment, no undo).")
    if rc is not None:
        return rc
    return emit(tools.confluence_delete(args.content_id, confirm=True, **_creds(args)),
                as_json=args.json)


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
    p.add_argument("--text", default=None,
                   help="comment body (plain text); '-' reads stdin")
    p.add_argument("--text-file", default=None, dest="text_file",
                   help="read the comment body from a file (wins over --text)")
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
