"""Delivery tests use synthetic mail and mocks; they never access a mail server."""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import tempfile
import unittest
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import MagicMock, patch

from thunderbird_mcp import outgoing


class OutgoingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        profile = self.root / "profile"
        profile.mkdir()
        self.env = patch.dict(os.environ, {
            "TB_PROFILE": str(profile), "TB_OUTBOX_DB": str(self.root / "outbox.sqlite"),
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
