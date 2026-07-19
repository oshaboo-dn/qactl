"""Bare routing-daemon names resolve to DNOS namespaced process tokens.

``request system process restart`` only accepts the namespaced form
(``routing:bgpd``); a bare ``bgpd`` is rejected on-box. ``_resolve_process_name``
reads the ``| Process Name |`` column of ``show system`` and rewrites a bare
name to its unique namespaced match. No real device — ``run_sequence`` faked.
"""

import pytest

from qactl.dnos.cli.core.session import Invocation
from qactl.dnos.cli.tools import restart


# Trimmed `show system ncc 0 container routing-engine` output.
_SHOW_SYSTEM = (
    "\tState: running\n"
    "| Process Name                 | State   | PID   | Uptime  |\n"
    "| bgpd_authentication_logger   | running | 1316  | 0:54:05 |\n"
    "| routing:bgpd                 | running | 58264 | 0:19:59 |\n"
    "| routing:fibmgrd              | running | 58717 | 0:19:57 |\n"
    "| standby_routing:bgpd_standby | down    |       | 0:00:00 |\n"
)


def _fake_show(monkeypatch, output=_SHOW_SYSTEM, hit_prompt=True):
    calls = []

    def _fake_run_sequence(registry, **kwargs):
        calls.append(kwargs)
        return Invocation(
            output=output, hit_prompt=hit_prompt, head_prompt_line="",
            tail_prompt="", host="10.0.0.1", device=kwargs.get("device"),
            steps=[],
        )

    monkeypatch.setattr(restart, "run_sequence", _fake_run_sequence)
    return calls


def test_bare_bgpd_resolves_to_namespaced(monkeypatch):
    calls = _fake_show(monkeypatch)
    name, note = restart._resolve_process_name(
        "bgpd", "ncc", "0", "routing-engine",
        device="cl", host=None, user="u", password="p", timeout=30,
    )
    assert name == "routing:bgpd"
    assert note and "routing:bgpd" in note
    # Looked up via a read-only show system on the right container.
    assert calls and calls[0]["commands"] == [
        "show system ncc 0 container routing-engine"
    ]


def test_bare_fibmgrd_resolves(monkeypatch):
    _fake_show(monkeypatch)
    name, note = restart._resolve_process_name(
        "fibmgrd", "ncc", "0", "routing-engine",
        device="cl", host=None, user="u", password="p", timeout=30,
    )
    assert name == "routing:fibmgrd"
    assert note


def test_already_namespaced_is_passthrough_no_ssh(monkeypatch):
    calls = _fake_show(monkeypatch)
    name, note = restart._resolve_process_name(
        "routing:bgpd", "ncc", "0", "routing-engine",
        device="cl", host=None, user="u", password="p", timeout=30,
    )
    assert name == "routing:bgpd"
    assert note is None
    assert calls == []  # colon present → no lookup


def test_unknown_name_passthrough(monkeypatch):
    _fake_show(monkeypatch)
    name, note = restart._resolve_process_name(
        "nosuchd", "ncc", "0", "routing-engine",
        device="cl", host=None, user="u", password="p", timeout=30,
    )
    assert name == "nosuchd"  # no match → let DNOS reject
    assert note is None


def test_lookup_failure_passthrough(monkeypatch):
    # show system never reached the prompt → don't guess, pass through.
    _fake_show(monkeypatch, hit_prompt=False)
    name, note = restart._resolve_process_name(
        "bgpd", "ncc", "0", "routing-engine",
        device="cl", host=None, user="u", password="p", timeout=30,
    )
    assert name == "bgpd"
    assert note is None
