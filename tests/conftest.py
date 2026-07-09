import pytest


@pytest.fixture(autouse=True)
def _isolate_device_journal(tmp_path_factory, monkeypatch):
    """Keep the always-on per-device journal (qactl ``finish``) out of the
    real ``~/.qactl/device-logs`` during the test run. Tests that exercise
    the journal directly override ``QACTL_DEVICE_LOG_DIR`` after this.
    """
    monkeypatch.setenv("QACTL_DEVICE_LOG_DIR", str(tmp_path_factory.mktemp("device-logs")))


@pytest.fixture(autouse=True)
def _isolate_session_daemon(monkeypatch):
    """Force direct in-process session execution during tests. The daemon's
    marker file lives in the REAL state dir, so a host where `qactl cli
    session on` was run would otherwise route fakes through the live daemon
    (UnknownDeviceError on scripted devices). The env knob beats the marker;
    daemon tests re-set it themselves.
    """
    monkeypatch.setenv("QACTL_SESSION_DAEMON", "0")
