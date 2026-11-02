"""Parse Thunderbird mbox folders into a SQLite FTS5 full-text index."""

from __future__ import annotations

import email
import email.policy
import hashlib
import json
import mailbox
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from collections import defaultdict, deque
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable

from .profile import Folder, discover_folders, find_profile

# Cap stored body length so the index stays manageable on huge newsletters.
MAX_BODY_CHARS = 50_000
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id          INTEGER PRIMARY KEY,
    folder      TEXT NOT NULL,
    sender      TEXT,
    recipients  TEXT,
    subject     TEXT,
    date_ts     INTEGER,            -- unix epoch seconds, 0 if unknown
    message_id  TEXT,
    body        TEXT,
    stable_key  TEXT,
    source_key  INTEGER,
    source_offset INTEGER,
    source_length INTEGER,
    to_header   TEXT,
    cc          TEXT,
    reply_to    TEXT,
    in_reply_to TEXT,
    references_json TEXT DEFAULT '[]',
    attachment_count INTEGER DEFAULT 0,
    body_truncated INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_emails_folder ON emails(folder);
CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date_ts);

CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
    subject, sender, recipients, body,
    content='emails', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

-- Keep the FTS index in sync with the content table.
CREATE TRIGGER IF NOT EXISTS emails_ai AFTER INSERT ON emails BEGIN
    INSERT INTO emails_fts(rowid, subject, sender, recipients, body)
    VALUES (new.id, new.subject, new.sender, new.recipients, new.body);
END;
CREATE TRIGGER IF NOT EXISTS emails_ad AFTER DELETE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, sender, recipients, body)
    VALUES ('delete', old.id, old.subject, old.sender, old.recipients, old.body);
END;
CREATE TRIGGER IF NOT EXISTS emails_au AFTER UPDATE OF subject, sender, recipients, body ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, sender, recipients, body)
    VALUES ('delete', old.id, old.subject, old.sender, old.recipients, old.body);
    INSERT INTO emails_fts(rowid, subject, sender, recipients, body)
    VALUES (new.id, new.subject, new.sender, new.recipients, new.body);
END;

-- Term list for typo correction in search.
CREATE VIRTUAL TABLE IF NOT EXISTS emails_vocab USING fts5vocab(emails_fts, 'row');

