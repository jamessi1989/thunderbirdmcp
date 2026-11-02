"""References and exports must survive refreshes and never return adjacent mail."""

import email
import os
from unittest.mock import patch

from thunderbird_mcp import attachments, index, messages
from test_index import MailFixture, mail


class MessageTests(MailFixture):
    def test_reference_survives_move_full_reindex_and_cites_without_body(self):
        path = self.folder("INBOX", [mail()])
        self.refresh()
        first = messages.reference_email(self.rows()[0]["id"], db_path=self.db)
        self.folder("Archive", [mail()])
        path.unlink()
        self.refresh(full=True)
        value = messages.get_email(first["reference"], db_path=self.db)
        self.assertEqual(value["message_id"], "<one@example.test>")
        self.assertIn("Archive", value["folder"])
        self.assertNotIn("body", first)
        self.assertIn(first["reference"], first["citation"])

    def test_full_headers_and_truncation_are_explicit(self):
        self.folder("INBOX", [mail(body="x" * (index.MAX_BODY_CHARS + 1),
             **{"Cc": "cc@example.test", "Reply-To": "reply@example.test",
                "In-Reply-To": "<parent@example.test>", "References": "<root@example.test> <parent@example.test>"})])
        self.refresh()
        value = messages.get_email(self.rows()[0]["id"], db_path=self.db, max_body_chars=30)
        self.assertEqual(value["to"], "Reader <reader@example.test>")
        self.assertEqual(value["cc"], "cc@example.test")
        self.assertEqual(value["reply_to"], "reply@example.test")
        self.assertEqual(value["references"], ["<root@example.test>", "<parent@example.test>"])
        self.assertEqual(len(value["body"]), 30)
        self.assertTrue(value["body_truncated"])

    def test_thread_follows_references_when_subject_changes(self):
        self.folder("INBOX", [mail("root", subject="Original"),
            mail("reply", subject="Changed subject", **{"In-Reply-To": "<root@example.test>"}),
            mail("last", subject="Another change", **{"References": "<root@example.test> <reply@example.test>"}),
            mail("unrelated", subject="Original")])
        self.refresh()
        result = messages.get_thread("<reply@example.test>", db_path=self.db)
        self.assertEqual(result["total"], 3)
        self.assertFalse(result["truncated"])
        self.assertTrue(all("body" not in message for message in result["messages"]))
        limited = messages.get_thread("<reply@example.test>", limit=2, db_path=self.db)
        self.assertTrue(limited["truncated"])

    def test_thread_links_siblings_with_missing_parent(self):
        self.folder("INBOX", [mail("first", **{"In-Reply-To": "<missing@example.test>"}),
                              mail("second", **{"In-Reply-To": "<missing@example.test>"})])
        self.refresh()
        self.assertEqual(messages.get_thread("<first@example.test>", db_path=self.db)["count"], 2)

    def test_raw_export_verifies_identity_after_compaction(self):
        self.folder("INBOX", [mail("first", body="A longer original message."), mail("target", body="Target text")])
        self.refresh()
        self.folder("INBOX", [mail("target", body="Target text"), mail("other", body="Do not export this mail")])
        destination = self.root / "mail.eml"
        result = messages.export_email("<target@example.test>", destination, db_path=self.db)
        self.assertGreater(result["bytes"], 0)
        content = destination.read_bytes()
        self.assertIn(b"Target text", content)
        self.assertNotIn(b"Do not export", content)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_stale_length_cannot_include_next_message(self):
        self.folder("INBOX", [mail("target", body="A long old body " * 50)])
        self.refresh()
        self.folder("INBOX", [mail("target", body="Short"), mail("other", body="Adjacent mail secret")])
        destination = self.root / "short.eml"
        messages.export_email("<target@example.test>", destination, db_path=self.db)
        self.assertNotIn(b"Adjacent mail secret", destination.read_bytes())

    def test_export_attachment_has_numbered_id_and_safe_filename(self):
        message = mail()
        message.add_attachment(b"synthetic attachment", maintype="application", subtype="octet-stream", filename="../../unsafe.bin")
        self.folder("INBOX", [message])
        self.refresh()
        attachments = messages.list_attachments("<one@example.test>", db_path=self.db)
        self.assertEqual(attachments["count"], 1)
        self.assertEqual(attachments["attachments"][0]["filename"], "unsafe.bin")
        destination = self.root / "attachment.bin"
        messages.export_attachment("<one@example.test>", "1", destination, db_path=self.db)
        self.assertEqual(destination.read_bytes(), b"synthetic attachment")
        with self.assertRaises(FileExistsError):
            messages.export_attachment("<one@example.test>", 1, destination, db_path=self.db)
        with self.assertRaises(ValueError):
            messages.export_email("<one@example.test>", self.account / "unsafe.eml", db_path=self.db)

    def test_expunged_source_is_not_exported(self):
        self.folder("INBOX", [mail()])
        self.refresh()
        self.folder("INBOX", [mail(**{"X-Mozilla-Status": "0008"})])
        with self.assertRaises(LookupError):
            messages.export_email("<one@example.test>", self.root / "deleted.eml", db_path=self.db)

    def test_attachment_text_uses_charset_and_only_extracts_requested_part(self):
        message = mail()
        message.add_attachment(b"unrelated PDF", maintype="application", subtype="pdf", filename="other.pdf")
        message.add_attachment("Café project notes", subtype="plain", charset="iso-8859-1", filename="notes.txt")
        self.folder("INBOX", [message])
        with patch.object(attachments, "extract_text", return_value={"text": "", "status": "unsupported", "truncated": False}):
            self.refresh()
        with patch.object(attachments, "extract_text", wraps=attachments.extract_text) as extract:
            value = messages.get_attachment_text("<one@example.test>", "2", max_chars=4, db_path=self.db)
        self.assertEqual(value["text"], "Café")
        self.assertEqual(value["status"], "extracted")
        self.assertTrue(value["truncated"])
        self.assertEqual(value["attachment"]["filename"], "notes.txt")
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(extract.call_args.kwargs["charset"], "iso-8859-1")

    def test_attachment_text_reports_unsupported_and_rejects_invalid_ids(self):
        message = mail()
        message.add_attachment(b"binary data", maintype="application", subtype="octet-stream", filename="file.bin")
        self.folder("INBOX", [message])
        self.refresh()
        value = messages.get_attachment_text("<one@example.test>", 1, db_path=self.db)
        self.assertEqual(value["status"], "unsupported")
        self.assertEqual(value["text"], "")
        for identifier in (0, -1, True, 1.5, "", "01", "../file.bin"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                messages.get_attachment_text("<one@example.test>", identifier, db_path=self.db)
        for count in (-1, True, 25_001):
            with self.subTest(count=count), self.assertRaises(ValueError):
                messages.get_attachment_text("<one@example.test>", 1, max_chars=count, db_path=self.db)
        with self.assertRaises(LookupError):
            messages.get_attachment_text("<one@example.test>", 2, db_path=self.db)

    def test_same_bytes_with_different_formats_keep_distinct_extraction_status(self):
        message = mail()
        for filename in ("notes.txt", "notes.bin"):
            message.add_attachment(b"same bytes", maintype="application", subtype="octet-stream", filename=filename)
        self.folder("INBOX", [message])
        self.refresh()
        value = messages.list_attachments("<one@example.test>", db_path=self.db)
        self.assertEqual([attachment["status"] for attachment in value["attachments"]], ["extracted", "unsupported"])

    def test_invalid_reference_and_out_of_range_limits_are_rejected(self):
        self.folder("INBOX", [mail()])
        self.refresh()
        with self.assertRaises(ValueError):
            messages.get_email("https://unrelated.test/", db_path=self.db)
        with self.assertRaises(ValueError):
            messages.get_email(1, "<one@example.test>", db_path=self.db)
        with self.assertRaises(ValueError):
            messages.get_thread(1, limit=0, db_path=self.db)
