"""Thin Confluence Cloud REST client (wiki API).

Shares one Atlassian token with Jira (same site, same
:class:`qactl.core.creds.AtlassianConfig`). Lifted from the workspace
``confluence_comment.py`` helper, which exists because the Confluence
MCP can read pages/comments but cannot attach files — so this hits the
REST API directly.

Posting a file as a *comment* (rather than a bare page attachment) puts
it in the page's comment thread where it's immediately visible; the file
is still attached to the page and the comment body embeds a link to it
via Confluence storage format.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape

import requests

from qactl.core.creds import AtlassianConfig


class ConfluenceError(RuntimeError):
    def __init__(self, status_code: int, body: str, *, method: str, url: str):
        self.status_code = status_code
        self.body = body
        self.method = method
        self.url = url
        super().__init__(
            f"Confluence REST {method} {url} -> HTTP {status_code} body={body[:300]!r}"
        )


class ConfluenceClient:
    def __init__(self, cfg: AtlassianConfig, timeout: float = 60.0):
        self.cfg = cfg
        self.timeout = timeout
        self.api = f"{cfg.base_url}/wiki/rest/api"
        self._session = requests.Session()
        self._session.auth = (cfg.email, cfg.api_token)
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "qactl/0.1 (+local)",
        })

    def _check(self, r: requests.Response, *, method: str) -> None:
        if 200 <= r.status_code < 300:
            return
        raise ConfluenceError(r.status_code, r.text or "", method=method, url=r.url)

    def list_attachments(self, page_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            r = self._session.get(
                f"{self.api}/content/{page_id}/child/attachment",
                params={"limit": 200, "start": start}, timeout=self.timeout,
            )
            self._check(r, method="GET")
            data = r.json()
            for a in data.get("results", []):
                out.append({
                    "id": a["id"], "title": a["title"],
                    "size": (a.get("extensions") or {}).get("fileSize", 0),
                })
            start += data.get("limit", 200)
            if start >= data.get("totalSize", 0) or not data.get("results"):
                break
        return out

    def list_comments(self, page_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            r = self._session.get(
                f"{self.api}/content/{page_id}/child/comment",
                params={"limit": 200, "start": start, "expand": "body.view"},
                timeout=self.timeout,
            )
            self._check(r, method="GET")
            data = r.json()
            for c in data.get("results", []):
                body = ((c.get("body") or {}).get("view") or {}).get("value", "") or ""
                out.append({"id": c["id"], "snippet": " ".join(body.split())[:80]})
            start += data.get("limit", 200)
            if start >= data.get("totalSize", 0) or not data.get("results"):
                break
        return out

    def upload_attachment(self, page_id: str, file_path: Path) -> dict[str, Any]:
        """Upload to a page, replacing any same-named attachment first."""
        target = file_path.name
        for a in self.list_attachments(page_id):
            if a["title"] == target:
                self.delete_content(a["id"])
        url = f"{self.api}/content/{page_id}/child/attachment"
        with file_path.open("rb") as fh:
            r = self._session.post(
                url,
                headers={"X-Atlassian-Token": "nocheck"},
                files={"file": (target, fh)},
                timeout=max(self.timeout, 300.0),
            )
        self._check(r, method="POST")
        results = (r.json() or {}).get("results", [])
        if not results:
            raise ConfluenceError(r.status_code, r.text or "", method="POST", url=url)
        att = results[0]
        return {"id": att["id"], "title": att["title"]}

    def post_comment(self, page_id: str, body_storage: str) -> str:
        payload = {
            "type": "comment",
            "container": {"id": page_id, "type": "page"},
            "body": {"storage": {"value": body_storage, "representation": "storage"}},
        }
        r = self._session.post(
            f"{self.api}/content",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload), timeout=self.timeout,
        )
        self._check(r, method="POST")
        return r.json()["id"]

    def delete_content(self, content_id: str) -> int:
        r = self._session.delete(f"{self.api}/content/{content_id}", timeout=self.timeout)
        if r.status_code in (200, 204):
            return r.status_code
        self._check(r, method="DELETE")
        return r.status_code

    @staticmethod
    def build_comment_body(text: Optional[str], attach_name: Optional[str]) -> str:
        parts: list[str] = []
        if text:
            parts.append(f"<p>{escape(text)}</p>")
        if attach_name:
            if not text:
                parts.append(f"<p>Attached: {escape(attach_name)}</p>")
            parts.append(
                f'<p><ac:link><ri:attachment '
                f'ri:filename="{escape(attach_name, {chr(34): "&quot;"})}"/></ac:link></p>'
            )
        return "".join(parts)

    @classmethod
    def from_env(cls, timeout: float = 60.0, **overrides: Optional[str]) -> "ConfluenceClient":
        return cls(AtlassianConfig.resolve(**overrides), timeout=timeout)
