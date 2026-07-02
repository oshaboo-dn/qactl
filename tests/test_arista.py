"""Arista EOS group (#62): parser wiring, envelope shapes, eAPI plumbing.

No live switch anywhere: tool-layer tests monkeypatch ``AristaClient``
with a canned fake; client-layer tests monkeypatch the HTTP session.
"""

from __future__ import annotations

import unittest
from unittest import mock

from qactl.__main__ import build_native_parser
from qactl.arista import tools
from qactl.arista.client import AristaClient, AristaError
from qactl.core.creds import AristaConfig, CredentialError


INTERFACES_STATUS = {
    "interfaceStatuses": {
        "Ethernet1": {"linkStatus": "connected", "description": "to-leaf",
                      "vlanInformation": {"vlanExplanation": "routed"}},
        "Ethernet10": {"linkStatus": "notconnect", "description": ""},
        "Ethernet2": {"linkStatus": "disabled", "description": ""},
        "Management1": {"linkStatus": "connected", "description": "oob"},
    }
}

LLDP_NEIGHBORS = {
    "lldpNeighbors": [
        {"port": "Ethernet1", "neighborDevice": "DNAAS-LEAF-F16",
         "neighborPort": "ge100-0/0/1", "ttl": 120},
    ]
}


class _FakeClient:
    """Stands in for AristaClient; returns canned per-command results."""

    def __init__(self, results=None, error=None):
        self.results = results or {}
        self.error = error
        self.calls = []

    def run_cmds(self, cmds, fmt="json"):
        self.calls.append((list(cmds), fmt))
        if self.error is not None:
            raise self.error
        return [self.results[c] for c in cmds]


def _patch_client(fake):
    return mock.patch.object(tools.AristaClient, "connect",
                             staticmethod(lambda *a, **k: fake))


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_native_parser()

    def test_interfaces(self):
        ns = self.parser.parse_args(["arista", "interfaces", "arista410", "--json"])
        self.assertEqual(ns.host, "arista410")
        self.assertTrue(ns.json)

    def test_config_interface_repeatable(self):
        ns = self.parser.parse_args([
            "arista", "config", "arista410",
            "--interface", "Ethernet1", "--interface", "Ethernet10",
        ])
        self.assertEqual(ns.interface, ["Ethernet1", "Ethernet10"])

    def test_lldp_and_version_and_creds_flags(self):
        ns = self.parser.parse_args([
            "arista", "lldp", "10.0.0.5", "--user", "qa", "--password", "x",
            "--port", "8443",
        ])
        self.assertEqual((ns.user, ns.password, ns.port), ("qa", "x", 8443))
        ns = self.parser.parse_args(["arista", "version", "arista410", "--http"])
        self.assertTrue(ns.http)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            cfg = AristaConfig.resolve("arista410")
        self.assertEqual((cfg.user, cfg.password, cfg.port), ("admin", "", 443))
        self.assertEqual(cfg.url, "https://arista410:443/command-api")

    def test_env_and_overrides(self):
        with mock.patch.dict("os.environ",
                             {"ARISTA_USER": "qa", "ARISTA_PASSWORD": "pw"}):
            cfg = AristaConfig.resolve("arista410")
            self.assertEqual((cfg.user, cfg.password), ("qa", "pw"))
            cfg = AristaConfig.resolve("arista410", user="other", password="")
            self.assertEqual((cfg.user, cfg.password), ("other", ""))

    def test_http_default_port(self):
        cfg = AristaConfig.resolve("h", http=True)
        self.assertEqual(cfg.url, "http://h:80/command-api")

    def test_empty_host_rejected(self):
        with self.assertRaises(CredentialError):
            AristaConfig.resolve("  ")
        self.assertEqual(tools.arista_version("")["status"], "bad_argument")


