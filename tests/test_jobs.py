"""Tests for the cross-family ``qactl jobs`` group (parser + list + show)."""

from __future__ import annotations

import unittest

import pytest

from qactl.__main__ import build_native_parser
from qactl.dnos.cli.core import job_store
from qactl.jobs import tools


# --- parser ---------------------------------------------------------------


class JobsParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_native_parser()

    def test_list_defaults(self):
        a = self.parser.parse_args(["jobs", "list"])
        self.assertEqual(a.group, "jobs")
        self.assertIsNone(a.kind)
        self.assertEqual(a.limit, 50)

    def test_list_filters(self):
        a = self.parser.parse_args(
            ["jobs", "list", "--kind", "orc", "--status", "running", "-d", "sa", "--limit", "5"])
        self.assertEqual((a.kind, a.status, a.device, a.limit), ("orc", "running", "sa", 5))

    def test_show_by_id_or_device(self):
        a = self.parser.parse_args(["jobs", "show", "sa-orc-load-abc123"])
        self.assertEqual(a.job_id, "sa-orc-load-abc123")
        b = self.parser.parse_args(["jobs", "show", "-d", "cl", "--kind", "tarload"])
        self.assertIsNone(b.job_id)
        self.assertEqual((b.device, b.kind), ("cl", "tarload"))


# --- fixtures -------------------------------------------------------------


@pytest.fixture
def _seeded(monkeypatch, tmp_path):
    """Isolated state dir with one job seeded per family."""
    monkeypatch.setenv("QACTL_STATE_DIR", str(tmp_path))
    job_store.save({"job_id": "sa-orc-build-a1", "device": "sa", "status": "ok",
                    "state": "ok", "kind": "orc_build", "phase": "done",
                    "started_utc": "2026-07-14T10:00:00+00:00"}, subdir="orc-jobs")
    job_store.save({"job_id": "cl-ts-c3", "device": "cl", "status": "ok",
                    "state": "done", "started_utc": "2026-07-14T08:00:00+00:00"},
                   subdir="techsupport-jobs")
    job_store.save({"job_id": "cl-tar-dead", "device": "cl", "status": "running",
                    "state": "loading", "worker_pid": 999999,
                    "started_utc": "2026-07-14T09:00:00+00:00"}, subdir="tarload-jobs")
    return tmp_path


# --- jobs_list ------------------------------------------------------------


def test_list_all_families(_seeded):
    env = tools.jobs_list()
    assert env["status"] == "ok"
    ids = {j["job_id"] for j in env["result"]["jobs"]}
    assert ids == {"sa-orc-build-a1", "cl-ts-c3", "cl-tar-dead"}
    fams = {j["job_id"]: j["family"] for j in env["result"]["jobs"]}
    assert fams["sa-orc-build-a1"] == "orc"
    assert fams["cl-ts-c3"] == "techsupport"
    assert fams["cl-tar-dead"] == "tarload"


def test_list_kind_filter(_seeded):
    env = tools.jobs_list(kind="orc")
    assert [j["job_id"] for j in env["result"]["jobs"]] == ["sa-orc-build-a1"]


def test_list_device_filter(_seeded):
    env = tools.jobs_list(device="cl")
    assert {j["job_id"] for j in env["result"]["jobs"]} == {"cl-ts-c3", "cl-tar-dead"}


def test_list_orphan_downgrade_and_status_filter(_seeded):
    # The running tarload job's worker (pid 999999) is dead -> reported error.
    env = tools.jobs_list(status="error")
    assert [j["job_id"] for j in env["result"]["jobs"]] == ["cl-tar-dead"]


def test_list_bad_kind(_seeded):
    env = tools.jobs_list(kind="nope")
    assert env["status"] == "bad_argument"


def test_list_limit_reports_total(_seeded):
    env = tools.jobs_list(limit=1)
    assert env["result"]["count"] == 1
    assert env["result"]["total"] == 3


# --- jobs_show ------------------------------------------------------------


def test_show_by_id_across_families(_seeded):
    env = tools.jobs_show(job_id="cl-ts-c3")
    assert env["job_id"] == "cl-ts-c3"
    assert env["status"] == "ok"


def test_show_by_id_orphan_downgrade(_seeded):
    env = tools.jobs_show(job_id="cl-tar-dead")
    assert env["status"] == "error"
    assert any("died mid-flight" in e for e in env["errors"])


def test_show_by_device_picks_newest(_seeded):
    # cl has both cl-tar-dead (09:00, saved last) and cl-ts-c3 — newest save wins.
    env = tools.jobs_show(device="cl")
    assert env["job_id"] == "cl-tar-dead"


def test_show_missing_id(_seeded):
    env = tools.jobs_show(job_id="does-not-exist")
    assert env["status"] == "error"


def test_show_needs_selector(_seeded):
    env = tools.jobs_show()
    assert env["status"] == "bad_argument"
