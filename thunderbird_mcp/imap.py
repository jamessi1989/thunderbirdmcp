"""Write operations (folders, moves, tags) against the IMAP server.

Changes go to the server, never to Thunderbird's local mbox/.msf files, so a
running Thunderbird simply syncs them. Every move is logged so it can be undone.
"""

from __future__ import annotations

import imaplib
import json
import os
import re
import ssl
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import index as _index
from .outgoing import account_config, password

CHUNK = 500

_LIST_RE = re.compile(r'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)')
_MSGID_RE = re.compile(rb"Message-ID:\s*(<[^>\r\n]+>)", re.I)


def undo_log_path() -> Path:
    return _index.default_db_path().parent / "moves.jsonl"


def _password() -> str:
    return password(account_config(), "imap")


def _q(name: str) -> str:
    if not isinstance(name, str) or not name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("Mailbox names must be nonempty text without control characters")
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


@contextmanager
def session(*, config: dict | None = None, require_move: bool = False):
    config = config or account_config()
    if not config["imap_host"] or not config["imap_user"]:
        raise ValueError("No IMAP account configured")
    secret = password(config, "imap")
    context = ssl.create_default_context()
    m = None
    try:
        if config["imap_tls"] == "ssl":
            m = imaplib.IMAP4_SSL(config["imap_host"], config["imap_port"],
                                  ssl_context=context, timeout=config["timeout"])
        else:
            m = imaplib.IMAP4(config["imap_host"], config["imap_port"], timeout=config["timeout"])
            _check(m.starttls(ssl_context=context), "STARTTLS")
        _check(m.login(config["imap_user"], secret), "LOGIN")
        # imaplib keeps the pre-auth capability list; Dovecot only advertises MOVE after login.
        caps = _check(m.capability(), "CAPABILITY")[0].decode().upper().split()
        if require_move and "MOVE" not in caps:
            raise RuntimeError("IMAP server lacks MOVE capability")
        yield m
    finally:
        try:
            if m is not None:
                m.logout()
        except Exception:
            pass


def _check(resp, what: str):
    typ, data = resp
    if typ != "OK":
        raise RuntimeError(f"{what} failed ({typ})")
    return data


def list_folders(m: imaplib.IMAP4) -> list[str]:
    names = []
    for line in _check(m.list(), "LIST"):
        if not isinstance(line, bytes):
            continue
        mt = _LIST_RE.match(line.decode("utf-8", errors="replace"))
        if mt:
            if "\\noselect" in mt["flags"].lower().split():
                continue
            name = mt["name"]
            if name.startswith('"') and name.endswith('"'):
                name = re.sub(r"\\(.)", r"\1", name[1:-1])
            names.append(name)
    return sorted(names)


def resolve_folder(m: imaplib.IMAP4, local: str) -> str:
    """Map an index folder name (e.g. 'mail.bsc.es/BSC RT-1') to a server folder.

    Thunderbird appends '-N' to local dirs on name clashes, so fall back to the
    name without that suffix when the exact one does not exist on the server.
    """
    prefix = account_config()["account_prefix"]
    name = local[len(prefix):] if local.startswith(prefix) else local
    server = set(list_folders(m))
    for cand in (name, re.sub(r"-\d+$", "", name)):
        if cand in server:
            return cand
    raise RuntimeError(f"No server folder matches {local!r}")


def _uid_map(m: imaplib.IMAP4, folder: str, readonly: bool) -> dict[str, list[bytes]]:
    """Message-ID -> UIDs for every message in folder."""
    _check(m.select(_q(folder), readonly=readonly), f"SELECT {folder}")
    data = _check(m.uid("FETCH", "1:*", "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"), "FETCH")
    out: dict[str, list[bytes]] = {}
    for item in data:
        if not isinstance(item, tuple):
            continue
        uid = re.search(rb"UID (\d+)", item[0])
        mid = _MSGID_RE.search(item[1])
        if uid and mid:
            out.setdefault(mid.group(1).decode().strip(), []).append(uid.group(1))
    return out


def _rows(ids: list[int]) -> list:
    if not ids:
        return []
    conn = _index.connect(read_only=True)
    try:
        q = f"SELECT id, folder, message_id FROM emails WHERE id IN ({','.join('?' * len(ids))})"
        return conn.execute(q, ids).fetchall()
    finally:
        conn.close()


def _move_by_msgid(m, src: str, dest: str, msgids: list[str], on_moved=None) -> tuple[list[str], list[str]]:
    uids_by_mid = _uid_map(m, src, readonly=False)
    found, missing, uids = [], [], []
    for mid in dict.fromkeys(msgids):
        if mid in uids_by_mid:
            if len(uids_by_mid[mid]) != 1:
                raise RuntimeError("Multiple server messages share a Message-ID; refusing an ambiguous move")
            found.append(mid)
            uids.extend(uids_by_mid[mid])
        else:
            missing.append(mid)
    for i in range(0, len(uids), CHUNK):
        chunk = b",".join(uids[i:i + CHUNK]).decode()
        _check(m.uid("MOVE", chunk, _q(dest)), f"MOVE to {dest}")
        if on_moved is not None:
            # One UID per Message-ID was checked above. Persist each completed
            # chunk so a later failure cannot erase the undo trail.
            on_moved(found[i:i + CHUNK])
    return found, missing


