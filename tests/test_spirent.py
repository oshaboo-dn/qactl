"""Offline tests for the qactl spirent (STC REST) scaffold.

Exercise everything that does *not* need a live Spirent REST server or the
``stcrestclient`` package: argument parsing, global-flag placement, the
missing-host guard, exit-code mapping, envelope rendering, the reattach
(join-vs-create) decision with a mocked ``StcHttp``, and the diag tools.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from qactl.spirent.ctl.__main__ import build_parser, main
from qactl.spirent.ctl.cli import common
from qactl.spirent.ctl.core import output
from qactl.spirent.core import session as session_mod
from qactl.spirent.core.envelope import error_envelope, make_envelope
from qactl.spirent.client.session import SpirentSession, full_session_name


# --------------------------------------------------------------------------
# A fake StcHttp — records calls, no network, no stcrestclient needed.
# --------------------------------------------------------------------------
class FakeStc:
    def __init__(self, host, port, timeout=None, existing=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._existing = list(existing or [])
        self.calls = []
        self._started = False

    def sessions(self):
        return list(self._existing)

    def new_session(self, user, name):
        self.calls.append(("new", user, name))
        self._started = True

    def join_session(self, full):
        self.calls.append(("join", full))
        self._started = True

    def started(self):
        return self._started


def _fake_loader(existing=None):
    """Return a `_load_stchttp` replacement yielding a FakeStc factory."""
    def loader():
        def factory(host, port, timeout=None):
            return FakeStc(host, port, timeout=timeout, existing=existing)
        return factory
    return loader


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_global_flags_after_subcommand(self):
        args = self.parser.parse_args(
            ["session", "connect", "--host", "stc1", "--json"]
        )
        self.assertEqual(args.host, "stc1")
        self.assertTrue(args.json)
        self.assertTrue(callable(args.func))

    def test_session_subcommands_present(self):
        for cmd in ("connect", "sessions", "describe"):
            args = self.parser.parse_args(["session", cmd, "--host", "h"])
            self.assertTrue(callable(args.func))

    def test_new_session_flag(self):
        args = self.parser.parse_args(
            ["session", "connect", "--host", "h", "--new-session"]
        )
        self.assertTrue(args.new_session)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        session_mod.reset_cache()

    def test_missing_host_is_bad_argument(self):
        args = build_parser().parse_args(["session", "connect", "--json"])
        args.host = None  # simulate no --host and no $SPIRENT_HOST
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = common.apply_session_policy(args)
        self.assertEqual(rc, 1)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["status"], "bad_argument")

    def test_policy_recorded(self):
        args = build_parser().parse_args(
            ["session", "connect", "--host", "h", "--session", "mine",
             "--new-session"]
        )
        self.assertIsNone(common.apply_session_policy(args))
        pol = session_mod.current_policy()
        self.assertEqual(pol["session_name"], "mine")
        self.assertTrue(pol["new_session"])


class EnvelopeTests(unittest.TestCase):
    def test_make_and_error_shapes(self):
        env = make_envelope(kind="spirent_connect", host="h", port=80)
        for key in ("status", "host", "port", "session", "kind", "result",
                    "warnings", "errors", "next_actions"):
            self.assertIn(key, env)
        self.assertEqual(env["status"], "ok")
        err = error_envelope("boom", kind="x", status="error")
        self.assertEqual(err["status"], "error")
        self.assertIn("boom", err["errors"])

    def test_exit_codes(self):
        self.assertEqual(output.exit_code_for({"status": "ok"}), 0)
        self.assertEqual(output.exit_code_for({"status": "warning"}), 0)
        self.assertEqual(output.exit_code_for({"status": "error"}), 1)


class ReattachTests(unittest.TestCase):
    """The join-vs-create-vs-new decision — the core of the session model."""

    def setUp(self):
        session_mod.reset_cache()
        session_mod.set_session_policy()

    def _session(self, existing, new_session=False, name="qactl-session"):
        sess = SpirentSession("h", 80, "dn", session_name=name,
                              new_session=new_session)
        with mock.patch(
            "qactl.spirent.client.session._load_stchttp",
            _fake_loader(existing),
        ):
            sess.connect()
        return sess

    def test_joins_existing(self):
        full = full_session_name("qactl-session", "dn")
        sess = self._session(existing=[full])
        self.assertTrue(sess.joined_existing)
        self.assertIn(("join", full), sess.stc.calls)

    def test_creates_when_absent(self):
        sess = self._session(existing=["someone-else - bob"])
        self.assertFalse(sess.joined_existing)
        self.assertEqual(sess.stc.calls[0][0], "new")

    def test_new_session_forces_create_even_if_present(self):
        full = full_session_name("qactl-session", "dn")
        sess = self._session(existing=[full], new_session=True)
        self.assertFalse(sess.joined_existing)
        self.assertEqual(sess.stc.calls[0][0], "new")


class DiagToolTests(unittest.TestCase):
    def setUp(self):
        session_mod.reset_cache()
        session_mod.set_session_policy()

    def test_connect_check_reports_join(self):
        full = full_session_name("qactl-session", "dn")
        with mock.patch(
            "qactl.spirent.client.session._load_stchttp",
            _fake_loader([full]),
        ):
            from qactl.spirent.tools.diag import spirent_connect_check
            env = spirent_connect_check("h", 80, "dn")
        self.assertEqual(env["status"], "ok")
        self.assertTrue(env["result"]["joined_existing"])

    def test_list_sessions(self):
        with mock.patch(
            "qactl.spirent.client.session._load_stchttp",
            _fake_loader(["a - dn", "b - dn"]),
        ):
            from qactl.spirent.tools.diag import spirent_list_sessions
            env = spirent_list_sessions("h", 80, "dn")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["result"]["count"], 2)

    def test_missing_stcrestclient_is_error_envelope(self):
        import qactl.spirent.client.session as cs
        with mock.patch.object(cs, "_load_stchttp",
                               side_effect=cs.SpirentConnectionError("missing")):
            from qactl.spirent.tools.diag import spirent_list_sessions
            env = spirent_list_sessions("h", 80, "dn")
        self.assertEqual(env["status"], "error")
        self.assertTrue(env["next_actions"])


class FakePortStc:
    """A richer fake modelling the project/port primitives used by tools/port."""

    def __init__(self, attach_link="UP"):
        self.attrs = {"system1": {"children-Project": "project1"},
                      "project1": {"children-Port": ""}}
        self.performed = []
        self.applied = 0
        self.deleted = []
        self._n = 0
        self._attach_link = attach_link

    def get(self, handle, attr):
        return self.attrs.get(handle, {}).get(attr, "")

    def create(self, obj_type, under=None, **kw):
        self._n += 1
        ref = f"{obj_type}{self._n}"
        self.attrs.setdefault(ref, {}).update({k: str(v) for k, v in kw.items()})
        if obj_type == "port":
            cur = self.attrs["project1"]["children-Port"].split()
            self.attrs["project1"]["children-Port"] = " ".join(cur + [ref])
            self.attrs[ref].setdefault("Online", "false")
            self.attrs[ref].setdefault("Active", "true")
            self.attrs[ref].setdefault("activephy-Targets", "")
        return ref

    def config(self, ref, **kw):
        self.attrs.setdefault(ref, {}).update({k: str(v) for k, v in kw.items()})

    def perform(self, command, **kw):
        self.performed.append((command, kw))
        if command == "AttachPorts":
            ref = kw["PortList"]
            phy = f"phy_{ref}"
            self.attrs[ref]["activephy-Targets"] = phy
            self.attrs.setdefault(phy, {})["LinkStatus"] = self._attach_link
            self.attrs[ref]["Online"] = "true"
        elif command == "ReleasePort":
            ref = kw["portList"]
            self.attrs[ref]["activephy-Targets"] = ""
            self.attrs[ref]["Online"] = "false"
        return {}

    def apply(self):
        self.applied += 1

    def delete(self, ref):
        self.deleted.append(ref)


class _StubSession:
    def __init__(self, stc):
        self.stc = stc
        self.full_name = "qactl-session - dn"


class PortToolTests(unittest.TestCase):
    def _patch(self, stc):
        return mock.patch(
            "qactl.spirent.core.session.get_session",
            return_value=_StubSession(stc),
        )

    def test_reserve_attaches_and_reports_up(self):
        stc = FakePortStc(attach_link="UP")
        with self._patch(stc):
            from qactl.spirent.tools.port import spirent_reserve_port
            env = spirent_reserve_port(
                "h", 80, "dn", location="//100.64.3.238/6/13", timeout=2,
            )
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["result"]["link_status"], "UP")
        self.assertTrue(env["result"]["online"])
        cmds = [c for c, _ in stc.performed]
        self.assertIn("AttachPorts", cmds)
        self.assertGreaterEqual(stc.applied, 1)

    def test_reserve_force_sets_revokeowner(self):
        stc = FakePortStc()
        with self._patch(stc):
            from qactl.spirent.tools.port import spirent_reserve_port
            spirent_reserve_port("h", 80, "dn",
                                 location="//1.2.3.4/6/13", force=True, timeout=1)
        _, kw = next(kv for kv in stc.performed if kv[0] == "AttachPorts")
        self.assertEqual(kw["RevokeOwner"], "TRUE")

    def test_reserve_link_down_is_warning(self):
        stc = FakePortStc(attach_link="DOWN")
        with self._patch(stc):
            from qactl.spirent.tools.port import spirent_reserve_port
            env = spirent_reserve_port("h", 80, "dn",
                                       location="//1.2.3.4/6/13", timeout=1)
        self.assertEqual(env["status"], "warning")
        self.assertTrue(env["warnings"])

    def test_reserve_reuses_existing_port_by_location(self):
        stc = FakePortStc()
        with self._patch(stc):
            from qactl.spirent.tools.port import spirent_reserve_port
            spirent_reserve_port("h", 80, "dn", location="//1.2.3.4/6/13", timeout=1)
            spirent_reserve_port("h", 80, "dn", location="//1.2.3.4/6/13", timeout=1)
        # Only one port object created for the same location.
        self.assertEqual(stc.attrs["project1"]["children-Port"].split().count("port1"), 1)
        self.assertEqual(len(stc.attrs["project1"]["children-Port"].split()), 1)

    def test_release_missing_port_is_warning(self):
        stc = FakePortStc()
        with self._patch(stc):
            from qactl.spirent.tools.port import spirent_release_port
            env = spirent_release_port("h", 80, "dn", location="//1.2.3.4/6/13")
        self.assertEqual(env["status"], "warning")
        self.assertFalse(env["result"]["released"])

    def test_release_existing_port(self):
        stc = FakePortStc()
        with self._patch(stc):
            from qactl.spirent.tools.port import spirent_reserve_port, spirent_release_port
            spirent_reserve_port("h", 80, "dn", location="//1.2.3.4/6/13", timeout=1)
            env = spirent_release_port("h", 80, "dn", location="//1.2.3.4/6/13")
        self.assertEqual(env["status"], "ok")
        self.assertTrue(env["result"]["released"])
        self.assertIn("ReleasePort", [c for c, _ in stc.performed])

    def test_status_lists_ports(self):
        stc = FakePortStc()
        with self._patch(stc):
            from qactl.spirent.tools.port import spirent_reserve_port, spirent_port_status
            spirent_reserve_port("h", 80, "dn", location="//1.2.3.4/6/13", timeout=1)
            env = spirent_port_status("h", 80, "dn")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["result"]["count"], 1)
        self.assertEqual(env["result"]["ports"][0]["location"], "//1.2.3.4/6/13")


class PortParserTests(unittest.TestCase):
    def test_port_subcommands(self):
        p = build_parser()
        args = p.parse_args(["port", "reserve", "--host", "h",
                             "--location", "//1.2.3.4/6/13"])
        self.assertEqual(args.location, "//1.2.3.4/6/13")
        self.assertTrue(callable(args.func))
        args = p.parse_args(["port", "status", "--host", "h"])
        self.assertTrue(callable(args.func))
        args = p.parse_args(["port", "release", "--host", "h",
                             "--location", "//1.2.3.4/6/13"])
        self.assertTrue(callable(args.func))


class FakeDevStc:
    """Models the project/device/bgp primitives used by tools/device + tools/bgp.

    Mirrors interface config onto the EmulatedDevice convenience attrs the way
    real STC does (Ipv4If.Address -> device.Ipv4Address, VlanIf.VlanId ->
    device.Vlan1), so _device_row reads back correctly.
    """

    def __init__(self):
        self.attrs = {"system1": {"children-Project": "project1"}, "project1": {}}
        self.parent = {}
        self.performed = []
        self.applied = 0
        self.deleted = []
        self._n = 0

    def _new(self, obj_type, under=None):
        self._n += 1
        ref = f"{obj_type.lower()}{self._n}"
        self.attrs.setdefault(ref, {})
        if under is not None:
            self.parent[ref] = under
            key = f"children-{obj_type}"
            cur = self.attrs.setdefault(under, {}).get(key, "").split()
            self.attrs[under][key] = " ".join(cur + [ref])
        return ref

    def get(self, handle, attr):
        return self.attrs.get(handle, {}).get(attr, "")

    def create(self, obj_type, under=None, **kw):
        ref = self._new(obj_type, under)
        self.config(ref, **kw)
        return ref

    def config(self, ref, **kw):
        d = self.attrs.setdefault(ref, {})
        for k, v in kw.items():
            s = str(v)
            # STC normalizes boolean literals to lowercase on read-back.
            d[k] = s.lower() if s in ("TRUE", "FALSE") else s
        # mirror interface config onto the parent device (STC convenience attrs)
        dev = self.parent.get(ref)
        if dev and ref.startswith("ipv4if"):
            m = {"Address": "Ipv4Address", "PrefixLength": "Ipv4Prefix",
                 "Gateway": "Ipv4GatewayAddress"}
            for src, dst in m.items():
                if src in kw:
                    self.attrs[dev][dst] = str(kw[src])
        if dev and ref.startswith("vlanif") and "VlanId" in kw:
            self.attrs[dev]["Vlan1"] = str(kw["VlanId"])

    def perform(self, command, **kw):
        self.performed.append((command, kw))
        if command == "DeviceCreate":
            dev = self._new("EmulatedDevice", under="project1")
            self.attrs[dev].update({"Active": "false"})
            self._new("Ipv4If", under=dev)
            if "VlanIf" in kw.get("IfStack", ""):
                self._new("VlanIf", under=dev)
            self._new("EthIIIf", under=dev)
            return {"ReturnList": dev}
        if command in ("DeviceStart", "DeviceStop"):
            self.attrs[kw["DeviceList"]]["Active"] = \
                "true" if command == "DeviceStart" else "false"
        return {}

    def apply(self):
        self.applied += 1

    def delete(self, ref):
        self.deleted.append(ref)


class DeviceBgpToolTests(unittest.TestCase):
    def _patch(self, stc):
        return mock.patch("qactl.spirent.core.session.get_session",
                          return_value=_StubSession(stc))

    def _reserve_port(self, stc):
        # tools/device requires a reserved port at the location
        stc.attrs["project1"]["children-Port"] = "port1"
        stc.attrs["port1"] = {"Location": "//100.64.3.238/6/13"}

    def test_device_create_builds_stack_and_ip(self):
        stc = FakeDevStc(); self._reserve_port(stc)
        with self._patch(stc):
            from qactl.spirent.tools.device import spirent_device_create
            env = spirent_device_create(
                "h", 80, "dn", port_location="//100.64.3.238/6/13",
                name="wan-stc-1", ip="123.4.1.1", prefix=24,
                gateway="123.4.1.4", vlan=1, mac="00:10:94:00:01:01",
            )
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["result"]["ipv4"], "123.4.1.1")
        self.assertEqual(env["result"]["gateway"], "123.4.1.4")
        self.assertEqual(env["result"]["vlan"], "1")
        self.assertIn("DeviceCreate", [c for c, _ in stc.performed])

    def test_device_create_without_reserved_port_errors(self):
        stc = FakeDevStc()
        with self._patch(stc):
            from qactl.spirent.tools.device import spirent_device_create
            env = spirent_device_create(
                "h", 80, "dn", port_location="//1.2.3.4/6/13",
                name="d", ip="1.1.1.1", prefix=24, gateway="1.1.1.2",
            )
        self.assertEqual(env["status"], "error")

    def test_bgp_add_4byte_as_and_strict(self):
        stc = FakeDevStc(); self._reserve_port(stc)
        with self._patch(stc):
            from qactl.spirent.tools.device import spirent_device_create
            from qactl.spirent.tools.bgp import spirent_bgp_add
            spirent_device_create("h", 80, "dn",
                                  port_location="//100.64.3.238/6/13",
                                  name="wan-stc-1", ip="123.4.1.1", prefix=24,
                                  gateway="123.4.1.4", vlan=1)
            env = spirent_bgp_add("h", 80, "dn", device="wan-stc-1",
                                  local_as=100001, bfd=True, strict=True)
        self.assertEqual(env["status"], "ok")
        # 100001 -> asdot 1.34465, 4-byte enabled
        self.assertEqual(env["result"]["local_as"], "1.34465")
        self.assertTrue(env["result"]["bfd"])
        self.assertTrue(env["result"]["strict_mode_cap74"])
        self.assertTrue(env["result"]["use_gateway_as_peer"])

    def test_bgp_add_2byte_as_no_strict(self):
        stc = FakeDevStc(); self._reserve_port(stc)
        with self._patch(stc):
            from qactl.spirent.tools.device import spirent_device_create
            from qactl.spirent.tools.bgp import spirent_bgp_add
            spirent_device_create("h", 80, "dn",
                                  port_location="//100.64.3.238/6/13",
                                  name="d", ip="10.0.0.1", prefix=30,
                                  gateway="10.0.0.2")
            env = spirent_bgp_add("h", 80, "dn", device="d", local_as=65001)
        self.assertEqual(env["result"]["local_as"], "65001")
        self.assertFalse(env["result"]["strict_mode_cap74"])

    def test_bgp_add_missing_device_errors(self):
        stc = FakeDevStc()
        with self._patch(stc):
            from qactl.spirent.tools.bgp import spirent_bgp_add
            env = spirent_bgp_add("h", 80, "dn", device="nope", local_as=100001)
        self.assertEqual(env["status"], "error")

    def test_device_start_and_list(self):
        stc = FakeDevStc(); self._reserve_port(stc)
        with self._patch(stc):
            from qactl.spirent.tools.device import (
                spirent_device_create, spirent_device_start, spirent_device_list)
            spirent_device_create("h", 80, "dn",
                                  port_location="//100.64.3.238/6/13",
                                  name="d", ip="1.1.1.1", prefix=24,
                                  gateway="1.1.1.2")
            spirent_device_start("h", 80, "dn", name="d")
            env = spirent_device_list("h", 80, "dn")
        self.assertEqual(env["result"]["count"], 1)
        self.assertTrue(env["result"]["devices"][0]["active"])


class DeviceBgpParserTests(unittest.TestCase):
    def test_new_subcommands_parse(self):
        p = build_parser()
        a = p.parse_args(["device", "create", "--host", "h",
                          "--port-location", "//1.2.3.4/6/13", "--name", "d",
                          "--ip", "123.4.1.1", "--gateway", "123.4.1.4", "--vlan", "1"])
        self.assertEqual(a.vlan, 1)
        self.assertTrue(callable(a.func))
        a = p.parse_args(["bgp", "add", "--host", "h", "--device", "d",
                          "--local-as", "100001", "--strict", "--bfd"])
        self.assertTrue(a.strict and a.bfd)
        self.assertEqual(a.local_as, 100001)
        self.assertTrue(callable(p.parse_args(["bgp", "status", "--host", "h"]).func))


class DispatchTests(unittest.TestCase):
    def test_top_level_routes_to_spirent(self):
        from qactl.__main__ import main as top_main
        buf = io.StringIO()
        # argparse's --version prints then raises SystemExit(0).
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                top_main(["spirent", "--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("qactl.spirent.ctl", buf.getvalue())

    def test_main_missing_host_exit_1(self):
        buf = io.StringIO()
        with mock.patch.dict("os.environ", {"SPIRENT_HOST": ""}, clear=False):
            with redirect_stdout(buf):
                rc = main(["session", "connect", "--json"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
