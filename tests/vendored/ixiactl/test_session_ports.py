"""Port lifecycle verbs (#52): assign / connect-ports / release.

The RestPy → IxNetwork wire path needs a live chassis; these cover
everything reachable without one — spec parsing, the confirm gate, the
ownership refusal, and that each verb drives the expected RestPy calls
(Vport.add / Location / AssignPorts / ConnectPort / ReleasePort /
UnassignPorts) against a faked ixnetwork object.
"""

from __future__ import annotations

import unittest
from unittest import mock

from qactl.ixia.ctl.__main__ import build_parser
from qactl.ixia.tools.ports import (
    _parse_port_spec,
    ixia_assign_port,
    ixia_connect_ports,
    ixia_release_port,
)


# --------------------------------------------------------------------------
# Fake RestPy object graph
# --------------------------------------------------------------------------

class _FakeVport:
    def __init__(self, name="", href="", assigned_to=""):
        self.Name = name
        self.href = href or f"/vport/{name or 'x'}"
        self.AssignedTo = assigned_to
        self.ConnectionState = "assignedUnconnected"
        self.State = "down"
        self.Location = ""
        self.calls = []

    def ConnectPort(self):
        self.calls.append(("ConnectPort",))
        self.ConnectionState = "connectedLinkUp"
        self.State = "up"

    def ReleasePort(self):
        self.calls.append(("ReleasePort",))
        self.AssignedTo = ""

    def UnassignPorts(self, Arg2=False):
        self.calls.append(("UnassignPorts", Arg2))
        self.AssignedTo = ""


class _FakeVportColl:
    def __init__(self, vports):
        self._vports = vports
        self.added = []

    def find(self):
        return list(self._vports)

    def add(self, Name=None):
        vp = _FakeVport(name=Name or "", href=f"/vport/{Name}")
        self._vports.append(vp)
        self.added.append(Name)
        return vp


class _FakePort:
    def __init__(self, port_id, owner=""):
        self.PortId = port_id
        self.Owner = owner
        self.Type = "novusHundredGigLan"
        self.State = "up"


class _FakeCardColl:
    def __init__(self, cards):
        self._cards = cards

    def find(self):
        return list(self._cards)


class _Card:
    def __init__(self, card_id, ports):
        self.CardId = card_id
        self.Port = _FakePortColl(ports)


class _FakePortColl:
    def __init__(self, ports):
        self._ports = ports

    def find(self):
        return list(self._ports)


class _Chassis:
    def __init__(self, hostname, cards):
        self.Hostname = hostname
        self.Ip = hostname
        self.Card = _FakeCardColl(cards)


class _ChassisColl:
    def __init__(self, chassis):
        self._chassis = chassis

    def find(self):
        return list(self._chassis)


class _AvailableHardware:
    def __init__(self, chassis):
        self.Chassis = _ChassisColl(chassis)


class _FakeIxn:
    def __init__(self, vports, chassis):
        self.href = "/api/v1/sessions/1/ixnetwork/"
        self.Vport = _FakeVportColl(vports)
        self.AvailableHardware = _AvailableHardware(chassis)
        self.assign_calls = []

    def AssignPorts(self, arg1, arg2, arg3):
        self.assign_calls.append((arg1, arg2, arg3))
        return arg2


class _FakeSession:
    def __init__(self, ixn):
        self.ixn = ixn


def _session(*, owner="", vports=None):
    port = _FakePort("5", owner=owner)
    card = _Card("10", [port])
    chassis = _Chassis("100.64.0.56", [card])
    ixn = _FakeIxn(list(vports or []), [chassis])
    return _FakeSession(ixn)


def _patch(sess):
    return mock.patch("qactl.ixia.tools.ports.get_session", return_value=sess)


# --------------------------------------------------------------------------
# spec parsing
# --------------------------------------------------------------------------

class ParsePortSpecTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(
            _parse_port_spec("100.64.0.56:10:5"), ("100.64.0.56", "10", "5")
        )

    def test_wrong_arity(self):
        with self.assertRaises(ValueError):
            _parse_port_spec("100.64.0.56:10")

    def test_non_numeric_card(self):
        with self.assertRaises(ValueError):
            _parse_port_spec("100.64.0.56:ten:5")


# --------------------------------------------------------------------------
# assign
# --------------------------------------------------------------------------

