"""`jira status` falls back to the JSM service-desk API on a 404 (#54).

Portal customers lack Browse-Project permission on a service desk, so a
JSM ticket is invisible to ``/rest/api/3/issue`` (404) but readable via
``/rest/servicedeskapi/request/{key}``. The status command must follow
that fallback instead of reporting the ticket as missing.
"""

import pytest

from qactl.core.creds import AtlassianConfig
from qactl.jira.client import JiraClient, JiraError


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.url = "https://example.atlassian.net/x"

    def json(self):
        return self._payload


def _client(routes):
    """Build a JiraClient whose session.get is scripted by URL substring.

    ``routes`` maps a substring of the request path to a _FakeResp.
    """
    c = JiraClient(AtlassianConfig(
        base_url="https://example.atlassian.net",
        email="e@x", api_token="t",
    ))

    def fake_get(url, **kwargs):
        for needle, resp in routes.items():
            if needle in url:
                return resp
        raise AssertionError(f"unexpected GET {url}")

    c._session.get = fake_get  # type: ignore[assignment]
    return c


def test_status_falls_back_to_servicedesk_on_404():
    routes = {
        "/rest/api/3/issue/": _FakeResp(404, text="not found"),
        "/rest/servicedeskapi/request/HD-15484": _FakeResp(200, {
            "issueKey": "HD-15484",
            "currentStatus": {
                "status": "Waiting for support",
                "statusCategory": "INDETERMINATE",
            },
            "requestFieldValues": [
                {"fieldId": "summary",
                 "value": "WDY194730000B new lab connections"},
                {"fieldId": "description", "value": "ignored"},
            ],
        }),
    }
    res = _client(routes).get_issue_status("HD-15484")
    assert res["issue_key"] == "HD-15484"
    assert res["status"] == "Waiting for support"
    assert res["summary"] == "WDY194730000B new lab connections"
    assert res["status_category"] == "INDETERMINATE"
    assert res["source"] == "servicedesk"


def test_status_uses_core_api_when_present():
    routes = {
        "/rest/api/3/issue/": _FakeResp(200, {
            "fields": {
                "summary": "core issue",
                "status": {"name": "In Progress",
                           "statusCategory": {"name": "In Progress"}},
            },
        }),
    }
    res = _client(routes).get_issue_status("SW-1")
    assert res["status"] == "In Progress"
    assert res["summary"] == "core issue"
    assert res["source"] == "jira"


def test_missing_everywhere_still_raises_404():
    routes = {
        "/rest/api/3/issue/": _FakeResp(404, text="nope"),
        "/rest/servicedeskapi/request/": _FakeResp(404, text="nope"),
    }
    with pytest.raises(JiraError) as ei:
        _client(routes).get_issue_status("NOPE-1")
    assert ei.value.status_code == 404


def test_auth_error_is_not_masked_by_fallback():
    # a 403 on the core API must surface, NOT trigger the servicedesk path
    routes = {"/rest/api/3/issue/": _FakeResp(403, text="forbidden")}
    with pytest.raises(JiraError) as ei:
        _client(routes).get_issue_status("SW-1")
    assert ei.value.status_code == 403
