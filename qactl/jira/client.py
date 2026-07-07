"""Thin Jira Cloud REST v3 client.

Wraps a ``requests.Session`` with HTTP-basic auth (email + API token
from :class:`qactl.core.creds.AtlassianConfig`). Methods return raw
decoded JSON (or ``None`` / status code for 204 responses); the CLI
layer shapes them into the qactl envelope.

Lifted from the local atlassian-mcp ``jira_client`` plus the workspace
``jira_upload`` / status helpers, collapsed into one client. The only
behavioural change vs. the MCP is the credential source: the MCP read
per-request HTTP headers; here credentials come from the environment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote

import requests

from qactl.core.creds import AtlassianConfig

# Jira Cloud custom field holding Story Points on this site (#85).
STORY_POINTS_FIELD = "customfield_10023"


class JiraError(RuntimeError):
    """Raised when the Jira REST API returns a non-2xx status."""

    def __init__(self, status_code: int, body: str, *, method: str, url: str):
        self.status_code = status_code
        self.body = body
        self.method = method
        self.url = url
        super().__init__(
            f"Jira REST {method} {url} -> HTTP {status_code} body={body[:300]!r}"
        )


class JiraClient:
    """Minimal Jira Cloud REST v3 client (one per invocation)."""

    def __init__(self, cfg: AtlassianConfig, timeout: float = 30.0):
        self.cfg = cfg
        self.timeout = timeout
        self._session = requests.Session()
        self._session.auth = (cfg.email, cfg.api_token)
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "qactl/0.1 (+local)",
        })

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.cfg.base_url}{path}"

    def _check(self, r: requests.Response, *, method: str) -> None:
        if 200 <= r.status_code < 300:
            return
        raise JiraError(r.status_code, r.text or "", method=method, url=r.url)

    # ---- diagnostics -------------------------------------------------

    def myself(self) -> dict[str, Any]:
        r = self._session.get(self._url("/rest/api/3/myself"), timeout=self.timeout)
        self._check(r, method="GET")
        return r.json()

    # ---- status ------------------------------------------------------

    def get_issue_status(self, issue_key: str) -> dict[str, Any]:
        """Return the issue's status, summary, assignee and story points (one GET).

        Falls back to the JSM service-desk API on a 404: portal customers
        lack Browse-Project permission on a service desk, so a JSM ticket
        (e.g. ``HD-*``) is invisible to ``/rest/api/3/issue`` but readable
        via ``/rest/servicedeskapi/request/{key}``. The fallback only fires
        on 404, so a real auth error (401/403) still surfaces unchanged.
        """
        r = self._session.get(
            self._url(f"/rest/api/3/issue/{quote(issue_key, safe='')}"),
            params={"fields": "status,summary,assignee," + STORY_POINTS_FIELD},
            timeout=self.timeout,
        )
        if r.status_code == 404:
            return self._get_servicedesk_status(issue_key)
        self._check(r, method="GET")
        fields = (r.json() or {}).get("fields") or {}
        status = fields.get("status") or {}
        return {
            "issue_key": issue_key,
            "summary": fields.get("summary"),
            "status": status.get("name"),
            "status_category": ((status.get("statusCategory") or {}).get("name")),
            "assignee": ((fields.get("assignee") or {}).get("displayName")),
            "story_points": fields.get(STORY_POINTS_FIELD),
            "source": "jira",
        }

    def _get_servicedesk_status(self, issue_key: str) -> dict[str, Any]:
        """Status + summary for a JSM service-desk request (portal ticket).

        Used as the 404 fallback for :meth:`get_issue_status`. If the key
        is genuinely missing this GET 404s too, so :meth:`_check` raises the
        usual :class:`JiraError` and the not-found behaviour is preserved.
        """
        r = self._session.get(
            self._url(
                f"/rest/servicedeskapi/request/{quote(issue_key, safe='')}"
            ),
            timeout=self.timeout,
        )
        self._check(r, method="GET")
        data = r.json() or {}
        current = data.get("currentStatus") or {}
        summary = None
        for fv in data.get("requestFieldValues") or []:
            if fv.get("fieldId") == "summary":
                summary = fv.get("value")
                break
        return {
            "issue_key": data.get("issueKey") or issue_key,
            "summary": summary,
            "status": current.get("status"),
            "status_category": current.get("statusCategory"),
            "assignee": None,
            "story_points": None,
            "source": "servicedesk",
        }

    # ---- watchers ----------------------------------------------------

    def list_watchers(self, issue_key: str) -> dict[str, Any]:
        r = self._session.get(
            self._url(f"/rest/api/3/issue/{quote(issue_key, safe='')}/watchers"),
            timeout=self.timeout,
        )
        self._check(r, method="GET")
        return r.json()

    def add_watcher(self, issue_key: str, account_id: str) -> int:
        """POST a bare-JSON-string accountId body. Success = 204."""
        r = self._session.post(
            self._url(f"/rest/api/3/issue/{quote(issue_key, safe='')}/watchers"),
            data=json.dumps(account_id),
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        self._check(r, method="POST")
        return r.status_code

    def remove_watcher(self, issue_key: str, account_id: str) -> int:
        r = self._session.delete(
            self._url(f"/rest/api/3/issue/{quote(issue_key, safe='')}/watchers"),
            params={"accountId": account_id},
            timeout=self.timeout,
        )
        self._check(r, method="DELETE")
        return r.status_code

    # ---- attachments -------------------------------------------------

    def list_attachments(self, issue_key: str) -> list[dict[str, Any]]:
        r = self._session.get(
            self._url(f"/rest/api/3/issue/{quote(issue_key, safe='')}"),
            params={"fields": "attachment"},
            timeout=self.timeout,
        )
        self._check(r, method="GET")
        data = r.json()
        return list(((data.get("fields") or {}).get("attachment") or []))

    def upload_attachment(
        self, issue_key: str, file_path: Path, name: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """POST a multipart attachment. Returns the created attachment list."""
        url = self._url(f"/rest/api/3/issue/{quote(issue_key, safe='')}/attachments")
        with file_path.open("rb") as fh:
            r = self._session.post(
                url,
                headers={"X-Atlassian-Token": "no-check"},
                files={"file": (name or file_path.name, fh)},
                timeout=max(self.timeout, 120.0),
            )
        self._check(r, method="POST")
        return list(r.json() or [])

    def delete_attachment(self, attachment_id: str) -> int:
        r = self._session.delete(
            self._url(f"/rest/api/3/attachment/{quote(str(attachment_id), safe='')}"),
            timeout=self.timeout,
        )
        self._check(r, method="DELETE")
        return r.status_code

    # ---- comments ----------------------------------------------------

    def delete_comment(self, issue_key: str, comment_id: str) -> int:
        r = self._session.delete(
            self._url(
                f"/rest/api/3/issue/{quote(issue_key, safe='')}"
                f"/comment/{quote(str(comment_id), safe='')}"
            ),
            timeout=self.timeout,
        )
        self._check(r, method="DELETE")
        return r.status_code

    # ---- transitions -------------------------------------------------

    def list_transitions(self, issue_key: str) -> list[dict[str, Any]]:
        r = self._session.get(
            self._url(f"/rest/api/3/issue/{quote(issue_key, safe='')}/transitions"),
            timeout=self.timeout,
        )
        self._check(r, method="GET")
        return list((r.json() or {}).get("transitions") or [])

    def transition_issue(self, issue_key: str, transition_id: str) -> int:
        r = self._session.post(
            self._url(f"/rest/api/3/issue/{quote(issue_key, safe='')}/transitions"),
            json={"transition": {"id": str(transition_id)}},
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        self._check(r, method="POST")
        return r.status_code

    @classmethod
    def from_env(
        cls, timeout: float = 30.0, **overrides: Optional[str],
    ) -> "JiraClient":
        return cls(AtlassianConfig.resolve(**overrides), timeout=timeout)
