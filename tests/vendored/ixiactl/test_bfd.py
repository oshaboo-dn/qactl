"""Tests for the BFD feature (#49): CLI parsing, validation, the rest
``OPTIONS`` fix, and relative-path resolution.

The wire path (RestPy → IxNetwork) still needs a live lab; these cover
everything reachable without one — argument parsing, the bad-argument
guards that fire before any session is opened, and the raw-REST escape
hatch with a faked connection.
"""

from __future__ import annotations

import unittest
from unittest import mock

from qactl.ixia.ctl.__main__ import build_parser


class BfdParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_bfd_create_flags(self):
        args = self.parser.parse_args([
            "bfd", "create", "--host", "h", "--topology", "CL",
            "--device-group", "DG1", "--name", "bfd1",
            "--tx-interval", "300", "--rx-interval", "300",
            "--detect-multiplier", "3", "--no-admin-state",
            "--control-plane-independent",
        ])
        self.assertEqual(args.tx_interval, 300)
        self.assertEqual(args.rx_interval, 300)
        self.assertEqual(args.detect_multiplier, 3)
        self.assertIs(args.admin_state, False)
        self.assertIs(args.control_plane_independent, True)

    def test_bfd_create_admin_state_default_is_none(self):
        args = self.parser.parse_args([
            "bfd", "create", "--host", "h", "--topology", "CL",
            "--device-group", "DG1", "--name", "bfd1",
        ])
        self.assertIsNone(args.admin_state)
        self.assertIsNone(args.control_plane_independent)
        self.assertIsNone(args.aggregate)

    def test_bfd_admin_state_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args([
                "bfd", "create", "--host", "h", "--topology", "CL",
                "--device-group", "DG1", "--name", "b",
                "--admin-state", "--no-admin-state",
            ])

    def test_bfd_get_defaults(self):
        args = self.parser.parse_args([
            "bfd", "get", "--host", "h", "--topology", "CL", "--name", "b",
        ])
        self.assertEqual(args.device_group, "1")
        self.assertEqual(args.ethernet, "1")
        self.assertEqual(args.ipv4, "1")

    def test_bfd_delete_func(self):
        args = self.parser.parse_args([
            "bfd", "delete", "--host", "h", "--topology", "CL",
            "--device-group", "DG1", "--name", "b",
        ])
        self.assertEqual(args.func.__name__, "_bfd_delete")

    def test_peer_create_bfd_registration(self):
        args = self.parser.parse_args([
            "bgp", "peer", "create", "--host", "h", "--topology", "CL",
            "--device-group", "DG1", "--name", "p", "--dut-ip", "1.1.1.1",
            "--local-as", "65000", "--bfd", "--bfd-mode", "multihop",
        ])
        self.assertIs(args.bfd, True)
        self.assertEqual(args.bfd_mode, "multihop")

    def test_peer_create_no_bfd(self):
        args = self.parser.parse_args([
            "bgp", "peer", "create", "--host", "h", "--topology", "CL",
            "--device-group", "DG1", "--name", "p", "--dut-ip", "1.1.1.1",
            "--local-as", "65000", "--no-bfd",
        ])
        self.assertIs(args.bfd, False)

    def test_peer_create_bfd_default_none(self):
        args = self.parser.parse_args([
            "bgp", "peer", "create", "--host", "h", "--topology", "CL",
            "--device-group", "DG1", "--name", "p", "--dut-ip", "1.1.1.1",
            "--local-as", "65000",
        ])
        self.assertIsNone(args.bfd)
        self.assertIsNone(args.bfd_mode)

    def test_peer_bfd_mode_rejects_unknown(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args([
                "bgp", "peer", "create", "--host", "h", "--topology", "CL",
                "--device-group", "DG1", "--name", "p", "--dut-ip", "1.1.1.1",
                "--local-as", "65000", "--bfd-mode", "bogus",
            ])


class BfdValidationTests(unittest.TestCase):
    """Bad-argument guards fire before any IxNetwork session is opened."""

    def test_negative_tx_interval(self):
        from qactl.ixia.tools.bfd import ixia_create_bfdv4_interface
        env = ixia_create_bfdv4_interface(
            host="h", topology="CL", device_group="DG1", name="b",
            tx_interval=-5,
        )
        self.assertEqual(env["status"], "bad_argument")

    def test_zero_detect_multiplier(self):
        from qactl.ixia.tools.bfd import ixia_create_bfdv4_interface
        env = ixia_create_bfdv4_interface(
            host="h", topology="CL", device_group="DG1", name="b",
            detect_multiplier=0,
        )
        self.assertEqual(env["status"], "bad_argument")

    def test_bad_no_of_sessions(self):
        from qactl.ixia.tools.bfd import ixia_create_bfdv4_interface
        env = ixia_create_bfdv4_interface(
            host="h", topology="CL", device_group="DG1", name="b",
            no_of_sessions=0,
        )
        self.assertEqual(env["status"], "bad_argument")

    def test_peer_bad_bfd_mode(self):
        from qactl.ixia.tools.stack import ixia_create_bgp_peer
        env = ixia_create_bgp_peer(
            host="h", topology="CL", device_group="DG1", name="p",
            dut_ip="1.1.1.1", local_as=65000, bfd_mode="sideways",
        )
        self.assertEqual(env["status"], "bad_argument")

    def test_delete_requires_confirm(self):
        from qactl.ixia.tools.bfd import ixia_delete_bfdv4_interface
        env = ixia_delete_bfdv4_interface(
            host="h", topology="CL", device_group="DG1", name="b",
            confirm=False,
        )
        self.assertEqual(env["status"], "confirmation_required")


class StateCountsTests(unittest.TestCase):
    def test_positional_args_relabelled(self):
        from qactl.ixia.tools.bfd import _normalise_state_counts
        self.assertEqual(
            _normalise_state_counts(
                {"arg1": 5, "arg2": 5, "arg3": 0, "arg4": 0}
            ),
            {"total": 5, "notStarted": 5, "down": 0, "up": 0},
        )

    def test_named_dict_passthrough(self):
        from qactl.ixia.tools.bfd import _normalise_state_counts
        named = {"total": 2, "notStarted": 0, "down": 1, "up": 1}
        self.assertEqual(_normalise_state_counts(named), named)

    def test_non_dict_passthrough(self):
        from qactl.ixia.tools.bfd import _normalise_state_counts
        self.assertIsNone(_normalise_state_counts(None))


class _FakeConn:
    def __init__(self):
        self.calls = []

    def _read(self, url):
        self.calls.append(("_read", url))
        return {"url": url}

    def _send_recv(self, method, url, payload=None):
        self.calls.append(("_send_recv", method, url))
        return {"method": method, "url": url}

    def _execute(self, url, payload):  # POST-only in real RestPy
        self.calls.append(("_execute", url))
        raise AssertionError("_execute must not be used for OPTIONS")


class _FakeIxn:
    def __init__(self, conn, href):
        self._connection = conn
        self.href = href


class _FakeSession:
    def __init__(self, conn, href):
        self.ixn = _FakeIxn(conn, href)
        self.connected = True


class RestOptionsTests(unittest.TestCase):
    """``ixia rest get --method OPTIONS`` must route through _send_recv."""

    def _patch_session(self, conn):
        href = "/api/v1/sessions/1/ixnetwork"
        return mock.patch(
            "qactl.ixia.tools.rest.get_session",
            return_value=_FakeSession(conn, href),
        )

    def test_options_uses_send_recv(self):
        from qactl.ixia.tools.rest import ixia_rest_get
        conn = _FakeConn()
        with self._patch_session(conn):
            env = ixia_rest_get(
                host="h",
                path="/api/v1/sessions/1/ixnetwork/globals",
                method="OPTIONS",
            )
        self.assertEqual(env["status"], "ok")
        self.assertIn(
            ("_send_recv", "OPTIONS",
             "/api/v1/sessions/1/ixnetwork/globals"),
            conn.calls,
        )

    def test_get_uses_read(self):
        from qactl.ixia.tools.rest import ixia_rest_get
        conn = _FakeConn()
        with self._patch_session(conn):
            env = ixia_rest_get(
                host="h",
                path="/api/v1/sessions/1/ixnetwork/globals",
                method="GET",
            )
        self.assertEqual(env["status"], "ok")
        self.assertEqual(conn.calls[0][0], "_read")


class NormalisePathTests(unittest.TestCase):
    def test_api_path_passthrough(self):
        from qactl.ixia.tools.rest import _normalise_path
        self.assertEqual(
            _normalise_path("/api/v1/sessions/1/ixnetwork/topology",
                            "/api/v1/sessions/1/ixnetwork"),
            "/api/v1/sessions/1/ixnetwork/topology",
        )

    def test_bare_api_prefix_gets_slash(self):
        from qactl.ixia.tools.rest import _normalise_path
        self.assertEqual(
            _normalise_path("api/v1/sessions/1/ixnetwork", None),
            "/api/v1/sessions/1/ixnetwork",
        )

    def test_relative_resolves_against_root(self):
        from qactl.ixia.tools.rest import _normalise_path
        self.assertEqual(
            _normalise_path("topology/1/deviceGroup/1",
                            "/api/v1/sessions/1/ixnetwork"),
            "/api/v1/sessions/1/ixnetwork/topology/1/deviceGroup/1",
        )

    def test_relative_without_root_falls_back_to_slash(self):
        from qactl.ixia.tools.rest import _normalise_path
        self.assertEqual(
            _normalise_path("topology/1", None), "/topology/1",
        )

    def test_empty_path_raises(self):
        from qactl.ixia.tools.rest import _normalise_path
        with self.assertRaises(ValueError):
            _normalise_path("", "/api/v1/sessions/1/ixnetwork")


if __name__ == "__main__":
    unittest.main()
