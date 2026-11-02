"""Account/UID-bound synthetic mutations; never touch a real mailbox."""
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from thunderbird_mcp import live, mailbox, policy, operations


class Connection:
    def __init__(self):
        self.calls = []
        self.validity = 42
        self.permanent = b"(\\Seen \\Flagged \\*)"
        self.capabilities = b"IMAP4rev1 UIDPLUS MOVE X-GM-EXT-1"
        self.fail_uid = None
        self.empty = True

    def select(self, folder, readonly=False):
        self.calls.append(("SELECT", folder, readonly))
        return "OK", [b"2"]

    def response(self, key):
        data = {"UIDVALIDITY": str(self.validity).encode(), "PERMANENTFLAGS": self.permanent,
                "COPYUID": b"84 7 12"}.get(key)
        return key, [data]

    def capability(self):
        return "OK", [self.capabilities]

    def list(self):
        return "OK", [b'() "/" "INBOX"', b'(\\Trash) "/" "Papierkorb"',
                      b'(\\Archive) "/" "Archives"', b'() "/" "Projects"']

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "FETCH":
            return "OK", [f"1 (UID {args[0]} FLAGS (\\Seen))".encode()]
        if command == "SEARCH":
            return "OK", [b"" if self.empty else b"7"]
        if self.fail_uid == args[0]:
            raise OSError("synthetic disconnect")
        return "OK", [b"completed"]

    def rename(self, *args):
        self.calls.append(("RENAME", *args))
        return "OK", [b"renamed"]

    def close(self):
        self.calls.append(("CLOSE",))
        return "OK", [b"closed"]

    def delete(self, *args):
        self.calls.append(("DELETE", *args))
        return "OK", [b"deleted"]


