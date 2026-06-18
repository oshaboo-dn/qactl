"""CLI-layer tests for qactl — everything that needs no live service.

Covers argument parsing, the global flag block, the destructive-op
confirm gate, exit-code mapping, payload reading, credential-error
envelopes, and envelope rendering. The wire path (REST to Jira /
Confluence / Jenkins) is covered by the README's acceptance smoke test
against real credentials.

Run with:  python -m pytest -q
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from qactl.__main__ import build_native_parser
from qactl.core import common, output
from qactl.core.creds import AtlassianConfig, JenkinsConfig, CredentialError


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_native_parser()

    def test_global_flags_after_subcommand(self):
        args = self.parser.parse_args(["jira", "whoami", "--json"])
        self.assertTrue(args.json)
        self.assertEqual(args.group, "jira")

    def test_jira_status_positional(self):
        args = self.parser.parse_args(["jira", "status", "SW-1", "--json"])
        self.assertEqual(args.issue_key, "SW-1")

    def test_nested_watchers_add(self):
        args = self.parser.parse_args(["jira", "watchers", "add", "SW-1", "acc-123"])
        self.assertEqual(args.issue_key, "SW-1")
        self.assertEqual(args.account_id, "acc-123")

    def test_confluence_comment(self):
        args = self.parser.parse_args(
            ["confluence", "comment", "12345", "--text", "hi", "--json"]
        )
        self.assertEqual(args.page_id, "12345")
        self.assertEqual(args.text, "hi")

    def test_confluence_comment_text_file(self):
        args = self.parser.parse_args(
            ["confluence", "comment", "12345", "--text-file", "note.md"]
        )
        self.assertEqual(args.text_file, "note.md")
        self.assertIsNone(args.text)

    def test_jenkins_trigger_defaults(self):
        args = self.parser.parse_args(["jenkins", "trigger", "feature/foo"])
        self.assertEqual(args.repo, "cheetah")
        self.assertEqual(args.org, "drivenets")
        self.assertFalse(args.wait)
        self.assertFalse(args.sanitizer)

    def test_jenkins_stop_branch_optional(self):
        args = self.parser.parse_args(["jenkins", "stop", "--queue-id", "42"])
        self.assertIsNone(args.branch)
        self.assertEqual(args.queue_id, 42)

    def test_jenkins_trigger_raw(self):
        args = self.parser.parse_args([
            "jenkins", "trigger-raw", "drivenets/myrepo/main",
            "--param", "FOO=1", "--param", "BAR=two",
        ])
        self.assertEqual(args.job_path, "drivenets/myrepo/main")
        self.assertEqual(args.param, ["FOO=1", "BAR=two"])


class ExitCodeTests(unittest.TestCase):
    def test_ok_and_warning_zero(self):
        self.assertEqual(output.exit_code_for({"status": "ok"}), 0)
        self.assertEqual(output.exit_code_for({"status": "warning"}), 0)

    def test_errors_nonzero(self):
        for s in ("error", "bad_argument", "confirmation_required", "aborted"):
            self.assertEqual(output.exit_code_for({"status": s}), 1, s)


class ConfirmGateTests(unittest.TestCase):
    def _args(self, **kw):
        ns = build_native_parser().parse_args(["jira", "comment", "delete", "SW-1", "9"])
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def test_yes_proceeds(self):
        self.assertIsNone(common.confirm_or_exit(self._args(yes=True),
                                                 kind="jira_delete_comment", action="x"))

    def test_off_tty_refuses(self):
        args = self._args(yes=False, json=True)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = common.confirm_or_exit(args, kind="jira_delete_comment", action="del")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue())["status"], "confirmation_required")


class PayloadTests(unittest.TestCase):
    def test_inline(self):
        self.assertEqual(output.read_payload("body", None), "body")

    def test_none(self):
        self.assertIsNone(output.read_payload(None, None))

    def test_stdin_dash(self):
        with mock.patch("sys.stdin", io.StringIO("piped body")):
            self.assertEqual(output.read_payload("-", None), "piped body")

    def test_file_wins_over_inline(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("from file")
            path = fh.name
        try:
            self.assertEqual(output.read_payload("inline", path), "from file")
        finally:
            os.unlink(path)


class CredsTests(unittest.TestCase):
    def test_atlassian_missing_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(CredentialError):
                AtlassianConfig.resolve()

    def test_atlassian_from_env(self):
        with mock.patch.dict(os.environ, {"ATLASSIAN_EMAIL": "a@b.c",
                                          "ATLASSIAN_API_TOKEN": "tok"}, clear=True):
            cfg = AtlassianConfig.resolve()
        self.assertEqual(cfg.email, "a@b.c")
        self.assertEqual(cfg.base_url, "https://drivenets.atlassian.net")

    def test_atlassian_flag_overrides_env(self):
        with mock.patch.dict(os.environ, {"ATLASSIAN_EMAIL": "env@x",
                                          "ATLASSIAN_API_TOKEN": "envtok"}, clear=True):
            cfg = AtlassianConfig.resolve(email="flag@x", api_token="flagtok",
                                          base_url="https://other.example/")
        self.assertEqual(cfg.email, "flag@x")
        self.assertEqual(cfg.base_url, "https://other.example")

    def test_jenkins_missing_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(CredentialError):
                JenkinsConfig.resolve()

    def test_jenkins_default_url(self):
        with mock.patch.dict(os.environ, {"JENKINS_USER": "u",
                                          "JENKINS_API_TOKEN": "t"}, clear=True):
            cfg = JenkinsConfig.resolve()
        self.assertEqual(cfg.url, "https://jenkins.dev.drivenets.net")


class RenderTests(unittest.TestCase):
    def test_json_roundtrip(self):
        env = {"status": "ok", "kind": "jira_status",
               "result": {"issue_key": "SW-1", "status": "To Do"},
               "warnings": [], "errors": [], "next_actions": []}
        out = io.StringIO()
        with redirect_stdout(out):
            rc = output.emit(env, as_json=True)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["kind"], "jira_status")

    def test_text_table(self):
        env = {"status": "ok", "kind": "jenkins_list",
               "result": {"count": 2, "builds": [
                   {"number": 1, "result": "SUCCESS"},
                   {"number": 2, "result": "FAILURE"}]},
               "warnings": [], "errors": [], "next_actions": []}
        out = io.StringIO()
        with redirect_stdout(out):
            output.emit(env, as_json=False)
        text = out.getvalue()
        self.assertIn("SUCCESS", text)
        self.assertIn("number", text)


class CheetahParamTests(unittest.TestCase):
    def test_named_overrides_map_to_jenkins_params(self):
        from qactl.jenkins.cli import build_cheetah_params

        args = build_native_parser().parse_args([
            "jenkins", "trigger", "feature/foo", "--sanitizer", "--baseos",
            "--no-smoke",
        ])
        params, warning = build_cheetah_params(args, client=None, job_path="x")
        self.assertIsNone(warning)
        self.assertEqual(params["TEST_NAMES"], "ENABLE_SANITIZER")
        self.assertEqual(params["SHOULD_BUILD_BASEOS_CONTAINERS"], "Yes")
        self.assertEqual(params["SHOULD_RUN_SMOKE_TESTS"], "No")
        self.assertEqual(params["SHOULD_LINT"], "Yes")


class RawParamTests(unittest.TestCase):
    def test_parse_params_pairs_and_json(self):
        from qactl.jenkins.cli import _parse_params
        self.assertEqual(
            _parse_params(["A=1", "B=x"], '{"C": "y"}'),
            {"A": "1", "B": "x", "C": "y"},
        )

    def test_parse_params_bad_pair(self):
        from qactl.jenkins.cli import _parse_params
        with self.assertRaises(ValueError):
            _parse_params(["noequals"], None)


if __name__ == "__main__":
    unittest.main()