class ToolEnvelopeTests(unittest.TestCase):
    def test_interfaces_free_candidates_sorted_naturally(self):
        fake = _FakeClient({"show interfaces status": INTERFACES_STATUS})
        with _patch_client(fake):
            env = tools.arista_interfaces("arista410")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["kind"], "arista_interfaces")
        self.assertEqual(env["result"]["count"], 4)
        self.assertEqual(env["result"]["free_candidates"], ["Ethernet2", "Ethernet10"])
        self.assertIn("Ethernet1", env["result"]["interfaces"])
        self.assertTrue(any("lldp" in a for a in env["next_actions"]))

    def test_lldp(self):
        fake = _FakeClient({"show lldp neighbors": LLDP_NEIGHBORS})
        with _patch_client(fake):
            env = tools.arista_lldp("arista410")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["result"]["count"], 1)
        self.assertEqual(env["result"]["neighbors"][0]["neighborDevice"],
                         "DNAAS-LEAF-F16")

    def test_config_whole_box_uses_text_format(self):
        fake = _FakeClient({"show running-config": {"output": "! cfg\nend\n"}})
        with _patch_client(fake):
            env = tools.arista_config("arista410")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["result"]["text"], "! cfg\nend\n")
        self.assertEqual(fake.calls, [(["show running-config"], "text")])

    def test_config_per_interface_sections(self):
        fake = _FakeClient({
            "show running-config interfaces Ethernet1": {"output": "interface Ethernet1\n"},
            "show running-config interfaces Ethernet10": {"output": "interface Ethernet10\n"},
        })
        with _patch_client(fake):
            env = tools.arista_config("arista410",
                                      interfaces=["Ethernet1", "Ethernet10"])
        self.assertEqual(env["status"], "ok")
        self.assertEqual(sorted(env["result"]["sections"]),
                         ["Ethernet1", "Ethernet10"])

    def test_version(self):
        fake = _FakeClient({"show version": {"modelName": "DCS-7050",
                                             "version": "4.30.1F"}})
        with _patch_client(fake):
            env = tools.arista_version("arista410")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["result"]["modelName"], "DCS-7050")

    def test_eapi_error_maps_to_error_envelope(self):
        fake = _FakeClient(error=AristaError("eAPI error 1002 on arista410: boom"))
        with _patch_client(fake):
            env = tools.arista_interfaces("arista410")
        self.assertEqual(env["status"], "error")
        self.assertIn("boom", env["errors"][0])


class ClientTests(unittest.TestCase):
    def _client(self, response):
        c = AristaClient(AristaConfig.resolve("arista410", user="a", password="b"))
        c._session = mock.Mock()
        c._session.post.return_value = response
        return c

    @staticmethod
    def _response(status_code=200, payload=None):
        r = mock.Mock()
        r.status_code = status_code
        r.json.return_value = payload or {}
        r.raise_for_status = mock.Mock()
        return r

    def test_run_cmds_posts_jsonrpc_and_unwraps_result(self):
        c = self._client(self._response(payload={"jsonrpc": "2.0", "id": "qactl",
                                                 "result": [{"ok": 1}]}))
        out = c.run_cmds(["show version"])
        self.assertEqual(out, [{"ok": 1}])
        body = c._session.post.call_args.kwargs["json"]
        self.assertEqual(body["method"], "runCmds")
        self.assertEqual(body["params"]["cmds"], ["show version"])
        self.assertEqual(body["params"]["format"], "json")

    def test_401_raises_credential_hint(self):
        c = self._client(self._response(status_code=401))
        with self.assertRaisesRegex(AristaError, "ARISTA_USER"):
            c.run_cmds(["show version"])

    def test_jsonrpc_error_surfaces_cli_errors(self):
        c = self._client(self._response(payload={"error": {
            "code": 1002, "message": "CLI command 1 failed",
            "data": [{"errors": ["Invalid input"]}],
        }}))
        with self.assertRaisesRegex(AristaError, "Invalid input"):
            c.run_cmds(["show bogus"])


class McpSurfaceTests(unittest.TestCase):
    def test_arista_group_exposes_read_tools(self):
        from qactl.mcp.registry import ALL_GROUPS, list_group_tools
        self.assertIn("arista", ALL_GROUPS)
        self.assertEqual(list_group_tools("arista"), [
            "arista_config", "arista_interfaces", "arista_lldp", "arista_version",
        ])


if __name__ == "__main__":
    unittest.main()
