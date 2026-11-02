"""Read-only Thunderbird/standard Maildir adapter with per-file checkpoints.

Thunderbird uses cur/tmp; standard Maildir also uses new. Temporary files are
never indexed, moved, renamed, marked read, or removed by this adapter.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .profile import Folder, _folder_name


MAX_MESSAGE_BYTES = 100 * 1024 * 1024


class SnapshotChanged(OSError):
    """Maildir changed while being read; retain the previous folder transaction."""


@dataclass(frozen=True)
class Entry:
    relative: str
    signature: str
    size: int
    mtime_ns: int


@dataclass
class MaildirFolder(Folder):
    entries: tuple[Entry, ...] = ()
    fingerprint: str = ""


def is_maildir(path: Path) -> bool:
    path = Path(path)
    return path.is_dir() and (path / "cur").is_dir() and ((path / "tmp").is_dir() or (path / "new").is_dir())


def _signature(state) -> str:
    fields = [state.st_size, state.st_mtime_ns, state.st_ctime_ns, state.st_ino, state.st_dev]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def snapshot(path: Path, name: str = "") -> MaildirFolder:
    path = Path(path)
    entries = []
    for subdir in ("cur", "new"):
        directory = path / subdir
        if not directory.is_dir():
            continue
        if directory.is_symlink():
            raise ValueError("Maildir cur/new directories must not be symlinks")
        try:
            for candidate in directory.iterdir():
                if candidate.name.startswith("."):
                    continue
                state = candidate.lstat()
                if not stat.S_ISREG(state.st_mode):
                    continue
                entries.append(Entry(candidate.relative_to(path).as_posix(), _signature(state), state.st_size, state.st_mtime_ns))
        except FileNotFoundError as exc:
            raise SnapshotChanged("Maildir changed during enumeration") from exc
    entries.sort(key=lambda item: item.relative)
    fingerprint = hashlib.sha256(json.dumps([(e.relative, e.signature) for e in entries], separators=(",", ":")).encode()).hexdigest()
    return MaildirFolder(name=name, path=path, size=sum(e.size for e in entries),
                         mtime=max((e.mtime_ns for e in entries), default=0) / 1_000_000_000,
                         entries=tuple(entries), fingerprint=fingerprint)


def discover(profile: Path) -> list[MaildirFolder]:
    """Discover only stores beneath Thunderbird's mail roots, including empty ones."""
    folders = []
    for name in ("ImapMail", "Mail"):
        root = profile / name
        if not root.is_dir():
            continue
        for current, children, _ in os.walk(root, followlinks=False):
            path = Path(current)
            children[:] = [child for child in children if child not in {"cur", "new", "tmp"}
                           and not (path / child).is_symlink()]
            if is_maildir(path):
                folders.append(snapshot(path, _folder_name(profile, path)))
    return sorted(folders, key=lambda folder: folder.name.casefold())


