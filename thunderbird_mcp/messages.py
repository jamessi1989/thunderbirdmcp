"""Stable mail references, conversation lookup, and read-only source exports."""

from __future__ import annotations

import datetime as dt
import email
import hashlib
import json
import mailbox
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from . import index

REFERENCE_PREFIX = "thunderbird://message/"
MAX_RAW_BYTES = 100 * 1024 * 1024


def reference_for(key: str) -> str:
    return REFERENCE_PREFIX + quote(key, safe="")


def key_for_row(row) -> str:
    return row["stable_key"] or index.stable_key(
        row["message_id"], row["sender"], row["recipients"], row["subject"], row["date_ts"], row["body"])


def _key(reference: str) -> str:
    if reference.startswith(REFERENCE_PREFIX):
        parsed = urlparse(reference)
        if parsed.query or parsed.fragment:
            raise ValueError("A mail reference must not contain a query or fragment")
        reference = unquote(parsed.path.lstrip("/"))
    if reference.startswith("mid:"):
        mid = index.normalize_message_id(reference[4:])
        if not mid:
            mid = index.normalize_message_id("<" + reference[4:] + ">")
        if mid:
            return "mid:" + mid
    if reference.startswith("sha256:") and re.fullmatch(r"sha256:[0-9a-f]{64}", reference):
        return reference
    if mid := index.normalize_message_id(reference):
        return "mid:" + mid
    raise ValueError("Use a search result ID, Message-ID, or thunderbird://message/ reference")


def _resolve(conn, value: int | str):
    if isinstance(value, bool):
        raise ValueError("Message ID must be a positive integer or stable reference")
    if isinstance(value, int) or (isinstance(value, str) and value.isdecimal()):
        row = conn.execute("SELECT * FROM emails WHERE id=?", (int(value),)).fetchone()
    elif isinstance(value, str):
        key = _key(value)
        row = conn.execute(
            "SELECT * FROM emails WHERE stable_key=? ORDER BY "
            "CASE WHEN folder GLOB '*-[0-9]*/*' THEN 1 ELSE 0 END,id LIMIT 1", (key,)).fetchone()
    else:
        raise ValueError("Supply a message ID or reference")
    if row is None:
        raise LookupError("Message is not in the local index; refresh or search again")
    if not _row_allowed(row):
        raise PermissionError("Message is outside the permitted account scope")
    return row


def _row_allowed(row) -> bool:
    from . import outgoing, policy
    accounts = policy.current().accounts
    return accounts is None or outgoing.infer_account(row["folder"]) in accounts


def _summary(row) -> dict:
    ref = reference_for(key_for_row(row))
    mid = row["message_id"] or ""
    return {
        "id": row["id"], "reference": ref, "uri": ref,
        "folder": row["folder"], "from": row["sender"] or "",
        "to": (row["to_header"] if row["to_header"] is not None else row["recipients"] or "").strip(),
        "cc": row["cc"] or "", "reply_to": row["reply_to"] or "",
        "subject": row["subject"] or "(no subject)", "message_id": mid,
        "in_reply_to": row["in_reply_to"] or "",
        "references": json.loads(row["references_json"] or "[]"),
        "date": dt.datetime.fromtimestamp(row["date_ts"], dt.timezone.utc).isoformat() if row["date_ts"] else "",
        "attachment_count": row["attachment_count"] or 0,
        "thunderbird_uri": "mid:" + quote(mid.strip("<>"), safe="@") if mid else None,
    }


def _select_argument(id, reference):
    if id is not None and reference is not None:
        raise ValueError("Supply either id or reference, not both")
    return reference if reference is not None else id


def get_email(id: int | str | None = None, reference: str | None = None, *,
              db_path: Path | None = None, max_body_chars: int = index.MAX_BODY_CHARS) -> dict:
    """Read indexed mail or route an account-bound live IMAP reference."""
    from . import live, providers
    value = _select_argument(id, reference)
    if providers.is_reference(value):
        return providers.get_email(value, max_body_chars=max_body_chars)
    if live.is_reference(value):
        return live.get_email(value, max_body_chars=max_body_chars)
    if (isinstance(max_body_chars, bool) or not isinstance(max_body_chars, int)
            or not 0 <= max_body_chars <= index.MAX_BODY_CHARS):
        raise ValueError(f"max_body_chars must be between 0 and {index.MAX_BODY_CHARS}")
    conn = index.connect(db_path, read_only=True)
    try:
        row = _resolve(conn, value)
        result = _summary(row)
        body = row["body"] or ""
        result.update(body=body[:max_body_chars],
                      body_truncated=bool(row["body_truncated"] or len(body) > max_body_chars))
        result["copies"] = [r["folder"] for r in conn.execute(
            "SELECT DISTINCT folder FROM emails WHERE stable_key=? ORDER BY folder", (key_for_row(row),)) if _row_allowed(r)]
        if row["to_header"] is None:
            try:
                raw = _source_bytes(conn, row)
            except (OSError, LookupError, ValueError):
                result["headers_complete"] = False
            else:
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                result.update(to=index._header(msg, "to"), cc=index._header(msg, "cc"),
                              reply_to=index._header(msg, "reply-to"),
                              in_reply_to=index.normalize_message_id(index._header(msg, "in-reply-to")),
                              references=index.message_ids(index._header(msg, "references")), headers_complete=True)
        else:
            result["headers_complete"] = True
        if "attachments_json" in row.keys():
            result["attachments"] = json.loads(row["attachments_json"] or "[]")
            result["attachments_indexed"] = bool(row["attachments_indexed"])
        return result
    finally:
        conn.close()


