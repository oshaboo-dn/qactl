"""``qactl power`` group: parser wiring, dialect, command build, orchestration.

No live PDU: the client's SSH is never opened — dialect/verb/state parsing are
pure, and the orchestration tests inject a fake PduClient + canned targets.
"""

from __future__ import annotations

import argparse
import unittest
from unittest import mock

from qactl.__main__ import build_native_parser
from qactl.core.creds import PduConfig, _pdu_rack_key
from qactl.power import tools
from qactl.power.client import PduClient


def _cfg(ol=("pdu-b10-1",)):
    return PduConfig(user="dn", password="p", password_alt="", ol_hosts=frozenset(ol))


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_native_parser()

    def test_verbs_and_targets(self):
        for verb in ("status", "on", "off", "cycle"):
            ns = self.parser.parse_args(["power", verb, "sa-host"])
            self.assertEqual((ns.cmd, ns.query), (verb, "sa-host"))
        ns = self.parser.parse_args(["power", "cycle", "--pdu", "RA01-PDU-B10-1",
                                     "--outlet", "9", "--yes"])
        self.assertEqual((ns.pdu, ns.outlet, ns.yes), ("RA01-PDU-B10-1", 9, True))


class DialectTests(unittest.TestCase):
    def test_rack_key_migration_safe(self):
        self.assertEqual(_pdu_rack_key("RA01-PDU-B10-1"), "pdu-b10-1")
        self.assertEqual(_pdu_rack_key("pdu-b10-1"), "pdu-b10-1")

    def test_dialect_picks_ol_for_listed_host(self):
        c = PduClient(_cfg(ol=("pdu-b10-1",)))
        self.assertEqual(c.dialect("RA01-PDU-B10-1"), "ol")     # new name, ol list
        self.assertEqual(c.dialect("RA01-PDU-F01-2"), "dev_outlet")  # default

    def test_verb_and_state(self):
        c = PduClient(_cfg())
        self.assertEqual(c._verb("dev_outlet", "off", 9), "dev outlet 1 9 off")
        self.assertEqual(c._verb("ol", "on", 5), "olOn 5")
        self.assertEqual(c._state("Outlet 9: Close", "dev_outlet"), "off")
        self.assertEqual(c._state("Outlet 9: Open", "dev_outlet"), "on")
        self.assertEqual(c._state("9: OFF", "ol"), "off")
        self.assertEqual(c._state("mystery", "dev_outlet"), "unknown")


class TargetResolveTests(unittest.TestCase):
    def test_manual_needs_both(self):
        _, targets, _, err = tools._resolve_targets("power_off", None, "PDU", None)
        self.assertEqual(err["status"], "bad_argument")

    def test_manual_ok(self):
        _, targets, _, err = tools._resolve_targets("power_off", None, "PDU-X", 7)
        self.assertIsNone(err)
        self.assertEqual(targets, [{"pdu": "PDU-X", "outlet": 7}])

    def test_no_query_no_manual(self):
        _, _, _, err = tools._resolve_targets("power_status", None, None, None)
        self.assertEqual(err["status"], "bad_argument")


class _FakePdu:
    def __init__(self):
        self.calls = []

    def cycle(self, host, outlet, pause=3.0):
        self.calls.append(("cycle", host, outlet))
        return {"pdu": host, "outlet": outlet, "action": "cycle",
                "off_verified": True, "state": "on", "ok": True, "raw": "Open"}


class OrchestrationTests(unittest.TestCase):
    def test_cycle_hits_all_feeds_dual_psu(self):
        fake = _FakePdu()
        targets = [{"pdu": "RA01-PDU-F01-1", "outlet": 34},
                   {"pdu": "RA01-PDU-F01-2", "outlet": 25}]
        with mock.patch.object(tools, "_resolve_targets",
                               return_value=("18ZP6S3", targets, [], None)), \
             mock.patch.object(tools, "_client", return_value=(fake, None)):
            env = tools.power_cycle("18ZP6S3")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["result"]["target_count"], 2)
        self.assertEqual(len(fake.calls), 2)             # both PSUs cycled
        self.assertTrue(all(r["ok"] for r in env["result"]["results"]))

    def test_failure_marks_envelope_error(self):
        from qactl.power.client import PduError

        class _Boom:
            def cycle(self, *a, **k):
                raise PduError("auth failed")
        with mock.patch.object(tools, "_resolve_targets",
                               return_value=("d", [{"pdu": "P", "outlet": 1}], [], None)), \
             mock.patch.object(tools, "_client", return_value=(_Boom(), None)):
            env = tools.power_cycle("d")
        self.assertEqual(env["status"], "error")
        self.assertTrue(env["errors"])


class GateTests(unittest.TestCase):
    def test_destructive_requires_yes(self):
        from qactl.power import cli
        ns = argparse.Namespace(query="x", pdu=None, outlet=None, yes=False, json=True)
        with mock.patch.object(cli.tools, "power_cycle") as m:
            cli._cycle(ns)
            m.assert_not_called()   # gate blocks before any action


if __name__ == "__main__":
    unittest.main()
