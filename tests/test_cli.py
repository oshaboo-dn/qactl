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
        self.assertEqual(args.issue_key, ["SW-1"])

    def test_jira_status_multi_key(self):
        args = self.parser.parse_args(["jira", "status", "SW-1", "SW-2", "SW-3"])
        self.assertEqual(args.issue_key, ["SW-1", "SW-2", "SW-3"])

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

    def test_jenkins_artifacts(self):
        args = self.parser.parse_args(["jenkins", "artifacts", "feature/foo", "7", "--all"])
        self.assertEqual(args.branch, "feature/foo")
        self.assertEqual(args.build_number, "7")
        self.assertTrue(args.all)

    def test_jenkins_artifacts_build_defaults_to_last(self):
        args = self.parser.parse_args(["jenkins", "artifacts", "feature/foo"])
        self.assertEqual(args.build_number, "lastBuild")
        self.assertFalse(args.all)

    def test_jenkins_trigger_raw(self):
        args = self.parser.parse_args([
            "jenkins", "trigger-raw", "drivenets/myrepo/main",
            "--param", "FOO=1", "--param", "BAR=two",
        ])
        self.assertEqual(args.job_path, "drivenets/myrepo/main")
        self.assertEqual(args.param, ["FOO=1", "BAR=two"])

    def test_jenkins_notify_slack_default_none(self):
        args = self.parser.parse_args(["jenkins", "trigger", "feature/foo"])
        self.assertIsNone(args.notify_slack)

    def test_jenkins_notify_slack_bare_flag(self):
        args = self.parser.parse_args(["jenkins", "trigger", "feature/foo", "--notify-slack"])
        self.assertEqual(args.notify_slack, "")  # webhook / default channel

    def test_jenkins_notify_slack_with_channel(self):
        args = self.parser.parse_args(
            ["jenkins", "trigger", "feature/foo", "--notify-slack", "#builds"])
        self.assertEqual(args.notify_slack, "#builds")

    def test_jenkins_watch_by_queue_id(self):
        args = self.parser.parse_args(
            ["jenkins", "watch", "feature/foo", "--queue-id", "99", "--notify-slack"])
        self.assertEqual(args.queue_id, 99)
        self.assertIsNone(args.build_number)
        self.assertEqual(args.notify_slack, "")

    def test_jenkins_watch_build_and_queue_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(
                ["jenkins", "watch", "feature/foo", "--queue-id", "9", "--build-number", "7"])


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

    def test_interactive_prompt_to_stderr_not_stdout(self):
        # On a TTY the prompt must go to stderr and stdout must stay clean,
        # so a piped `--json | jq` neither hangs nor gets the prompt mixed
        # into the JSON. A 'y' reply proceeds (returns None).
        args = self._args(yes=False, json=False)
        err = io.StringIO()
        out = io.StringIO()
        with mock.patch.object(common.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(common.sys, "stderr",
                               mock.Mock(isatty=lambda: True,
                                         write=err.write, flush=lambda: None)), \
             mock.patch("builtins.input", return_value="y"), \
             redirect_stdout(out):
            rc = common.confirm_or_exit(args, kind="jira_delete_comment",
                                        action="delete the thing")
        self.assertIsNone(rc)
        self.assertIn("Proceed? [y/N]", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_interactive_decline_aborts(self):
        args = self._args(yes=False, json=True)
        err = io.StringIO()
        out = io.StringIO()
        with mock.patch.object(common.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(common.sys, "stderr",
                               mock.Mock(isatty=lambda: True,
                                         write=err.write, flush=lambda: None)), \
             mock.patch("builtins.input", return_value="n"), \
             redirect_stdout(out):
            rc = common.confirm_or_exit(args, kind="jira_delete_comment", action="del")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue())["status"], "aborted")


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


class ArtifactParseTests(unittest.TestCase):
    def test_first_line_skips_blanks(self):
        from qactl.jenkins.tools import _first_line
        self.assertEqual(_first_line("\n\n  http://x/y.tar \n"), "http://x/y.tar")
        self.assertEqual(_first_line(""), "")

    def test_parse_kv_lines(self):
        from qactl.jenkins.tools import _parse_kv_lines
        self.assertEqual(
            _parse_kv_lines("# comment\nCDNOS_IMAGE=reg/cdnos:tag\nNOEQ\n"),
            {"CDNOS_IMAGE": "reg/cdnos:tag"},
        )


class _FakeJenkins:
    """Stands in for JenkinsClient: serves a build's artifacts from a dict."""

    def __init__(self, files):
        self._files = files

    @classmethod
    def make_from_env(cls, files):
        def _from_env(*a, **k):
            return cls(files)
        return _from_env

    def get_build_artifacts(self, job_path, build_number="lastBuild"):
        return {
            "number": 7, "result": "SUCCESS", "building": False,
            "url": "https://j/job/x/7/",
            "artifacts": [{"fileName": n, "relativePath": n} for n in self._files],
        }

    def get_artifact_text(self, build_url, relative_path):
        return self._files[relative_path]


class _FakeTriggerJenkins:
    """Stands in for JenkinsClient across a trigger→wait→result flow."""

    def __init__(self, result="SUCCESS"):
        self._result = result

    @classmethod
    def make_from_env(cls, result):
        def _from_env(*a, **k):
            return cls(result)
        return _from_env

    def _job_url(self, job_path):
        return "https://j/job/x"

    def trigger_build(self, job_path, parameters=None):
        return {"job_url": "https://j/job/x", "queue_id": 99,
                "queue_url": "https://j/queue/item/99"}

    def wait_for_build_number(self, queue_id, timeout_s=300.0, poll_s=1.0):
        return {"status": "started", "build_number": 764, "build_url": "https://j/job/x/764/"}

    def wait_for_build_result(self, job_path, build_number, timeout_s=300.0, poll_s=1.0):
        return {"status": "done", "result": self._result, "building": False}


class JenkinsNotifySlackTests(unittest.TestCase):
    """notify_slack posts start + terminal updates on the inline --wait path."""

    def _drive(self, result="SUCCESS", notify_slack="", wait=True):
        from qactl.jenkins import tools
        posts = []
        with mock.patch.object(tools.JenkinsClient, "from_env",
                               _FakeTriggerJenkins.make_from_env(result)), \
             mock.patch.object(tools, "_notify",
                               lambda ch, text, warn: posts.append((ch, text))):
            env = tools.jenkins_trigger("feature/foo", confirm=True,
                                        notify_slack=notify_slack, wait=wait)
        return env, posts

    def test_success_posts_start_and_success(self):
        env, posts = self._drive(result="SUCCESS", wait=True)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["result"]["build_number"], 764)
        self.assertEqual(len(posts), 2)  # start + terminal
        self.assertIn("started", posts[0][1])
        self.assertIn("SUCCESS", posts[1][1])

    def test_failure_posts_failure(self):
        env, posts = self._drive(result="FAILURE", wait=True)
        self.assertEqual(env["status"], "error")
        self.assertIn("FAILURE", posts[1][1])

    def test_no_wait_no_inline_notify(self):
        # wait=False at the tool layer just returns queued; the CLI arranges
        # a detached watcher, so the tool itself posts nothing.
        env, posts = self._drive(notify_slack="", wait=False)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(posts, [])