CREATE TABLE IF NOT EXISTS folders (
    folder      TEXT PRIMARY KEY,
    path        TEXT,
    size        INTEGER,
    mtime       REAL,
    count       INTEGER,
    indexed_at  INTEGER,
    schema_version INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO index_meta(key, value) VALUES ('revision', 0);
CREATE TRIGGER IF NOT EXISTS emails_revision_ai AFTER INSERT ON emails BEGIN
    UPDATE index_meta SET value = value + 1 WHERE key = 'revision';
END;
CREATE TRIGGER IF NOT EXISTS emails_revision_au AFTER UPDATE ON emails BEGIN
    UPDATE index_meta SET value = value + 1 WHERE key = 'revision';
END;
CREATE TRIGGER IF NOT EXISTS emails_revision_ad AFTER DELETE ON emails BEGIN
    UPDATE index_meta SET value = value + 1 WHERE key = 'revision';
END;
"""

_POLICY = email.policy.default


def default_db_path() -> Path:
    override = os.environ.get("TB_MAIL_INDEX_DB")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / "mail-index.sqlite"


def connect(db_path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(db_path or default_db_path()).expanduser().resolve()
    if read_only:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with private permissions before SQLite can create journal files.
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        conn = sqlite3.connect(path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    if not read_only:
        try:
            _migrate(conn)
        except Exception:
            conn.close()
            raise
    return conn


def normalize_message_id(value: str | None) -> str:
    match = re.search(r"<[^<>\s]+>", value or "")
    return match.group(0) if match else ""


def message_ids(value: str | None) -> list[str]:
    return list(dict.fromkeys(re.findall(r"<[^<>\s]+>", value or "")))


def stable_key(message_id: str | None, sender: str | None, recipients: str | None,
               subject: str | None, date_ts: int | None, body: str | None) -> str:
    """Content identity unaffected by folder moves or Thunderbird status headers."""
    if mid := normalize_message_id(message_id):
        return "mid:" + mid
    content = json.dumps([sender or "", (recipients or "").strip(), subject or "",
                          date_ts or 0, (body or "")[:MAX_BODY_CHARS]], ensure_ascii=False)
    return "sha256:" + hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()


def _migrate(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    had_fts = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='emails_fts'").fetchone())
    conn.executescript(SCHEMA)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(emails)")}
    additions = {
        "stable_key": "TEXT", "source_key": "INTEGER", "source_offset": "INTEGER",
        "source_length": "INTEGER", "to_header": "TEXT", "cc": "TEXT", "reply_to": "TEXT",
        "in_reply_to": "TEXT", "references_json": "TEXT DEFAULT '[]'",
        "attachment_count": "INTEGER DEFAULT 0", "body_truncated": "INTEGER DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE emails ADD COLUMN {name} {definition}")
    if "schema_version" not in {r[1] for r in conn.execute("PRAGMA table_info(folders)")}:
        conn.execute("ALTER TABLE folders ADD COLUMN schema_version INTEGER DEFAULT 0")
    # Older releases updated the FTS index even for unrelated metadata changes.
    conn.execute("DROP TRIGGER IF EXISTS emails_au")
    conn.execute("""CREATE TRIGGER emails_au AFTER UPDATE OF subject, sender, recipients, body
        ON emails BEGIN
        INSERT INTO emails_fts(emails_fts,rowid,subject,sender,recipients,body)
        VALUES ('delete',old.id,old.subject,old.sender,old.recipients,old.body);
        INSERT INTO emails_fts(rowid,subject,sender,recipients,body)
        VALUES (new.id,new.subject,new.sender,new.recipients,new.body); END""")
    for row in conn.execute("SELECT id,message_id,sender,recipients,subject,date_ts,body FROM emails WHERE stable_key IS NULL"):
        key = stable_key(row["message_id"], row["sender"], row["recipients"], row["subject"], row["date_ts"], row["body"])
        conn.execute("UPDATE emails SET stable_key = ?, body_truncated = ? WHERE id = ?",
                     (key, int(len(row["body"] or "") >= MAX_BODY_CHARS), row["id"]))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_stable_key ON emails(stable_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_message_id ON emails(message_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_in_reply_to ON emails(in_reply_to)")
    conn.execute("CREATE TABLE IF NOT EXISTS message_links (email_id INTEGER NOT NULL, message_id TEXT NOT NULL, PRIMARY KEY(email_id,message_id))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_message_links_mid ON message_links(message_id)")
    conn.execute("CREATE TRIGGER IF NOT EXISTS emails_links_ad AFTER DELETE ON emails BEGIN DELETE FROM message_links WHERE email_id=old.id; END")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'").fetchone():
        conn.execute("""CREATE TRIGGER IF NOT EXISTS embeddings_invalidate
            AFTER UPDATE OF subject,sender,body ON emails
            WHEN old.subject IS NOT new.subject OR old.sender IS NOT new.sender OR old.body IS NOT new.body BEGIN
            DELETE FROM embeddings WHERE key IN (old.stable_key,old.message_id); END""")
    conn.execute("INSERT OR IGNORE INTO index_meta(key,value) SELECT 'next_message_id',COALESCE(MAX(id),0)+1 FROM emails")
    if not had_fts:
        conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('rebuild')")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


# --- message parsing --------------------------------------------------------


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self._parts).split())


def _html_to_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        return html
    return p.text()


def _extract_body(msg) -> str:
    try:
        if msg.is_multipart():
            part = msg.get_body(preferencelist=("plain", "html"))
            if part is None:
                return ""
            content = part.get_content()
            if part.get_content_type() == "text/html":
                content = _html_to_text(content)
            return content
        ctype = msg.get_content_type()
        if ctype == "text/html":
            return _html_to_text(msg.get_content())
        if ctype.startswith("text/"):
            return msg.get_content()
        return ""
    except Exception:
        # Last-ditch fallback for messages the modern API chokes on.
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode("utf-8", "replace")
        except Exception:
            pass
        return ""


def _header(msg, name: str) -> str:
    try:
        value = msg[name]
        return str(value).strip() if value is not None else ""
    except Exception:
        return ""


def _date_ts(msg) -> int:
    raw = _header(msg, "date")
    if not raw:
        return 0
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp()) if dt else 0
    except Exception:
        return 0


@dataclass
class ParsedMessage:
    sender: str
    recipients: str
    subject: str
    date_ts: int
    message_id: str
    body: str
    stable_key: str = ""
    source_key: int | None = None
    source_offset: int | None = None
    source_length: int | None = None
    to_header: str = ""
    cc: str = ""
    reply_to: str = ""
    in_reply_to: str = ""
    references_json: str = "[]"
    attachment_count: int = 0
    body_truncated: int = 0


def parse_message(msg, **location) -> ParsedMessage:
    body = _extract_body(msg) or ""
    to, cc = _header(msg, "to"), _header(msg, "cc")
    item = ParsedMessage(
        sender=_header(msg, "from"), recipients=to + " " + cc,
        subject=_header(msg, "subject"), date_ts=_date_ts(msg),
        message_id=normalize_message_id(_header(msg, "message-id")),
        body=body[:MAX_BODY_CHARS], to_header=to, cc=cc,
        reply_to=_header(msg, "reply-to"),
        in_reply_to=normalize_message_id(_header(msg, "in-reply-to")),
        references_json=json.dumps(message_ids(_header(msg, "references"))),
        attachment_count=sum(1 for p in msg.walk() if not p.is_multipart()
                             and (p.get_content_disposition() == "attachment" or p.get_filename())),
        body_truncated=int(len(body) > MAX_BODY_CHARS), **location,
    )
    item.stable_key = stable_key(item.message_id, item.sender, item.recipients,
                                item.subject, item.date_ts, item.body)
    return item


def _iter_messages(mbox_path: Path) -> Iterable[ParsedMessage]:
    box = mailbox.mbox(
        str(mbox_path),
        factory=lambda f: email.message_from_binary_file(f, policy=_POLICY),
        create=False,
    )
    try:
        for key in box.iterkeys():
            msg = box[key]
            try:
                expunged = int(_header(msg, "x-mozilla-status") or "0", 16) & 0x0008
            except ValueError:
                expunged = False
            if expunged:
                continue
            # mbox has no public offset API. PartialFile's bounds are optional
            # acceleration only; readers validate identity and can scan instead.
            with box.get_file(key) as source:
                offset = getattr(source, "_start", None)
                stop = getattr(source, "_stop", None)
            yield parse_message(msg, source_key=key, source_offset=offset,
                                source_length=stop - offset if offset is not None and stop is not None else None)
    finally:
        box.close()


# --- indexing ---------------------------------------------------------------


@dataclass
class IndexStats:
    folders_indexed: int = 0
    folders_skipped: int = 0
    messages_added: int = 0
    seconds: float = 0.0
    folders_deferred: int = 0


def reindex(
    db_path: Path | None = None,
    *,
    full: bool = False,
    profile: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> IndexStats:
    """Refresh the index atomically per folder while preserving message IDs.

    A folder is re-parsed only when its mbox size or mtime changed since the
    last run, so day-to-day reindexing is cheap.
    """
    started = time.time()
    profile = profile or find_profile()
    folders = discover_folders(profile)
    stats = IndexStats()

    conn = connect(db_path)
    try:
        seen = {row["folder"]: row for row in conn.execute("SELECT * FROM folders")}

        for folder in folders:
            prior = seen.get(folder.name)
            if (
                not full
                and prior is not None
                and prior["schema_version"] == SCHEMA_VERSION
                and prior["path"] == str(folder.path)
                and prior["size"] == folder.size
                and prior["mtime"] == folder.mtime
            ):
                stats.folders_skipped += 1
                continue

            if progress:
                progress(f"indexing {folder.name} ({folder.size // (1024*1024)} MB)")

            conn.execute("BEGIN IMMEDIATE")
            existing = defaultdict(deque)
            for row in conn.execute("SELECT id,stable_key FROM emails WHERE folder=? ORDER BY id", (folder.name,)):
                existing[row["stable_key"]].append(row["id"])
            next_id = max(conn.execute("SELECT value FROM index_meta WHERE key='next_message_id'").fetchone()[0],
                          conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM emails").fetchone()[0])
            count = 0
            for m in _iter_messages(folder.path):
                row_id = existing[m.stable_key].popleft() if existing[m.stable_key] else next_id
                if row_id == next_id:
                    next_id += 1
                fields = ("folder", *m.__dataclass_fields__)
                values = (folder.name, *(getattr(m, f) for f in m.__dataclass_fields__))
                assignments = ",".join(f"{f}=excluded.{f}" for f in fields)
                changed = " OR ".join(f"emails.{f} IS NOT excluded.{f}" for f in fields)
                conn.execute(
                    f"INSERT INTO emails(id,{','.join(fields)}) VALUES ({','.join('?' for _ in range(len(values)+1))}) "
                    f"ON CONFLICT(id) DO UPDATE SET {assignments} WHERE {changed}", (row_id, *values))
                links = message_ids(m.in_reply_to) + json.loads(m.references_json)
                conn.execute("DELETE FROM message_links WHERE email_id=?", (row_id,))
                conn.executemany("INSERT OR IGNORE INTO message_links(email_id,message_id) VALUES (?,?)",
                                 [(row_id, link) for link in links])
                count += 1
            after = folder.path.stat()
            if after.st_size != folder.size or after.st_mtime != folder.mtime:
                conn.rollback()
                stats.folders_deferred += 1
                if progress:
                    progress(f"deferred changing folder {folder.name}; retaining prior index")
                continue
            for ids in existing.values():
                conn.executemany("DELETE FROM emails WHERE id=?", [(i,) for i in ids])
            conn.execute("UPDATE index_meta SET value=? WHERE key='next_message_id'", (next_id,))
            conn.execute(
                "INSERT INTO folders(folder, path, size, mtime, count, indexed_at, schema_version)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(folder) DO UPDATE SET"
                " path=excluded.path, size=excluded.size, mtime=excluded.mtime,"
                " count=excluded.count, indexed_at=excluded.indexed_at, schema_version=excluded.schema_version",
                (folder.name, str(folder.path), folder.size, folder.mtime, count, int(time.time()), SCHEMA_VERSION),
            )
            conn.commit()
            stats.folders_indexed += 1
            stats.messages_added += count

        # Drop rows for folders that no longer exist on disk.
        live = {f.name for f in folders}
        stale = [name for name in seen if name not in live]
        for name in stale:
            conn.execute("DELETE FROM emails WHERE folder = ?", (name,))
            conn.execute("DELETE FROM folders WHERE folder = ?", (name,))
        conn.execute("UPDATE folders SET count=(SELECT count(*) FROM emails WHERE emails.folder=folders.folder)")
        conn.commit()
    finally:
        conn.close()

    stats.seconds = round(time.time() - started, 1)
    return stats


def cli() -> None:
    """`thunderbird-mcp-index` entry point: build the index from the terminal."""
    full = "--full" in sys.argv
    stats = reindex(full=full, progress=lambda s: print(s, file=sys.stderr))
    print(
        f"done: {stats.messages_added} messages from {stats.folders_indexed} folders"
        f" ({stats.folders_skipped} unchanged) in {stats.seconds}s"
    )


if __name__ == "__main__":
    cli()
