"""Direct invocation of the vendored `dnctl` / `ixiactl` commands should
nudge users to the umbrella `qactl` CLI.

The notice must go to stderr only, so `--json` output on stdout stays
lossless. When `qactl` delegates (it leaves/sets ``sys.argv[0]`` to
``qactl``), no notice is emitted.

Run with:  python -m pytest -q
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from dnctl.__main__ import _warn_if_standalone as dnctl_warn
from ixiactl.__main__ import _warn_if_standalone as ixiactl_warn


def _capture(fn, argv0):
    err, out = io.StringIO(), io.StringIO()
    with mock.patch("sys.argv", [argv0, "whatever"]):
        with redirect_stderr(err), redirect_stdout(out):
            fn()
    return out.getvalue(), err.getvalue()


class DnctlDeprecationTests(unittest.TestCase):
    def test_standalone_warns_on_stderr(self):
        out, err = _capture(dnctl_warn, "/home/u/.local/bin/dnctl")
        self.assertEqual(out, "")
        self.assertIn("deprecated", err)
        self.assertIn("qactl", err)

    def test_windows_exe_suffix_warns(self):
        _out, err = _capture(dnctl_warn, "dnctl.exe")
        self.assertIn("deprecated", err)

    def test_delegated_via_qactl_is_silent(self):
        out, err = _capture(dnctl_warn, "qactl")
        self.assertEqual(out, "")
        self.assertEqual(err, "")


class IxiactlDeprecationTests(unittest.TestCase):
    def test_standalone_warns_on_stderr(self):
        out, err = _capture(ixiactl_warn, "/home/u/.local/bin/ixiactl")
        self.assertEqual(out, "")
        self.assertIn("deprecated", err)
        self.assertIn("qactl ixia", err)

    def test_delegated_via_qactl_is_silent(self):
        out, err = _capture(ixiactl_warn, "qactl")
        self.assertEqual(out, "")
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
