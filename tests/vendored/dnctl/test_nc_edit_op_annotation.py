"""nc edit --op handling (#72) — no device.

Two defects pinned here:

1. Annotation ran *before* wrapper extraction, so a payload wrapped in
   ``<drivenets-top>`` got the ``nc:operation`` attribute on the wrapper,
   which extraction then stripped — remove/delete silently executed as a
   plain merge (exit 0, nothing removed).

2. When the annotation does reach the device on a top-level section
   element, the DNOS delete resolver rejects it with
   ``Unknown element 'network_services'`` (its internal underscore name,
   observed live on Hybrid-CL). The envelope must carry an actionable
   hint pointing at the working recipe (inline annotation + merge).
"""

from __future__ import annotations

import pytest

from qactl.dnos.nc.core.change_ops import (
    _op_reject_hint,
    annotate_operation,
    prepare_edit_payload,
)

_NC_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"

_WRAPPED = (
    '<drivenets-top xmlns="http://drivenets.com/ns/yang/dn-top">'
    "<network-services><vrfs><vrf><vrf-name>x</vrf-name></vrf></vrfs>"
    "</network-services></drivenets-top>"
)
_BARE = (
    "<network-services><vrfs><vrf><vrf-name>x</vrf-name></vrf></vrfs>"
    "</network-services>"
)


# --------------------------------------------------------------------------
# prepare_edit_payload: extraction before annotation
# --------------------------------------------------------------------------

def test_remove_survives_dn_top_wrapper():
    payload = prepare_edit_payload(_WRAPPED, "remove")
    assert payload.startswith("<network-services")
    assert 'nc:operation="remove"' in payload
    assert "drivenets-top" not in payload


def test_remove_on_bare_section_annotates_root():
    payload = prepare_edit_payload(_BARE, "remove")
    assert payload.startswith("<network-services")
    assert 'nc:operation="remove"' in payload
    assert f'xmlns:nc="{_NC_NS}"' in payload


def test_merge_is_untouched():
    assert prepare_edit_payload(_BARE, "merge") == _BARE
    assert "nc:operation" not in prepare_edit_payload(_WRAPPED, "merge")


def test_config_wrapped_rpc_payload_keeps_operation():
    wrapped = f"<config>{_WRAPPED}</config>"
    payload = prepare_edit_payload(wrapped, "delete")
    assert payload.startswith("<network-services")
    assert 'nc:operation="delete"' in payload


def test_multi_section_non_merge_rejected():
    multi = f'<drivenets-top xmlns="http://drivenets.com/ns/yang/dn-top">{_BARE}<system><name>x</name></system></drivenets-top>'
    with pytest.raises(ValueError, match="multiple top-level sections"):
        prepare_edit_payload(multi, "remove")
    # merge never annotates, so multiple sections stay fine
    assert "<system>" in prepare_edit_payload(multi, "merge")


def test_invalid_op_still_rejected():
    with pytest.raises(ValueError, match="op must be one of"):
        prepare_edit_payload(_BARE, "obliterate")


def test_annotate_operation_unchanged_for_merge():
    assert annotate_operation(_BARE, "merge") == _BARE


# --------------------------------------------------------------------------
# hint for the DNOS underscore-lookup rejection
# --------------------------------------------------------------------------

def test_hint_on_unknown_element_remove():
    err = "Unknown element 'network_services' in path '/drivenets-top/network-services'"
    hint = _op_reject_hint("remove", err)
    assert hint is not None
    assert "inline" in hint
    assert "op=merge" in hint


def test_hint_on_unknown_element_delete():
    err = "Unknown element 'system' in path '/drivenets-top/system'"
    assert _op_reject_hint("delete", err) is not None


def test_no_hint_for_other_errors_or_ops():
    assert _op_reject_hint("remove", "Node does not exist") is None
    assert _op_reject_hint("merge", "Unknown element 'x' in path '/y'") is None
    assert _op_reject_hint("create", "Unknown element 'x' in path '/y'") is None
