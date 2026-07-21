"""``qactl console`` group: parser wiring + Device42 console resolution.

The interactive connect (SSH + menu-nav + PTY) is not exercised here — it
needs a real TTY. Tests cover the lookup/parse tool and the CLI arg contract.
"""

from __future__ import annotations

import unittest
from unittest import mock

from qactl.__main__ import build_native_parser
from qactl.console import tools


CONSOLE_ROWS = [{"dev": "WDY1CAV800048", "vn": "Console9 @ console-b08"}]


class _FakeClient:
    def __init__(self, resolve_name="WDY1CAV800048", console_rows=None):
        self._resolve_name = resolve_name
        self._console_rows = console_rows if console_rows is not None else CONSOLE_ROWS

    def doql(self, sql):
        if "SELECT name" in sql and "view_device_v1" in sql:
            return [{"name": self._resolve_name}] if self._resolve_name else []
        if "view_netport_v1" in sql:
            return list(self._console_rows)
        return []

    def close(self):
        pass


def _patch_client(fake):
    # console_resolve reaches Device42 through device42.tools._run/_client,
    # which builds a Device42Client via .connect().
    from qactl.device42.client import Device42Client
    return mock.patch.object(Device42Client, "connect",
                             classmethod(lambda cls, *a, **k: fake))


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_native_parser()

    def test_lookup_form(self):
        ns = self.parser.parse_args(["console", "WDY1CAV800048"])
        self.assertEqual((ns.group, ns.query, ns.server, ns.port),
                         ("console", "WDY1CAV800048", None, None))

    def test_manual_form(self):
        ns = self.parser.parse_args(["console", "--server", "B10", "--port", "9"])
        self.assertEqual((ns.query, ns.server, ns.port), (None, "B10", 9))


class ResolveTests(unittest.TestCase):
    def test_clean_mapping_parsed(self):
        with _patch_client(_FakeClient()):
            env = tools.console_resolve("WDY1CAV800048")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["result"]["console_server"], "CONSOLE-B08")
        self.assertEqual(env["result"]["port"], 9)
        self.assertEqual(env["result"]["source"], "device42")

    def test_unparseable_warns_with_raw(self):
        rows = [{"dev": "X", "vn": "console-c02-DMZ,Console @ X"}]
        with _patch_client(_FakeClient(console_rows=rows)):
            env = tools.console_resolve("X")
        self.assertEqual(env["status"], "warning")
        self.assertIsNone(env["result"]["console_server"])
        self.assertIn("console-c02-DMZ,Console @ X", env["result"]["unparsed"])

    def test_unknown_device_bad_argument(self):
        with _patch_client(_FakeClient(resolve_name=None)):
            env = tools.console_resolve("NOPE")
        self.assertEqual(env["status"], "bad_argument")


if __name__ == "__main__":
    unittest.main()
