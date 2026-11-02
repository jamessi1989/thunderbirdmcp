"""Synthetic Thunderbird/standard Maildir fixtures, without mailbox mutations."""

import mailbox
import os
import sqlite3
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from thunderbird_mcp import index, maildir, messages, profile


class MaildirTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "profile"
        self.folder = self.profile / "ImapMail" / "example.test" / "INBOX"
        for name in ("cur", "new", "tmp"):
            (self.folder / name).mkdir(parents=True)
        self.database = self.root / "index.sqlite"
        env = {name: value for name, value in os.environ.items() if not name.startswith("TB_")}
        env.update(TB_PROFILE=str(self.profile), TB_MAIL_INDEX_DB=str(self.database))
        self.environment = patch.dict(os.environ, env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def message(self, name, *, body="Synthetic maildir body", mid=None, subdir="cur", attachment=False):
        message = EmailMessage()
        message["From"] = "sender@example.test"
        message["To"] = "reader@example.test"
        message["Subject"] = "Maildir fixture"
        message["Message-ID"] = f"<{mid or name}@example.test>"
        message.set_content(body)
        if attachment:
            message.add_attachment("Maildirattachmentquasar", subtype="plain", filename="note.txt")
        path = self.folder / subdir / name
        path.write_bytes(message.as_bytes())
        return path

    def refresh(self, **kwargs):
        return index.reindex(self.database, profile=self.profile, **kwargs)

    def rows(self):
        with sqlite3.connect(self.database) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM emails ORDER BY id")]

    def test_cur_new_are_indexed_tmp_and_symlinks_are_ignored(self):
        self.message("first", attachment=True)
        self.message("second", subdir="new")
        self.message("temporary-secret", subdir="tmp")
        folders = profile.discover_folders(self.profile)
        self.assertEqual(len(folders), 1)
        self.assertIsInstance(folders[0], maildir.MaildirFolder)
        self.assertEqual(self.refresh().messages_added, 2)
        self.assertEqual(len(self.rows()), 2)
        self.assertIn("Maildirattachmentquasar", self.rows()[0]["attachment_text"])
        self.assertTrue((self.folder / "new" / "second").exists())
        self.assertTrue((self.folder / "tmp" / "temporary-secret").exists())

    def test_incremental_refresh_only_parses_changed_message_files(self):
        first = self.message("first")
        self.message("second")
        self.refresh()
        original = {row["stable_key"]: row["id"] for row in self.rows()}
        with patch.object(index, "parse_message", wraps=index.parse_message) as parse:
            self.assertEqual(self.refresh().folders_skipped, 1)
        parse.assert_not_called()
        self.message("third", subdir="new")
        with patch.object(index, "parse_message", wraps=index.parse_message) as parse:
            self.refresh()
        self.assertEqual(parse.call_count, 1)
        self.assertTrue(original.items() <= {row["stable_key"]: row["id"] for row in self.rows()}.items())
        self.message("first", body="Changedmaildirtoken")
        with patch.object(index, "parse_message", wraps=index.parse_message) as parse:
            self.refresh()
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(self.rows()[0]["id"], original["mid:<first@example.test>"])
        self.assertIn("Changedmaildirtoken", self.rows()[0]["body"])
        first.unlink()
        self.refresh()
        self.assertEqual(len(self.rows()), 2)

    def test_flag_and_new_to_cur_renames_keep_id_reference_and_original_export(self):
        path = self.message("first", subdir="new", attachment=True)
        self.refresh()
        original = self.rows()[0]
        reference = messages.reference_for(original["stable_key"])
        renamed = self.folder / "cur" / ("first!2,S" if os.name == "nt" else "first:2,S")
        path.rename(renamed)
        # A copied reference remains usable before the next indexing pass.
        result = messages.list_attachments(reference, db_path=self.database)
        self.assertEqual(result["count"], 1)
        self.refresh()
        after = self.rows()[0]
        self.assertEqual(after["id"], original["id"])
        self.assertEqual(after["stable_key"], original["stable_key"])
        self.refresh(full=True)
        self.assertEqual(self.rows()[0]["id"], original["id"])
        destination = self.root / "exported.eml"
        exported = messages.export_email(reference, destination, db_path=self.database)
        self.assertEqual(exported["bytes"], renamed.stat().st_size)
        self.assertEqual(destination.read_bytes(), renamed.read_bytes())

    def test_mbox_to_maildir_conversion_preserves_existing_ids(self):
        other = self.profile / "ImapMail" / "example.test" / "Archive"
        message = EmailMessage()
        message["Message-ID"] = "<converted@example.test>"
        message.set_content("Conversion fixture")
        box = mailbox.mbox(other)
        box.add(message)
        box.flush()
        box.close()
        Path(str(other) + ".msf").touch()
        self.refresh()
        original = next(row for row in self.rows() if row["message_id"] == "<converted@example.test>")
        other.unlink()
        (other / "cur").mkdir(parents=True)
        (other / "tmp").mkdir()
        (other / "cur" / "converted").write_bytes(message.as_bytes())
        self.refresh()
        converted = next(row for row in self.rows() if row["message_id"] == "<converted@example.test>")
        self.assertEqual(converted["id"], original["id"])
        self.assertEqual(converted["stable_key"], original["stable_key"])

    def test_changed_snapshot_rolls_back_folder_and_source_checkpoints(self):
        self.message("first")
        self.refresh()
        original = self.rows()
        iterator = maildir.iter_messages
        def changing(folder, conn, **kwargs):
            yield from iterator(folder, conn, **kwargs)
            self.message("new-during-refresh")
        with patch.object(maildir, "iter_messages", side_effect=changing):
            self.assertEqual(self.refresh(full=True).folders_deferred, 1)
        self.assertEqual(self.rows(), original)
        self.refresh()
        self.assertEqual(len(self.rows()), 2)

    def test_empty_folder_remains_discoverable_and_deleted_files_are_removed(self):
        path = self.message("first")
        self.refresh()
        old_id = self.rows()[0]["id"]
        path.unlink()
        self.refresh()
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(profile.discover_folders(self.profile)), 1)
        self.message("second")
        self.refresh()
        self.assertGreater(self.rows()[0]["id"], old_id)

    def test_source_identity_and_symlinks_never_return_different_content(self):
        path = self.message("first")
        self.refresh()
        original = self.rows()[0]
        self.message("first", mid="replacement")
        conn = index.connect(self.database, read_only=True)
        try:
            with self.assertRaises(LookupError):
                maildir.source_bytes(conn, original, self.folder)
        finally:
            conn.close()
        path.unlink()
        external = self.root / "private-file"
        external.write_text("This is not mail")
        try:
            path.symlink_to(external)
        except OSError:
            self.skipTest("Symlink creation is not permitted on this host")
        self.assertEqual(maildir.snapshot(self.folder).entries, ())


if __name__ == "__main__":
    unittest.main()