def _reflect_in_index(msgids: list[str], from_local: str, to_local: str) -> None:
    # Scoped to the source folder: the same Message-ID often also lives in Sent.
    if not msgids:
        return
    conn = _index.connect()
    try:
        for i in range(0, len(msgids), CHUNK):
            part = msgids[i:i + CHUNK]
            conn.execute(
                "UPDATE emails SET folder = ? WHERE folder = ? AND "
                f"message_id IN ({','.join('?' * len(part))})",
                [to_local, from_local, *part],
            )
        conn.commit()
    finally:
        conn.close()


def move_emails(ids: list[int], dest: str, dry_run: bool = True) -> dict:
    _q(dest)
    if not isinstance(ids, list) or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in ids):
        raise ValueError("ids must be a list of positive integer message ids")
    if not ids:
        return {"dry_run": dry_run, "dest": dest, "moved": {}, "unknown_ids": []}
    rows = _rows(ids)
    unknown = sorted(set(ids) - {r["id"] for r in rows})
    no_msgid = [r["id"] for r in rows if not r["message_id"]]
    by_folder: dict[str, list[str]] = {}
    for r in rows:
        if r["message_id"]:
            by_folder.setdefault(r["folder"], []).append(r["message_id"])

    with session(require_move=not dry_run) as m:
        if dest not in list_folders(m):
            raise RuntimeError(f"Destination {dest!r} does not exist; create_folder first")
        plan = [(f, resolve_folder(m, f), mids) for f, mids in by_folder.items()]
        if dry_run:
            return {
                "dry_run": True,
                "dest": dest,
                "would_move": {src: len(mids) for _, src, mids in plan},
                "unknown_ids": unknown,
                "ids_without_message_id": no_msgid,
            }
        batch = uuid.uuid4().hex[:8]
        moved, missing = {}, []
        log_path = undo_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a") as log:
            for src_local, src, mids in plan:
                if src == dest:
                    continue
                def record_chunk(found):
                    log.write(json.dumps({"batch": batch, "ts": int(time.time()), "src": src,
                                          "src_local": src_local, "dest": dest,
                                          "message_ids": found}) + "\n")
                    log.flush()
                    os.fsync(log.fileno())
                    moved[src] = moved.get(src, 0) + len(found)
                    _reflect_in_index(found, src_local, account_config()["account_prefix"] + dest)

                try:
                    _, miss = _move_by_msgid(m, src, dest, mids, on_moved=record_chunk)
                except Exception as exc:
                    return {"batch": batch, "dest": dest, "moved": moved,
                            "complete": False, "error": type(exc).__name__,
                            "note": "The move stopped. Completed chunks are logged for undo; verify server state before retrying.",
                            "unknown_ids": unknown, "ids_without_message_id": no_msgid}
                missing += miss
    return {"batch": batch, "dest": dest, "moved": moved, "not_found_on_server": len(missing),
            "unknown_ids": unknown, "ids_without_message_id": no_msgid}


def undo_batch(batch: str) -> dict:
    entries = []
    path = undo_log_path()
    if path.exists():
        entries = [e for e in map(json.loads, path.read_text().splitlines()) if e["batch"] == batch]
    if not entries:
        return {"error": f"No moves logged for batch {batch!r}"}
    restored = {}
    with session(require_move=True) as m:
        for e in entries:
            def record_restored(found):
                restored[e["src"]] = restored.get(e["src"], 0) + len(found)
                _reflect_in_index(found, account_config()["account_prefix"] + e["dest"], e["src_local"])

            try:
                _move_by_msgid(m, e["dest"], e["src"], e["message_ids"], on_moved=record_restored)
            except Exception as exc:
                return {"batch": batch, "restored": restored, "complete": False,
                        "error": type(exc).__name__, "note": "Undo stopped; verify server state before retrying."}
    return {"batch": batch, "restored": restored}


def create_folder(name: str) -> dict:
    _q(name)
    with session() as m:
        if name in list_folders(m):
            return {"folder": name, "created": False}
        _check(m.create(_q(name)), f"CREATE {name}")
        subscribed = m.subscribe(_q(name))[0] == "OK"
        return {"folder": name, "created": True, "subscribed": subscribed}


def server_folders() -> dict:
    with session() as m:
        out = []
        for name in list_folders(m):
            typ, data = m.status(_q(name), "(MESSAGES)")
            match = re.search(rb"MESSAGES (\d+)", data[0]) if typ == "OK" and data and isinstance(data[0], bytes) else None
            count = int(match.group(1)) if match else None
            out.append({"folder": name, "messages": count})
        return {"folders": out}


def send_email(to: list[str], subject: str, body: str, cc: list[str] | None = None,
               dry_run: bool = True, **kwargs) -> dict:
    from .outgoing import send_email as _send_email
    return _send_email(to, subject, body, cc=cc, dry_run=dry_run, **kwargs)
