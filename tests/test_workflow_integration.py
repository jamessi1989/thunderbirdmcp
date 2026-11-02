"""Real CLI/MCP workflow regressions using isolated synthetic mail only."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from thunderbird_mcp import cli, exports, outgoing, policy, server
from test_accounts import account_prefs
from test_index import MailFixture, mail


class WorkflowIntegrationTests(MailFixture):
    def setUp(self):
        super().setUp()
        self.environment = {
            "TB_PROFILE": str(self.profile), "TB_MAIL_INDEX_DB": str(self.db),
            "TB_OUTBOX_DB": str(self.root / "outbox.sqlite"),
            "TB_OPERATIONS_DB": str(self.root / "operations.sqlite"),
            "TB_AUTOMATION_DB": str(self.root / "automation.sqlite"),
            "TB_EXPORTS_DB": str(self.root / "exports.sqlite"),
            "TB_SEARCH_SNAPSHOTS_DB": str(self.root / "snapshots.sqlite"),
        }
        environment = patch.dict(os.environ, self.environment, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.prefs = account_prefs()
        preferences = patch.object(outgoing, "_prefs", return_value=self.prefs)
        preferences.start()
        self.addCleanup(preferences.stop)
        self.account = self.profile / "ImapMail" / "imap.example.test-1"
        self.folder("INBOX", [mail("one", body="Visible message one"), mail("three", body="Visible message three")])
        self.account = self.profile / "ImapMail" / "imap.example.test-2"
        self.folder("Other", [mail("two", body="Private message in other account")])
        self.refresh()

    def call(self, tool, values):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("thunderbird_mcp.runtime.configure_session_environment"):
            code = cli.main(["call", tool, "--args", json.dumps(values), "--json"])
        return code, json.loads(output.getvalue())

    def test_generic_cli_tool_errors_are_structured_without_uncaught_toolerror(self):
        code, result = self.call("get_draft", {"draft_id": "invalid"})
        self.assertEqual(code, 1)
        self.assertIn("error", result)
        code, result = self.call("draft_email", {"subject": "Missing required recipients and body"})
        self.assertEqual(code, 1)
        self.assertIn("error", result)
        with patch.dict(os.environ, {"TB_MCP_PROFILE": "read-only"}):
            code, result = self.call("draft_email", {"to": ["recipient@example.test"], "subject": "Denied", "body": "Body"})
        self.assertEqual(code, 1)
        self.assertIn("error", result)

    def test_generic_cli_rich_draft_revision_and_native_schedule_workflow(self):
        image = self.root / "chart.png"
        image.write_bytes(b"synthetic inline image")
        code, draft = self.call("draft_email", {
            "to": ["recipient@example.test"], "subject": "Synthetic review", "body": "Plain alternative",
            "html_body": '<p>Chart <img src="cid:chart"></p>',
            "inline_attachments": [{"path": str(image), "cid": "chart"}], "account": "account1", "identity": "id1"})
        self.assertEqual(code, 0, draft)
        self.assertEqual(draft["revision"], 1)
        self.assertEqual(draft["attachments"][0]["cid"], "chart")
        image.unlink()
        code, edited = self.call("edit_draft", {"draft_id": draft["draft_id"], "revision": 1,
                                                 "changes": {"subject": "Reviewed updated subject"}})
        self.assertEqual(code, 0, edited)
        self.assertEqual(edited["revision"], 2)
        self.assertEqual(edited["attachments"], draft["attachments"])
        code, scheduled = self.call("schedule_draft", {"draft_id": draft["draft_id"], "revision": 2,
            "due_at": "2099-11-10T09:00:00+01:00", "approved": True, "transport": "graph"})
        self.assertEqual(code, 0, scheduled)
        self.assertEqual((scheduled["status"], scheduled["transport"]), ("queued", "graph"))
        code, cancelled = self.call("cancel_schedule", {"schedule_id": scheduled["schedule_id"]})
        self.assertEqual(code, 0, cancelled)
        self.assertEqual(cancelled["status"], "cancelled")

    def test_old_unscoped_search_snapshot_cannot_bypass_later_account_restriction(self):
        first = server.search_page(query="", limit=1, semantic=False, deduplicate_threads=False)
        self.assertIsNotNone(first["next_cursor"])
        with policy.context(policy.Policy(accounts=frozenset({"account1"}))):
            with self.assertRaises(PermissionError):
                server.search_page(cursor=first["next_cursor"], limit=100)

    def test_export_plan_cannot_mislabel_foreign_local_bytes_as_selected_account(self):
        with self.assertRaises((ValueError, PermissionError)):
            exports.plan_export([{"reference": "<two@example.test>"}], str(self.root / "export"), account="account1")

    def test_account_scoped_query_diagnostics_do_not_disclose_foreign_vocabulary(self):
        token = "foreignquasarprivatekeyword"
        before = server.search_email(query=token, semantic=False, account="account1")
        self.account = self.profile / "ImapMail" / "imap.example.test-2"
        self.folder("Other", [mail("two", body=token)])
        self.refresh()
        after = server.search_email(query=token, semantic=False, account="account1")
        self.assertEqual(before["count"], 0)
        self.assertEqual(after["count"], 0)
        self.assertEqual(before["diagnostics"], after["diagnostics"])

    def test_stdio_schema_and_revision_calls_work_from_another_directory(self):
        # The child process reads only this synthetic profile. Persist the
        # preferences because parent-process mocks do not cross stdio transport.
        (self.profile / "prefs.js").write_text("\n".join(
            f"user_pref({json.dumps(key)}, {json.dumps(value)});" for key, value in self.prefs.items()))
        repo = Path(__file__).resolve().parents[1]
        environment = {**self.environment, "PYTHONPATH": str(repo), "PYTHONIOENCODING": "utf-8",
                       "TB_MCP_PROFILE": "draft-only"}
        async def exercise():
            parameters = StdioServerParameters(command=sys.executable, args=["-m", "thunderbird_mcp.server"],
                                                env=environment, cwd=str(self.root))
            with tempfile.TemporaryFile(mode="w+") as errors:
                async with stdio_client(parameters, errlog=errors) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        tools = {tool.name: tool.inputSchema for tool in (await session.list_tools()).tools}
                        self.assertNotIn("send_draft", tools)
                        self.assertNotIn("schedule_draft", tools)
                        self.assertIn("html_body", tools["draft_email"]["properties"])
                        self.assertIn("identity", tools["draft_email"]["properties"])
                        self.assertIn("revision", tools["edit_draft"]["required"])
                        prepared = await session.call_tool("draft_email", {"to": ["recipient@example.test"],
                            "subject": "Protocol preview", "body": "Plain body", "html_body": "<p>Rich body</p>", "account": "account1"})
                        self.assertFalse(prepared.isError)
                        draft = prepared.structuredContent or json.loads(prepared.content[0].text)
                        edited = await session.call_tool("edit_draft", {"draft_id": draft["draft_id"], "revision": 1,
                            "changes": {"body": "Updated plain body"}})
                        self.assertFalse(edited.isError)
                        stale = await session.call_tool("edit_draft", {"draft_id": draft["draft_id"], "revision": 1,
                            "changes": {"body": "Stale edit"}})
                        self.assertTrue(stale.isError)
                        denied = await session.call_tool("send_draft", {"draft_id": draft["draft_id"], "revision": 2})
                        self.assertTrue(denied.isError)
        asyncio.run(exercise())