def reference_email(id: int | str | None = None, reference: str | None = None, *,
                    db_path: Path | None = None) -> dict:
    """Return a durable citation that can be pasted into another terminal session."""
    from . import live, providers
    value = _select_argument(id, reference)
    if providers.is_reference(value):
        result = providers.get_email(value, max_body_chars=0)
        result.pop("body", None)
        label = str(result.get("subject", "Mail")).replace("[", "\\[").replace("]", "\\]")
        result["citation"] = f"[{label}]({value})"
        return result
    if live.is_reference(value):
        return live.reference_email(value)
    conn = index.connect(db_path, read_only=True)
    try:
        row = _resolve(conn, value)
        result = _summary(row)
        label = f"{result['subject']} — {result['from']} ({result['date'][:10] or 'undated'})"
        label = re.sub(r"[\r\n]+", " ", label).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        result["citation"] = f"[{label}]({result['reference']})"
        return result
    finally:
        conn.close()


def get_thread(reference: str | int, *, limit: int = 50, include_bodies: bool = False,
               db_path: Path | None = None) -> dict:
    """Follow Message-ID, References, and In-Reply-To links, including shared missing ancestors."""
    from . import live, providers
    if providers.is_reference(reference):
        return providers.get_thread(reference, limit=limit, include_bodies=include_bodies)
    if live.is_reference(reference):
        return live.get_thread(reference, limit=limit, include_bodies=include_bodies)
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    conn = index.connect(db_path, read_only=True)
    try:
        first = _resolve(conn, reference)
        found = {key_for_row(first): first}
        pending = set(index.message_ids(first["message_id"]) + index.message_ids(first["in_reply_to"]) +
                      json.loads(first["references_json"] or "[]"))
        visited = set()
        capped = False
        while pending and len(found) <= 2000:
            batch = sorted(pending - visited)[:300]
            if not batch:
                break
            visited.update(batch)
            marks = ",".join("?" for _ in batch)
            for row in conn.execute(
                f"SELECT DISTINCT e.* FROM emails e WHERE e.message_id IN ({marks}) "
                f"OR e.id IN (SELECT email_id FROM message_links WHERE message_id IN ({marks}))", batch + batch):
                if not _row_allowed(row):
                    continue
                found.setdefault(key_for_row(row), row)
                pending.update(index.message_ids(row["message_id"]) + index.message_ids(row["in_reply_to"]) +
                               json.loads(row["references_json"] or "[]"))
            pending.difference_update(visited)
        if pending:
            capped = True
        ordered = sorted(found.values(), key=lambda row: (row["date_ts"] or 0, row["id"]))
        messages = []
        for row in ordered[:limit]:
            value = _summary(row)
            if include_bodies:
                value.update(body=row["body"] or "", body_truncated=bool(row["body_truncated"]))
            messages.append(value)
        return {"reference": reference_for(key_for_row(first)), "count": len(messages),
                "total": len(found), "truncated": capped or len(found) > limit,
                "match_method": "message_headers", "messages": messages}
    finally:
        conn.close()


def _matches(raw: bytes, key: str) -> bool:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    try:
        if int(index._header(msg, "x-mozilla-status") or "0", 16) & 0x0008:
            return False
    except ValueError:
        pass
    return index.parse_message(msg, extract_attachments=False).stable_key == key


