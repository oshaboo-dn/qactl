"""All request/device logs must live under the local state dir, never the package."""

import importlib
from pathlib import Path

import pytest

import qactl.dnctl as dnctl
from qactl.dnctl.core.paths import state_dir


PACKAGE_DIR = Path(dnctl.__file__).resolve().parent
STATE_DIR = state_dir()


@pytest.mark.parametrize(
    "modname,attr",
    [
        ("qactl.dnctl.gnmi.core.request_log", "MCP_LOG_DIR"),
        ("qactl.dnctl.rc.core.request_log", "MCP_LOG_DIR"),
        ("qactl.dnctl.nc.core.request_log", "MCP_LOG_DIR"),
        ("qactl.dnctl.cli.core.logging", "_MCP_LOGS_DIR"),
        ("qactl.dnctl.cli.core.logging", "_LOGS_DIR"),
    ],
)
def test_logs_under_state_dir(modname, attr):
    mod = importlib.import_module(modname)
    d = Path(str(getattr(mod, attr)))
    assert str(d).startswith(str(STATE_DIR)), f"{modname}.{attr} -> {d} (not under {STATE_DIR})"
    assert PACKAGE_DIR not in d.parents, f"{modname}.{attr} resolves inside the package"