class AssignTests(unittest.TestCase):
    def test_bad_spec_is_bad_argument(self):
        env = ixia_assign_port(host="h", port_spec="nope", confirm=True)
        self.assertEqual(env["status"], "bad_argument")

    def test_requires_confirm(self):
        env = ixia_assign_port(host="h", port_spec="100.64.0.56:10:5")
        self.assertEqual(env["status"], "confirmation_required")

    def test_happy_path_creates_and_connects(self):
        sess = _session(owner="")
        with _patch(sess):
            env = ixia_assign_port(
                host="h", port_spec="100.64.0.56:10:5",
                name="R3-1005", confirm=True,
            )
        self.assertEqual(env["status"], "ok")
        self.assertTrue(env["result"]["created_vport"])
        self.assertTrue(env["result"]["connected"])
        self.assertEqual(env["result"]["port"]["location"], "100.64.0.56;10;5")
        # vport created + located + AssignPorts called with clearOwnership=False
        self.assertEqual(sess.ixn.Vport.added, ["R3-1005"])
        self.assertEqual(len(sess.ixn.assign_calls), 1)
        _arg1, arg2, arg3 = sess.ixn.assign_calls[0]
        self.assertEqual(arg3, False)
        self.assertEqual(sess.ixn.Vport.find()[0].Location, "100.64.0.56;10;5")

    def test_owned_port_refused_without_force(self):
        sess = _session(owner="someone-else")
        with _patch(sess):
            env = ixia_assign_port(
                host="h", port_spec="100.64.0.56:10:5", confirm=True,
            )
        self.assertEqual(env["status"], "error")
        self.assertTrue(any("owned by" in e for e in env["errors"]))
        self.assertEqual(sess.ixn.assign_calls, [])

    def test_owned_port_seized_with_force(self):
        sess = _session(owner="someone-else")
        with _patch(sess):
            env = ixia_assign_port(
                host="h", port_spec="100.64.0.56:10:5",
                force=True, confirm=True,
            )
        self.assertEqual(env["status"], "ok")
        self.assertTrue(env["result"]["forced"])
        self.assertEqual(env["result"]["port"]["previous_owner"], "someone-else")
        _arg1, _arg2, arg3 = sess.ixn.assign_calls[0]
        self.assertEqual(arg3, True)

    def test_unknown_port_errors(self):
        sess = _session(owner="")
        with _patch(sess):
            env = ixia_assign_port(
                host="h", port_spec="100.64.0.56:99:99", confirm=True,
            )
        self.assertEqual(env["status"], "error")
        self.assertTrue(any("not found" in e for e in env["errors"]))

    def test_no_connect_skips_assignports(self):
        sess = _session(owner="")
        with _patch(sess):
            env = ixia_assign_port(
                host="h", port_spec="100.64.0.56:10:5",
                connect=False, confirm=True,
            )
        self.assertEqual(env["status"], "ok")
        self.assertFalse(env["result"]["connected"])
        self.assertEqual(sess.ixn.assign_calls, [])


# --------------------------------------------------------------------------
# connect-ports
# --------------------------------------------------------------------------

class ConnectPortsTests(unittest.TestCase):
    def test_missing_vport_is_bad_argument(self):
        env = ixia_connect_ports(host="h", vport="", confirm=True)
        self.assertEqual(env["status"], "bad_argument")

    def test_requires_confirm(self):
        env = ixia_connect_ports(host="h", vport="R3-1005")
        self.assertEqual(env["status"], "confirmation_required")

    def test_not_found_errors(self):
        sess = _session(vports=[])
        with _patch(sess):
            env = ixia_connect_ports(host="h", vport="ghost", confirm=True)
        self.assertEqual(env["status"], "error")

    def test_happy_path_calls_connectport(self):
        vp = _FakeVport(name="R3-1005", assigned_to="100.64.0.56:10:5")
        sess = _session(vports=[vp])
        with _patch(sess):
            env = ixia_connect_ports(host="h", vport="R3-1005", confirm=True)
        self.assertEqual(env["status"], "ok")
        self.assertIn(("ConnectPort",), vp.calls)


# --------------------------------------------------------------------------
# release
# --------------------------------------------------------------------------

class ReleaseTests(unittest.TestCase):
    def test_requires_identifier(self):
        env = ixia_release_port(host="h", confirm=True)
        self.assertEqual(env["status"], "bad_argument")

    def test_requires_confirm(self):
        env = ixia_release_port(host="h", vport="R3-1005")
        self.assertEqual(env["status"], "confirmation_required")

    def test_release_by_port_spec_matches_assigned_to(self):
        vp = _FakeVport(name="R3-1005", assigned_to="100.64.0.56:10:5")
        sess = _session(vports=[vp])
        with _patch(sess):
            env = ixia_release_port(
                host="h", port_spec="100.64.0.56:10:5", confirm=True,
            )
        self.assertEqual(env["status"], "ok")
        self.assertFalse(env["result"]["deleted_vport"])
        self.assertIn(("ReleasePort",), vp.calls)

    def test_delete_uses_unassign(self):
        vp = _FakeVport(name="R3-1005", assigned_to="100.64.0.56:10:5")
        sess = _session(vports=[vp])
        with _patch(sess):
            env = ixia_release_port(
                host="h", vport="R3-1005", delete=True, confirm=True,
            )
        self.assertEqual(env["status"], "ok")
        self.assertTrue(env["result"]["deleted_vport"])
        self.assertIn(("UnassignPorts", True), vp.calls)


# --------------------------------------------------------------------------
# CLI confirm gate (off-TTY)
# --------------------------------------------------------------------------

class CliGateTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_assign_registered(self):
        args = self.parser.parse_args(
            ["session", "assign", "--host", "h",
             "--chassis-port", "100.64.0.56:10:5"]
        )
        self.assertEqual(args.port_spec, "100.64.0.56:10:5")
        self.assertTrue(args.connect)

    def test_assign_refuses_without_yes_offtty(self):
        args = self.parser.parse_args(
            ["session", "assign", "--host", "h",
             "--chassis-port", "100.64.0.56:10:5", "--json"]
        )
        with mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch("sys.stderr.isatty", return_value=False):
            rc = args.func(args)
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