class MailboxTests(unittest.TestCase):
    def setUp(self):
        contexts = contextlib.ExitStack()
        self.addCleanup(contexts.close)
        root = Path(contexts.enter_context(tempfile.TemporaryDirectory()))
        contexts.enter_context(patch.dict(os.environ, {"TB_OPERATIONS_DB": str(root / "ops.sqlite"),
                                                       "TB_MCP_PROFILE": "full"}))
        self.config = {"account_id": "synthetic", "imap_host": "imap.example.test",
                       "imap_port": 993, "imap_user": "test@example.test"}
        self.connection = Connection()
        contexts.enter_context(patch.object(live.outgoing, "account_config", return_value=self.config))
        contexts.enter_context(patch.object(live.imap, "session", side_effect=lambda **kw: contextlib.nullcontext(self.connection)))
        self.reference = live.Locator("synthetic", live._fingerprint(self.config), "INBOX", 42, 7).reference

    def test_flags_preview_never_selects_writable_or_mutates(self):
        result = mailbox.update_email_flags([self.reference], read=False, flagged=True)
        self.assertTrue(result["complete"])
        self.assertEqual(result["results"][0]["remove_flags"], ["\\Seen"])
        self.assertTrue(all(c[2] for c in self.connection.calls if c[0] == "SELECT"))
        self.assertFalse(any(c[0] == "STORE" for c in self.connection.calls))

    def test_keywords_and_provider_labels_are_separate_commands(self):
        result = mailbox.update_email_flags([self.reference], add_keywords=["$label1"],
                                             add_labels=["Client café"], dry_run=False)
        self.assertTrue(result["complete"])
        commands = [c for c in self.connection.calls if c[0] == "STORE"]
        self.assertEqual(commands[0][2], "+FLAGS.SILENT")
        self.assertEqual(commands[1][2], "+X-GM-LABELS")
        self.assertIn("&", commands[1][3])
        self.assertEqual(operations.get_operation(result["operation_id"])["status"], "completed")

    def test_unsupported_keywords_and_gmail_labels_fail_before_store(self):
        self.connection.permanent = b"(\\Seen \\Flagged)"
        result = mailbox.update_email_flags([self.reference], add_keywords=["New"], dry_run=False)
        self.assertFalse(result["complete"])
        self.connection.capabilities = b"IMAP4rev1"
        result = mailbox.update_email_flags([self.reference], add_labels=["New"], dry_run=False)
        self.assertFalse(result["complete"])
        self.assertFalse(any(c[0] == "STORE" for c in self.connection.calls))

    def test_stale_reference_cannot_mutate_and_batch_is_bounded(self):
        self.connection.validity = 43
        result = mailbox.organize_emails([self.reference], "trash", dry_run=False)
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertFalse(any(c[0] == "MOVE" for c in self.connection.calls))
        with self.assertRaises(ValueError):
            mailbox.organize_emails([self.reference] * 101, "trash")

    def test_trash_uses_special_use_and_receipt_supports_safe_restore(self):
        result = mailbox.organize_emails([self.reference], "trash", dry_run=False)
        item = result["results"][0]
        self.assertEqual(item["destination"], "Papierkorb")
        self.assertEqual(live.parse_reference(item["destination_reference"]).uid, 12)
        self.assertEqual(item["undo"]["destination"], "INBOX")
        self.assertIn(("MOVE", "7", '"Papierkorb"'), self.connection.calls)

    def test_copy_does_not_delete_and_uncertain_result_is_not_retried(self):
        mailbox.organize_emails([self.reference], "copy", "Projects", dry_run=False)
        self.assertFalse(any(c[0] in {"STORE", "EXPUNGE"} for c in self.connection.calls))
        self.connection.fail_uid = "7"
        result = mailbox.organize_emails([self.reference], "move", "Projects", dry_run=False)
        self.assertEqual(result["results"][0]["status"], "uncertain")
        self.assertEqual(sum(c[0] == "MOVE" for c in self.connection.calls), 1)

    def test_delete_requires_explicit_confirmation_and_uidplus(self):
        with self.assertRaises(ValueError):
            mailbox.organize_emails([self.reference], "delete", dry_run=False)
        self.connection.capabilities = b"IMAP4rev1 MOVE"
        result = mailbox.organize_emails([self.reference], "delete", confirm_permanent=True, dry_run=False)
        self.assertFalse(result["complete"])
        self.assertFalse(any(c[0] == "STORE" for c in self.connection.calls))
        self.connection.capabilities += b" UIDPLUS"
        mailbox.organize_emails([self.reference], "delete", confirm_permanent=True, dry_run=False)
        self.assertIn(("EXPUNGE", "7"), self.connection.calls)

    def test_only_empty_ordinary_folders_can_be_deleted(self):
        with self.assertRaises(ValueError):
            mailbox.manage_folder("delete", "Papierkorb", dry_run=False)
        self.connection.empty = False
        with self.assertRaises(ValueError):
            mailbox.manage_folder("delete", "Projects", dry_run=False)
        self.connection.empty = True
        mailbox.manage_folder("delete", "Projects", dry_run=False)
        self.assertIn(("DELETE", '"Projects"'), self.connection.calls)
        self.assertTrue(all(c[2] for c in self.connection.calls if c[0] == "SELECT"))

    def test_read_only_policy_blocks_underlying_mutations(self):
        with patch.dict(os.environ, {"TB_MCP_PROFILE": "read-only"}):
            with self.assertRaises(PermissionError):
                mailbox.organize_emails([self.reference], "trash", dry_run=False)
        self.assertEqual(self.connection.calls, [])

    def test_cancellation_cannot_hide_an_uncertain_mailbox_effect(self):
        job = operations.create_operation("organize_emails", account="synthetic", total=1)
        operations.claim_operation(job["operation_id"])
        operations.request_cancel(job["operation_id"])
        result = mailbox._result(False, [{"reference": self.reference, "status": "uncertain"}], job["operation_id"])
        self.assertTrue(result["cancellation_requested"])
        self.assertFalse(result["complete"])
        self.assertEqual(operations.get_operation(job["operation_id"])["status"], "uncertain")


if __name__ == "__main__":
    unittest.main()
