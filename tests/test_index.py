"""Synthetic mail stores exercise index stability without reading personal mail."""

import mailbox
import json
import os
import sqlite3
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from thunderbird_mcp import index


def mail(mid="one", subject="Project update", body="A synthetic project message.", **headers):
    msg = EmailMessage()
    msg["From"] = "Sender <sender@example.test>"
    msg["To"] = "Reader <reader@example.test>"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 02 Nov 2026 10:00:00 +0000"
    if mid is not None:
        msg["Message-ID"] = f"<{mid}@example.test>"
    for name, value in headers.items():
        if name in msg:
            msg.replace_header(name, value)
        else:
            msg[name] = value
    msg.set_content(body)
    return msg


class MailFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "profile"
        self.account = self.profile / "ImapMail" / "example.test"
        self.account.mkdir(parents=True)
        self.db = self.root / "index.sqlite"

    def folder(self, name, messages):
        path = self.account / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        box = mailbox.mbox(path)
        try:
            for msg in messages:
                box.add(msg)
            box.flush()
        finally:
            box.close()
        Path(str(path) + ".msf").touch()
        return path

    def refresh(self, **kwargs):
        return index.reindex(db_path=self.db, profile=self.profile, **kwargs)

    def rows(self):
        conn = index.connect(self.db, read_only=True)
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM emails ORDER BY id")]
        finally:
            conn.close()


