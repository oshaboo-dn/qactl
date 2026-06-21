"""Confluence tool layer: envelope-returning functions for both fronts.

Shared by the CLI ([qactl confluence ...]) and the stdio MCP server. The
CLI resolves a comment body from inline / file / stdin before calling
:func:`confluence_comment`; the MCP passes ``text`` directly. ``delete``
is destructive and gated by ``confirm`` for the MCP side.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from qactl.core.creds import CredentialError
from qactl.core.envelope import error_envelope, ok_envelope
from qactl.confluence.client import ConfluenceClient, ConfluenceError


def _client(
    kind: str, *, timeout: float = 60.0, email: Optional[str] = None,
    token: Optional[str] = None, base_url: Optional[str] = None,
) -> Tuple[Optional[ConfluenceClient], Optional[dict]]:
    try:
        return ConfluenceClient.from_env(
            timeout=timeout, email=email, api_token=token, base_url=base_url,
        ), None
    except CredentialError as e:
        return None, error_envelope(str(e), kind=kind, status="bad_argument")


def _run(
    kind: str, fn: Callable[[ConfluenceClient], dict], *,
    timeout: float = 60.0, email: Optional[str] = None,
    token: Optional[str] = None, base_url: Optional[str] = None,
) -> dict:
    client, err = _client(kind, timeout=timeout, email=email, token=token, base_url=base_url)
    if err is not None:
        return err
    try:
        return fn(client)
    except ConfluenceError as e:
        return error_envelope(
            f"Confluence REST {e.method} -> HTTP {e.status_code}: {e.body[:300]}",
            kind=kind,
            next_actions=["Check the page/content id and that the token can edit this space."],
            result={"http_status": e.status_code, "http_body": e.body[:1000]},
        )
    except Exception as e:  # noqa: BLE001
        return error_envelope(f"{kind} failed: {e}", kind=kind)


def confluence_list(
    page_id: str, *, timeout: float = 60.0, email: Optional[str] = None,
    token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """List a page's comments and attachments."""
    def fn(c: ConfluenceClient) -> dict:
        return ok_envelope(kind="confluence_list", result={
            "page_id": page_id,
            "attachments": c.list_attachments(page_id),
            "comments": c.list_comments(page_id),
        })
    return _run("confluence_list", fn, timeout=timeout, email=email, token=token, base_url=base_url)


def confluence_comment(
    page_id: str, text: Optional[str] = None, attach: Optional[str] = None, *,
    timeout: float = 60.0, email: Optional[str] = None,
    token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Post a comment on a page, optionally attaching+embedding a local file."""
    if not text and not attach:
        return error_envelope("nothing to post: provide text and/or attach",
                              kind="confluence_comment", status="bad_argument")

    def fn(c: ConfluenceClient) -> dict:
        attach_name = None
        attached = None
        if attach:
            path = Path(attach)
            if not path.is_file():
                return error_envelope(f"not a file: {path}", kind="confluence_comment",
                                      status="bad_argument")
            att = c.upload_attachment(page_id, path)
            attach_name = att["title"]
            attached = att
        body = c.build_comment_body(text, attach_name)
        comment_id = c.post_comment(page_id, body)
        return ok_envelope(kind="confluence_comment", result={
            "page_id": page_id, "comment_id": comment_id, "attachment": attached,
        })
    return _run("confluence_comment", fn, timeout=timeout, email=email, token=token, base_url=base_url)


def confluence_delete(
    content_id: str, *, confirm: bool = False, timeout: float = 60.0,
    email: Optional[str] = None, token: Optional[str] = None, base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete a comment or attachment by id (destructive; needs confirm=true)."""
    if not confirm:
        return error_envelope(
            f"Refusing destructive operation without confirm=true: delete content {content_id}.",
            kind="confluence_delete", status="confirmation_required",
            next_actions=["Re-call with confirm=true to proceed."],
        )

    def fn(c: ConfluenceClient) -> dict:
        code = c.delete_content(content_id)
        return ok_envelope(kind="confluence_delete",
                           result={"content_id": content_id, "http_status": code})
    return _run("confluence_delete", fn, timeout=timeout, email=email, token=token, base_url=base_url)


def register(mcp) -> None:
    """Wire the Confluence tools onto a FastMCP (or compatible) instance."""
    mcp.tool()(confluence_list)
    mcp.tool()(confluence_comment)
    mcp.tool()(confluence_delete)
