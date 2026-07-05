"""nc get filter rooting (#75): OpenConfig/IETF top-level trees are siblings
of <drivenets-top>, so wrapping every subtree filter under it made them
unreachable. ``auto`` now sends non-DriveNets-namespace top elements as-is,
while dn-* / unnamespaced / pre-wrapped filters keep the historical wrap.
``root="dn-top"`` / ``root="none"`` force either behavior.
"""

from unittest.mock import MagicMock

from dnctl.nc.core.netconf_rpc import (
    DN_TOP_NS,
    _build_subtree_filter,
    get,
    get_config,
)

OC_NI = '<network-instances xmlns="http://openconfig.net/yang/network-instances"/>'
DN_SYSTEM = '<system xmlns="http://drivenets.com/ns/yang/dn-system"/>'
WRAPPED = f'<drivenets-top xmlns="{DN_TOP_NS}">{DN_SYSTEM}</drivenets-top>'


# --- _build_subtree_filter: auto ---

def test_auto_sends_openconfig_filter_unwrapped():
    assert _build_subtree_filter(OC_NI) == OC_NI


def test_auto_sends_ietf_filter_unwrapped():
    f = '<modules-state xmlns="urn:ietf:params:xml:ns:yang:ietf-yang-library"/>'
    assert _build_subtree_filter(f) == f


def test_auto_wraps_dn_namespace_filter():
    assert _build_subtree_filter(DN_SYSTEM) == WRAPPED


def test_auto_wraps_unnamespaced_filter():
    out = _build_subtree_filter("<system/>")
    assert out == f'<drivenets-top xmlns="{DN_TOP_NS}"><system/></drivenets-top>'


def test_auto_keeps_explicit_dn_top_wrapper_single():
    # pre-wrapped input is stripped and re-wrapped exactly once
    assert _build_subtree_filter(WRAPPED) == WRAPPED


def test_auto_wraps_multi_sibling_fragment():
    frag = "<system/><interfaces/>"
    assert _build_subtree_filter(frag) == (
        f'<drivenets-top xmlns="{DN_TOP_NS}">{frag}</drivenets-top>'
    )


# --- _build_subtree_filter: explicit root ---

def test_root_none_sends_as_is():
    assert _build_subtree_filter(DN_SYSTEM, root="none") == DN_SYSTEM


def test_root_none_still_strips_dn_top_wrapper():
    assert _build_subtree_filter(WRAPPED, root="none") == DN_SYSTEM


def test_root_dn_top_forces_wrap_on_openconfig():
    assert _build_subtree_filter(OC_NI, root="dn-top") == (
        f'<drivenets-top xmlns="{DN_TOP_NS}">{OC_NI}</drivenets-top>'
    )


# --- get / get_config forward the built filter ---

def _mock_mgr():
    m = MagicMock()
    reply = MagicMock()
    reply.data_xml = "<data/>"
    m.get.return_value = reply
    m.get_config.return_value = reply
    return m


def test_get_oper_openconfig_filter_not_nested():
    m = _mock_mgr()
    get(m, subtree=OC_NI)
    assert m.get.call_args.kwargs["filter"] == ("subtree", OC_NI)


def test_get_config_dn_filter_still_wrapped():
    m = _mock_mgr()
    get_config(m, subtree=DN_SYSTEM)
    filter_arg = m.get_config.call_args.kwargs["filter"]
    assert filter_arg == ("subtree", WRAPPED)


def test_get_config_root_none_passthrough():
    m = _mock_mgr()
    get_config(m, subtree=OC_NI, root="none")
    assert m.get_config.call_args.kwargs["filter"] == ("subtree", OC_NI)


# --- tool layer rejects a bad root before connecting ---

def test_netconf_get_rejects_unknown_root():
    from dnctl.nc.tools.rw import netconf_get

    res = netconf_get(xml=OC_NI, device="cl", root="bogus")
    assert res["status"] == "error"
    assert "root=" in str(res)
