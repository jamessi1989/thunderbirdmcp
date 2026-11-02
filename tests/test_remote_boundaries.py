"""Independent HTTP boundary checks against real tool implementations.

All stores, configured accounts and credentials are synthetic and isolated.
"""

from __future__ import annotations

import hashlib
import json
import os
from unittest.mock import patch

from starlette.testclient import TestClient

from thunderbird_mcp import index, operations, outgoing, policy, remote, server
from test_accounts import account_prefs
from test_index import MailFixture, mail


class RemoteBoundaryTests(MailFixture):
    def setUp(self):
        super().setUp()
        self.allowed = self.root / "allowed"
        self.allowed.mkdir()
        environment = patch.dict(os.environ, {
            "TB_PROFILE": str(self.profile), "TB_MAIL_INDEX_DB": str(self.db),
            "TB_OUTBOX_DB": str(self.root / "outbox.sqlite"),
            "TB_OPERATIONS_DB": str(self.root / "operations.sqlite"),
            "TB_EXPORTS_DB": str(self.root / "exports.sqlite"),
            "TB_SEARCH_SNAPSHOTS_DB": str(self.root / "snapshots.sqlite"),
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        preferences = patch.object(outgoing, "_prefs", return_value=account_prefs())
        preferences.start()
        self.addCleanup(preferences.stop)
        health = patch.object(outgoing, "health_check", return_value={"ready": True})
        health.start()
        self.addCleanup(health.stop)
        self.account = self.profile / "ImapMail" / "imap.example.test-1"
        self.folder("INBOX", [mail("one", body="Account one visible body"),
                              mail("own-reply", body="Account one reply", **{"In-Reply-To": "<one@example.test>"})])
        self.account = self.profile / "ImapMail" / "imap.example.test-2"
        self.folder("PrivateFolderTwo", [mail("two", body="FORBIDDEN account two body"),
                                         mail("foreign-reply", body="FORBIDDEN linked body", **{"In-Reply-To": "<one@example.test>"})])
        self.refresh()
        self.token = "synthetic-boundary-token-" + "x" * 32
        self.other_token = "synthetic-other-token-" + "y" * 32
        auth = self.root / "auth.json"
        auth.write_text(json.dumps({"allowed_hosts": ["testserver"], "principals": [
            {"name": "one", "token_sha256": hashlib.sha256(self.token.encode()).hexdigest(),
             "accounts": ["account1"], "profile": "full", "file_roots": [str(self.allowed)]},
            {"name": "two", "token_sha256": hashlib.sha256(self.other_token.encode()).hexdigest(),
             "accounts": ["account2"], "profile": "full", "file_roots": []}]}))
        auth.chmod(0o600)
        self.mcp = policy.PolicyFastMCP("boundary-real-tools")
        names = ("get_email", "get_thread", "search_email", "search_page", "mail_status", "list_accounts", "export_email",
                 "draft_email", "edit_draft", "get_draft", "get_operation", "list_operations", "plan_export", "execute_export")
        for name in names:
            function = getattr(server, name)
            self.mcp.tool(name=name, capability=server.mcp._policy_capabilities[name])(function.__wrapped__)
        replace = patch.object(server, "mcp", self.mcp)
        replace.start()
        self.addCleanup(replace.stop)
        self.client = TestClient(remote.create_app(auth))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def call(self, name, arguments=None, *, other=False):
        headers = {"Authorization": "Bearer " + (self.other_token if other else self.token),
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
        response = self.client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
            "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}})
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]

    def value(self, result):
        self.assertFalse(result.get("isError"), result)
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def test_real_local_reads_threads_and_search_do_not_cross_accounts(self):
        visible = self.value(self.call("get_email", {"reference": "<one@example.test>"}))
        self.assertIn("Account one", visible["body"])
        denied = self.call("get_email", {"reference": "<two@example.test>"})
        self.assertTrue(denied["isError"])
        self.assertNotIn("FORBIDDEN", json.dumps(denied))
        thread = self.value(self.call("get_thread", {"reference": "<one@example.test>", "include_bodies": True}))
        self.assertEqual(thread["count"], 2)
        self.assertNotIn("FORBIDDEN", json.dumps(thread))
        search = self.value(self.call("search_email", {"query": "", "semantic": False, "deduplicate_threads": False}))
        self.assertEqual(search["count"], 2)
        self.assertTrue(self.call("search_email", {"account": "account2", "semantic": False})["isError"])

    def test_remote_status_is_account_scoped_and_hides_profile_paths(self):
        status = self.value(self.call("mail_status"))
        encoded = json.dumps(status)
        self.assertNotIn("PrivateFolderTwo", encoded)
        self.assertNotIn(str(self.root), encoded)
        self.assertEqual(status["index"]["messages"], 2)
        accounts = self.value(self.call("list_accounts"))
        self.assertEqual([account["account_id"] for account in accounts["accounts"]], ["account1"])

    def test_file_roots_apply_to_exports_and_nested_draft_edits(self):
        outside = self.root / "outside.txt"
        outside.write_text("FORBIDDEN local file")
        denied = self.call("export_email", {"reference": "<one@example.test>", "destination": str(self.root / "outside.eml")})
        self.assertTrue(denied["isError"])
        self.assertFalse((self.root / "outside.eml").exists())
        exported = self.value(self.call("export_email", {"reference": "<one@example.test>", "destination": str(self.allowed / "mail.eml")}))
        self.assertTrue((self.allowed / "mail.eml").is_file())
        self.assertGreater(exported["bytes"], 0)
        denied = self.call("draft_email", {"to": ["person@example.test"], "subject": "Synthetic", "body": "Body", "attachments": [str(outside)]})
        self.assertTrue(denied["isError"])
        draft = self.value(self.call("draft_email", {"to": ["person@example.test"], "subject": "Synthetic", "body": "Body"}))
        denied = self.call("edit_draft", {"draft_id": draft["draft_id"], "revision": draft["revision"],
                                           "changes": {"attachments": [str(outside)]}})
        self.assertTrue(denied["isError"])
        self.assertNotIn("FORBIDDEN", json.dumps(denied))
        self.assertTrue(self.call("get_draft", {"draft_id": draft["draft_id"]}, other=True)["isError"])

    def test_snapshots_and_operations_keep_principal_ownership(self):
        first = self.value(self.call("search_page", {"query": "", "limit": 1, "semantic": False, "deduplicate_threads": False}))
        cursor = first["next_cursor"]
        self.assertIsNotNone(cursor)
        self.assertTrue(self.call("search_page", {"cursor": cursor}, other=True)["isError"])
        with policy.context(policy.Policy(principal="one", accounts=frozenset({"account1"}))):
            operation = operations.create_operation("send_draft", account="account1", total=1, metadata={"synthetic": True})
        own = self.value(self.call("get_operation", {"operation_id": operation["operation_id"]}))
        self.assertEqual(own["operation_id"], operation["operation_id"])
        self.assertTrue(self.call("get_operation", {"operation_id": operation["operation_id"]}, other=True)["isError"])

    def test_export_plan_owner_is_checked_before_execution(self):
        plan = self.value(self.call("plan_export", {"items": [{"reference": "<one@example.test>"}], "directory": str(self.allowed)}))
        self.assertTrue(self.call("execute_export", {"plan_id": plan["plan_id"]}, other=True)["isError"])
        completed = self.value(self.call("execute_export", {"plan_id": plan["plan_id"]}))
        self.assertEqual(completed["files"][0]["status"], "completed")


if __name__ == "__main__":
    import unittest
    unittest.main()