class JenkinsWatchTests(unittest.TestCase):
    """jenkins_watch attaches to an existing build without re-triggering."""

    def _watch(self, result="SUCCESS", **kw):
        from qactl.jenkins import tools
        posts = []
        with mock.patch.object(tools.JenkinsClient, "from_env",
                               _FakeTriggerJenkins.make_from_env(result)), \
             mock.patch.object(tools, "_notify",
                               lambda ch, text, warn: posts.append((ch, text))):
            env = tools.jenkins_watch("feature/foo", notify_slack="", **kw)
        return env, posts

    def test_watch_by_queue_id_posts_start_and_finish(self):
        env, posts = self._watch(queue_id=99)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(len(posts), 2)
        self.assertIn("started", posts[0][1])

    def test_watch_by_build_number_posts_finish_only(self):
        env, posts = self._watch(build_number=764)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(len(posts), 1)  # already running → no 'started'
        self.assertIn("SUCCESS", posts[0][1])

    def test_watch_never_triggers(self):
        from qactl.jenkins import tools
        fake = _FakeTriggerJenkins("SUCCESS")
        fake.trigger_build = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("watch must not trigger a build"))
        with mock.patch.object(tools.JenkinsClient, "from_env", lambda *a, **k: fake), \
             mock.patch.object(tools, "_notify", lambda *a, **k: None):
            env = tools.jenkins_watch("feature/foo", build_number=764)
        self.assertEqual(env["status"], "ok")


