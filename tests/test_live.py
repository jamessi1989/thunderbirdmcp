"""Synthetic IMAP reads must be bounded, account-scoped, and leave flags alone."""

import contextlib
import email.policy
import imaplib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from thunderbird_mcp import live, messages
from test_index import mail


class FakeIMAP:
    def __init__(self, values=None):
        self.messages = values or {7: mail()}
        self.validity = 1234
        self.calls = []
        self.search_result = None
        self.wrong_uid = False
        self.declared_size = None
        self.flags = b""

    def select(self, folder, readonly=False):
        self.calls.append(("SELECT", folder, readonly))
        if readonly is not True:
            raise AssertionError("A live read selected a writable mailbox")
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        self.calls.append(("RESPONSE", name))
        return name, [str(self.validity).encode()] if self.validity is not None else [None]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            uids = self.search_result if self.search_result is not None else list(self.messages)
            return "OK", [b" ".join(str(uid).encode() for uid in uids)]
        if command != "FETCH":
            raise AssertionError(f"Unexpected mutating command {command}")
        uid, request = int(args[0]), args[1]
        if uid not in self.messages:
            return "OK", [None]
        raw = self.messages[uid].as_bytes(policy=email.policy.SMTP)
        size = self.declared_size if self.declared_size is not None else len(raw)
        response_uid = uid + 1 if self.wrong_uid else uid
        prefix = f"1 (UID {response_uid} RFC822.SIZE {size} FLAGS (".encode() + self.flags + b")"
        if "BODY" not in request:
            return "OK", [prefix + b")"]
        if "BODY.PEEK" not in request:
            raise AssertionError("A live read used a fetch that can set Seen")
        section = "HEADER" if "[HEADER]" in request else ""
        data = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n" if section else raw
        cap = int(request.rsplit(".", 1)[1].split(">")[0])
        data = data[:cap]
        return "OK", [(prefix + f" BODY[{section}]<0> {{{len(data)}}}".encode(), data), b")"]


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "profile"
        (self.profile / "ImapMail").mkdir(parents=True)
        self.db = self.root / "absent.sqlite"
        self.config = {"account_id": "account2", "imap_host": "imap.example.test",
                       "imap_port": 993, "imap_user": "reader@example.test"}
        self.connection = FakeIMAP()
        contexts = contextlib.ExitStack()
        self.addCleanup(contexts.close)
        self.account_mock = contexts.enter_context(patch.object(live.outgoing, "account_config", return_value=self.config))
        contexts.enter_context(patch.object(live.imap, "session", side_effect=lambda **kw: contextlib.nullcontext(self.connection)))
        contexts.enter_context(patch.object(live.index, "find_profile", return_value=self.profile))
        contexts.enter_context(patch.object(live.outgoing, "_prefs", return_value={}))
        self.locator = live.Locator("account2", live._fingerprint(self.config), "INBOX", 1234, 7)
        self.reference = self.locator.reference

    def test_reference_roundtrip_and_changed_account_rejected(self):
        locator = live.Locator("account/2", live._fingerprint(self.config), "Saved/Receipts & café", 1234, 7)
        self.assertEqual(live.parse_reference(locator.reference), locator)
        self.config["imap_user"] = "different@example.test"
        with self.assertRaisesRegex(LookupError, "account has changed"):
            live.get_email(self.reference)
        self.assertEqual(self.connection.calls, [])

    def test_invalid_references_rejected_before_network(self):
        for value in (self.reference + "?extra=1", self.reference + "#extra", self.reference.rsplit("/", 1)[0] + "/0",
                      self.reference.replace("INBOX", "%0D%0ASTORE"), self.reference.replace("INBOX", "%ZZ"),
                      self.reference.replace("/1234/", "/4294967296/"), self.reference.replace("INBOX", "%FF")):
            with self.subTest(reference=value), self.assertRaises(ValueError):
                live.get_email(value)
        self.assertEqual(self.connection.calls, [])

    def test_changed_or_missing_uidvalidity_never_fetches_message(self):
        self.connection.validity = 4321
        with self.assertRaisesRegex(LookupError, "UIDVALIDITY changed"):
            live.get_email(self.reference)
        self.assertFalse(any(call[0] == "FETCH" for call in self.connection.calls))
        self.connection.validity = None
        with self.assertRaisesRegex(RuntimeError, "UIDVALIDITY"):
            live.search_email()

    def test_read_routes_without_index_and_never_marks_read(self):
        self.connection.messages[7] = mail(body="Fresh server body", **{"Cc": "copy@example.test"})
        result = messages.get_email(reference=self.reference, db_path=self.db, max_body_chars=5)
        self.assertEqual(result["body"], "Fresh")
        self.assertEqual(result["cc"], "copy@example.test")
        self.assertTrue(result["body_truncated"])
        self.assertEqual(result["account_id"], "account2")
        self.assertEqual(result["reference"], self.reference)
        self.account_mock.assert_called_with("account2")
        self.assertFalse(self.db.exists())
        fetches = [call for call in self.connection.calls if call[0] == "FETCH"]
        self.assertTrue(any("BODY.PEEK[]" in call[2] for call in fetches))

    def test_search_limits_header_fetches_and_reports_unread(self):
        self.connection.messages = {uid: mail(str(uid)) for uid in range(1, 10)}
        self.connection.flags = b"\\seen"
        result = live.search_email("meeting", limit=2, account="account2")
        self.assertEqual([row["uid"] for row in result["results"]], [9, 8])
        self.assertEqual(result["matched"], 9)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["results"][0]["unread"])
        self.assertNotIn("body", result["results"][0])
        self.assertTrue(result["diagnostics"]["headers_only"])
        fetches = [call for call in self.connection.calls if call[0] == "FETCH"]
        self.assertEqual(len(fetches), 2)
        self.assertTrue(all("BODY.PEEK[HEADER]" in call[2] for call in fetches))

    def test_search_operators_quotes_dates_and_utf8(self):
        live.search_email('"project notes" from:"José Example" to:reader@example.test '
                          'folder:"Saved Mail" after:2026-01-01 before:2026-02-01', unread=True)
        select = next(call for call in self.connection.calls if call[0] == "SELECT")
        self.assertEqual(select[1], '"Saved Mail"')
        call = next(call for call in self.connection.calls if call[0] == "SEARCH")
        self.assertEqual(call[1:3], ("CHARSET", "UTF-8"))
        self.assertIn(b'"project notes"', call)
        self.assertIn('"José Example"'.encode(), call)
        self.assertIn("CC", call)
        self.assertIn("01-Jan-2026", call)
        self.assertIn("01-Feb-2026", call)
        self.assertIn("UNSEEN", call)
        self.connection.calls.clear()
        live.search_email(until="2026-02-28")
        call = next(call for call in self.connection.calls if call[0] == "SEARCH")
        self.assertIn("01-Mar-2026", call)

    def test_query_injection_and_invalid_filters_are_rejected(self):
        for kwargs in ({"query": "hello\r\nSTORE 1 +FLAGS (\\Seen)"}, {"sender": ""},
                       {"since": "2026-01-01T12:00:00Z"}, {"since": "2026-02-30"},
                       {"query": "from:"}, {"query": 'folder:"unclosed'},
                       {"since": "2026-02-01", "until": "2026-01-01"}, {"limit": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                live.search_email(**kwargs)
        self.assertEqual(self.connection.calls, [])
        live.search_email(subject='one" OR ALL "two')
        call = next(call for call in self.connection.calls if call[0] == "SEARCH")
        self.assertIn(b'"one\\" OR ALL \\"two"', call)

    def test_wrong_uid_does_not_return_someone_elses_message(self):
        self.connection.wrong_uid = True
        with self.assertRaises(LookupError):
            live.get_email(self.reference)
        self.assertFalse(any("BODY.PEEK[]" in str(call) for call in self.connection.calls))

    def test_large_message_rejected_before_body_fetch(self):
        self.connection.declared_size = live.MAX_RAW_BYTES + 1
        with self.assertRaisesRegex(ValueError, "byte limit"):
            live.get_email(self.reference)
        self.assertFalse(any("BODY.PEEK" in str(call) for call in self.connection.calls))

    def test_incomplete_body_is_never_exported(self):
        self.connection.declared_size = 123456
        path = self.root / "incomplete.eml"
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            messages.export_email(self.reference, path, db_path=self.db)
        self.assertFalse(path.exists())

    def test_attachment_and_mime_exports_are_private_and_protect_storage(self):
        message = mail()
        message.add_attachment(b"synthetic file", maintype="application", subtype="octet-stream",
                               filename="../../payload.bin")
        self.connection.messages[7] = message
        result = messages.list_attachments(self.reference, db_path=self.db)
        self.assertEqual(result["attachments"][0]["filename"], "payload.bin")
        destination = self.root / "file.bin"
        messages.export_attachment(self.reference, "1", destination, db_path=self.db)
        self.assertEqual(destination.read_bytes(), b"synthetic file")
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            messages.export_attachment(self.reference, "1", destination, db_path=self.db)
        with self.assertRaises(ValueError):
            messages.export_email(self.reference, self.profile / "ImapMail" / "new.eml", db_path=self.db)
        with self.assertRaises(ValueError):
            messages.export_email(self.reference, self.db, db_path=self.db)
        destination = self.root / "mail.eml"
        messages.export_email(self.reference, destination, db_path=self.db)
        self.assertEqual(destination.read_bytes(), message.as_bytes(policy=email.policy.SMTP))

    def test_reference_citation_does_not_download_body(self):
        value = messages.reference_email(self.reference, db_path=self.db)
        self.assertIn(self.reference, value["citation"])
        self.assertNotIn("body", value)
        self.assertTrue(all("BODY.PEEK[HEADER]" in call[2] for call in self.connection.calls if call[0] == "FETCH"))

    def test_no_index_exports_protect_external_and_symlinked_mail_storage(self):
        external = self.root / "external-store"
        external.mkdir()
        with patch.object(live.outgoing, "_prefs", return_value={"mail.server.server2.directory": str(external)}):
            with self.assertRaisesRegex(ValueError, "mail storage"):
                messages.export_email(self.reference, external / "unsafe.eml", db_path=self.db)
        (self.profile / "ImapMail").rmdir()
        (self.profile / "ImapMail").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "mail storage"):
            messages.export_email(self.reference, external / "unsafe.eml", db_path=self.db)

    def test_oversized_literal_is_rejected_before_allocation(self):
        from unittest.mock import Mock
        connection = Mock()
        original_read = connection.read
        with self.assertRaisesRegex(RuntimeError, "literal byte limit"):
            with live._literal_limit(connection, 12):
                connection.read(13)
        original_read.assert_not_called()
        connection.shutdown.assert_called_once()
        self.assertIs(connection.read, original_read)
        with live._literal_limit(connection, 12):
            connection.read(7)
            with self.assertRaises(RuntimeError):
                connection.read(6)

    def test_live_attachment_text_routes_without_an_index(self):
        message = mail()
        message.add_attachment("Fresh café attachment", subtype="plain", charset="iso-8859-1", filename="notes.txt")
        self.connection.messages[7] = message
        value = messages.get_attachment_text(self.reference, "1", max_chars=10, db_path=self.db)
        self.assertEqual(value["text"], "Fresh café")
        self.assertTrue(value["truncated"])
        self.assertEqual(value["status"], "extracted")
        self.assertEqual(value["reference"], self.reference)
        self.assertFalse(self.db.exists())
        self.connection.calls.clear()
        with self.assertRaises(ValueError):
            live.get_attachment_text(self.reference, True)
        with self.assertRaises(ValueError):
            live.get_attachment_text(self.reference, 1, max_chars=30_000)
        self.assertEqual(self.connection.calls, [])

    def test_thread_follows_exact_headers_and_reports_single_folder_scope(self):
        self.connection.messages = {
            7: mail("root", subject="Start"),
            9: mail("reply", subject="Changed subject", **{"In-Reply-To": "<root@example.test>"}),
            10: mail("other", subject="Start"),
            12: mail("later", **{"References": "<root@example.test> <reply@example.test>"}),
        }
        result = messages.get_thread(self.reference, db_path=self.db, include_bodies=True)
        self.assertEqual({message["uid"] for message in result["messages"]}, {7, 9, 12})
        self.assertEqual(result["scope"], "this_folder_only")
        self.assertFalse(result["truncated"])
        self.assertTrue(all("body" in message for message in result["messages"]))
        limited = messages.get_thread(self.reference, db_path=self.db, limit=1)
        self.assertTrue(limited["truncated"])

    def test_network_errors_are_sanitized(self):
        with patch.object(self.connection, "select", side_effect=imaplib.IMAP4.error("private-token-and-server-response")):
            with self.assertRaises(RuntimeError) as raised:
                live.get_email(self.reference)
        self.assertNotIn("private-token", str(raised.exception))

    def test_deadline_stops_further_commands(self):
        with self.assertRaises(TimeoutError):
            live._search_uids(self.connection, [], time.monotonic() - 1)
        self.assertEqual(self.connection.calls, [])


if __name__ == "__main__":
    unittest.main()
