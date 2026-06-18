"""CLI-layer tests for ixiactl.

These exercise everything that does *not* require a live IxNetwork API
server or ``ixnetwork-restpy``: argument parsing, the destructive-op
confirm gate, exit-code mapping, payload reading, the small value
parsers, and envelope rendering. The wire path (RestPy → IxNetwork) is
covered by the acceptance smoke test in the README against a real lab.

Run with:  python -m unittest discover -s tests
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from ixiactl.__main__ import build_parser, main
from ixiactl.cli import common
from ixiactl.core import output


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_global_flags_after_subcommand(self):
        args = self.parser.parse_args(
            ["topo", "list", "--host", "h1", "--json"]
        )
        self.assertEqual(args.host, "h1")
        self.assertTrue(args.json)
        self.assertEqual(args.port, 11009)
        self.assertEqual(args.user, "dn")

    def test_defaults_match_mcp(self):
        args = self.parser.parse_args(["session", "connect", "--host", "h"])
        self.assertEqual(args.port, 11009)
        self.assertEqual(args.user, "dn")

    def test_nested_dg_create(self):
        args = self.parser.parse_args([
            "topo", "dg", "create", "--host", "h",
            "--topology", "CL", "--name", "DG1", "--multiplier", "3",
        ])
        self.assertEqual(args.topology, "CL")
        self.assertEqual(args.name, "DG1")
        self.assertEqual(args.multiplier, 3)

    def test_repeatable_vport(self):
        args = self.parser.parse_args([
            "topo", "create", "--host", "h", "--name", "t1",
            "--vport", "/v/1", "--vport", "/v/2",
        ])
        self.assertEqual(args.vport, ["/v/1", "/v/2"])

    def test_bgp_peer_capabilities(self):
        args = self.parser.parse_args([
            "bgp", "peer", "create", "--host", "h", "--topology", "CL",
            "--device-group", "DG1", "--name", "p", "--dut-ip", "1.1.1.1",
            "--local-as", "65000", "--capability", "ipv4_mpls=true",
            "--capability", "evpn=false",
        ])
        caps = common.parse_capabilities(args.capability)
        self.assertEqual(caps, {"ipv4_mpls": True, "evpn": False})

    def test_route_action_requires_advertise_or_withdraw(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args([
                "proto", "route", "action", "frob", "--host", "h",
                "--topology", "CL", "--network-group", "NG",
            ])


class LoadPathResolutionTests(unittest.TestCase):
    """`session load` resolves a bare name against the configs folder."""

    def setUp(self):
        from ixiactl.cli import session_cmds
        self.sc = session_cmds
        self.default = session_cmds.DEFAULT_CONFIG_FOLDER

    def test_bare_name_resolves_against_default_folder(self):
        self.assertEqual(
            self.sc._resolve_config_path("bgp-leak.ixncfg", self.default),
            r"C:\Users\dn\Desktop\ixia\bgp-leak.ixncfg",
        )

    def test_bare_name_resolves_against_custom_folder(self):
        self.assertEqual(
            self.sc._resolve_config_path("x.ixncfg", r"D:\cfgs"),
            r"D:\cfgs\x.ixncfg",
        )

    def test_trailing_separator_on_folder_is_not_doubled(self):
        self.assertEqual(
            self.sc._resolve_config_path("x.ixncfg", "D:\\cfgs\\"),
            r"D:\cfgs\x.ixncfg",
        )

    def test_absolute_windows_path_passes_through(self):
        p = r"C:\other\dir\x.ixncfg"
        self.assertEqual(self.sc._resolve_config_path(p, self.default), p)

    def test_relative_windows_path_passes_through(self):
        p = r"sub\x.ixncfg"
        self.assertEqual(self.sc._resolve_config_path(p, self.default), p)

    def test_forward_slash_path_passes_through(self):
        p = "some/dir/x.ixncfg"
        self.assertEqual(self.sc._resolve_config_path(p, self.default), p)

    def test_is_bare_filename(self):
        self.assertTrue(self.sc._is_bare_filename("x.ixncfg"))
        self.assertFalse(self.sc._is_bare_filename(r"C:\x.ixncfg"))
        self.assertFalse(self.sc._is_bare_filename("dir/x.ixncfg"))
        self.assertFalse(self.sc._is_bare_filename(""))

    def test_load_subparser_has_folder_default(self):
        args = build_parser().parse_args(
            ["session", "load", "x.ixncfg", "--host", "h"]
        )
        self.assertEqual(args.folder, self.default)


class HostDefaultTests(unittest.TestCase):
    """`--host` is optional when $IXIA_HOST is set; flag always wins."""

    def test_env_host_is_default(self):
        with mock.patch.dict(os.environ, {"IXIA_HOST": "env-host"}):
            args = build_parser().parse_args(["topo", "list"])
        self.assertEqual(args.host, "env-host")

    def test_flag_overrides_env_host(self):
        with mock.patch.dict(os.environ, {"IXIA_HOST": "env-host"}):
            args = build_parser().parse_args(
                ["topo", "list", "--host", "other"]
            )
        self.assertEqual(args.host, "other")

    def test_env_port_and_user(self):
        with mock.patch.dict(
            os.environ,
            {"IXIA_HOST": "h", "IXIA_PORT": "443", "IXIA_USER": "admin"},
        ):
            args = build_parser().parse_args(["topo", "list"])
        self.assertEqual(args.port, 443)
        self.assertEqual(args.user, "admin")

    def test_missing_host_is_bad_argument(self):
        env_clear = {k: "" for k in ("IXIA_HOST",)}
        with mock.patch.dict(os.environ, env_clear, clear=False):
            os.environ.pop("IXIA_HOST", None)
            args = build_parser().parse_args(["topo", "list", "--json"])
            out = io.StringIO()
            with redirect_stdout(out):
                rc = common.apply_session_policy(args)
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue())["status"], "bad_argument")


class CredentialPolicyTests(unittest.TestCase):
    """--password / --api-key resolve from flag then env into the policy."""

    def setUp(self):
        from ixia_core import session as core_session
        self.core_session = core_session

    def test_flag_password_recorded(self):
        args = build_parser().parse_args(
            ["topo", "list", "--host", "h", "--password", "s3cret"]
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IXIA_PASSWORD", None)
            self.assertIsNone(common.apply_session_policy(args))
        self.assertEqual(
            self.core_session.current_policy()["password"], "s3cret"
        )

    def test_env_api_key_recorded(self):
        args = build_parser().parse_args(["topo", "list", "--host", "h"])
        with mock.patch.dict(os.environ, {"IXIA_API_KEY": "abc123"}):
            self.assertIsNone(common.apply_session_policy(args))
        self.assertEqual(
            self.core_session.current_policy()["api_key"], "abc123"
        )


class ValueParserTests(unittest.TestCase):
    def test_name_or_index(self):
        self.assertEqual(common.name_or_index("1"), 1)
        self.assertEqual(common.name_or_index("DG1"), "DG1")
        self.assertIsNone(common.name_or_index(None))

    def test_parse_bool(self):
        self.assertTrue(common.parse_bool("true"))
        self.assertFalse(common.parse_bool("no"))
        with self.assertRaises(ValueError):
            common.parse_bool("maybe")

    def test_parse_rt_string_and_json(self):
        self.assertEqual(common.parse_rt("65000:1"), "65000:1")
        self.assertEqual(
            common.parse_rt('{"type":"ip","ip":"1.2.3.4","assigned":5}'),
            {"type": "ip", "ip": "1.2.3.4", "assigned": 5},
        )

    def test_parse_lines(self):
        self.assertEqual(common.parse_lines("all", []), "all")
        self.assertEqual(common.parse_lines(None, []), "all")
        self.assertEqual(common.parse_lines("2", []), 2)
        self.assertEqual(common.parse_lines("1,3,5", []), [1, 3, 5])
        self.assertEqual(common.parse_lines("all", [4, 6]), [4, 6])


class PayloadTests(unittest.TestCase):
    def test_inline(self):
        self.assertEqual(output.read_payload('{"a":1}', None), '{"a":1}')

    def test_parse_json(self):
        self.assertEqual(output.parse_json_payload('{"a":1}', None), {"a": 1})
        self.assertIsNone(output.parse_json_payload(None, None))
        with self.assertRaises(ValueError):
            output.parse_json_payload("{not json", None)


class ExitCodeTests(unittest.TestCase):
    def test_ok_and_warning_are_zero(self):
        self.assertEqual(output.exit_code_for({"status": "ok"}), 0)
        self.assertEqual(output.exit_code_for({"status": "warning"}), 0)

    def test_errors_are_nonzero(self):
        for s in ("error", "connect_error", "timeout", "bad_argument",
                  "confirmation_required", "aborted"):
            self.assertEqual(output.exit_code_for({"status": s}), 1, s)


class ConfirmGateTests(unittest.TestCase):
    def _args(self, **kw):
        ns = build_parser().parse_args(
            ["topo", "delete", "t1", "--host", "h"]
        )
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def test_yes_proceeds(self):
        args = self._args(yes=True)
        self.assertIsNone(
            common.confirm_or_exit(args, kind="delete_topology", action="x")
        )

    def test_off_tty_refuses(self):
        # stdin/stderr are not TTYs under the test runner → must refuse.
        args = self._args(yes=False, json=True)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = common.confirm_or_exit(
                args, kind="delete_topology", action="delete t1",
            )
        self.assertEqual(rc, 1)
        env = json.loads(out.getvalue())
        self.assertEqual(env["status"], "confirmation_required")

    def test_interactive_prompt_to_stderr_not_stdout(self):
        # On a TTY the prompt goes to stderr; stdout stays clean so a piped
        # `--json` is neither corrupted nor blocked on a swallowed prompt.
        args = self._args(yes=False, json=False)
        err = io.StringIO()
        out = io.StringIO()
        with mock.patch.object(common.sys, "stdin",
                               mock.Mock(isatty=lambda: True)), \
             mock.patch.object(common.sys, "stderr",
                               mock.Mock(isatty=lambda: True,
                                         write=err.write, flush=lambda: None)), \
             mock.patch("builtins.input", return_value="y"), \
             redirect_stdout(out):
            rc = common.confirm_or_exit(
                args, kind="delete_topology", action="delete t1",
            )
        self.assertIsNone(rc)
        self.assertIn("Proceed? [y/N]", err.getvalue())
        self.assertEqual(out.getvalue(), "")


class RenderTests(unittest.TestCase):
    def test_json_emit_roundtrips(self):
        env = {"status": "ok", "kind": "list_topologies", "host": "h",
               "session_id": 1,
               "result": {"count": 1, "topologies": [
                   {"name": "CL", "ports": ["1"], "href": "/t/1"}]},
               "warnings": [], "errors": [], "next_actions": []}
        out = io.StringIO()
        with redirect_stdout(out):
            rc = output.emit(env, as_json=True)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["kind"], "list_topologies")

    def test_text_table(self):
        env = {"status": "ok", "kind": "list_vports", "host": "h",
               "session_id": 1,
               "result": {"count": 2, "vports": [
                   {"name": "v1", "link_state": "up"},
                   {"name": "v2", "link_state": "down"}]},
               "warnings": [], "errors": [], "next_actions": []}
        out = io.StringIO()
        with redirect_stdout(out):
            output.emit(env, as_json=False)
        text = out.getvalue()
        self.assertIn("v1", text)
        self.assertIn("link_state", text)


if __name__ == "__main__":
    unittest.main()
