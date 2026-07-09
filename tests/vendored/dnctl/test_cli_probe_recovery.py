"""Recovery-mode / machine-state detection for ``show system`` (issue #66).

Covers the stable ``state`` enum (``running`` | ``running-degraded`` |
``recovery`` | ``gi`` | ``unknown`` | ``unreachable``) and the raw
``system_status`` field, plus the connect-path classification: transient
connect failures (the signature a rebooting / recovery-mode box shows —
banner reset, auth timeout) tag the envelope ``state: unreachable`` and hint
at recovery mode, while deterministic failures don't.

The degraded fixture is the live Hybrid-CL capture from the 2026-07-02
SW-279187 HA-escalation episode (request log, 14:45:50). No device traffic.
"""

import paramiko
import pytest

from qactl.dnctl.core.cli_probe import (
    classify_system_state,
    detect_system_mode,
    parse_system_status,
)
from qactl.dnctl.cli.core.session import (
    ConnectError,
    UnknownDeviceError,
    connect_error_next_actions,
    _is_transient_connect_error,
)
from qactl.dnctl.cli.tools.discovery import _annotate_system_mode


RUNNING_OUTPUT = """\
System Name: Hybrid-CL, System-Id: 9755c68c-85ac-4f9f-a004-354d11a0c4c2
System Type: CL-86, Family: NCR
System status: running
Version: DNOS [26.3.0] build [11_priv], Copyright 2026 DRIVENETS LTD.
Escalation-stop-failovers
  Max-failover(remaining): 2(2)
  Failover-period(remaining): 30min(0 days, 0:30:00)
Recovery-mode: supported
BGP NSR: ready
"""

# Live capture (trimmed) minutes after the node restart, before full NCF
# bring-up: qualified running status.
DEGRADED_OUTPUT = """\
System Name: Hybrid-CL, System-Id: 9755c68c-85ac-4f9f-a004-354d11a0c4c2
System Type: CL-86, Family: NCR
System status: running (insufficient-ncfs)
System Uptime: 0 days, 0:01:03
Version: DNOS [26.3.0] build [11_priv], Copyright 2026 DRIVENETS LTD.
Recovery-mode: supported
"""

# Active recovery, operational-style schema with an explicit status value.
RECOVERY_STATUS_OUTPUT = """\
System Name: Hybrid-CL, System-Id: 9755c68c-85ac-4f9f-a004-354d11a0c4c2
System Type: CL-86, Family: NCR
System status: recovery
Version: DNOS [26.3.0] build [11_priv], Copyright 2026 DRIVENETS LTD.
"""

# Active recovery, minimal-environment schema: no status line at all, just
# a banner-style statement.
RECOVERY_BANNER_OUTPUT = """\
System is in recovery mode.
Only 'request system restart' and 'show system' are available.
"""

GI_OUTPUT = """\
System status: running
Active NCC: CZ22500CW4

| Type | Id | Status | Serial Number | GI version |
|------+----+--------+---------------+------------|
| NCC  | 0  | stable | CZ22500CW4    | 26.3.0.50  |
"""


# --------------------------------------------------------------------------
# parse_system_status — the raw line
# --------------------------------------------------------------------------

def test_parse_system_status_running():
    assert parse_system_status(RUNNING_OUTPUT) == "running"


def test_parse_system_status_keeps_qualifier():
    assert parse_system_status(DEGRADED_OUTPUT) == "running (insufficient-ncfs)"


def test_parse_system_status_recovery():
    assert parse_system_status(RECOVERY_STATUS_OUTPUT) == "recovery"


def test_parse_system_status_absent():
    assert parse_system_status(RECOVERY_BANNER_OUTPUT) is None
    assert parse_system_status("") is None


# --------------------------------------------------------------------------
# classify_system_state — the stable enum
# --------------------------------------------------------------------------

def test_state_running():
    assert classify_system_state(RUNNING_OUTPUT) == "running"


def test_state_running_degraded():
    assert classify_system_state(DEGRADED_OUTPUT) == "running-degraded"


def test_state_recovery_from_status_line():
    assert classify_system_state(RECOVERY_STATUS_OUTPUT) == "recovery"


def test_state_recovery_from_banner_text():
    assert classify_system_state(RECOVERY_BANNER_OUTPUT) == "recovery"


