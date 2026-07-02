"""Arista EOS group (#62): parser wiring, envelope shapes, SSH plumbing.

No live switch anywhere: tool-layer tests monkeypatch ``AristaClient``
with a canned fake; client-layer tests monkeypatch the SSH exec.
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

    def close(self):
        self.closed = True


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
            "--port", "2222",
        ])
        self.assertEqual((ns.user, ns.password, ns.port), ("qa", "x", 2222))
        ns = self.parser.parse_args(["arista", "version", "arista410"])
        self.assertEqual(ns.host, "arista410")


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            cfg = AristaConfig.resolve("arista410")
        self.assertEqual((cfg.user, cfg.password, cfg.port), ("admin", "", 22))

    def test_env_and_overrides(self):
        with mock.patch.dict("os.environ",
                             {"ARISTA_USER": "qa", "ARISTA_PASSWORD": "pw"}):
            cfg = AristaConfig.resolve("arista410")
            self.assertEqual((cfg.user, cfg.password), ("qa", "pw"))
            cfg = AristaConfig.resolve("arista410", user="other", password="", port=2222)
            self.assertEqual((cfg.user, cfg.password, cfg.port), ("other", "", 2222))

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
    """Exercise run_cmds' SSH plumbing by faking the exec layer."""

    def _client(self, replies):
        c = AristaClient(AristaConfig.resolve("arista410", user="a", password="b"))
        c.executed = []

        def fake_exec(command):
            c.executed.append(command)
            return replies.pop(0)
        c._exec = fake_exec
        return c

    def test_json_cmds_pipe_json_and_prefix_enable(self):
        c = self._client([(0, '{"modelName": "DCS-7260CX-64-F"}')])
        out = c.run_cmds(["show version"])
        self.assertEqual(out, [{"modelName": "DCS-7260CX-64-F"}])
        self.assertEqual(c.executed, ["enable\nshow version | json"])

    def test_text_cmds_skip_json_pipe(self):
        c = self._client([(0, "interface Ethernet2/1\n")])
        out = c.run_cmds(["show running-config interfaces Ethernet2/1"], fmt="text")
        self.assertEqual(out, [{"output": "interface Ethernet2/1\n"}])
        self.assertEqual(
            c.executed, ["enable\nshow running-config interfaces Ethernet2/1"])

    def test_cli_rejection_surfaces_percent_line(self):
        c = self._client([(1, "\n> show bogus\n% Invalid input at line 1\n")])
        with self.assertRaisesRegex(AristaError, "Invalid input"):
            c.run_cmds(["show bogus"])

    def test_unparseable_json_raises(self):
        c = self._client([(0, "not json at all")])
        with self.assertRaisesRegex(AristaError, "no parseable JSON"):
            c.run_cmds(["show version"])

    def test_auth_failure_hints_env_vars(self):
        import paramiko
        c = AristaClient(AristaConfig.resolve("arista410", user="a", password="b"))
        with mock.patch("paramiko.SSHClient") as cls:
            cls.return_value.connect.side_effect = paramiko.AuthenticationException()
            with self.assertRaisesRegex(AristaError, "ARISTA_USER"):
                c.run_cmds(["show version"])


class McpSurfaceTests(unittest.TestCase):
    def test_arista_group_exposes_read_tools(self):
        from qactl.mcp.registry import ALL_GROUPS, list_group_tools
        self.assertIn("arista", ALL_GROUPS)
        self.assertEqual(list_group_tools("arista"), [
            "arista_config", "arista_interfaces", "arista_lldp", "arista_version",
        ])


if __name__ == "__main__":
    unittest.main()
