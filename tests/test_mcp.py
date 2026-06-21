"""Tests for the qactl MCP front — registration, surface map, confirm gate.

No live services: these cover the group -> tool surface map (including the
CLI-only denylist), the destructive-op ``confirm`` gate on the native tool
layer, the ``qactl mcp`` dispatcher arg handling, and that a real FastMCP
stdio server can actually be built (schemas introspect cleanly through the
request-log wrapper).

Run with:  python -m pytest -q
"""

from __future__ import annotations

import asyncio
import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr

from qactl.mcp import registry
from qactl.mcp.registry import ALL_GROUPS, CLI_ONLY, list_group_tools, register_group
from qactl.mcp import server


class SurfaceMapTests(unittest.TestCase):
    def test_every_group_exposes_tools(self):
        for g in ALL_GROUPS:
            self.assertGreater(len(list_group_tools(g)), 0, g)

    def test_native_tool_names(self):
        self.assertIn("jira_whoami", list_group_tools("jira"))
        self.assertIn("jira_delete_comment", list_group_tools("jira"))
        self.assertIn("confluence_comment", list_group_tools("confluence"))
        self.assertIn("jenkins_trigger", list_group_tools("jenkins"))

    def test_cli_only_tools_are_not_on_mcp(self):
        cli_tools = list_group_tools("cli")
        for name in CLI_ONLY["cli"]:
            self.assertNotIn(name, cli_tools, name)
        # but the operational ones still are
        self.assertIn("show", cli_tools)
        self.assertIn("edit_config", cli_tools)

    def test_cli_techsupport_and_polls_are_on_mcp(self):
        # Tech-support is fire-and-forget (lands on remote dnftp), and the
        # read-only / job-poll tools are cheap + bounded — all MCP-shaped.
        cli_tools = list_group_tools("cli")
        for name in (
            "create_techsupport", "get_techsupport_job",
            "list_backups", "read_backup",
            "request_system_pre_check", "get_tar_load_job",
        ):
            self.assertIn(name, cli_tools, name)

    def test_cli_backup_and_restore_on_mcp(self):
        # Backups are non-destructive; restore is gated behind confirm=true
        # (confirm=false is a dry-run) — both are MCP-shaped.
        cli_tools = list_group_tools("cli")
        self.assertIn("backup_device", cli_tools)
        self.assertIn("restore_device", cli_tools)

    def test_cli_only_keeps_ungated_destructive_tools(self):
        # The long, destructive, not-yet-confirm-gated ops stay CLI-only.
        cli_tools = list_group_tools("cli")
        for name in ("request_system_tar_load", "scale_deploy"):
            self.assertNotIn(name, cli_tools, name)

    def test_nc_all_backup_tools_on_mcp(self):
        # Nothing in the nc group is CLI-only any more: list/read are pure
        # SFTP reads, backup is non-destructive, restore is confirm-gated.
        self.assertNotIn("nc", CLI_ONLY)
        nc_tools = list_group_tools("nc")
        for name in (
            "netconf_backup", "netconf_restore",
            "netconf_list_backups", "netconf_read_backup",
            "netconf_get",
        ):
            self.assertIn(name, nc_tools, name)

    def test_unknown_group_raises(self):
        with self.assertRaises(ValueError):
            list_group_tools("nope")


class SelectorTests(unittest.TestCase):
    def test_selector_skips_and_wraps(self):
        recorded = []

        class FakeMCP:
            def tool(self, *a, **k):
                def deco(fn):
                    recorded.append(fn.__name__)
                    return fn
                return deco

        marks = []

        def wrap(fn):
            def inner(*a, **k):
                marks.append(fn.__name__)
                return fn(*a, **k)
            inner.__name__ = fn.__name__
            return inner

        sel = registry._Selector(FakeMCP(), skip={"skipme"}, wrap=wrap)

        @sel.tool()
        def keepme():
            return "k"

        @sel.tool()
        def skipme():
            return "s"

        self.assertEqual(sel.registered, ["keepme"])
        self.assertEqual(sel.skipped, ["skipme"])
        self.assertEqual(recorded, ["keepme"])  # only kept one reached the real mcp


class ConfirmGateTests(unittest.TestCase):
    def test_jira_destructive_requires_confirm(self):
        from qactl.jira import tools as jt
        self.assertEqual(
            jt.jira_remove_watcher("SW-1", "acc", confirm=False)["status"],
            "confirmation_required",
        )
        self.assertEqual(
            jt.jira_delete_comment("SW-1", "9", confirm=False)["status"],
            "confirmation_required",
        )
        self.assertEqual(
            jt.jira_transition_issue("SW-1", "11", confirm=False)["status"],
            "confirmation_required",
        )

    def test_confluence_delete_requires_confirm(self):
        from qactl.confluence import tools as ct
        self.assertEqual(ct.confluence_delete("123", confirm=False)["status"],
                         "confirmation_required")

    def test_jenkins_destructive_requires_confirm(self):
        from qactl.jenkins import tools as kt
        self.assertEqual(kt.jenkins_trigger("feature/x", confirm=False)["status"],
                         "confirmation_required")
        self.assertEqual(kt.jenkins_trigger_raw("a/b/c", confirm=False)["status"],
                         "confirmation_required")
        self.assertEqual(
            kt.jenkins_stop("b", build_number=1, confirm=False)["status"],
            "confirmation_required",
        )

    def test_jenkins_stop_needs_a_target(self):
        from qactl.jenkins import tools as kt
        self.assertEqual(kt.jenkins_stop(confirm=True)["status"], "bad_argument")


class DispatcherTests(unittest.TestCase):
    def test_resolve_all(self):
        self.assertEqual(server._resolve_groups(["all"]), list(ALL_GROUPS))

    def test_resolve_dedup(self):
        self.assertEqual(server._resolve_groups(["jira", "jira"]), ["jira"])

    def test_resolve_unknown(self):
        with self.assertRaises(ValueError):
            server._resolve_groups(["bogus"])

    def test_help_returns_zero(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = server.main(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("qactl mcp", out.getvalue())

    def test_list_emits_json(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = server.main(["--list", "jira"])
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertIn("jira_whoami", data["jira"])

    def test_unknown_group_returns_two(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = server.main(["bogus"])
        self.assertEqual(rc, 2)


class FastMCPBuildTests(unittest.TestCase):
    def test_build_native_server(self):
        srv = server.build_server(["jira", "confluence", "jenkins"], log=False)
        tools = asyncio.run(srv.list_tools())
        names = {t.name for t in tools}
        self.assertIn("jira_whoami", names)
        self.assertIn("jenkins_trigger", names)
        self.assertIn("confluence_comment", names)

    def test_build_with_request_log_wrap(self):
        # The request-log wrapper must not break FastMCP schema introspection.
        srv = server.build_server(["jira"], log=True)
        tools = asyncio.run(srv.list_tools())
        self.assertTrue(any(t.name == "jira_status" for t in tools))


if __name__ == "__main__":
    unittest.main()