def test_capability_line_is_not_recovery():
    # Every healthy box prints ``Recovery-mode: supported`` — the capability
    # flag must never read as the active state.
    assert classify_system_state(RUNNING_OUTPUT) == "running"
    assert classify_system_state(DEGRADED_OUTPUT) == "running-degraded"


def test_state_gi():
    assert classify_system_state(GI_OUTPUT) == "gi"


def test_state_unknown_on_garbage_or_bare_status_line():
    assert classify_system_state("") == "unknown"
    assert classify_system_state("% some DNOS error\n") == "unknown"
    # A bare running line with no recognisable schema proves nothing.
    assert classify_system_state("System status: running\n") == "unknown"


# --------------------------------------------------------------------------
# _annotate_system_mode — envelope surface
# --------------------------------------------------------------------------

def _envelope(stdout):
    return {"status": "ok", "stdout": stdout, "warnings": [], "next_actions": []}


def test_annotate_running_envelope():
    response = _envelope(RUNNING_OUTPUT)
    _annotate_system_mode(response)
    assert response["state"] == "running"
    assert response["system_status"] == "running"
    assert response["warnings"] == []


def test_annotate_degraded_envelope():
    response = _envelope(DEGRADED_OUTPUT)
    _annotate_system_mode(response)
    assert response["state"] == "running-degraded"
    assert response["system_status"] == "running (insufficient-ncfs)"


def test_annotate_recovery_envelope_warns_and_hints():
    response = _envelope(RECOVERY_STATUS_OUTPUT)
    _annotate_system_mode(response)
    assert response["state"] == "recovery"
    assert response["status"] == "ok"  # the command itself succeeded
    assert any("RECOVERY MODE" in w for w in response["warnings"])
    assert any("restart" in a for a in response["next_actions"])


def test_annotate_recovery_banner_envelope():
    response = _envelope(RECOVERY_BANNER_OUTPUT)
    _annotate_system_mode(response)
    assert response["state"] == "recovery"
    assert response["system_status"] is None


def test_annotate_gi_envelope_state():
    response = _envelope(GI_OUTPUT)
    _annotate_system_mode(response)
    assert response["state"] == "gi"
    assert response["mode"] == "gi"


# --------------------------------------------------------------------------
# Connect path — transient classification + unreachable state
# --------------------------------------------------------------------------

def test_transient_connect_error_hints_recovery():
    exc = ConnectError(
        "Could not connect to Hybrid-CL: Authentication timeout.",
        transient=True,
    )
    actions = connect_error_next_actions(exc)
    assert any("recovery" in a for a in actions)


def test_deterministic_connect_error_stays_generic():
    exc = ConnectError(
        "Could not connect to Hybrid-CL: Authentication failed.",
        transient=False,
    )
    actions = connect_error_next_actions(exc)
    assert not any("recovery" in a for a in actions)


def test_registry_miss_keeps_registry_hint():
    actions = connect_error_next_actions(UnknownDeviceError("'x' is not in the device registry."))
    assert len(actions) == 1
    assert "registry" in actions[0]


@pytest.mark.parametrize(
    "exc",
    [
        # The three shapes observed live during the 2026-07-02 episode.
        paramiko.SSHException(
            "Error reading SSH protocol banner[Errno 104] Connection reset by peer"
        ),
        paramiko.AuthenticationException("Authentication timeout."),
        ConnectionResetError(104, "Connection reset by peer"),
    ],
)
def test_observed_recovery_episode_errors_are_transient(exc):
    assert _is_transient_connect_error(exc)


def test_run_on_device_connect_error_sets_unreachable(monkeypatch):
    from qactl.dnctl.cli.core import runner

    def boom(*args, **kwargs):
        raise ConnectError(
            "Could not connect to Hybrid-CL: Authentication timeout.",
            transient=True,
        )

    monkeypatch.setattr(runner, "run_once", boom)
    monkeypatch.setattr(runner, "log_request", lambda *a, **k: None)
    response = runner._run_on_device(
        "show_system", "Hybrid-CL", None, "u", "p", "show system", 5, "retry",
    )
    assert response["status"] == "connect_error"
    assert response["state"] == "unreachable"
    assert any("recovery" in a for a in response["next_actions"])
