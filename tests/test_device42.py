"""Device42 group: parser wiring, cred resolution, envelope shapes.

No live Device42 anywhere: tool-layer tests monkeypatch
``Device42Client.connect`` with a canned fake that answers ``doql`` and
``rest_get`` from lookup tables.
"""

from __future__ import annotations

import unittest
from unittest import mock

from qactl.__main__ import build_native_parser
from qactl.core.creds import CredentialError, Device42Config
from qactl.device42 import tools
from qactl.device42.client import Device42Error, doql_quote


DEVICE_DETAIL = {
    "name": "WDV1D2VR0000E",
    "serial_no": "WDV1D2VR0000E",
    "asset_no": "",
    "category": "NCP2",
    "customer": "CS::Infrastructure",
    "type": "physical",
    "in_service": True,
    "service_level": "Production",
    "os": None,
    "manufacturer": "UfiSpace",
    "hw_model": "UfiSpace S9700-23D",
    "last_updated": "2026-05-06T09:11:30Z",
    "notes": "",
    "ip_addresses": [{"ip": "100.64.7.46"}, {"ip": ""}],
    "custom_fields": [
        {"key": "End User", "value": "Ohad Dahan", "notes": None},
        {"key": "PDU", "value": "", "notes": None},
    ],
}

RACK_ROW = {
    "device": "WDV1D2VR0000E", "serial_no": "WDV1D2VR0000E", "u_position": 3.0,
    "rack": "ZH.C05", "rack_row": "C", "room": "Zarhin DC", "building": "Zarhin",
}


PDU_ROWS = [
    {"device": "18ZP6S3", "pdu": "RA01-PDU-F01-1", "outlet": "34", "model": "APDU10350SW"},
    {"device": "18ZP6S3", "pdu": "RA01-PDU-F01-2", "outlet": "B7", "model": "APDU10350SW"},
]


class _FakeClient:
    """Stands in for Device42Client; canned doql/rest_get answers."""

    def __init__(self, resolve_name="WDV1D2VR0000E", detail=None, rack_row=None,
                 pdu_rows=None):
        self._resolve_name = resolve_name
        self._detail = detail if detail is not None else DEVICE_DETAIL
        self._rack_row = rack_row if rack_row is not None else RACK_ROW
        self._pdu_rows = pdu_rows if pdu_rows is not None else PDU_ROWS
        self.doql_calls = []
        self.rest_calls = []

    def doql(self, sql):
        self.doql_calls.append(sql)
        if "FROM view_device_v1 " in sql and "SELECT name" in sql:
            return [{"name": self._resolve_name}] if self._resolve_name else []
        if "LEFT JOIN view_rack_v1" in sql:
            return [self._rack_row] if self._rack_row else []
        if "view_pduports_v1" in sql:
            return list(self._pdu_rows)
        return []

    def rest_get(self, path, params=None):
        self.rest_calls.append(path)
        return self._detail

    def close(self):
        self.closed = True


def _patch_client(fake):
    return mock.patch.object(tools.Device42Client, "connect",
                             classmethod(lambda cls, *a, **k: fake))


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_native_parser()

    def test_device_rack_power_wiring(self):
        ns = self.parser.parse_args(["d42", "device", "WDY1A17P0001A", "--json"])
        self.assertEqual((ns.cmd, ns.query), ("device", "WDY1A17P0001A"))
        self.assertTrue(ns.json)
        ns = self.parser.parse_args(["d42", "rack", "sa-hostname"])
        self.assertEqual((ns.cmd, ns.query), ("rack", "sa-hostname"))
        ns = self.parser.parse_args(["d42", "power", "18ZP6S3"])
        self.assertEqual((ns.cmd, ns.query), ("power", "18ZP6S3"))


class ConfigTests(unittest.TestCase):
    def test_missing_env_rejected(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(CredentialError):
                Device42Config.resolve()

    def test_rest_base_derived_from_endpoint(self):
        cfg = Device42Config.resolve(
            endpoint="https://d42.example.net/services/data/v1.0/query/",
            auth="Basic abc",
        )
        self.assertEqual(cfg.rest_base, "https://d42.example.net")
        self.assertFalse(cfg.verify_tls)


class DoqlQuoteTests(unittest.TestCase):
    def test_single_quote_doubled(self):
        self.assertEqual(doql_quote("O'Brien"), "O''Brien")


class OutletNormalizeTests(unittest.TestCase):
    def test_bank_and_bare(self):
        self.assertEqual(tools._normalize_outlet("B7"), 19)   # bank B -> +12
        self.assertEqual(tools._normalize_outlet("A5"), 5)    # bank A -> drop
        self.assertEqual(tools._normalize_outlet("14"), 14)   # bare number
        self.assertIsNone(tools._normalize_outlet("xY"))      # unparseable
        self.assertIsNone(tools._normalize_outlet(""))


class ToolEnvelopeTests(unittest.TestCase):
    def test_device_curated_fields_and_owner(self):
        fake = _FakeClient()
        with _patch_client(fake):
            env = tools.d42_device("WDV1D2VR0000E")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["kind"], "d42_device")
        r = env["result"]
        self.assertEqual(r["category"], "NCP2")
        self.assertEqual(r["owner"], "Ohad Dahan")
        self.assertEqual(r["ip_addresses"], ["100.64.7.46"])  # empty ip dropped
        self.assertTrue(any("rack" in a for a in env["next_actions"]))

    def test_device_not_found_is_bad_argument(self):
        fake = _FakeClient(resolve_name=None)
        with _patch_client(fake):
            env = tools.d42_device("NOPE")
        self.assertEqual(env["status"], "bad_argument")
        self.assertFalse(fake.rest_calls)  # never hit REST once resolution missed

    def test_rack_placement(self):
        fake = _FakeClient()
        with _patch_client(fake):
            env = tools.d42_rack("WDV1D2VR0000E")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["result"]["rack"], "ZH.C05")
        self.assertEqual(env["result"]["building"], "Zarhin")

    def test_rack_unmounted_warns(self):
        fake = _FakeClient(rack_row={"device": "WDV1D2VR0000E"})
        with _patch_client(fake):
            env = tools.d42_rack("WDV1D2VR0000E")
        self.assertEqual(env["status"], "warning")
        self.assertTrue(env["warnings"])

    def test_power_feeds_and_outlet_normalization(self):
        fake = _FakeClient()
        with _patch_client(fake):
            env = tools.d42_power("18ZP6S3")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["result"]["feed_count"], 2)
        feeds = env["result"]["feeds"]
        self.assertEqual(feeds[0]["pdu"], "RA01-PDU-F01-1")
        self.assertEqual(feeds[0]["outlet_number"], 34)
        self.assertEqual(feeds[1]["outlet"], "B7")        # raw preserved
        self.assertEqual(feeds[1]["outlet_number"], 19)   # normalized

    def test_power_no_mapping_warns(self):
        fake = _FakeClient(pdu_rows=[])
        with _patch_client(fake):
            env = tools.d42_power("18ZP6S3")
        self.assertEqual(env["status"], "warning")
        self.assertEqual(env["result"]["feed_count"], 0)
        self.assertTrue(env["warnings"])

    def test_doql_error_surfaces_as_error_envelope(self):
        fake = _FakeClient()
        fake.doql = mock.Mock(side_effect=Device42Error("boom"))
        with _patch_client(fake):
            env = tools.d42_rack("x")
        self.assertEqual(env["status"], "error")
        self.assertIn("boom", env["errors"][0])


if __name__ == "__main__":
    unittest.main()