def ensure_schema(conn) -> None:
    """Add adapter checkpoints without changing existing message rows or IDs."""
    conn.execute("CREATE TABLE IF NOT EXISTS maildir_folders(folder TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS maildir_entries(
        folder TEXT NOT NULL, relative TEXT NOT NULL, signature TEXT NOT NULL,
        email_id INTEGER NOT NULL, PRIMARY KEY(folder,relative))""")
    conn.execute("CREATE INDEX IF NOT EXISTS maildir_entries_email ON maildir_entries(email_id)")


def fingerprint_matches(conn, folder: MaildirFolder) -> bool:
    row = conn.execute("SELECT fingerprint FROM maildir_folders WHERE folder=?", (folder.name,)).fetchone()
    return row is not None and row[0] == folder.fingerprint


def read_entry(folder: Path, entry: Entry) -> bytes:
    folder = Path(folder).resolve()
    parts = entry.relative.split("/")
    if len(parts) != 2 or parts[0] not in {"cur", "new"} or parts[1] in {"", ".", ".."} or "\\" in parts[1]:
        raise ValueError("Invalid Maildir source path")
    if entry.size > MAX_MESSAGE_BYTES:
        raise ValueError("Maildir message exceeds the 100 MiB limit")
    source = folder / parts[0] / parts[1]
    if source.parent.is_symlink() or source.is_symlink():
        raise SnapshotChanged("Maildir source became a symlink")
    try:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or _signature(before) != entry.signature:
                raise SnapshotChanged("Maildir message changed before reading")
            raw = stream.read(MAX_MESSAGE_BYTES + 1)
            if len(raw) > MAX_MESSAGE_BYTES:
                raise ValueError("Maildir message exceeds the 100 MiB limit")
            if _signature(os.fstat(stream.fileno())) != entry.signature or len(raw) != entry.size:
                raise SnapshotChanged("Maildir message changed while reading")
        return raw
    except FileNotFoundError as exc:
        raise SnapshotChanged("Maildir message moved while reading") from exc


def iter_messages(folder: MaildirFolder, conn, *, full: bool = False):
    """Reuse unchanged parsed rows; only new/changed files run MIME extraction."""
    from . import index
    previous = {row["relative"]: row for row in conn.execute(
        "SELECT * FROM maildir_entries WHERE folder=?", (folder.name,))}
    fields = tuple(index.ParsedMessage.__dataclass_fields__)
    for entry in folder.entries:
        old = previous.get(entry.relative)
        cached = None
        if old is not None and old["signature"] == entry.signature and not full:
            cached = conn.execute("SELECT * FROM emails WHERE id=? AND folder=?", (old["email_id"], folder.name)).fetchone()
        if cached is not None:
            parsed = index.ParsedMessage(**{field: cached[field] for field in fields})
        else:
            raw = read_entry(folder.path, entry)
            message = email.message_from_bytes(raw, policy=email.policy.default)
            try:
                expunged = int(index._header(message, "x-mozilla-status") or "0", 16) & 0x0008
            except ValueError:
                expunged = False
            if expunged:
                continue
            parsed = index.parse_message(message, source_length=len(raw))
        parsed._maildir_entry = entry
        parsed._maildir_previous_id = old["email_id"] if old is not None else None
        yield parsed


def record_entry(conn, folder: MaildirFolder, parsed, email_id: int) -> None:
    entry = parsed._maildir_entry
    conn.execute("INSERT INTO maildir_entries(folder,relative,signature,email_id) VALUES(?,?,?,?) "
                 "ON CONFLICT(folder,relative) DO UPDATE SET signature=excluded.signature,email_id=excluded.email_id",
                 (folder.name, entry.relative, entry.signature, email_id))


def finish_folder(conn, folder: MaildirFolder) -> bool:
    """Validate the entire snapshot before the surrounding transaction commits."""
    if snapshot(folder.path, folder.name).fingerprint != folder.fingerprint:
        return False
    active = {entry.relative for entry in folder.entries}
    for row in conn.execute("SELECT relative,email_id FROM maildir_entries WHERE folder=?", (folder.name,)).fetchall():
        if row["relative"] not in active or conn.execute("SELECT 1 FROM emails WHERE id=?", (row["email_id"],)).fetchone() is None:
            conn.execute("DELETE FROM maildir_entries WHERE folder=? AND relative=?", (folder.name, row["relative"]))
    conn.execute("INSERT INTO maildir_folders(folder,fingerprint) VALUES(?,?) "
                 "ON CONFLICT(folder) DO UPDATE SET fingerprint=excluded.fingerprint", (folder.name, folder.fingerprint))
    return True


def source_bytes(conn, row, folder: Path, *, maximum: int = MAX_MESSAGE_BYTES) -> bytes:
    """Resolve the original by stable identity, including flag/new→cur renames."""
    from . import index
    current = snapshot(Path(folder))
    preferred = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='maildir_entries'").fetchone():
        stored = conn.execute("SELECT relative FROM maildir_entries WHERE email_id=?", (row["id"],)).fetchone()
        preferred = stored[0] if stored else None
    candidates = sorted(current.entries, key=lambda entry: entry.relative != preferred)
    for entry in candidates:
        if entry.size > maximum:
            continue
        try:
            raw = read_entry(folder, entry)
        except SnapshotChanged:
            continue
        message = email.message_from_bytes(raw, policy=email.policy.default)
        try:
            if int(index._header(message, "x-mozilla-status") or "0", 16) & 0x0008:
                continue
        except ValueError:
            pass
        if index.parse_message(message, extract_attachments=False).stable_key == row["stable_key"]:
            return raw
    raise LookupError("Original message is unavailable in the current Maildir; refresh the index")
