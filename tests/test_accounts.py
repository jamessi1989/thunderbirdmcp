"""Account isolation checks use synthetic Thunderbird preferences and secrets."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from thunderbird_mcp import imap, outgoing


def account_prefs():
    prefs = {"mail.accountmanager.accounts": "account1,account2,account3",
             "mail.accountmanager.defaultaccount": "account1"}
    for number, label in ((1, "work"), (2, "personal")):
        prefs.update({
            f"mail.account.account{number}.server": f"server{number}",
            f"mail.account.account{number}.identities": f"id{number}",
            f"mail.identity.id{number}.useremail": f"{label}@example.test",
            f"mail.identity.id{number}.smtpServer": f"smtp{number}",
            f"mail.server.server{number}.type": "imap",
            f"mail.server.server{number}.hostname": "imap.example.test",
            f"mail.server.server{number}.userName": label,
            f"mail.server.server{number}.socketType": 3,
            f"mail.server.server{number}.directory-rel": f"[ProfD]ImapMail/imap.example.test-{number}",
            f"mail.smtpserver.smtp{number}.hostname": "smtp.example.test",
            f"mail.smtpserver.smtp{number}.username": label,
            f"mail.smtpserver.smtp{number}.port": 465,
            f"mail.smtpserver.smtp{number}.try_ssl": 3,
        })
    prefs.update({"mail.account.account3.server": "server3", "mail.server.server3.type": "none"})
    return prefs


class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"TB_OUTBOX_DB": str(Path(self.directory.name) / "outbox.sqlite")}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.prefs = account_prefs()
        self.settings = patch.object(outgoing, "_prefs", return_value=self.prefs)
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def test_list_accounts_excludes_local_folders_and_preserves_unique_directories(self):
        result = outgoing.list_accounts()
        self.assertEqual(result["default_account"], "account1")
        self.assertEqual([value["account_id"] for value in result["accounts"]], ["account1", "account2"])
        self.assertEqual(result["accounts"][1]["account_prefixes"], ["imap.example.test-2/"])
        self.assertEqual(outgoing.infer_account("imap.example.test-2/INBOX"), "account2")
        self.assertIsNone(outgoing.infer_account("Local Folders/INBOX"))

    def test_secondary_account_does_not_inherit_default_identity_or_credentials(self):
        with patch.dict(os.environ, {"TB_FROM": "override@example.test", "TB_IMAP_HOST": "override.example.test",
                                     "TB_IMAP_PASSWORD": "wrong-secret", "TB_ACCOUNT_ACCOUNT2_IMAP_PASSWORD": "right-secret"}):
            default = outgoing.account_config()
            selected = outgoing.account_config("personal@example.test")
            self.assertEqual(default["from_email"], "override@example.test")
            self.assertEqual(selected["account_id"], "account2")
            self.assertEqual(selected["from_email"], "personal@example.test")
            self.assertEqual(selected["imap_host"], "imap.example.test")
            self.assertEqual(outgoing.password(selected, "imap"), "right-secret")

    def test_unknown_ambiguous_and_local_accounts_fail_closed(self):
        with self.assertRaises(ValueError):
            outgoing.account_config("unknown")
        with self.assertRaises(ValueError):
            outgoing.account_config("account3")
        self.prefs["mail.identity.id2.useremail"] = "work@example.test"
        with self.assertRaisesRegex(ValueError, "multiple"):
            outgoing.account_config("work@example.test")
        self.prefs["mail.server.server2.directory-rel"] = "[ProfD]ImapMail/imap.example.test-1"
        self.assertIsNone(outgoing.infer_account("imap.example.test-1/INBOX"))

    def test_email_selector_never_silently_uses_another_identity(self):
        self.prefs["mail.account.account2.identities"] = "id2,id4"
        self.prefs["mail.identity.id4.useremail"] = "alias@example.test"
        self.assertEqual(outgoing.account_config("account2")["aliases"], ["personal@example.test", "alias@example.test"])
        with self.assertRaisesRegex(ValueError, "Unknown Thunderbird account"):
            outgoing.account_config("alias@example.test")

    def test_thunderbird_oauth_settings_and_scoped_overrides_are_respected(self):
        self.prefs["mail.server.server2.authMethod"] = 10
        self.prefs["mail.smtpserver.smtp2.authMethod"] = 10
        with patch.dict(os.environ, {"TB_IMAP_AUTH": "password"}):
            config = outgoing.account_config("account2")
            self.assertEqual(config["imap_auth"], "oauth2")
            self.assertEqual(config["smtp_auth"], "oauth2")
            with patch.dict(os.environ, {"TB_ACCOUNT_ACCOUNT2_IMAP_AUTH": "password"}):
                self.assertEqual(outgoing.account_config("account2")["imap_auth"], "password")

    def test_credentials_stay_bound_after_default_or_endpoint_changes(self):
        config = outgoing.account_config()
        with patch.dict(os.environ, {"TB_ACCOUNT": "account2", "TB_IMAP_PASSWORD": "new-default-secret"}), \
                patch.object(outgoing.keyring, "get_password", return_value=None) as keyring:
            with self.assertRaisesRegex(RuntimeError, "No IMAP password"):
                outgoing.password(config, "imap")
            self.assertTrue(all(call.args[1].startswith("account:") for call in keyring.call_args_list))
        with patch.dict(os.environ, {"TB_IMAP_HOST": "changed.example.test", "TB_IMAP_PASSWORD": "changed-secret"}), \
                patch.object(outgoing.keyring, "get_password", return_value=None):
            with self.assertRaises(RuntimeError):
                outgoing.password(config, "imap")

    def test_scoped_keyring_preferred_and_legacy_only_for_default(self):
        calls = []
        def retrieve(service, user):
            calls.append(user)
            return "legacy-secret" if user == "work" else None
        with patch.object(outgoing.keyring, "get_password", side_effect=retrieve):
            self.assertEqual(outgoing.password(outgoing.account_config(), "imap"), "legacy-secret")
            self.assertTrue(calls[0].startswith("account:"))
            with self.assertRaises(RuntimeError):
                outgoing.password(outgoing.account_config("account2"), "imap")
        self.assertNotIn("personal", calls)

    def test_legacy_password_never_crosses_same_username_on_different_hosts(self):
        self.prefs["mail.server.server2.userName"] = "work"
        self.prefs["mail.server.server2.hostname"] = "other.example.test"
        calls = []
        def retrieve(service, user):
            calls.append(user)
            return "first-account-secret" if user == "work" else None
        with patch.object(outgoing.keyring, "get_password", side_effect=retrieve):
            for selected in ("account1", "account2"):
                with self.subTest(selected=selected), patch.dict(os.environ, {"TB_ACCOUNT": selected}):
                    with self.assertRaisesRegex(RuntimeError, "No IMAP password"):
                        outgoing.password(outgoing.account_config(), "imap")
        self.assertNotIn("work", calls)

    def test_password_import_is_scoped_and_does_not_return_the_secret(self):
        values = {}
        config = outgoing.account_config("account2")
        with patch.object(outgoing.keyring, "set_password", side_effect=lambda service, user, value: values.__setitem__((service, user), value)), \
                patch.object(outgoing.keyring, "get_password", side_effect=lambda service, user: values.get((service, user))):
            result = outgoing.store_password(config, "imap", "secret with spaces ")
            self.assertEqual(outgoing.password(config, "imap"), "secret with spaces ")
            self.assertNotIn("secret", json.dumps(result))
            self.assertTrue(next(iter(values))[1].startswith("account:"))
            with self.assertRaises(RuntimeError):
                outgoing.password(config, "smtp")
            with self.assertRaises(RuntimeError):
                outgoing.password(outgoing.account_config("account1"), "imap")

    def test_draft_keeps_selected_sender_and_lists_by_account(self):
        first = outgoing.prepare_draft(["recipient@example.test"], "Subject", "Body", account="account2")
        outgoing.prepare_draft(["recipient@example.test"], "Subject", "Other", account="account1")
        self.assertEqual(first["account_id"], "account2")
        self.assertEqual(first["from"], "personal@example.test")
        self.assertEqual(outgoing.list_drafts(account="account2")["count"], 1)
        self.prefs["mail.accountmanager.defaultaccount"] = "account2"
        with outgoing._db() as conn:
            row = conn.execute("SELECT config FROM drafts WHERE id=?", (first["draft_id"],)).fetchone()
        self.assertEqual(json.loads(row["config"])["smtp_user"], "personal")
        self.assertEqual(json.loads(row["config"])["account_id"], "account2")

    def test_imap_move_rejects_other_account_before_connecting(self):
        with patch.object(imap, "_rows", return_value=[{"id": 1, "folder": "imap.example.test-2/INBOX", "message_id": "<id@example.test>"}]), \
                patch.object(imap, "session") as session:
            with self.assertRaisesRegex(ValueError, "different"):
                imap.move_emails([1], "Archive", account="account1")
        session.assert_not_called()

    def test_undo_uses_logged_account_even_after_default_changes(self):
        path = Path(self.directory.name) / "moves.jsonl"
        config = outgoing.account_config("account2")
        entry = {"batch": "batch1", "account_id": "account2", "account_binding": outgoing.credential_key(config, "imap"),
                 "src_local": "imap.example.test-2/INBOX", "src": "INBOX", "dest": "Archive", "message_ids": ["<id@example.test>"]}
        path.write_text(json.dumps(entry) + "\n")
        with patch.object(imap, "undo_log_path", return_value=path), patch.object(imap, "session") as session, \
                patch.object(imap, "_move_by_msgid"):
            imap.undo_batch("batch1")
            self.assertEqual(session.call_args.kwargs["config"]["account_id"], "account2")
            with self.assertRaisesRegex(ValueError, "different account"):
                imap.undo_batch("batch1", account="account1")
            self.prefs["mail.server.server2.hostname"] = "changed.example.test"
            with self.assertRaisesRegex(ValueError, "settings changed"):
                imap.undo_batch("batch1")


if __name__ == "__main__":
    unittest.main()
