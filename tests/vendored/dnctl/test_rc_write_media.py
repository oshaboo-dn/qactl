"""Legacy-ODL write media types + YANG-PATCH conversion (issue #80).

bierman02/draft02 ODL returns HTTP 415 for the RFC 8040 dash-form
``application/yang-data+json`` and doesn't support plain-merge PATCH at
all. Against a ``kind=odl`` + ``uri_style=legacy`` endpoint, writes must
go out as ``application/yang.data+json`` (dot form) and ``rc patch``
must be converted to a YANG-PATCH (``application/yang.patch+json``) with
module-prefixed one-leaf-per-edit targets and string-quoted scalars.
"""

import pytest

from qactl.dnctl.rc.tools import rw


LEGACY_EP = {"kind": "odl", "base_url": "http://odl:8181/restconf",
             "uri_style": "legacy"}
RFC_EP = {"kind": "odl", "base_url": "http://odl:8181/restconf",
          "uri_style": "rfc8040"}


@pytest.fixture
def capture(monkeypatch):
    """Stub the HTTP layer and record what the tool would send."""
    calls = []

    def fake_request(**kw):
        calls.append(kw)
        return 200, {}, b"{}", 0.01

    monkeypatch.setattr(rw, "http_request", fake_request)
    return calls


def _use_endpoint(monkeypatch, cfg):
    monkeypatch.setattr(rw, "get_endpoint", lambda alias: cfg)


def test_put_legacy_odl_uses_dot_form_media_type(monkeypatch, capture):
    _use_endpoint(monkeypatch, LEGACY_EP)
    env = rw.restconf_put(
        endpoint="odl", segments="drivenets-top/dn-bfd:bfd/strict-mode",
        payload={"dn-bfd:strict-mode": {"admin-state": "enabled"}},
        confirm=True,
    )
    assert env["status"] == "ok"
    (call,) = capture
    assert call["extra_headers"]["Content-Type"] == "application/yang.data+json"
    assert call["extra_headers"]["Accept"].startswith("application/yang.data+json")
    assert call["json_body"] == {"dn-bfd:strict-mode": {"admin-state": "enabled"}}


def test_put_rfc8040_keeps_default_media_type(monkeypatch, capture):
    """Non-legacy endpoints keep the RFC 8040 dash form (session default)."""
    _use_endpoint(monkeypatch, RFC_EP)
    env = rw.restconf_put(
        endpoint="odl", segments="drivenets-top/system",
        payload={"dn-system:system": {"name": "x"}}, confirm=True,
    )
    assert env["status"] == "ok"
    (call,) = capture
    assert not (call.get("extra_headers") or {}).get("Content-Type")


def test_patch_legacy_converts_to_yang_patch(monkeypatch, capture):
    """Plain-merge payload becomes a YANG-PATCH against the parent URL."""
    _use_endpoint(monkeypatch, LEGACY_EP)
    env = rw.restconf_patch(
        endpoint="odl", segments="drivenets-top/dn-bfd:bfd/strict-mode",
        payload={"dn-bfd:strict-mode": {"hold-time": 300,
                                        "no-negotiation": "enabled"}},
        confirm=True,
    )
    assert env["status"] == "ok"
    (call,) = capture
    assert call["extra_headers"]["Content-Type"] == "application/yang.patch+json"
    # yang-patch responses are patch-status, not data — Accept must say so
    # or bierman02 refuses with HTTP 406
    assert call["extra_headers"]["Accept"] == "application/yang.patch-status+json"
    # PATCH goes to the parent of the wrapper resource
    assert call["url"].endswith("/dn-top:drivenets-top/dn-bfd:bfd")

    patch = call["json_body"]["ietf-yang-patch:yang-patch"]
    edits = patch["edit"]
    assert [e["operation"] for e in edits] == ["merge", "merge"]
    # module-prefixed one-leaf-per-edit targets, numbers string-quoted
    by_target = {e["target"]: e["value"] for e in edits}
    assert by_target["/dn-bfd:strict-mode/dn-bfd:hold-time"] == {"hold-time": "300"}
    assert by_target["/dn-bfd:strict-mode/dn-bfd:no-negotiation"] == {
        "no-negotiation": "enabled"
    }
    # envelope stays lossless: original payload + transformed body both shown
    assert env["request"]["payload"] == {"dn-bfd:strict-mode": {
        "hold-time": 300, "no-negotiation": "enabled"}}
    assert env["request"]["yang_patch"] == call["json_body"]
    assert env["request"]["content_type"] == "application/yang.patch+json"