def _source_bytes(conn, row) -> bytes:
    key = key_for_row(row)
    copies = conn.execute(
        "SELECT e.*,f.path,f.size AS source_size,f.mtime AS source_mtime "
        "FROM emails e JOIN folders f ON f.folder=e.folder WHERE e.stable_key=? "
        "ORDER BY CASE WHEN e.id=? THEN 0 ELSE 1 END", (key, row["id"])).fetchall()
    for copy in copies:
        if not _row_allowed(copy):
            continue
        path = Path(copy["path"])
        if path.is_dir():
            from . import maildir
            try:
                return maildir.source_bytes(conn, copy, path, maximum=MAX_RAW_BYTES)
            except (LookupError, maildir.SnapshotChanged):
                continue
        try:
            state = path.stat()
            fresh = state.st_size == copy["source_size"] and state.st_mtime == copy["source_mtime"]
            if fresh and copy["source_offset"] is not None and copy["source_length"] is not None:
                if copy["source_length"] > MAX_RAW_BYTES:
                    raise ValueError("Message exceeds the 100 MiB local export limit")
                with path.open("rb") as source:
                    source.seek(copy["source_offset"])
                    raw = source.read(copy["source_length"])
                after = path.stat()
                if state.st_mtime_ns == after.st_mtime_ns and state.st_size == after.st_size and _matches(raw, key):
                    return raw
            # Offsets can move when Thunderbird compacts an mbox. Never return a
            # different message from an old offset; scan and verify identity.
            box = mailbox.mbox(str(path), create=False)
            try:
                for source_key in box.iterkeys():
                    with box.get_file(source_key) as source:
                        raw = source.read(MAX_RAW_BYTES + 1)
                    if len(raw) > MAX_RAW_BYTES:
                        continue
                    if _matches(raw, key):
                        return raw
            finally:
                box.close()
        except (OSError, KeyError):
            continue
    raise LookupError("Original message is unavailable in local Thunderbird mail; synchronize Thunderbird and refresh the index")


