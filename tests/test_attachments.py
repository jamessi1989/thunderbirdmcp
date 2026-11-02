"""Synthetic documents exercise extraction and resource limits without user files."""

import io
import json
import subprocess
import unittest
import zipfile
from unittest.mock import patch

from thunderbird_mcp import attachments
from test_index import mail


def document(name, contents):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, contents)
    return data.getvalue()


def pdf(text="Synthetic lunar budget", *, password=None):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class AttachmentTests(unittest.TestCase):
    def test_text_formats_and_unicode(self):
        for filename, content_type, data in (
            ("note.txt", "text/plain", "Synthetic café contract".encode()),
            ("records.csv", "text/csv", b"project,budget\nlunar,12"),
            ("data.json", "application/json", b'{"project": "lunar"}'),
            ("wide.txt", "text/plain", "Synthetic lunar".encode("utf-16")),
        ):
            with self.subTest(filename=filename):
                result = attachments.extract_text(data, filename, content_type)
                self.assertEqual(result["status"], "extracted")
                self.assertTrue(result["text"])
                self.assertFalse(result["truncated"])

    def test_html_removes_scripts_and_xml_reads_only_text(self):
        result = attachments.extract_text(b"<html><head>secret</head><body>Lunar<script>hidden()</script><p>budget</p></body></html>", "note.html")
        self.assertEqual(result["text"], "Lunar budget")
        result = attachments.extract_text(b"<report><project>Lunar</project><amount>12</amount></report>", "data.xml")
        self.assertEqual(result["text"], "Lunar 12")

    def test_xml_external_entity_is_rejected_without_reading_files(self):
        payload = b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///private/secret">]><a>&x;</a>'
        result = attachments.extract_text(payload, "unsafe.xml")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["text"], "")
        self.assertNotIn("private", json.dumps(result))

    def test_office_document_formats(self):
        for filename, member, xml in (
            ("report.docx", "word/document.xml", '<w:document xmlns:w="urn:word"><w:p><w:r><w:t>Lunar budget</w:t></w:r></w:p></w:document>'),
            ("report.xlsx", "xl/sharedStrings.xml", '<sst><si><t>Lunar budget</t></si></sst>'),
            ("report.pptx", "ppt/slides/slide1.xml", '<slide xmlns:a="urn:drawing"><a:t>Lunar budget</a:t></slide>'),
        ):
            with self.subTest(filename=filename):
                result = attachments.extract_text(document(member, xml), filename)
                self.assertEqual(result["status"], "extracted")
                self.assertEqual(result["text"], "Lunar budget")

    def test_archive_expansion_and_entry_count_are_bounded(self):
        payload = document("word/document.xml", "x" * 1000)
        with patch.object(attachments, "MAX_EXPANDED_BYTES", 100):
            self.assertEqual(attachments.extract_text(payload, "large.docx")["status"], "too_large")
        with patch.object(attachments, "MAX_ZIP_ENTRIES", 0):
            self.assertEqual(attachments.extract_text(payload, "large.docx")["status"], "too_large")

    def test_pdf_text_and_encrypted_pdf(self):
        result = attachments.extract_text(pdf(), "report.pdf")
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["text"], "Synthetic lunar budget")
        encrypted = attachments.extract_text(pdf(password="synthetic-password"), "locked.pdf")
        self.assertEqual(encrypted["status"], "encrypted")
        self.assertEqual(encrypted["text"], "")

    def test_pdf_timeout_and_parser_failure_do_not_abort_indexing(self):
        with patch.object(attachments.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 10)):
            self.assertEqual(attachments.extract_text(b"%PDF", "slow.pdf")["status"], "too_large")
        result = attachments.extract_text(b"Not a valid PDF or private document text", "bad.pdf")
        self.assertEqual(result["status"], "error")
        self.assertNotIn("private", json.dumps(result))

    def test_unsupported_empty_and_truncated_are_distinct(self):
        self.assertEqual(attachments.extract_text(b"image", "image.png", "image/png")["status"], "unsupported")
        self.assertEqual(attachments.extract_text(b"", "empty.txt")["status"], "empty")
        with patch.object(attachments, "MAX_ATTACHMENT_CHARS", 4):
            result = attachments.extract_text(b"lunar budget", "short.txt")
            self.assertEqual(result["text"], "luna")
            self.assertTrue(result["truncated"])
        with patch.object(attachments, "MAX_ATTACHMENT_BYTES", 4):
            self.assertEqual(attachments.extract_text(b"lunar", "large.txt")["status"], "too_large")

    def test_mime_metadata_and_shared_message_budgets(self):
        message = mail()
        for name in ("../budget.txt", "notes.txt"):
            message.add_attachment(b"lunar text", maintype="text", subtype="plain", filename=name)
        with patch.object(attachments, "MAX_MESSAGE_BYTES", 12):
            result = attachments.extract_message(message)
            self.assertEqual(result[0]["filename"], "budget.txt")
            self.assertEqual(result[0]["size"], 10)
            self.assertEqual(result[0]["status"], "extracted")
            self.assertEqual(len(result[0]["sha256"]), 64)
            self.assertEqual(result[1]["status"], "too_large")
        with patch.object(attachments, "MAX_ATTACHMENTS", 1):
            result = attachments.extract_message(message)
            self.assertEqual(result[-1]["status"], "too_many")
        summary = attachments.summarise_message(message)
        self.assertEqual(summary["attachments_indexed"], 1)
        self.assertIn("lunar text", summary["attachment_text"])
        self.assertNotIn("text", json.loads(summary["attachments_json"])[0])


if __name__ == "__main__":
    unittest.main()
