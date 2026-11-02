"""Delivery tests use synthetic mail and mocks; they never access a mail server."""

from __future__ import annotations

import json
import imaplib
import os
import smtplib
import ssl
import tempfile
import unittest
from contextlib import contextmanager
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import MagicMock, patch

from thunderbird_mcp import imap, outgoing


class OutgoingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        profile = self.root / "profile"
        profile.mkdir()
        self.env = patch.dict(os.environ, {
            "TB_PROFILE": str(profile), "TB_OUTBOX_DB": str(self.root / "outbox.sqlite"),
            "TB_OPERATIONS_DB": str(self.root / "operations.sqlite"),
            "TB_FROM": "Test User <me@example.test>", "TB_IMAP_HOST": "imap.example.test",
            "TB_IMAP_USER": "test", "TB_SMTP_HOST": "smtp.example.test", "TB_SMTP_USER": "test",
            "TB_IMAP_PASSWORD": "synthetic-imap-secret", "TB_SMTP_PASSWORD": "synthetic-smtp-secret",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def draft(self, **kwargs):
        return outgoing.prepare_draft(["person@example.test"], "Subject", "Synthetic body", **kwargs)

    @contextmanager
    def smtp(self, *, error=None, refused=None):
        connection = MagicMock()
        connection.sendmail.side_effect = error
        connection.sendmail.return_value = refused or {}
        @contextmanager
        def session(config):
            yield connection
        with patch.object(outgoing, "smtp_session", session), patch.object(outgoing, "_append_sent") as append:
            yield connection, append

    @contextmanager
    def draft_imap(self, *, folders=None):
        with patch.object(imap, "session") as session, patch.object(imap, "list_folders", return_value=folders or ["Drafts"]):
            connection = session.return_value.__enter__.return_value
            connection.append.return_value = ("OK", [b"APPEND completed"])
            yield connection

    def test_native_draft_preserves_content_and_blocks_stale_local_send(self):
        attachment = self.root / "document.txt"
        attachment.write_bytes(b"captured attachment")
        draft = self.draft(bcc=["private@example.test"], cc=["copy@example.test"],
                           attachments=[str(attachment)], in_reply_to="<parent@example.test>",
                           references=["<root@example.test>", "<parent@example.test>"])
        attachment.unlink()
        with self.draft_imap() as connection, patch.object(outgoing, "smtp_session") as smtp:
            result = outgoing.save_draft_to_thunderbird(draft["draft_id"])
            repeated = outgoing.save_draft_to_thunderbird(draft["draft_id"])
            send = outgoing.send_draft(draft["draft_id"], retry_failed=True)
        self.assertTrue(result["saved"])
        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], "saved_to_thunderbird")
        self.assertTrue(repeated["duplicate_prevented"])
        self.assertTrue(send["duplicate_prevented"])
        smtp.assert_not_called()
        connection.append.assert_called_once()
        args = connection.append.call_args.args
        self.assertEqual(args[:2], ('"Drafts"', r"(\Draft \Seen)"))
        message = BytesParser(policy=policy.default).parsebytes(args[3])
        self.assertEqual(message["Message-ID"], draft["message_id"])
        self.assertEqual(message["Bcc"], "private@example.test")
        self.assertEqual(message["Cc"], "copy@example.test")
        self.assertEqual(message["In-Reply-To"], "<parent@example.test>")
        self.assertEqual(message["References"], "<root@example.test> <parent@example.test>")
        self.assertEqual(message.get_body(preferencelist=("plain",)).get_content().strip(), "Synthetic body")
        self.assertEqual(next(message.iter_attachments()).get_payload(decode=True), b"captured attachment")
        self.assertIn("thunderbird_draft", outgoing.get_draft(draft["draft_id"]))

    def test_edit_keeps_frozen_attachments_and_requires_reviewed_revision(self):
        path = self.root / "frozen.txt"
        path.write_bytes(b"frozen attachment")
        draft = self.draft(attachments=[str(path)], bcc=["hidden@example.test"],
                           in_reply_to="<parent@example.test>")
        path.unlink()
        edited = outgoing.edit_draft(draft["draft_id"], draft["revision"], {"subject": "Updated", "body": "Reviewed"})
        self.assertEqual(edited["revision"], 2)
        self.assertEqual(edited["bcc"], ["hidden@example.test"])
        with self.assertRaisesRegex(ValueError, "revision"):
            outgoing.edit_draft(draft["draft_id"], 1, {"body": "Stale edit"})
        with self.smtp() as (smtp, _):
            for revision in (None, 1, True):
                with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, "revision"):
                    outgoing.send_draft(draft["draft_id"], revision=revision)
            smtp.sendmail.assert_not_called()
            result = outgoing.send_draft(draft["draft_id"], revision=edited["revision"])
        self.assertTrue(result["sent"])
        msg = BytesParser(policy=policy.default).parsebytes(smtp.sendmail.call_args.args[2])
        self.assertEqual(msg["Subject"], "Updated")
        self.assertEqual(msg["In-Reply-To"], "<parent@example.test>")
        self.assertEqual(next(msg.iter_attachments()).get_payload(decode=True), b"frozen attachment")
        self.assertNotIn("Bcc", msg)

    def test_discard_prevents_sending_and_does_not_delete_native_copy(self):
        draft = self.draft()
        discarded = outgoing.discard_draft(draft["draft_id"], 1)
        self.assertEqual(discarded["status"], "discarded")
        with self.smtp() as (smtp, _):
            self.assertTrue(outgoing.send_draft(draft["draft_id"])["duplicate_prevented"])
        smtp.sendmail.assert_not_called()
        native = self.draft()
        with self.draft_imap() as connection:
            outgoing.save_draft_to_thunderbird(native["draft_id"])
            result = outgoing.discard_draft(native["draft_id"], 1)
        self.assertTrue(result["delivery"]["server_draft_retained"])
        connection.uid.assert_not_called()

    def test_html_inline_content_and_identity_are_frozen(self):
        prefs = {"mail.accountmanager.defaultaccount": "account1",
                 "mail.account.account1.server": "server1", "mail.account.account1.identities": "id1,id2",
                 "mail.identity.id1.useremail": "me@example.test", "mail.identity.id2.useremail": "alias@example.test",
                 "mail.identity.id2.fullName": "Alias", "mail.identity.id2.smtpServer": "smtp2",
                 "mail.smtpserver.smtp2.hostname": "smtp.alias.example.test",
                 "mail.smtpserver.smtp2.username": "alias-login", "mail.smtpserver.smtp2.port": 587,
                 "mail.smtpserver.smtp2.try_ssl": 2}
        inline = self.root / "chart.png"
        inline.write_bytes(b"synthetic png bytes")
        with patch.object(outgoing, "_prefs", return_value=prefs):
            identities = outgoing.list_identities()
            self.assertEqual([item["identity_id"] for item in identities["identities"]], ["id1", "id2"])
            draft = self.draft(identity="id2", html_body='<p>Chart <img src="cid:chart"></p>',
                               inline_attachments=[{"path": str(inline), "cid": "chart"}])
            with outgoing._db() as conn:
                stored = conn.execute("SELECT config,message FROM drafts WHERE id=?", (draft["draft_id"],)).fetchone()
                config = json.loads(stored["config"])
            self.assertEqual(config["smtp_host"], "smtp.alias.example.test")
            self.assertEqual(config["smtp_user"], "alias-login")
            self.assertEqual(config["smtp_port"], 587)
            with self.assertRaises(ValueError):
                self.draft(identity="unconfigured@example.test")
        inline.unlink()
        with self.smtp() as (smtp, _):
            outgoing.send_draft(draft["draft_id"])
        msg = BytesParser(policy=policy.default).parsebytes(smtp.sendmail.call_args.args[2])
        self.assertEqual(str(msg["From"]), "Alias <alias@example.test>")
        self.assertEqual(smtp.sendmail.call_args.args[0], "alias@example.test")
        self.assertEqual(msg.get_body(preferencelist=("plain",)).get_content().strip(), "Synthetic body")
        self.assertIn('cid:chart', msg.get_body(preferencelist=("html",)).get_content())
        image = next(part for part in msg.walk() if part.get("Content-ID") == "<chart>")
        self.assertEqual(image.get_content_disposition(), "inline")
        self.assertEqual(image.get_payload(decode=True), b"synthetic png bytes")

    def test_inline_attachments_require_html_and_unique_cids(self):
        inline = self.root / "chart.png"
        inline.write_bytes(b"png")
        entry = {"path": str(inline), "cid": "chart"}
        with self.assertRaisesRegex(ValueError, "HTML"):
            self.draft(inline_attachments=[entry])
        with self.assertRaisesRegex(ValueError, "unique"):
            self.draft(html_body="<p>HTML</p>", inline_attachments=[entry, entry])
        with self.assertRaisesRegex(ValueError, "cid"):
            self.draft(html_body="<p>HTML</p>", inline_attachments=[{"path": str(inline), "cid": "bad\r\nheader"}])

    def test_forward_has_explicit_recipients_attachment_selection_and_no_reply_headers(self):
        from thunderbird_mcp import live
        msg = EmailMessage()
        msg["From"] = "sender@example.test"
        msg["To"] = "original@example.test"
        msg["Bcc"] = "never-forward@example.test"
        msg["Subject"] = "Original"
        msg["Message-ID"] = "<original@example.test>"
        msg["References"] = "<private-thread@example.test>"
        msg.set_content("Original body")
        msg.add_attachment(b"first", maintype="application", subtype="octet-stream", filename="one.dat")
        msg.add_attachment(b"second", maintype="application", subtype="octet-stream", filename="two.dat")
        locator = live.Locator("environment", live._fingerprint(outgoing.account_config()), "INBOX", 11, 5)
        with patch.object(live, "_original", return_value=(locator, msg.as_bytes())):
            draft = outgoing.draft_forward(locator.reference, ["new@example.test"], "Please review", attachment_ids=[2])
            with self.assertRaisesRegex(ValueError, "attachment ID"):
                outgoing.draft_forward(locator.reference, ["new@example.test"], attachment_ids=[3])
        self.assertEqual(draft["subject"], "Fwd: Original")
        self.assertEqual(draft["to"], ["new@example.test"])
        self.assertEqual(draft["bcc"], [])
        self.assertEqual(draft["references"], [])
        self.assertIsNone(draft["in_reply_to"])
        self.assertEqual([item["filename"] for item in draft["attachments"]], ["two.dat"])
        self.assertNotIn("never-forward", draft["body"])
        with self.smtp() as (smtp, _):
            outgoing.send_draft(draft["draft_id"])
        sent = BytesParser(policy=policy.default).parsebytes(smtp.sendmail.call_args.args[2])
        self.assertNotIn("References", sent)
        self.assertNotIn("In-Reply-To", sent)
        self.assertEqual(next(sent.iter_attachments()).get_payload(decode=True), b"second")

    def test_native_special_use_unicode_and_appenduid_are_preserved(self):
        from thunderbird_mcp import live
        draft = self.draft()
        folder = "Borradores/Équipe"
        with self.draft_imap(folders=[folder]) as connection:
            connection.list.return_value = ("OK", [f'(\\Drafts \\HasNoChildren) "/" "{imap.encode_folder(folder)}"'.encode()])
            connection.append.return_value = ("OK", [b"[APPENDUID 101 202] appended"])
            result = outgoing.save_draft_to_thunderbird(draft["draft_id"])
        self.assertEqual(result["folder"], folder)
        locator = live.parse_reference(result["reference"])
        self.assertEqual((locator.folder, locator.uidvalidity, locator.uid), (folder, 101, 202))
        self.assertEqual(outgoing.get_draft(draft["draft_id"])["server_draft"]["reference"], locator.reference)

    def test_reconcile_edit_and_native_replace_preserves_bcc_and_locks_send(self):
        draft = self.draft(bcc=["hidden@example.test"])
        with self.draft_imap() as connection:
            connection.append.return_value = ("OK", [b"[APPENDUID 8 21] appended"])
            saved = outgoing.save_draft_to_thunderbird(draft["draft_id"])
            native = connection.append.call_args.args[3]
        msg = BytesParser(policy=policy.SMTP).parsebytes(native)
        msg.replace_header("Subject", "Externally edited — café")
        msg["X-Identity-Key"] = "id-old"
        changed = msg.as_bytes()
        with self.draft_imap(), patch.object(outgoing, "_server_draft_bytes", return_value=changed):
            reconciled = outgoing.reconcile_draft(draft["draft_id"], saved["reference"], revision=1)
        self.assertEqual(reconciled["revision"], 2)
        self.assertEqual(reconciled["subject"], "Externally edited — café")
        edited = outgoing.edit_draft(draft["draft_id"], 2, {"body": "New local edit"})
        self.assertEqual(edited["status"], "edited_for_thunderbird")
        with self.smtp() as (smtp, _):
            self.assertTrue(outgoing.send_draft(draft["draft_id"], revision=3)["duplicate_prevented"])
        smtp.sendmail.assert_not_called()
        with self.draft_imap() as connection, patch.object(outgoing, "_server_draft_bytes", return_value=changed):
            connection.append.return_value = ("OK", [b"[APPENDUID 8 22] appended"])
            connection.select.return_value = ("OK", [b"2"])
            connection.response.return_value = ("UIDVALIDITY", [b"8"])
            connection.uid.return_value = ("OK", [None])
            result = outgoing.save_draft_to_thunderbird(draft["draft_id"], revision=3)
        self.assertTrue(result["saved"])
        self.assertTrue(result["previous_draft_retired"])
        connection.uid.assert_called_once_with("STORE", "21", "+FLAGS.SILENT", r"(\Deleted)")
        connection.expunge.assert_not_called()
        published = BytesParser(policy=policy.default).parsebytes(connection.append.call_args.args[3])
        self.assertEqual(published.get_all("Bcc"), ["hidden@example.test"])
        self.assertIsNone(published.get("X-Identity-Key"))
        self.assertEqual(published.get_body(preferencelist=("plain",)).get_content().strip(), "New local edit")

    def test_native_sync_conflict_and_cleanup_failure_never_duplicate_append(self):
        draft = self.draft()
        with self.draft_imap() as connection:
            connection.append.return_value = ("OK", [b"[APPENDUID 8 21] appended"])
            saved = outgoing.save_draft_to_thunderbird(draft["draft_id"])
            raw = connection.append.call_args.args[3]
        with self.draft_imap(), patch.object(outgoing, "_server_draft_bytes", return_value=raw):
            outgoing.reconcile_draft(draft["draft_id"], saved["reference"], 1)
        outgoing.edit_draft(draft["draft_id"], 2, {"body": "Changed"})
        with self.draft_imap() as connection, patch.object(outgoing, "_server_draft_bytes", return_value=raw + b"changed"):
            conflict = outgoing.save_draft_to_thunderbird(draft["draft_id"], revision=3)
        self.assertFalse(conflict["saved"])
        self.assertEqual(conflict["status"], "edited_for_thunderbird")
        connection.append.assert_not_called()
        with self.draft_imap() as connection, patch.object(outgoing, "_server_draft_bytes", side_effect=[raw, ValueError("old UID disappeared")]):
            result = outgoing.save_draft_to_thunderbird(draft["draft_id"], revision=3)
            repeated = outgoing.save_draft_to_thunderbird(draft["draft_id"], revision=3)
        self.assertTrue(result["saved"])
        self.assertFalse(result["previous_draft_retired"])
        self.assertTrue(repeated["duplicate_prevented"])
        connection.append.assert_called_once()

    def test_server_draft_read_requires_draft_flag_and_uidvalidity(self):
        from thunderbird_mcp import live
        locator = live.Locator("environment", live._fingerprint(outgoing.account_config()), "Drafts", 8, 21)
        connection = MagicMock()
        connection.select.return_value = ("OK", [b"1"])
        connection.response.return_value = ("UIDVALIDITY", [b"8"])
        for flags in (r"\Seen", r"\Draft \Deleted"):
            connection.uid.return_value = ("OK", [f"1 (UID 21 FLAGS ({flags}))".encode()])
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, "undeleted draft"):
                outgoing._server_draft_bytes(connection, locator)
        connection.response.return_value = ("UIDVALIDITY", [b"9"])
        with self.assertRaisesRegex(LookupError, "UIDVALIDITY"):
            outgoing._server_draft_bytes(connection, locator)

    def test_composition_policy_blocks_direct_mutations(self):
        draft = self.draft()
        with patch.dict(os.environ, {"TB_MCP_PROFILE": "read-only"}):
            with self.assertRaises(PermissionError):
                self.draft()
            with self.assertRaises(PermissionError):
                outgoing.edit_draft(draft["draft_id"], 1, {"body": "blocked"})
        with patch.dict(os.environ, {"TB_MCP_PROFILE": "draft-only"}), self.smtp() as (smtp, _):
            outgoing.edit_draft(draft["draft_id"], 1, {"body": "allowed"})
            with self.assertRaises(PermissionError):
                outgoing.send_draft(draft["draft_id"], revision=2)
        smtp.sendmail.assert_not_called()

    def test_native_draft_uses_profile_folder_and_matching_identity(self):
        prefs = {"mail.accountmanager.defaultaccount": "account1",
                 "mail.account.account1.server": "server1", "mail.account.account1.identities": "id1,id2",
                 "mail.identity.id1.useremail": "other@example.test",
                 "mail.identity.id2.useremail": "me@example.test",
                 "mail.identity.id2.draft_folder": "imap://test@imap.example.test/Draft%20Mail"}
        with patch.object(outgoing, "_prefs", return_value=prefs):
            draft = self.draft()
            with self.draft_imap(folders=["Draft Mail"]) as connection:
                result = outgoing.save_draft_to_thunderbird(draft["draft_id"])
        self.assertEqual(result["folder"], "Draft Mail")
        self.assertEqual(connection.append.call_args.args[0], '"Draft Mail"')
        message = BytesParser(policy=policy.default).parsebytes(connection.append.call_args.args[3])
        self.assertEqual(message["X-Identity-Key"], "id2")

    def test_native_draft_rejects_local_and_other_account_destinations(self):
        for uri in ("mailbox://nobody@Local%20Folders/Drafts", "imap://test@other.example.test/Drafts",
                    "imap://someone@imap.example.test/Drafts", "imap://test@imap.example.test:995/Drafts"):
            prefs = {"mail.identity.default.draft_folder": uri}
            with self.subTest(uri=uri), patch.object(outgoing, "_prefs", return_value=prefs):
                draft = self.draft()
                with patch.object(imap, "session") as session, self.assertRaisesRegex(ValueError, "Drafts must"):
                    outgoing.save_draft_to_thunderbird(draft["draft_id"])
                session.assert_not_called()
                self.assertEqual(outgoing.get_draft(draft["draft_id"])["status"], "prepared")

    def test_native_draft_cannot_switch_to_changed_account(self):
        draft = self.draft()
        with patch.dict(os.environ, {"TB_IMAP_USER": "someone"}), patch.object(imap, "session") as session:
            with self.assertRaisesRegex(ValueError, "account has changed"):
                outgoing.save_draft_to_thunderbird(draft["draft_id"])
        session.assert_not_called()

    def test_concurrent_native_save_and_send_are_blocked(self):
        draft = self.draft()
        concurrent = []
        def during_append(*args):
            concurrent.append(outgoing.save_draft_to_thunderbird(draft["draft_id"]))
            concurrent.append(outgoing.send_draft(draft["draft_id"]))
            return ("OK", [b"done"])
        with self.draft_imap() as connection, patch.object(outgoing, "smtp_session") as smtp:
            connection.append.side_effect = during_append
            outgoing.save_draft_to_thunderbird(draft["draft_id"])
        self.assertTrue(all(item["duplicate_prevented"] for item in concurrent))
        connection.append.assert_called_once()
        smtp.assert_not_called()

    def test_ambiguous_native_append_is_not_retried_or_sent(self):
        draft = self.draft()
        with self.draft_imap() as connection, patch.object(outgoing, "smtp_session") as smtp:
            connection.append.side_effect = imaplib.IMAP4.abort("synthetic secret in server response")
            first = outgoing.save_draft_to_thunderbird(draft["draft_id"])
            second = outgoing.save_draft_to_thunderbird(draft["draft_id"])
            outgoing.send_draft(draft["draft_id"], retry_failed=True)
        self.assertEqual(first["status"], "thunderbird_save_uncertain")
        self.assertIsNone(first["saved"])
        self.assertFalse(first["safe_to_retry"])
        self.assertNotIn("synthetic secret", json.dumps(first))
        self.assertTrue(second["duplicate_prevented"])
        connection.append.assert_called_once()
        smtp.assert_not_called()

    def test_native_append_rejection_or_missing_folder_is_safe_to_retry(self):
        for folders in (["Drafts"], ["Inbox"]):
            draft = self.draft()
            with self.subTest(folders=folders), self.draft_imap(folders=folders) as connection:
                connection.append.return_value = ("NO", [b"synthetic private server response"])
                first = outgoing.save_draft_to_thunderbird(draft["draft_id"])
                self.assertFalse(first["saved"])
                self.assertEqual(first["status"], "prepared")
                self.assertTrue(first["safe_to_retry"])
                if folders == ["Inbox"]:
                    connection.append.assert_not_called()
            with self.draft_imap():
                self.assertTrue(outgoing.save_draft_to_thunderbird(draft["draft_id"])["saved"])

    def test_native_logout_failure_preserves_success(self):
        draft = self.draft()
        with patch.object(imap, "session") as session, patch.object(imap, "list_folders", return_value=["Drafts"]):
            connection = session.return_value.__enter__.return_value
            connection.append.return_value = ("OK", [b"done"])
            session.return_value.__exit__.side_effect = OSError("logout failed")
            result = outgoing.save_draft_to_thunderbird(draft["draft_id"])
            repeated = outgoing.save_draft_to_thunderbird(draft["draft_id"])
        self.assertTrue(result["saved"])
        self.assertTrue(repeated["duplicate_prevented"])
        connection.append.assert_called_once()

    def test_sent_draft_cannot_be_saved_back_to_thunderbird(self):
        draft = self.draft()
        with self.smtp():
            outgoing.send_draft(draft["draft_id"])
        with patch.object(imap, "session") as session:
            result = outgoing.save_draft_to_thunderbird(draft["draft_id"])
        session.assert_not_called()
        self.assertFalse(result["saved"])

    def test_account_defaults_use_profile_and_allow_environment_overrides(self):
        prefs = {
            "mail.accountmanager.defaultaccount": "account1", "mail.account.account1.server": "server1",
            "mail.account.account1.identities": "id1", "mail.identity.id1.useremail": "profile@example.test",
            "mail.identity.id1.fullName": "Profile Name", "mail.identity.id1.smtpServer": "smtp1",
            "mail.identity.id1.fcc_folder": "imap://test@imap.example.test/Sent%20Mail",
            "mail.server.server1.hostname": "profile-imap.example.test", "mail.server.server1.userName": "profile-user",
            "mail.server.server1.socketType": 3, "mail.smtpserver.smtp1.hostname": "profile-smtp.example.test",
            "mail.smtpserver.smtp1.username": "profile-smtp-user", "mail.smtpserver.smtp1.port": 587,
            "mail.smtpserver.smtp1.try_ssl": 2,
        }
        with patch.dict(os.environ, {"TB_PROFILE": str(self.root / "profile")}, clear=True), patch.object(outgoing, "_prefs", return_value=prefs):
            config = outgoing.account_config()
        self.assertEqual(config["from_email"], "profile@example.test")
        self.assertEqual(config["smtp_tls"], "starttls")
        self.assertEqual(config["smtp_port"], 587)
        self.assertEqual(config["imap_user"], "profile-user")
        self.assertEqual(config["sent_folder"], "Sent Mail")

    def test_plaintext_auth_is_rejected(self):
        with patch.dict(os.environ, {"TB_SMTP_TLS": "none"}):
            with self.assertRaises(ValueError):
                outgoing.account_config()

    def test_headers_and_recipients_reject_injection_and_ambiguity(self):
        for recipient in ("person@example.test\r\nBcc: injected@example.test", "not-a-mailbox",
                          "one@example.test, two@example.test", "Team: person@example.test;", "é@example.test"):
            with self.subTest(recipient=recipient), self.assertRaises(ValueError):
                outgoing.prepare_draft([recipient], "Subject", "Body")
        for arguments in ({"subject": "Subject\nBcc: injected@example.test"}, {"in_reply_to": "<bad>"},
                          {"references": ["<valid@example.test>\r\nInjected: yes"]}):
            values = {"to": ["person@example.test"], "subject": "Subject", "body": "Body", **arguments}
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                outgoing.prepare_draft(**values)
        with self.assertRaises(ValueError):
            outgoing.prepare_draft([], "Subject", "Body")

    def test_draft_captures_attachments_and_thread_headers_without_bcc_header(self):
        attachment = self.root / "document.txt"
        attachment.write_bytes(b"original attachment")
        draft = self.draft(bcc=["private@example.test"], cc=["copy@example.test"], attachments=[str(attachment)],
                           in_reply_to="<parent@example.test>", references=["<root@example.test>"])
        attachment.write_bytes(b"changed attachment")
        with self.smtp() as (smtp, append):
            result = outgoing.send_draft(draft["draft_id"])
        self.assertTrue(result["sent"])
        self.assertTrue(result["sent_copy_saved"])
        envelope = smtp.sendmail.call_args.args[1]
        message = BytesParser(policy=policy.default).parsebytes(smtp.sendmail.call_args.args[2])
        self.assertIn("private@example.test", envelope)
        self.assertNotIn("Bcc", message)
        self.assertEqual(message["In-Reply-To"], "<parent@example.test>")
        self.assertEqual(message["References"], "<root@example.test> <parent@example.test>")
        self.assertEqual(next(message.iter_attachments()).get_payload(decode=True), b"original attachment")
        self.assertEqual(outgoing.outbox_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(outgoing.list_drafts()["count"], 1)
        self.assertNotIn("body", outgoing.list_drafts()["drafts"][0])
        self.assertNotIn("synthetic-smtp-secret", outgoing.outbox_path().read_bytes().decode(errors="ignore"))
        append.assert_called_once()

    def test_idempotency_reuses_draft_and_rejects_different_content(self):
        first = self.draft(idempotency_key="request-123")
        second = self.draft(idempotency_key="request-123")
        self.assertEqual(first["draft_id"], second["draft_id"])
        with self.assertRaises(ValueError):
            outgoing.prepare_draft(["other@example.test"], "Subject", "Body", idempotency_key="request-123")

    def test_recipients_are_deduplicated_across_to_cc_bcc(self):
        draft = self.draft(cc=["PERSON@example.test"], bcc=["person@EXAMPLE.test"])
        with self.smtp() as (smtp, _):
            outgoing.send_draft(draft["draft_id"])
        self.assertEqual(smtp.sendmail.call_args.args[1], ["person@example.test"])

    def test_total_attachment_limit_uses_actual_bytes(self):
        first = self.root / "one.txt"
        second = self.root / "two.txt"
        first.write_bytes(b"123456")
        second.write_bytes(b"123456")
        with patch.object(outgoing, "_MAX_ATTACHMENT_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "total"):
                self.draft(attachments=[str(first), str(second)])
        self.assertEqual(outgoing.list_drafts()["count"], 0)

    def test_quit_error_does_not_change_accepted_delivery(self):
        with patch.object(smtplib, "SMTP_SSL") as constructor:
            constructor.return_value.quit.side_effect = smtplib.SMTPServerDisconnected("after delivery")
            constructor.return_value.close.side_effect = OSError("close failed")
            with outgoing.smtp_session(outgoing.account_config()) as connection:
                self.assertIs(connection, constructor.return_value)

    def test_duplicate_send_is_blocked_after_success(self):
        draft = self.draft()
        with self.smtp() as (smtp, _):
            first = outgoing.send_draft(draft["draft_id"])
            second = outgoing.send_draft(draft["draft_id"])
        self.assertTrue(first["sent"])
        self.assertTrue(second["duplicate_prevented"])
        smtp.sendmail.assert_called_once()

    def test_delivery_and_native_handoff_have_unified_operation_history(self):
        from thunderbird_mcp import operations
        draft = self.draft()
        with self.smtp():
            sent = outgoing.send_draft(draft["draft_id"])
        operation = operations.get_operation(sent["operation_id"])
        self.assertEqual(operation["kind"], "send_draft")
        self.assertEqual(operation["status"], "completed")
        self.assertTrue(operation["result"]["sent_copy_saved"])
        self.assertEqual(outgoing.get_draft(draft["draft_id"])["operation_id"], sent["operation_id"])
        native = self.draft()
        with self.draft_imap():
            saved = outgoing.save_draft_to_thunderbird(native["draft_id"])
        handoff = operations.get_operation(saved["operation_id"])
        self.assertEqual(handoff["kind"], "save_draft_to_thunderbird")
        self.assertTrue(handoff["result"]["saved"])

    def test_journal_failure_after_smtp_acceptance_does_not_enable_resend(self):
        from thunderbird_mcp import operations
        draft = self.draft()
        with self.smtp() as (smtp, _), patch.object(operations, "finish_operation", side_effect=OSError("journal unavailable")):
            sent = outgoing.send_draft(draft["draft_id"])
            repeated = outgoing.send_draft(draft["draft_id"])
        self.assertTrue(sent["sent"])
        self.assertIn("journal_error", sent)
        self.assertTrue(repeated["duplicate_prevented"])
        smtp.sendmail.assert_called_once()

    def test_cancellation_during_smtp_login_stops_before_send_and_requires_new_review(self):
        from thunderbird_mcp import operations
        draft = self.draft()
        connection = MagicMock()
        @contextmanager
        def login_then_cancel(config):
            active = outgoing.get_draft(draft["draft_id"])
            operations.request_cancel(active["operation_id"])
            yield connection
        with patch.object(outgoing, "smtp_session", login_then_cancel), patch.object(outgoing, "_append_sent") as append:
            cancelled = outgoing.send_draft(draft["draft_id"])
            repeated = outgoing.send_draft(draft["draft_id"], retry_failed=True)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(cancelled["sent"])
        self.assertTrue(repeated["duplicate_prevented"])
        connection.sendmail.assert_not_called()
        append.assert_not_called()
        self.assertEqual(operations.get_operation(cancelled["operation_id"])["status"], "cancelled")
        edited = outgoing.edit_draft(draft["draft_id"], 1, {"body": "Freshly reviewed after cancellation"})
        self.assertEqual((edited["status"], edited["revision"]), ("prepared", 2))
        with self.smtp() as (smtp, _):
            self.assertTrue(outgoing.send_draft(draft["draft_id"], revision=2)["sent"])
        smtp.sendmail.assert_called_once()

    def test_native_handoff_cancellation_before_append_keeps_server_unchanged(self):
        from thunderbird_mcp import operations
        draft = self.draft()
        def resolve_then_cancel(connection, folder):
            operations.request_cancel(outgoing.get_draft(draft["draft_id"])["operation_id"])
            return "Drafts"
        with self.draft_imap() as connection, patch.object(outgoing, "_resolve_drafts_folder", side_effect=resolve_then_cancel):
            cancelled = outgoing.save_draft_to_thunderbird(draft["draft_id"])
            repeated = outgoing.save_draft_to_thunderbird(draft["draft_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(cancelled["saved"])
        self.assertTrue(repeated["duplicate_prevented"])
        connection.append.assert_not_called()

    def test_concurrent_attempt_cannot_send_twice(self):
        draft = self.draft()
        second = []
        with self.smtp() as (smtp, _):
            def during_send(*args):
                second.append(outgoing.send_draft(draft["draft_id"]))
                return {}
            smtp.sendmail.side_effect = during_send
            outgoing.send_draft(draft["draft_id"])
        self.assertTrue(second[0]["duplicate_prevented"])
        self.assertEqual(second[0]["status"], "sending")
        smtp.sendmail.assert_called_once()

    def test_smtp_success_is_preserved_when_sent_append_fails(self):
        draft = self.draft()
        with self.smtp() as (smtp, append):
            append.side_effect = RuntimeError("synthetic private server response")
            result = outgoing.send_draft(draft["draft_id"])
            repeated = outgoing.send_draft(draft["draft_id"])
        self.assertTrue(result["sent"])
        self.assertFalse(result["sent_copy_saved"])
        self.assertIn("sent_copy_error", result)
        self.assertNotIn("private server response", json.dumps(result))
        self.assertTrue(repeated["duplicate_prevented"])
        smtp.sendmail.assert_called_once()

    def test_partial_recipient_acceptance_is_accurate_and_not_retried(self):
        draft = self.draft(cc=["refused@example.test"])
        with self.smtp(refused={"refused@example.test": (550, b"private response")}) as (smtp, _):
            result = outgoing.send_draft(draft["draft_id"])
            repeated = outgoing.send_draft(draft["draft_id"], retry_failed=True)
        self.assertEqual(result["status"], "partially_sent")
        self.assertFalse(result["delivery_complete"])
        self.assertEqual(result["accepted_recipients"], ["person@example.test"])
        self.assertEqual(result["refused_recipients"], {"refused@example.test": {"smtp_code": 550}})
        self.assertTrue(repeated["duplicate_prevented"])
        smtp.sendmail.assert_called_once()

    def test_disconnect_during_delivery_is_uncertain_and_never_retried(self):
        draft = self.draft()
        with self.smtp(error=smtplib.SMTPServerDisconnected("response lost")) as (smtp, append):
            result = outgoing.send_draft(draft["draft_id"])
            repeated = outgoing.send_draft(draft["draft_id"], retry_failed=True)
        self.assertIsNone(result["sent"])
        self.assertTrue(result["delivery_uncertain"])
        self.assertFalse(result["safe_to_retry"])
        self.assertTrue(repeated["duplicate_prevented"])
        smtp.sendmail.assert_called_once()
        append.assert_not_called()

    def test_definitive_rejection_can_only_retry_explicitly(self):
        draft = self.draft()
        with self.smtp(error=smtplib.SMTPDataError(550, b"Rejected")) as (smtp, _):
            result = outgoing.send_draft(draft["draft_id"])
            blocked = outgoing.send_draft(draft["draft_id"])
            smtp.sendmail.side_effect = None
            retry = outgoing.send_draft(draft["draft_id"], retry_failed=True)
        self.assertFalse(result["sent"])
        self.assertTrue(result["safe_to_retry"])
        self.assertTrue(blocked["duplicate_prevented"])
        self.assertTrue(retry["sent"])
        self.assertEqual(smtp.sendmail.call_count, 2)

    def test_auth_failure_cannot_leak_credentials(self):
        draft = self.draft()
        with patch.object(outgoing, "smtp_session", side_effect=smtplib.SMTPAuthenticationError(535, b"synthetic-smtp-secret")):
            result = outgoing.send_draft(draft["draft_id"])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["sent"])
        self.assertNotIn("synthetic-smtp-secret", json.dumps(result))

    def test_ssl_and_starttls_verify_certificates_before_authenticating(self):
        for mode, port in (("ssl", "465"), ("starttls", "587")):
            with self.subTest(mode=mode), patch.dict(os.environ, {"TB_SMTP_TLS": mode, "TB_SMTP_PORT": port}):
                config = outgoing.account_config()
                constructor_name = "SMTP_SSL" if mode == "ssl" else "SMTP"
                with patch.object(smtplib, constructor_name) as constructor:
                    connection = constructor.return_value
                    with outgoing.smtp_session(config):
                        pass
                    context = constructor.call_args.kwargs["context"] if mode == "ssl" else connection.starttls.call_args.kwargs["context"]
                    self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                    self.assertTrue(context.check_hostname)
                    connection.login.assert_called_once_with("test", "synthetic-smtp-secret")
                    if mode == "starttls":
                        calls = [call[0] for call in connection.method_calls]
                        self.assertLess(calls.index("starttls"), calls.index("login"))

    def test_health_is_read_only(self):
        from thunderbird_mcp import imap
        imap_connection = MagicMock()
        imap_connection.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "Sent"'])
        smtp_connection = MagicMock()
        smtp_connection.noop.return_value = (250, b"OK")
        @contextmanager
        def imap_session(**kwargs):
            self.assertFalse(kwargs["require_move"])
            yield imap_connection
        @contextmanager
        def smtp_session(config):
            yield smtp_connection
        with patch.object(imap, "session", imap_session), patch.object(outgoing, "smtp_session", smtp_session):
            result = outgoing.health_check(check_network=True)
        self.assertTrue(result["ready"])
        smtp_connection.sendmail.assert_not_called()
        imap_connection.append.assert_not_called()
        imap_connection.select.assert_not_called()


if __name__ == "__main__":
    unittest.main()