def test_patch_legacy_nested_containers_and_bools(monkeypatch, capture):
    """Nested containers extend the target path; booleans string-quote."""
    _use_endpoint(monkeypatch, LEGACY_EP)
    rw.restconf_patch(
        endpoint="odl", segments="drivenets-top/dn-bfd:bfd",
        payload={"dn-bfd:bfd": {"strict-mode": {"enabled": True}}},
        confirm=True,
    )
    (call,) = capture
    # URL already ends at the wrapper's parent-of-target; wrapper matches
    # the last segment so PATCH goes one level up
    assert call["url"].endswith("/dn-top:drivenets-top")
    (edit,) = call["json_body"]["ietf-yang-patch:yang-patch"]["edit"]
    assert edit["target"] == "/dn-bfd:bfd/dn-bfd:strict-mode/dn-bfd:enabled"
    assert edit["value"] == {"enabled": "true"}


def test_patch_legacy_passes_explicit_yang_patch_through(monkeypatch, capture):
    """A ready-made yang-patch body is sent as-is to the given URL."""
    _use_endpoint(monkeypatch, LEGACY_EP)
    doc = {"ietf-yang-patch:yang-patch": {"patch-id": "mine", "edit": []}}
    rw.restconf_patch(
        endpoint="odl", segments="drivenets-top/dn-bfd:bfd",
        payload=doc, confirm=True,
    )
    (call,) = capture
    assert call["json_body"] == doc
    assert call["url"].endswith("/dn-top:drivenets-top/dn-bfd:bfd")
    assert call["extra_headers"]["Content-Type"] == "application/yang.patch+json"
    assert call["extra_headers"]["Accept"] == "application/yang.patch-status+json"


def test_patch_legacy_unconvertible_payload_errors(monkeypatch, capture):
    """Multi-key payloads can't be auto-converted — refuse before sending."""
    _use_endpoint(monkeypatch, LEGACY_EP)
    env = rw.restconf_patch(
        endpoint="odl", segments="drivenets-top/dn-bfd:bfd",
        payload={"a:x": {"l": 1}, "b:y": {"m": 2}}, confirm=True,
    )
    assert env["status"] == "error"
    assert "YANG-PATCH" in env["errors"][0]
    assert capture == []  # nothing was sent


def test_patch_rfc8040_stays_plain_merge(monkeypatch, capture):
    _use_endpoint(monkeypatch, RFC_EP)
    rw.restconf_patch(
        endpoint="odl", segments="drivenets-top/dn-bfd:bfd",
        payload={"dn-bfd:bfd": {"x": 1}}, confirm=True,
    )
    (call,) = capture
    assert call["json_body"] == {"dn-bfd:bfd": {"x": 1}}
    assert not (call.get("extra_headers") or {}).get("Content-Type")


def test_patch_dry_run_shows_yang_patch(monkeypatch, capture):
    """Dry run must preview the converted request, not the raw payload."""
    _use_endpoint(monkeypatch, LEGACY_EP)
    env = rw.restconf_patch(
        endpoint="odl", segments="drivenets-top/dn-bfd:bfd/strict-mode",
        payload={"dn-bfd:strict-mode": {"hold-time": 150}},
    )
    assert env["status"] == "dry_run"
    assert capture == []
    assert env["request"]["url"].endswith("/dn-top:drivenets-top/dn-bfd:bfd")
    assert "ietf-yang-patch:yang-patch" in env["request"]["yang_patch"]


def test_session_content_type_override(monkeypatch):
    """extra_headers Content-Type wins over the RFC 8040 default."""
    from qactl.dnctl.rc.core import session as sess

    captured = {}

    class FakeResp:
        status_code = 200
        headers = {}
        content = b""

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, auth=None, headers=None, content=None):
            captured["headers"] = headers
            return FakeResp()

    monkeypatch.setattr(sess.httpx, "Client", FakeClient)

    sess.request(method="PUT", url="http://x", json_body={"a": 1})
    assert captured["headers"]["Content-Type"] == "application/yang-data+json"

    sess.request(
        method="PUT", url="http://x", json_body={"a": 1},
        extra_headers={"Content-Type": "application/yang.data+json"},
    )
    assert captured["headers"]["Content-Type"] == "application/yang.data+json"
