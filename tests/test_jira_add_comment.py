"""`qactl jira comment add` — post a plain-text comment rendered as ADF.

Covers the ADF rendering, the happy-path POST (body shape + returned
comment id), and the empty-text rejection.
"""

from qactl.core.creds import AtlassianConfig
from qactl.core.output import exit_code_for
from qactl.jira import tools
from qactl.jira.client import JiraClient
from qactl.jira.tools import _text_to_adf


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.url = "https://example.atlassian.net/x"

    def json(self):
        return self._payload


def _client_capturing(sink):
    c = JiraClient(AtlassianConfig(
        base_url="https://example.atlassian.net", email="e@x", api_token="t",
    ))

    def fake_post(url, **kwargs):
        sink["url"] = url
        sink["json"] = kwargs.get("json")
        return _FakeResp(201, {"id": "1085621"})

    c._session.post = fake_post  # type: ignore[assignment]
    return c


# ---- ADF rendering ---------------------------------------------------------

def test_text_to_adf_single_paragraph():
    adf = _text_to_adf("hello world")
    assert adf["type"] == "doc" and adf["version"] == 1
    assert adf["content"] == [
        {"type": "paragraph", "content": [{"type": "text", "text": "hello world"}]}
    ]


def test_text_to_adf_blank_line_splits_paragraphs():
    adf = _text_to_adf("a\n\nb")
    assert [p.get("content", [{}])[0].get("text") for p in adf["content"]] == ["a", "b"]


def test_text_to_adf_single_newline_is_hardbreak():
    adf = _text_to_adf("a\nb")
    nodes = adf["content"][0]["content"]
    assert [n["type"] for n in nodes] == ["text", "hardBreak", "text"]


# ---- tool ------------------------------------------------------------------

def test_add_comment_posts_adf_and_returns_id(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(tools.JiraClient, "from_env",
                        classmethod(lambda cls, **kw: _client_capturing(sink)))
    env = tools.jira_add_comment("SW-1", "hi there")
    assert exit_code_for(env) == 0
    assert env["result"]["comment_id"] == "1085621"
    assert "/rest/api/3/issue/SW-1/comment" in sink["url"]
    assert sink["json"]["body"]["type"] == "doc"


def test_add_comment_rejects_empty_text():
    env = tools.jira_add_comment("SW-1", "   \n  ")
    assert env["status"] == "bad_argument"
    assert exit_code_for(env) != 0
