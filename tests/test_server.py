"""Exercise public MCP schemas and workflows through a real stdio subprocess."""

from __future__ import annotations

import asyncio
import json
import mailbox
import os
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from thunderbird_mcp import index


class ReplyRecipientsTests(unittest.TestCase):
    def test_reply_refuses_incomplete_legacy_headers(self):
        from thunderbird_mcp import server
        with patch.object(server._messages, "get_email", return_value={"headers_complete": False}), \
                patch.object(server._outgoing, "prepare_draft") as prepare:
            with self.assertRaisesRegex(ValueError, "reply headers"):
                server.draft_reply("123", "Hello")
            prepare.assert_not_called()

    def test_reply_all_honors_reply_to_and_excludes_self_and_duplicates(self):
        from thunderbird_mcp.server import _reply_headers
        result = _reply_headers({
            "from": "Robot <robot@example.test>", "reply_to": "Team <team@example.test>",
            "to": "Me <me@example.test>, Person <person@example.test>",
            "cc": "TEAM@example.test, other@example.test, me@example.test",
            "subject": "The project", "message_id": "<child@example.test>",
            "references": "<root@example.test> <child@example.test>",
        }, {"from_email": "me@example.test"}, True)
        self.assertEqual(result["to"], ["team@example.test", "person@example.test"])
        self.assertEqual(result["cc"], ["other@example.test"])
        self.assertEqual(result["references"], ["<root@example.test>", "<child@example.test>"])
        self.assertEqual(result["in_reply_to"], "<child@example.test>")
        self.assertEqual(result["subject"], "Re: The project")

    def test_reply_to_sent_mail_uses_original_recipient(self):
        from thunderbird_mcp.server import _reply_headers
        result = _reply_headers({"from": "me@example.test", "to": "person@example.test",
                                 "subject": "Re: Follow up"}, {"from_email": "me@example.test"}, False)
        self.assertEqual(result["to"], ["person@example.test"])
        self.assertEqual(result["subject"], "Re: Follow up")

    def test_reply_uses_account_bound_to_live_reference(self):
        from thunderbird_mcp import server
        message = {"account_id": "account2", "from": "sender@example.test",
                   "subject": "An update", "message_id": "<update@example.test>"}
        with patch.object(server._messages, "get_email", return_value=message), \
             patch.object(server._outgoing, "account_config", return_value={"from_email": "me@example.test"}) as config, \
             patch.object(server._outgoing, "prepare_draft", return_value={"draft_id": "synthetic"}) as prepare:
            server.draft_reply("thunderbird://imap/fixture", "Reviewed text")
        config.assert_called_once_with("account2")
        self.assertEqual(prepare.call_args.kwargs["account"], "account2")

    def test_reply_does_not_guess_between_unknown_local_accounts(self):
        from thunderbird_mcp import server
        with patch.object(server._messages, "get_email", return_value={"folder": "Local Folders/Archive"}), \
             patch.object(server._outgoing, "infer_account", return_value=None), \
             patch.object(server._outgoing, "list_accounts", return_value={"accounts": [{"account_id": "account1"}, {"account_id": "account2"}]}), \
             patch.object(server._outgoing, "prepare_draft") as prepare:
            with self.assertRaisesRegex(ValueError, "Select an account explicitly"):
                server.draft_reply("42", "Reviewed text")
        prepare.assert_not_called()

    def test_live_resource_preserves_encoded_folder_segments(self):
        from thunderbird_mcp import server
        folder = "Archive%2F100%25%20complete"
        with patch.object(server._messages, "get_email", return_value={"body": "fixture"}) as read:
            json.loads(server.live_message_resource("account1", "fingerprint", folder, "11", "22"))
        read.assert_called_once_with(reference=f"thunderbird://imap/account1/fingerprint/{folder}/11/22")


class MCPProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_profiles_hide_and_reject_disallowed_protocol_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = {key: value for key, value in os.environ.items() if not key.startswith("TB_")}
            base.update(TB_PROFILE=str(root / "unused-profile"),
                        TB_MAIL_INDEX_DB=str(root / "unused-index.sqlite"),
                        TB_OUTBOX_DB=str(root / "unused-outbox.sqlite"))
            for profile, names in (("read-only", "get_email,send_draft"),
                                   ("draft-only", "draft_email,send_draft")):
                env = {**base, "TB_MCP_PROFILE": profile, "TB_MCP_TOOLS": names}
                parameters = StdioServerParameters(command=sys.executable,
                    args=["-m", "thunderbird_mcp.server"], env=env,
                    cwd=str(Path(__file__).resolve().parents[1]))
                async with stdio_client(parameters) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        listed = {tool.name for tool in (await session.list_tools()).tools}
                        expected = {"get_email"} if profile == "read-only" else {"draft_email"}
                        self.assertEqual(listed, expected)
                        response = await session.call_tool("send_draft", {"draft_id": "synthetic"})
                        self.assertTrue(response.isError)
                        # Resource reads must not bypass a get_email allowlist.
                        if profile == "draft-only":
                            with self.assertRaises(Exception):
                                await session.read_resource("thunderbird://message/synthetic")
            self.assertFalse((root / "unused-outbox.sqlite").exists())
            self.assertFalse((root / "unused-index.sqlite").exists())

    async def test_tools_resources_prompts_search_read_and_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "profile"
            account = profile / "ImapMail" / "mail.example.test"
            account.mkdir(parents=True)
            path = account / "INBOX"
            message = EmailMessage()
            message["From"] = "Sender <sender@example.test>"
            message["To"] = "Me <me@example.test>"
            message["Subject"] = "Synthetic mailbox integration"
            message["Message-ID"] = "<integration@example.test>"
            message["Date"] = "Mon, 02 Nov 2026 09:00:00 +0000"
            message.set_content("Fixture body for protocol verification.")
            message.add_attachment("The Zephyrlattice release is approved.\n", subtype="plain", filename="release.txt")
            box = mailbox.mbox(path)
            box.add(message)
            box.flush()
            box.close()
            path.with_suffix(".msf").touch()
            (profile / "prefs.js").write_text('\n'.join([
                'user_pref("mail.accountmanager.defaultaccount", "account1");',
                'user_pref("mail.account.account1.identities", "id1");',
                'user_pref("mail.account.account1.server", "server1");',
                'user_pref("mail.identity.id1.useremail", "me@example.test");',
                'user_pref("mail.identity.id1.fullName", "Test User");',
                'user_pref("mail.identity.id1.smtpServer", "smtp1");',
                'user_pref("mail.smtpserver.smtp1.hostname", "smtp.example.test");',
                'user_pref("mail.smtpserver.smtp1.port", 465);',
                'user_pref("mail.smtpserver.smtp1.try_ssl", 3);',
                'user_pref("mail.smtpserver.smtp1.username", "test");',
                'user_pref("mail.server.server1.hostname", "imap.example.test");',
                'user_pref("mail.server.server1.userName", "test");',
                'user_pref("mail.server.server1.directory-rel", "[ProfD]ImapMail/mail.example.test");',
            ]))
            database = root / "mail.sqlite"
            index.reindex(db_path=database, profile=profile)
            env = {key: value for key, value in os.environ.items() if not key.startswith("TB_")}
            env.update(TB_PROFILE=str(profile), TB_MAIL_INDEX_DB=str(database),
                       TB_OUTBOX_DB=str(root / "outbox.sqlite"),
                       TB_SMTP_FROM="me@example.test", TB_FROM="me@example.test",
                       TB_IMAP_HOST="imap.example.test", TB_IMAP_USER="test")
            parameters = StdioServerParameters(command=sys.executable,
                args=["-m", "thunderbird_mcp.server"], env=env,
                cwd=str(Path(__file__).resolve().parents[1]))
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialization = await session.initialize()
                    self.assertEqual(initialization.serverInfo.name, "thunderbird")
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    self.assertTrue({"search_email", "draft_reply", "send_draft", "get_email_reference",
                                     "get_thread", "mail_status", "export_attachment", "search_live_email",
                                     "list_accounts", "get_attachment_text", "save_draft_to_thunderbird"} <= set(tools))
                    self.assertTrue(tools["search_email"].annotations.readOnlyHint)
                    self.assertFalse(tools["send_draft"].annotations.readOnlyHint)
                    self.assertFalse(tools["save_draft_to_thunderbird"].annotations.readOnlyHint)
                    self.assertTrue(tools["save_draft_to_thunderbird"].annotations.openWorldHint)
                    self.assertTrue(tools["search_live_email"].annotations.readOnlyHint)
                    self.assertTrue(tools["search_live_email"].annotations.openWorldHint)
                    self.assertIn("account", tools["draft_email"].inputSchema["properties"])
                    self.assertIn("has_attachment", tools["search_email"].inputSchema["properties"])
                    self.assertIn("find_mail", [p.name for p in (await session.list_prompts()).prompts])
                    templates = await session.list_resource_templates()
                    self.assertIn("thunderbird://message/{key}",
                                  [t.uriTemplate for t in templates.resourceTemplates])

                    async def call(name, arguments):
                        response = await session.call_tool(name, arguments)
                        self.assertFalse(response.isError, response.content)
                        return response.structuredContent or json.loads(response.content[0].text)

                    result = await call("search_email", {"query": "integration", "semantic": False})
                    self.assertEqual(result["count"], 1)
                    reference = result["results"][0]["reference"]
                    result = await call("get_email", {"reference": reference})
                    self.assertIn("Fixture body", result["body"])
                    resource = await session.read_resource(reference)
                    resource_message = json.loads(resource.contents[0].text)
                    self.assertEqual(resource_message["message_id"], "<integration@example.test>")
                    citation = await call("get_email_reference", {"reference": reference})
                    self.assertEqual(citation["reference"], reference)
                    accounts = await call("list_accounts", {})
                    self.assertEqual(accounts["default_account"], "account1")
                    scoped = await call("search_email", {"query": "integration", "account": "account1", "semantic": False})
                    self.assertEqual(scoped["count"], 1)
                    folders = await call("list_folders", {"account": "account1"})
                    self.assertEqual(folders["total_messages"], 1)
                    attachment_match = await call("search_email", {"query": "has:attachment Zephyrlattice", "semantic": False})
                    self.assertEqual(attachment_match["results"][0]["reference"], reference)
                    self.assertEqual(attachment_match["results"][0]["match_source"], "attachment")
                    attachment = await call("get_attachment_text", {"reference": reference, "attachment_id": "1"})
                    self.assertIn("Zephyrlattice release", attachment["text"])
                    draft = await call("draft_reply", {"reference": reference, "body": "A synthetic reply."})
                    self.assertIn("draft_id", draft)
                    # No network send tool is called. Inspecting a persistent draft
                    # verifies the full process contract without mailing anyone.
                    saved = await call("get_draft", {"draft_id": draft["draft_id"]})
                    self.assertIn("sender@example.test", json.dumps(saved))
                    self.assertIn("<integration@example.test>", json.dumps(saved))
                    error = await session.call_tool("search_email", {"query": "before:nonsense"})
                    self.assertTrue(error.isError)


if __name__ == "__main__":
    unittest.main()
