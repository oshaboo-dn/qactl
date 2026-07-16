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