class IndexTests(MailFixture):
    def test_attachment_text_stays_separate_and_refresh_removes_old_terms(self):
        message = mail(None, body="Ordinary body")
        message.add_attachment(b"attachmentonlyquasar", maintype="text", subtype="plain", filename="budget.txt")
        self.folder("INBOX", [message])
        self.refresh()
        before = self.rows()[0]
        self.assertEqual(before["body"].strip(), "Ordinary body")
        self.assertIn("attachmentonlyquasar", before["attachment_text"])
        self.assertEqual(json.loads(before["attachments_json"])[0]["status"], "extracted")
        self.assertEqual(before["attachment_count"], 1)
        self.assertEqual(before["attachments_indexed"], 1)
        plain = mail(None, body="Ordinary body")
        self.folder("INBOX", [plain])
        self.refresh()
        after = self.rows()[0]
        self.assertEqual(after["stable_key"], before["stable_key"])
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["attachment_text"], "")
        conn = index.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM emails_fts WHERE emails_fts MATCH 'attachmentonlyquasar'").fetchone()[0], 0)
            conn.execute("INSERT INTO emails_fts(emails_fts,rank) VALUES('integrity-check',1)")
        finally:
            conn.close()

    def test_identity_parse_can_skip_attachment_extraction(self):
        message = mail()
        message.add_attachment(b"synthetic", maintype="text", subtype="plain", filename="note.txt")
        with patch.object(index, "summarise_message", side_effect=AssertionError("must not extract")):
            parsed = index.parse_message(message, extract_attachments=False)
        self.assertEqual(parsed.attachments_indexed, 0)
        self.assertEqual(parsed.attachment_count, 1)

    def test_v2_fts_migration_preserves_content_ids_and_refreshes_attachments(self):
        self.folder("INBOX", [mail("one", body="existingquasar")])
        self.refresh()
        original = self.rows()[0]
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            DROP TRIGGER emails_ai; DROP TRIGGER emails_ad; DROP TRIGGER emails_au;
            DROP TABLE emails_vocab; DROP TABLE emails_fts;
            CREATE VIRTUAL TABLE emails_fts USING fts5(subject,sender,recipients,body,content='emails',content_rowid='id');
            INSERT INTO emails_fts(emails_fts) VALUES('rebuild');
            CREATE VIRTUAL TABLE emails_vocab USING fts5vocab(emails_fts,'row');
            UPDATE folders SET schema_version=2;
            PRAGMA user_version=2;
        """)
        conn.close()
        conn = index.connect(self.db)
        try:
            row = dict(conn.execute("SELECT * FROM emails").fetchone())
            self.assertEqual(row, original)
            self.assertEqual(conn.execute("SELECT count(*) FROM emails_fts WHERE emails_fts MATCH 'existingquasar'").fetchone()[0], 1)
            self.assertIn("attachment_text", {r[1] for r in conn.execute("PRAGMA table_info(emails_fts)")})
            conn.execute("INSERT INTO emails_fts(emails_fts,rank) VALUES('integrity-check',1)")
        finally:
            conn.close()
        self.assertEqual(self.refresh().folders_indexed, 1)
        self.assertEqual(self.rows()[0]["id"], original["id"])

    def test_failed_fts_migration_rolls_back_search_index(self):
        self.folder("INBOX", [mail(body="retainedquasar")])
        self.refresh()
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            DROP TRIGGER emails_ai; DROP TRIGGER emails_ad; DROP TRIGGER emails_au;
            DROP TABLE emails_vocab; DROP TABLE emails_fts;
            CREATE VIRTUAL TABLE emails_fts USING fts5(subject,sender,recipients,body,content='emails',content_rowid='id');
            INSERT INTO emails_fts(emails_fts) VALUES('rebuild');
            CREATE VIRTUAL TABLE emails_vocab USING fts5vocab(emails_fts,'row');
            PRAGMA user_version=2;
        """)
        conn.close()

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.startswith("CREATE TRIGGER emails_ai"):
                    raise sqlite3.OperationalError("Synthetic interrupted migration")
                return super().execute(sql, *args, **kwargs)

        conn = sqlite3.connect(self.db, factory=FailingConnection)
        conn.row_factory = sqlite3.Row
        with self.assertRaises(sqlite3.OperationalError):
            index._migrate(conn)
        conn.close()
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM emails_fts WHERE emails_fts MATCH 'retainedquasar'").fetchone()[0], 1)
            self.assertNotIn("attachment_text", {r[1] for r in conn.execute("PRAGMA table_info(emails_fts)")})
        finally:
            conn.close()

    def test_incremental_and_full_refresh_keep_ids_and_references(self):
        self.folder("INBOX", [mail("one"), mail(None, body="No Message-ID")])
        self.refresh()
        before = {r["stable_key"]: r["id"] for r in self.rows()}
        self.folder("INBOX", [mail("one"), mail(None, body="No Message-ID"), mail("two")])
        self.refresh()
        self.assertTrue(before.items() <= {r["stable_key"]: r["id"] for r in self.rows()}.items())
        self.refresh(full=True)
        self.assertTrue(before.items() <= {r["stable_key"]: r["id"] for r in self.rows()}.items())
        self.assertEqual(self.refresh().folders_skipped, 1)

    def test_removed_ids_are_never_reused(self):
        self.folder("INBOX", [mail("one"), mail("two")])
        self.refresh()
        deleted_id = self.rows()[-1]["id"]
        self.folder("INBOX", [mail("one")])
        self.refresh()
        self.folder("INBOX", [mail("one"), mail("three")])
        self.refresh()
        self.assertGreater(self.rows()[-1]["id"], deleted_id)

    def test_body_changes_update_fts_without_changing_identity(self):
        self.folder("INBOX", [mail(body="beforetoken")])
        self.refresh()
        before = self.rows()[0]
        self.folder("INBOX", [mail(body="aftertoken")])
        self.refresh()
        self.assertEqual(self.rows()[0]["id"], before["id"])
        conn = index.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM emails_fts WHERE emails_fts MATCH 'beforetoken'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM emails_fts WHERE emails_fts MATCH 'aftertoken'").fetchone()[0], 1)
            conn.execute("INSERT INTO emails_fts(emails_fts,rank) VALUES('integrity-check',1)")
        finally:
            conn.close()

    def test_parse_failure_preserves_previous_folder_transaction(self):
        self.folder("INBOX", [mail()])
        self.refresh()
        original = self.rows()
        with patch.object(index, "_iter_messages", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                self.refresh(full=True)
        self.assertEqual(self.rows(), original)

    def test_changing_folder_is_deferred(self):
        path = self.folder("INBOX", [mail()])
        self.refresh()
        original = self.rows()
        parser = index._iter_messages
        def changing(source):
            yield from parser(source)
            with path.open("ab") as stream:
                stream.write(b"\n")
        with patch.object(index, "_iter_messages", side_effect=changing):
            self.assertEqual(self.refresh(full=True).folders_deferred, 1)
        self.assertEqual(self.rows(), original)

    def test_deleted_thunderbird_messages_are_not_indexed(self):
        self.folder("INBOX", [mail("one"), mail("two", **{"X-Mozilla-Status": "0008"})])
        self.refresh()
        self.assertEqual(len(self.rows()), 1)

    def test_missing_message_id_fallback_distinguishes_bodies(self):
        self.folder("INBOX", [mail(None, body="first"), mail(None, body="second")])
        self.refresh()
        self.assertEqual(len({r["stable_key"] for r in self.rows()}), 2)

    def test_new_database_has_private_permissions(self):
        self.folder("INBOX", [mail()])
        self.refresh()
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)

    def test_legacy_schema_migrates_without_losing_id(self):
        conn = sqlite3.connect(self.db)
        conn.executescript("""CREATE TABLE emails(id INTEGER PRIMARY KEY,folder TEXT,sender TEXT,
            recipients TEXT,subject TEXT,date_ts INTEGER,message_id TEXT,body TEXT);
            CREATE TABLE folders(folder TEXT PRIMARY KEY,path TEXT,size INTEGER,mtime REAL,count INTEGER,indexed_at INTEGER);
            INSERT INTO emails VALUES(42,'example/INBOX','sender','reader','Legacy',0,'<legacy@example.test>','old body');""")
        conn.close()
        conn = index.connect(self.db)
        try:
            row = conn.execute("SELECT * FROM emails").fetchone()
            self.assertEqual(row["id"], 42)
            self.assertEqual(row["stable_key"], "mid:<legacy@example.test>")
            self.assertEqual(conn.execute("SELECT count(*) FROM emails_fts WHERE emails_fts MATCH 'Legacy'").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], index.SCHEMA_VERSION)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
