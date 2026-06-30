import pytest


@pytest.fixture(autouse=True)
def _isolate_device_journal(tmp_path_factory, monkeypatch):
    """Keep the always-on per-device journal (dnctl ``finish``) out of the
    real ``~/.qactl/device-logs`` during the test run. Tests that exercise
    the journal directly override ``QACTL_DEVICE_LOG_DIR`` after this.
    """
    monkeypatch.setenv("QACTL_DEVICE_LOG_DIR", str(tmp_path_factory.mktemp("device-logs")))
