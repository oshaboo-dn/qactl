"""`jira status` bulk multi-key mode + assignee (#84).

`qactl jira status KEY [KEY ...]` must return status + assignee per key in
one envelope; a failing key lands in ``result.errors`` instead of failing
the batch (exit 0 while at least one key resolved, error only if all fail).
"""

from unittest import mock

from qactl.core.creds import AtlassianConfig
from qactl.core.output import exit_code_for
from qactl.jira import tools
from qactl.jira.client import STORY_POINTS_FIELD, JiraClient, JiraError


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.url = "https://example.atlassian.net/x"

    def json(self):
        return self._payload


def _client(routes):
    """JiraClient whose session.get is scripted by URL substring."""
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


def _issue_resp(status, summary, assignee, story_points=None):
    fields = {"summary": summary,
              "status": {"name": status, "statusCategory": {"name": status}}}
    if assignee is not None:
        fields["assignee"] = {"displayName": assignee}
    if story_points is not None:
        fields[STORY_POINTS_FIELD] = story_points
    return _FakeResp(200, {"fields": fields})


# ---- client: assignee ------------------------------------------------------

def test_status_includes_assignee_display_name():
    c = _client({"/rest/api/3/issue/": _issue_resp("In Progress", "s", "Ohad Zvi Shaboo")})
    assert c.get_issue_status("SW-1")["assignee"] == "Ohad Zvi Shaboo"


def test_status_assignee_null_when_unassigned():
    c = _client({"/rest/api/3/issue/": _issue_resp("To Do", "s", None)})
    assert c.get_issue_status("SW-1")["assignee"] is None


def test_status_includes_story_points():
    c = _client({"/rest/api/3/issue/": _issue_resp("Blocked", "s", "A", story_points=0.1)})
    assert c.get_issue_status("SW-1")["story_points"] == 0.1


def test_status_story_points_null_when_unset():
    c = _client({"/rest/api/3/issue/": _issue_resp("To Do", "s", "A")})
    assert c.get_issue_status("SW-1")["story_points"] is None


def test_status_requests_story_points_field():
    captured = {}
    c = _client({"/rest/api/3/issue/": _issue_resp("Done", "s", "A")})
    real_get = c._session.get

    def spy_get(url, **kwargs):
        captured.update(kwargs.get("params") or {})
        return real_get(url, **kwargs)

    c._session.get = spy_get  # type: ignore[assignment]
    c.get_issue_status("SW-1")
    assert STORY_POINTS_FIELD in captured["fields"]


def test_servicedesk_fallback_has_assignee_key():
    routes = {
        "/rest/api/3/issue/": _FakeResp(404, text="not found"),
        "/rest/servicedeskapi/request/": _FakeResp(200, {
            "issueKey": "HD-1",
            "currentStatus": {"status": "Open", "statusCategory": "NEW"},
            "requestFieldValues": [],
        }),
    }
    res = _client(routes).get_issue_status("HD-1")
    assert res["assignee"] is None
    assert res["story_points"] is None


# ---- tool layer: jira_status_bulk ------------------------------------------

class _ScriptedClient:
    """get_issue_status scripted per key: a dict result or an exception."""

    def __init__(self, script):
        self.script = script

    def get_issue_status(self, key):
        out = self.script[key]
        if isinstance(out, Exception):
            raise out
        return out


def _bulk(script, keys):
    fake = _ScriptedClient(script)
    with mock.patch.object(tools, "_client", return_value=(fake, None)):
        return tools.jira_status_bulk(keys)


def _row(status, assignee):
    return {"status": status, "assignee": assignee}


def test_bulk_all_ok():
    env = _bulk({"SW-1": _row("In Progress", "A"), "SW-2": _row("Done", None)},
                ["SW-1", "SW-2"])
    assert env["status"] == "ok"
    assert env["kind"] == "jira_status_bulk"
    assert env["result"]["resolved"] == 2 and env["result"]["failed"] == 0
    assert env["result"]["issues"]["SW-1"]["assignee"] == "A"
    assert exit_code_for(env) == 0


def test_bulk_partial_failure_is_warning_exit_0():
    err = JiraError(404, "no issue", method="GET", url="u")
    env = _bulk({"SW-1": _row("Done", "A"), "SW-2": err}, ["SW-1", "SW-2"])
    assert env["status"] == "warning"
    assert exit_code_for(env) == 0
    assert env["result"]["issues"] == {"SW-1": _row("Done", "A")}
    assert env["result"]["errors"]["SW-2"].startswith("HTTP 404")
    assert any("SW-2" in w for w in env["warnings"])


def test_bulk_all_failed_is_error():
    err = JiraError(404, "no issue", method="GET", url="u")
    env = _bulk({"SW-1": err, "SW-2": err}, ["SW-1", "SW-2"])
    assert env["status"] == "error"
    assert exit_code_for(env) == 1
    assert set(env["result"]["errors"]) == {"SW-1", "SW-2"}


def test_bulk_dedups_keys_preserving_order():
    env = _bulk({"SW-1": _row("Done", None)}, ["SW-1", "SW-1"])
    assert env["result"]["requested"] == 1


def test_bulk_no_keys_is_bad_argument():
    env = tools.jira_status_bulk([])
    assert env["status"] == "bad_argument"
    assert exit_code_for(env) == 1


# ---- CLI dispatch -----------------------------------------------------------

def _dispatch(argv):
    from qactl.__main__ import build_native_parser
    args = build_native_parser().parse_args(argv)
    with mock.patch.object(tools, "jira_status") as single, \
         mock.patch.object(tools, "jira_status_bulk") as bulk, \
         mock.patch("qactl.jira.cli.emit", return_value=0):
        args.func(args)
    return single, bulk


def test_cli_single_key_uses_jira_status():
    single, bulk = _dispatch(["jira", "status", "SW-1", "--json"])
    single.assert_called_once()
    assert single.call_args.args == ("SW-1",)
    bulk.assert_not_called()


def test_cli_multi_key_uses_bulk():
    single, bulk = _dispatch(["jira", "status", "SW-1", "SW-2", "--json"])
    bulk.assert_called_once()
    assert bulk.call_args.args == (["SW-1", "SW-2"],)
    single.assert_not_called()
