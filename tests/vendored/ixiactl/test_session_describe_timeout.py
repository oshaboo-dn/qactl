"""``session describe`` must honor a timeout instead of hanging (#51).

A live session whose deep read (typically the per-peer BGP stat view)
blocks used to make ``describe`` hang with no output and exit non-zero
only when externally killed. The tool now bounds the whole snapshot and
returns a ``timeout`` envelope (non-zero exit) with an actionable hint.

No live IxNetwork: ``get_session`` is faked and the deep read is forced
to block so the bound is exercised deterministically.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from qactl.ixia.tools.inspect import DEFAULT_DESCRIBE_TIMEOUT_S, ixia_describe_session
from qactl.ixia.ctl.__main__ import build_parser


class _SlowTopologyColl:
    def find(self):
        # Simulate RestPy blocking on a tree walk / stat view.
        time.sleep(5)
        return []


class _FastTopologyColl:
    def find(self):
        return []


class _Vport:
    def find(self):
        return []


class _Ixn:
    href = "/api/v1/sessions/1/ixnetwork/"

    def __init__(self, slow: bool):
        self.Topology = _SlowTopologyColl() if slow else _FastTopologyColl()
        self.Vport = _Vport()


class _FakeSession:
    def __init__(self, slow: bool):
        self.ixn = _Ixn(slow)


class DescribeTimeoutTests(unittest.TestCase):
    def test_slow_deep_read_returns_timeout_not_hang(self):
        with mock.patch(
            "qactl.ixia.tools.inspect.get_session",
            return_value=_FakeSession(slow=True),
        ):
            start = time.monotonic()
            env = ixia_describe_session(host="h", timeout_s=1)
            elapsed = time.monotonic() - start

        # Returned promptly (well under the 5s simulated block).
        self.assertLess(elapsed, 4.0)
        self.assertEqual(env["status"], "timeout")
        self.assertTrue(any("exceeded" in e for e in env["errors"]))
        self.assertTrue(
            any("--no-route-counts" in n for n in env["next_actions"])
        )
        self.assertTrue(
            any("--timeout" in n for n in env["next_actions"])
        )

    def test_timeout_status_maps_to_nonzero_exit(self):
        from qactl.ixia.ctl.core.output import exit_code_for

        self.assertNotEqual(exit_code_for({"status": "timeout"}), 0)

    def test_fast_session_succeeds_within_budget(self):
        with mock.patch(
            "qactl.ixia.tools.inspect.get_session",
            return_value=_FakeSession(slow=False),
        ):
            env = ixia_describe_session(host="h", timeout_s=5)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["result"]["topologies"], [])
        self.assertEqual(env["request"]["timeout_s"], 5)

    def test_default_timeout_constant_is_reasonable(self):
        self.assertGreaterEqual(DEFAULT_DESCRIBE_TIMEOUT_S, 5)

    def test_cli_timeout_flag_overrides_describe_budget(self):
        # The global --timeout must reach the describe handler.
        parser = build_parser()
        args = parser.parse_args(
            ["session", "describe", "--host", "h", "--timeout", "7"]
        )
        self.assertEqual(args.timeout, 7)


if __name__ == "__main__":
    unittest.main()