class JenkinsTriggerDetachTests(unittest.TestCase):
    """`trigger --notify-slack` (no --wait) detaches a watcher, returns now."""

    def _args(self, **over):
        import types
        base = dict(
            branch="feature/foo", repo="cheetah", org="drivenets", json=False,
            sanitizer=False, baseos=False, no_lint=False, no_dnos=False,
            no_tarballs=False, no_smoke=False, delta_build=False, single_test="",
            single_test_label="test-tiny", single_test_parallel=1, single_test_loop=1,
            keep_setup_on_failure=False, nightly=False, qa_version=False,
            slack_channel="", inherit_from=None, extra_params=None, wait=False,
            wait_timeout=100.0, poll=5.0, notify_slack="", yes=True, timeout=30.0,
            user=None, token=None, url=None,
        )
        base.update(over)
        return types.SimpleNamespace(**base)

    def test_detaches_and_passes_notify_none_to_tool(self):
        from qactl.jenkins import cli
        queued = {"status": "ok", "kind": "jenkins_trigger",
                  "result": {"queue_id": 99}, "next_actions": []}
        captured = {}

        def fake_trigger(branch, **kw):
            captured.update(kw)
            return queued

        with mock.patch.object(cli.tools, "jenkins_trigger", fake_trigger), \
             mock.patch.object(cli, "_spawn_detached_watch", return_value=True) as spawn, \
             mock.patch.object(cli, "emit", lambda env, as_json=False: env) as _e:
            env = cli._trigger(self._args())
        # tool got notify_slack=None (detach owns the notifying), and we spawned
        self.assertIsNone(captured["notify_slack"])
        spawn.assert_called_once()
        self.assertTrue(any("background" in n for n in env["next_actions"]))

    def test_wait_path_notifies_inline_no_spawn(self):
        from qactl.jenkins import cli
        done = {"status": "ok", "kind": "jenkins_trigger", "result": {"build_number": 7}}
        captured = {}

        def fake_trigger(branch, **kw):
            captured.update(kw)
            return done

        with mock.patch.object(cli.tools, "jenkins_trigger", fake_trigger), \
             mock.patch.object(cli, "_spawn_detached_watch") as spawn, \
             mock.patch.object(cli, "emit", lambda env, as_json=False: env):
            cli._trigger(self._args(wait=True))
        self.assertEqual(captured["notify_slack"], "")  # inline notify
        spawn.assert_not_called()


class JenkinsArtifactsTests(unittest.TestCase):
    def _run(self, files):
        from qactl.jenkins import tools
        with mock.patch.object(tools.JenkinsClient, "from_env",
                               _FakeJenkins.make_from_env(files)):
            return tools.jenkins_artifacts("feature/foo", "7")

    def test_collects_download_links_and_images(self):
        env = self._run({
            "gi_base_os_artifact.txt": "http://minio/drivenets_baseos_2.x.tar\n",
            "gi_GI_artifact.txt": "http://minio/drivenets_gi_26.tar",
            "gi_DNOS_artifact.txt": "http://minio/drivenets_dnos_26.tar",
            "cdnos_images.txt": "CDNOS_IMAGE=pr-registry/cdnos:tag\n",
            "metadata.images": "pr-registry/gi:tag@sha256:abc\npr-registry/re:tag@sha256:def\n",
        })
        self.assertEqual(env["status"], "ok")
        res = env["result"]
        self.assertEqual(res["build_number"], 7)
        self.assertEqual(res["downloads"]["baseos_tar"], "http://minio/drivenets_baseos_2.x.tar")
        self.assertEqual(res["downloads"]["gi_tar"], "http://minio/drivenets_gi_26.tar")
        self.assertEqual(res["downloads"]["dnos_tar"], "http://minio/drivenets_dnos_26.tar")
        self.assertEqual(res["images"]["cdnos"], "pr-registry/cdnos:tag")
        self.assertEqual(len(res["images"]["registry"]), 2)
        self.assertEqual(res["artifact_base_url"], "https://j/job/x/7/artifact/")
        self.assertNotIn("artifacts", res)

    def test_warns_when_no_links(self):
        env = self._run({"some_other_file.txt": "irrelevant"})
        self.assertEqual(env["status"], "warning")
        self.assertEqual(env["result"]["downloads"], {})
        self.assertTrue(env["warnings"])


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
