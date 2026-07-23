"""Tests for TrafficItemProxy.start()/stop().

Regression: per-item start/stop must invoke
StartStatelessTrafficBlocking / StopStatelessTrafficBlocking ON the
resolved TrafficItem object, NOT on ixn.Traffic with the item as a
positional arg (the global op takes no args, so passing one errors with
"startstatelesstrafficblocking is not a valid operation").
"""

from __future__ import annotations

import unittest

from qactl.ixia.client.traffic import TrafficItemProxy
from qactl.ixia.client.models import IxiaOperationError


class _FakeTrafficItem:
    def __init__(self, name):
        self.Name = name
        self.started = 0
        self.stopped = 0

    def StartStatelessTrafficBlocking(self, *args):
        assert not args, "per-item op must take no positional args"
        self.started += 1

    def StopStatelessTrafficBlocking(self, *args):
        assert not args, "per-item op must take no positional args"
        self.stopped += 1


class _FakeTrafficItemCollection:
    def __init__(self, item):
        self._item = item

    def find(self, Name=None):
        return self._item if Name == self._item.Name else None


class _FakeTraffic:
    """Global Traffic node. If start/stop is called here with an arg, the
    real IxNetwork raises 'not a valid operation' — mimic that."""

    def __init__(self, item):
        self.TrafficItem = _FakeTrafficItemCollection(item)

    def StartStatelessTrafficBlocking(self, *args):
        if args:
            raise AssertionError(
                "startstatelesstrafficblocking is not a valid operation")

    def StopStatelessTrafficBlocking(self, *args):
        if args:
            raise AssertionError(
                "stopstatelesstrafficblocking is not a valid operation")


class _FakeIxn:
    def __init__(self, item):
        self.Traffic = _FakeTraffic(item)


class _FakeSession:
    def __init__(self, item):
        self.ixn = _FakeIxn(item)


class TrafficItemProxyStartStopTests(unittest.TestCase):
    def test_start_invokes_op_on_item(self):
        item = _FakeTrafficItem("TI-1")
        proxy = TrafficItemProxy(_FakeSession(item), "TI-1")
        result = proxy.start()
        self.assertIs(result, proxy)  # fluent
        self.assertEqual(item.started, 1)

    def test_stop_invokes_op_on_item(self):
        item = _FakeTrafficItem("TI-1")
        proxy = TrafficItemProxy(_FakeSession(item), "TI-1")
        result = proxy.stop()
        self.assertIs(result, proxy)
        self.assertEqual(item.stopped, 1)

    def test_start_error_wrapped(self):
        item = _FakeTrafficItem("TI-1")

        def boom(*args):
            raise RuntimeError("wire failure")

        item.StartStatelessTrafficBlocking = boom
        proxy = TrafficItemProxy(_FakeSession(item), "TI-1")
        with self.assertRaises(IxiaOperationError):
            proxy.start()


if __name__ == "__main__":
    unittest.main()