def _attachments(msg):
    count = 0
    for part in msg.walk():
        if part.is_multipart() or not (part.get_content_disposition() == "attachment" or part.get_filename()):
            continue
        count += 1
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename() or f"attachment-{count}"
        # Names are display metadata, never filesystem paths.
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        yield {"id": count, "filename": filename, "content_type": part.get_content_type(),
               "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}, payload


def _attachment_id(value: int | str) -> int:
    if (isinstance(value, bool) or not isinstance(value, (int, str))
            or (isinstance(value, str) and not re.fullmatch(r"[1-9][0-9]*", value))
            or int(value) < 1):
        raise ValueError("attachment_id must be the positive numeric ID from list_attachments")
    return int(value)


def _attachment_text(msg, attachment_id: int, max_chars: int) -> dict:
    from . import attachments
    number = 0
    for part in msg.walk():
        if part.is_multipart() or not (part.get_content_disposition() == "attachment" or part.get_filename()):
            continue
        number += 1
        if number != attachment_id:
            continue
        filename = (part.get_filename() or f"attachment-{number}").replace("\\", "/").rsplit("/", 1)[-1]
        meta = {"id": number, "filename": filename, "content_type": part.get_content_type(), "size": None}
        raw = part.get_payload()
        if not isinstance(raw, str) or len(raw) > attachments.MAX_ATTACHMENT_BYTES * 2:
            return {"attachment": meta, "text": "", "status": "too_large", "truncated": False,
                    "reason": "attachment_byte_limit"}
        payload = part.get_payload(decode=True) or b""
        meta.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        extracted = attachments.extract_text(payload, filename, part.get_content_type(),
                                              charset=part.get_content_charset())
        if len(extracted["text"]) > max_chars:
            extracted.update(text=extracted["text"][:max_chars], truncated=True, reason="requested_text_limit")
        return {"attachment": meta, **extracted}
    raise LookupError("No attachment with that ID; use list_attachments first")


def get_attachment_text(reference: str | int, attachment_id: int | str, max_chars: int = 25_000, *,
                        db_path: Path | None = None) -> dict:
    """Extract bounded text from one attachment without opening files or writing an index."""
    from . import attachments, live, providers
    attachment_id = _attachment_id(attachment_id)
    if (isinstance(max_chars, bool) or not isinstance(max_chars, int)
            or not 0 <= max_chars <= attachments.MAX_ATTACHMENT_CHARS):
        raise ValueError(f"max_chars must be between 0 and {attachments.MAX_ATTACHMENT_CHARS}")
    if live.is_reference(reference):
        return live.get_attachment_text(reference, attachment_id, max_chars=max_chars)
    if providers.is_reference(reference):
        msg = email.message_from_bytes(providers.original(reference)[1], policy=email.policy.default)
        return {"reference": reference, **_attachment_text(msg, attachment_id, max_chars)}
    conn = index.connect(db_path, read_only=True)
    try:
        row = _resolve(conn, reference)
        msg = email.message_from_bytes(_source_bytes(conn, row), policy=email.policy.default)
        return {"reference": reference_for(key_for_row(row)), **_attachment_text(msg, attachment_id, max_chars)}
    finally:
        conn.close()


def list_attachments(reference: str | int, *, db_path: Path | None = None) -> dict:
    from . import live, providers
    if live.is_reference(reference):
        return live.list_attachments(reference)
    if providers.is_reference(reference):
        msg = email.message_from_bytes(providers.original(reference)[1], policy=email.policy.default)
        values = [meta for meta, _ in _attachments(msg)]
        return {"reference": reference, "count": len(values), "attachments": values}
    conn = index.connect(db_path, read_only=True)
    try:
        row = _resolve(conn, reference)
        msg = email.message_from_bytes(_source_bytes(conn, row), policy=email.policy.default)
        attachments = [meta for meta, _ in _attachments(msg)]
        if "attachments_json" in row.keys() and row["attachments_indexed"]:
            stored = {entry["id"]: entry for entry in json.loads(row["attachments_json"] or "[]")
                      if entry.get("id")}
            for attachment in attachments:
                extracted = stored.get(attachment["id"], {})
                if any(extracted.get(field) != attachment[field]
                       for field in ("sha256", "filename", "content_type")):
                    continue
                for field in ("status", "truncated", "reason"):
                    if field in extracted:
                        attachment[field] = extracted[field]
        return {"reference": reference_for(key_for_row(row)), "count": len(attachments), "attachments": attachments}
    finally:
        conn.close()


def _write_export(conn, destination: str | Path, data: bytes, db_path) -> dict:
    from . import policy
    policy.require("file-write", paths=[destination])
    path = Path(destination).expanduser().resolve()
    db = Path(db_path or index.default_db_path()).expanduser().resolve()
    if path == db or str(path) in {str(db) + "-wal", str(db) + "-shm"}:
        raise ValueError("Export destination must not be the mail index")
    stores = []
    for row in conn.execute("SELECT path FROM folders WHERE path IS NOT NULL") if conn is not None else ():
        source = Path(row[0]).resolve()
        stores.append(next((p for p in source.parents if p.name in {"Mail", "ImapMail"}), source.parent))
    # Live reads work before an index exists. Still protect the configured
    # Thunderbird store when a caller explicitly chooses an export destination.
    try:
        profile = index.find_profile().resolve()
        stores.extend(((profile / "Mail").resolve(), (profile / "ImapMail").resolve()))
        # Thunderbird permits accounts whose storage lives outside the profile.
        # A live export must protect those too, even before local indexing.
        from .outgoing import _prefs
        for key, value in _prefs().items():
            if not key.startswith("mail.server.") or not isinstance(value, str):
                continue
            if key.endswith(".directory") and Path(value).expanduser().is_absolute():
                stores.append(Path(value).expanduser().resolve())
            elif key.endswith(".directory-rel") and value.startswith("[ProfD]"):
                stores.append((profile / value[len("[ProfD]"):].lstrip("/\\")).resolve())
    except FileNotFoundError:
        pass
    for store in stores:
        if path == store or store in path.parents:
            raise ValueError("Export destination must be outside Thunderbird mail storage")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def export_email(reference: str | int, destination: str | Path, *, db_path: Path | None = None) -> dict:
    """Export the original MIME mail as .eml without modifying Thunderbird."""
    from . import live, providers
    if providers.is_reference(reference):
        return {"reference": reference, **live._export(destination, providers.original(reference)[1], db_path)}
    if live.is_reference(reference):
        return live.export_email(reference, destination, db_path=db_path)
    conn = index.connect(db_path, read_only=True)
    try:
        row = _resolve(conn, reference)
        return {"reference": reference_for(key_for_row(row)),
                **_write_export(conn, destination, _source_bytes(conn, row), db_path)}
    finally:
        conn.close()


def export_attachment(reference: str | int, attachment_id: int | str, destination: str | Path, *,
                      db_path: Path | None = None) -> dict:
    from . import live, providers
    if providers.is_reference(reference):
        attachment_id = _attachment_id(attachment_id)
        msg = email.message_from_bytes(providers.original(reference)[1], policy=email.policy.default)
        for meta, payload in _attachments(msg):
            if meta["id"] == attachment_id:
                return {"reference": reference, "attachment": meta, **live._export(destination, payload, db_path)}
        raise LookupError("No attachment with that ID; use list_attachments first")
    if live.is_reference(reference):
        return live.export_attachment(reference, attachment_id, destination, db_path=db_path)
    attachment_id = _attachment_id(attachment_id)
    conn = index.connect(db_path, read_only=True)
    try:
        row = _resolve(conn, reference)
        msg = email.message_from_bytes(_source_bytes(conn, row), policy=email.policy.default)
        for meta, payload in _attachments(msg):
            if meta["id"] == attachment_id:
                return {"reference": reference_for(key_for_row(row)), "attachment": meta,
                        **_write_export(conn, destination, payload, db_path)}
        raise LookupError("No attachment with that ID; use list_attachments first")
    finally:
        conn.close()
