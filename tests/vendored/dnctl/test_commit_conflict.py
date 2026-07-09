"""Commit-conflict (stale-candidate rebase) handling — no device.

When another session commits while our candidate is open, a live DNOS
``commit`` is interrupted by::

    Warning: User 'dnroot' committed at 03-Jul-2025 06:48:02 UTC, your
    configuration is out of sync.
    What would you like to do (commit, merge-only, abort) [abort]?

The apply path must answer that prompt (default ``abort``) so the SSH
channel never hangs, classify the outcome as ``commit_conflict``, and
tell the caller to re-run. These tests pin that behaviour end to end.
"""

from __future__ import annotations

import pytest

from qactl.dnctl.cli.core.commit_sequence import parse_commit_output
from qactl.dnctl.cli.core.errors import COMMIT_CONFLICT_NEXT_ACTION
from qactl.dnctl.cli.core.session import Invocation
from qactl.dnctl.cli.core.shell import (
    _COMMIT_CONFLICT_RE,
    send_command_with_commit_conflict,
)


_CONFLICT_OUTPUT = (
    "Warning: User 'dnroot' committed at 03-Jul-2025 06:48:02 UTC, your "
    "configuration is out of sync.\n"
    "What would you like to do (commit, merge-only, abort) [abort]? abort\n"
)


# --------------------------------------------------------------------------
# prompt matcher
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tail",
    [
        "What would you like to do (commit, merge-only, abort) [abort]?",
        "What would you like to do (commit, merge-only, abort) [abort]? ",
        "what would you like to do (commit, merge-only, abort)?",
        "What would you like to do ( commit , merge-only , abort ) [commit]?",
    ],
)
def test_conflict_prompt_matches(tail):
    assert _COMMIT_CONFLICT_RE.search(tail)


@pytest.mark.parametrize(
    "tail",
    [
        "HOST(cfg)#",
        "What would you like to do (commit, merge-only, abort) [abort]? abort",
        "Commit succeeded by dnroot at 21-Apr-2026 09:55:32 UTC",
        "some unrelated text",
    ],
)
def test_conflict_prompt_rejects(tail):
    assert not _COMMIT_CONFLICT_RE.search(tail)


# --------------------------------------------------------------------------
# parse_commit_output classification
# --------------------------------------------------------------------------

def test_parse_reports_commit_conflict():
    res = parse_commit_output(_CONFLICT_OUTPUT)
    assert res.status == "commit_conflict"
    assert res.user == "dnroot"
    assert res.timestamp == "03-Jul-2025 06:48:02 UTC"
    assert res.error_lines


def test_parse_success_wins_over_conflict_warning():
    # A future explicit 'merge' that succeeds carries both shapes; the
    # applied-commit verdict must win.
    out = (
        "Warning: User 'x' committed at 1-Jan-2026 00:00:00 UTC, your "
        "configuration is out of sync.\n"
        "Commit succeeded by dnroot at 1-Jan-2026 00:00:05 UTC\n"
    )
    assert parse_commit_output(out).status == "ok"


def test_parse_clean_commit_unaffected():
    out = "Commit succeeded by dnroot at 21-Apr-2026 09:55:32 UTC\n"
    assert parse_commit_output(out).status == "ok"


# --------------------------------------------------------------------------
# send_command_with_commit_conflict answers the prompt instead of hanging
# --------------------------------------------------------------------------

class FakeChannel:
    """Scripted paramiko-like channel.

    ``reactions`` maps a stripped sent line to the bytes emitted back.
    Unknown lines just re-emit the DNOS prompt.
    """

    def __init__(self, reactions, prompt_line="HOST(cfg)#"):
        self._reactions = reactions
        self._prompt_line = prompt_line
        self._buf = b""
        self.sent = []

    def send(self, data):
        self.sent.append(data)
        line = data.strip()
        if line in self._reactions:
            self._buf += self._reactions[line].encode()
        else:
            self._buf += self._prompt_line.encode()
        return len(data)

    def recv_ready(self):
        return bool(self._buf)

    def recv(self, n):
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk


def test_sender_answers_abort_and_returns():
    reactions = {
        "commit and-exit": (
            "commit and-exit\r\n"
            "Warning: User 'dnroot' committed at 03-Jul-2025 06:48:02 UTC, "
            "your configuration is out of sync.\r\n"
            "What would you like to do (commit, merge-only, abort) [abort]? "
        ),
        # the answer echoes and DNOS returns to the config prompt
        "abort": "abort\r\nHOST(cfg)#",
    }
    ch = FakeChannel(reactions)

    out, _head, _tail, hit = send_command_with_commit_conflict(
        ch, "commit and-exit", "HOST#", overall_timeout=5.0,
    )

    assert hit is True
    assert "abort\n" in ch.sent           # the prompt was answered
    assert "out of sync" in out           # warning preserved for parsing
    assert parse_commit_output(out).status == "commit_conflict"


def test_sender_plain_commit_no_prompt():
    reactions = {
        "commit and-exit": (
            "commit and-exit\r\n"
            "Commit succeeded by dnroot at 21-Apr-2026 09:55:32 UTC\r\n"
            "HOST(cfg)#"
        ),
    }
    ch = FakeChannel(reactions)
    out, _h, _t, hit = send_command_with_commit_conflict(
        ch, "commit and-exit", "HOST#", overall_timeout=5.0,
    )
    assert hit is True
    assert "abort\n" not in ch.sent       # nothing to answer
    assert parse_commit_output(out).status == "ok"


# --------------------------------------------------------------------------
# tool surface: edit_config / load_override / rollback report the conflict
# --------------------------------------------------------------------------

def _inv(output_str, *, hit_prompt=True):
    return Invocation(
        output=output_str, hit_prompt=hit_prompt,
        head_prompt_line="", tail_prompt="", host="h", device="cl",
        steps=[],
    )


def test_edit_config_conflict_is_error(monkeypatch):
    from qactl.dnctl.cli.tools import edit

    monkeypatch.setattr(
        edit, "drive_configure_commit",
        lambda *a, **k: _inv(_CONFLICT_OUTPUT),
    )
    # edit_config runs a cleanup channel on failure; stub it out.
    monkeypatch.setattr(edit, "abort_shared_candidate", lambda *a, **k: None)

    resp = edit.edit_config(["system name foo"], device="cl")
    assert resp["status"] == "error"
    assert resp["commit"]["status"] == "commit_conflict"
    assert resp["commit"]["user"] == "dnroot"
    assert any("another session committed" in e for e in resp["errors"])
    assert COMMIT_CONFLICT_NEXT_ACTION in resp["next_actions"]


def test_rollback_conflict_is_error(monkeypatch):
    from qactl.dnctl.cli.tools import edit

    monkeypatch.setattr(
        edit, "drive_configure_commit",
        lambda *a, **k: _inv(_CONFLICT_OUTPUT),
    )
    resp = edit.rollback_config(rollback_id=1, device="cl")
    assert resp["status"] == "error"
    assert resp["commit"]["status"] == "commit_conflict"
    assert COMMIT_CONFLICT_NEXT_ACTION in resp["next_actions"]


def test_load_override_conflict_is_error(monkeypatch):
    from qactl.dnctl.cli.tools import edit

    monkeypatch.setattr(
        edit, "drive_configure_commit",
        lambda *a, **k: _inv(_CONFLICT_OUTPUT),
    )
    resp = edit.load_override_factory_default(device="cl")
    assert resp["status"] == "error"
    assert resp["commit"]["status"] == "commit_conflict"
    assert COMMIT_CONFLICT_NEXT_ACTION in resp["next_actions"]
