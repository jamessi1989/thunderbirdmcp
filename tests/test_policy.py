"""Capability checks use synthetic callbacks, never a real profile or keyring."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from thunderbird_mcp import policy


class PolicyTests(unittest.TestCase):
    def setUp(self):
        environment = {name: value for name, value in os.environ.items() if not name.startswith("TB_MCP_")}
        self.env = patch.dict(os.environ, environment, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_profiles_and_empty_allowlist(self):
        with patch.dict(os.environ, TB_MCP_PROFILE="read-only"):
            policy.require("read", tool="get_email")
            for capability in policy.CAPABILITIES - {"read"}:
                with self.subTest(capability=capability), self.assertRaises(policy.PolicyError):
                    policy.require(capability, tool="send_draft")
        with patch.dict(os.environ, TB_MCP_PROFILE="draft-only"):
            policy.require("draft", tool="save_draft_to_thunderbird")
            with self.assertRaises(policy.PolicyError):
                policy.require("send", tool="send_draft")
        with patch.dict(os.environ, TB_MCP_TOOLS=""):
            self.assertFalse(policy.allowed("get_email"))

    def test_invalid_profile_fails_closed(self):
        with patch.dict(os.environ, TB_MCP_PROFILE="readonly-ish"), self.assertRaises(ValueError):
            policy.require("send")

    def test_allowlisted_composite_may_use_helper_but_not_escalate(self):
        def compose():
            policy.require("draft", tool="draft_email")
            return "prepared"
        wrapped = policy.guard(compose, "draft_reply", "draft")
        with patch.dict(os.environ, TB_MCP_PROFILE="draft-only", TB_MCP_TOOLS="draft_reply"):
            self.assertEqual(wrapped(), "prepared")
            with self.assertRaises(policy.PolicyError):
                policy.require("draft", tool="draft_email")
            def hidden_send():
                policy.require("send", tool="send_draft")
            with self.assertRaises(policy.PolicyError):
                policy.guard(hidden_send, "draft_reply", "draft")()

    def test_contexts_only_narrow_and_restore(self):
        with policy.context(policy.Policy(profile="draft-only", tools={"draft_reply", "get_email"}, accounts={"a", "b"})):
            with policy.context(policy.Policy(profile="full", tools={"get_email"}, accounts={"a"})):
                self.assertEqual(policy.current().profile, "draft-only")
                policy.require("read", tool="get_email", account="a")
                for account in (None, "b"):
                    with self.assertRaises(policy.PolicyError):
                        policy.require("read", tool="get_email", account=account)
                self.assertFalse(policy.allowed("draft_reply"))
            self.assertTrue(policy.allowed("draft_reply"))
        self.assertIsNone(policy.current().accounts)
        with policy.context(policy.Policy(principal="alice")):
            with self.assertRaises(policy.PolicyError):
                with policy.context(policy.Policy(principal="bob")):
                    self.fail("Principal escalation must fail before entry")

    def test_file_roots_reject_sibling_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed = root / "allowed"
            allowed.mkdir()
            outside = root / "outside"
            outside.mkdir()
            with policy.context(policy.Policy(file_roots=(allowed,))):
                policy.require("file-write", tool="export_email", paths=[allowed / "new.eml"])
                with self.assertRaises(policy.PolicyError):
                    policy.require("file-write", tool="export_email", paths=[outside / "new.eml"])
                if hasattr(os, "symlink"):
                    try:
                        (allowed / "escape").symlink_to(outside, target_is_directory=True)
                    except OSError:
                        return
                    with self.assertRaises(policy.PolicyError):
                        policy.require("file-write", tool="export_email", paths=[allowed / "escape" / "new.eml"])

    def test_remote_reference_supplies_account_and_disagreement_fails(self):
        callback = Mock(return_value={"ok": True})
        def get(reference, account=None):
            return callback()
        wrapped = policy.guard(get, "get_email", "read")
        with policy.context(policy.Policy(accounts={"a"})):
            self.assertTrue(wrapped("thunderbird://imap/a/fp/INBOX/1/2")["ok"])
            with self.assertRaises(policy.PolicyError):
                wrapped("thunderbird://imap/b/fp/INBOX/1/2")
            with self.assertRaises(policy.PolicyError):
                wrapped("thunderbird://imap/a/fp/INBOX/1/2", account="b")
            with self.assertRaises(policy.PolicyError):
                wrapped("thunderbird://message/local")
        callback.assert_called_once()

    def test_server_direct_and_resource_calls_cannot_bypass_policy(self):
        from thunderbird_mcp import server
        with patch.dict(os.environ, TB_MCP_PROFILE="read-only"), patch.object(server._outgoing, "send_draft") as send:
            with self.assertRaises(policy.PolicyError):
                server.send_draft("synthetic")
            send.assert_not_called()
        with patch.dict(os.environ, TB_MCP_TOOLS="list_accounts"), patch.object(server._messages, "get_email") as read:
            with self.assertRaises(policy.PolicyError):
                server.message_resource("synthetic")
            with self.assertRaises(policy.PolicyError):
                server.live_message_resource("account1", "fp", "INBOX", "1", "2")
            read.assert_not_called()

    def test_cli_send_cannot_bypass_read_only(self):
        from thunderbird_mcp import cli, server
        with patch.dict(os.environ, TB_MCP_PROFILE="read-only"), patch.object(server._outgoing, "send_draft") as send:
            with self.assertRaises(policy.PolicyError):
                cli.dispatch(cli.parser().parse_args(["send", "synthetic", "--confirm"]))
            send.assert_not_called()

    def test_tool_listing_and_hidden_dispatch_are_consistent(self):
        service = policy.PolicyFastMCP("synthetic")
        called = []
        @service.tool(name="get_email")
        def read() -> dict:
            return {"ok": True}
        @service.tool(name="send_draft")
        def send() -> dict:
            called.append(True)
            return {"sent": True}
        async def check():
            with patch.dict(os.environ, TB_MCP_PROFILE="read-only"):
                self.assertEqual([t.name for t in await service.list_tools()], ["get_email"])
                with self.assertRaises(policy.PolicyError):
                    await service.call_tool("send_draft", {})
                # The registered callback has its own guard, too.
                with self.assertRaises(Exception):
                    await service._tool_manager.call_tool("send_draft", {})
        asyncio.run(check())
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
