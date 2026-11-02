from __future__ import annotations

import ssl
import unittest
from unittest.mock import MagicMock, patch

from thunderbird_mcp import imap


class IMAPTests(unittest.TestCase):
    def test_mailbox_command_injection_is_rejected(self):
        for name in ("Sent\r\nEXPUNGE", "bad\x00name", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                imap._q(name)
        self.assertEqual(imap._q('A "quoted" folder'), '"A \\"quoted\\" folder"')

    def test_listing_unescapes_names_and_skips_nonselectable_mailboxes(self):
        connection = MagicMock()
        connection.list.return_value = ("OK", [None, b'(\\Noselect) "/" "Parent"',
            b'(\\HasNoChildren) "/" "A \\"quoted\\" folder"', b'(\\HasNoChildren) "/" INBOX'])
        self.assertEqual(imap.list_folders(connection), ['A "quoted" folder', "INBOX"])

    def test_unicode_mailbox_names_round_trip_modified_utf7(self):
        for name in ("INBOX", "Research & Reports", "Enviados/日本語", "Données/📨"):
            with self.subTest(name=name):
                wire = imap.encode_folder(name)
                wire.encode("ascii")
                self.assertEqual(imap.decode_folder(wire), name)
        self.assertEqual(imap.encode_folder("~peter/mail/台北/日本語"), "~peter/mail/&U,BTFw-/&ZeVnLIqe-")
        connection = MagicMock()
        connection.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "&ZeVnLIqe-"'])
        self.assertEqual(imap.list_folders(connection), ["日本語"])

    def test_session_verifies_tls_and_read_operations_do_not_require_move(self):
        config = {"imap_host": "imap.example.test", "imap_port": 993, "imap_user": "test", "imap_tls": "ssl", "timeout": 15}
        with patch.object(imap.imaplib, "IMAP4_SSL") as constructor, patch.object(imap, "password", return_value="synthetic"):
            connection = constructor.return_value
            connection.login.return_value = ("OK", [b"Logged in"])
            connection.capability.return_value = ("OK", [b"IMAP4rev1"])
            with imap.session(config=config) as session:
                self.assertIs(session, connection)
            context = constructor.call_args.kwargs["ssl_context"]
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            with self.assertRaisesRegex(RuntimeError, "MOVE"):
                with imap.session(config=config, require_move=True):
                    pass
        connection.logout.assert_called()

    def test_move_rejects_ambiguous_duplicate_message_ids_before_moving(self):
        connection = MagicMock()
        with patch.object(imap, "_uid_map", return_value={"<duplicate@example.test>": [b"1", b"2"]}):
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                imap._move_by_msgid(connection, "INBOX", "Sent", ["<duplicate@example.test>"])
        connection.uid.assert_not_called()

    def test_empty_move_does_not_connect(self):
        with patch.object(imap, "session") as session:
            result = imap.move_emails([], "Archive", dry_run=False)
        self.assertEqual(result["moved"], {})
        session.assert_not_called()

    def test_move_logs_each_completed_chunk_before_a_later_failure(self):
        connection = MagicMock()
        connection.uid.side_effect = [("OK", [b"Moved"]), ("NO", [b"Rejected"])]
        progress = MagicMock()
        mapping = {"<one@example.test>": [b"1"], "<two@example.test>": [b"2"]}
        with patch.object(imap, "CHUNK", 1), patch.object(imap, "_uid_map", return_value=mapping):
            with self.assertRaises(RuntimeError):
                imap._move_by_msgid(connection, "INBOX", "Sent", list(mapping), on_moved=progress)
        progress.assert_called_once_with(["<one@example.test>"])


if __name__ == "__main__":
    unittest.main()
