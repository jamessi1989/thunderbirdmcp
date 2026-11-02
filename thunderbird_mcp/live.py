"""Bounded, read-only IMAP access for mail not yet downloaded by Thunderbird.

References bind a configured account, its endpoint, folder, UIDVALIDITY and UID.
They deliberately fail if a folder is recreated or the account points elsewhere;
returning a different message from an old reference would be worse than expiry.
"""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import hashlib
import imaplib
import json
import re
import shlex
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from . import imap, index, outgoing

REFERENCE_PREFIX = "thunderbird://imap/"
MAX_RAW_BYTES = 25 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_THREAD_MESSAGES = 100
MAX_THREAD_SEARCHES = 40
OPERATION_SECONDS = 90
_UID = re.compile(rb"(?:^|[ (])UID (\d+)(?=[ )])", re.I)
_SIZE = re.compile(rb"(?:^|[ (])RFC822\.SIZE (\d+)(?=[ )])", re.I)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def is_reference(value) -> bool:
    return isinstance(value, str) and value.startswith(REFERENCE_PREFIX)


def _text(value, name: str, *, limit: int = 4096, empty: bool = False) -> str:
    if (not isinstance(value, str) or (not value and not empty) or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError(f"{name} must be text without control characters, at most {limit} characters")
    return value


def _number(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**32 - 1:
        raise ValueError(f"{name} must be a positive 32-bit integer")
    return value


def _fingerprint(config: dict) -> str:
    identity = [str(config["imap_host"]).lower().rstrip("."),
                int(config["imap_port"]), config["imap_user"]]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Locator:
    account: str
    fingerprint: str
    folder: str
    uidvalidity: int
    uid: int

    @property
    def reference(self) -> str:
        return REFERENCE_PREFIX + "/".join((quote(self.account, safe=""), self.fingerprint,
            quote(self.folder, safe=""), str(self.uidvalidity), str(self.uid)))


def parse_reference(reference: str) -> Locator:
    if not is_reference(reference) or len(reference) > 8192:
        raise ValueError("Use a thunderbird://imap/ reference from a live search")
    parsed = urlsplit(reference)
    parts = parsed.path.lstrip("/").split("/")
    if (parsed.scheme != "thunderbird" or parsed.netloc != "imap" or parsed.query
            or parsed.fragment or len(parts) != 5 or re.search(r"%(?![0-9a-fA-F]{2})", reference)):
        raise ValueError("Invalid live mail reference")
    try:
        account, fingerprint, folder, validity, uid = [unquote(p, errors="strict") for p in parts]
        _text(account, "Account", limit=256)
        _text(folder, "Folder", limit=1024)
        if not re.fullmatch(r"[0-9a-f]{24}", fingerprint):
            raise ValueError("Invalid account fingerprint")
        if not re.fullmatch(r"[1-9][0-9]{0,9}", validity) or not re.fullmatch(r"[1-9][0-9]{0,9}", uid):
            raise ValueError("Invalid UID")
        return Locator(account, fingerprint, folder, _number(int(validity), "UIDVALIDITY"),
                       _number(int(uid), "UID"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Invalid live mail reference") from exc


def _config(locator: Locator) -> dict:
    config = outgoing.account_config(locator.account)
    if config["account_id"] != locator.account or _fingerprint(config) != locator.fingerprint:
        raise LookupError("The referenced account has changed; run a new live search")
    return config


@contextmanager
def _session(config):
    try:
        with imap.session(config=config) as connection:
            yield connection
    except (imaplib.IMAP4.error, OSError) as exc:
        # Protocol exceptions can include server responses and credentials.
        raise RuntimeError("Live IMAP request failed; check account connectivity and authentication") from None


def _deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("Live IMAP operation reached its time limit; narrow the search")


@contextmanager
def _literal_limit(connection, maximum: int):
    """Reject a server's oversized literal before imaplib allocates its body."""
    original = getattr(connection, "read", None)
    if not callable(original):  # Minimal protocol fakes need not implement sockets.
        yield
        return
    remaining = maximum

    def bounded(size):
        nonlocal remaining
        if not isinstance(size, int) or size < 0 or size > remaining:
            # A literal cannot be skipped safely. Close this connection instead
            # of issuing further commands into an unread response stream.
            try:
                connection.shutdown()
            except Exception:
                pass
            raise RuntimeError("IMAP server exceeded the requested literal byte limit")
        remaining -= size
        return original(size)

    connection.read = bounded
    try:
        yield
    finally:
        connection.read = original


def _select(connection, folder: str, expected: int | None = None) -> int:
    _text(folder, "Folder", limit=1024)
    with _literal_limit(connection, 0):
        imap._check(connection.select(imap._q(folder), readonly=True), "Read-only SELECT")
    _, values = connection.response("UIDVALIDITY")
    value = values[0] if values else None
    if not isinstance(value, bytes) or not re.fullmatch(rb"[1-9][0-9]{0,9}", value):
        raise RuntimeError("IMAP server did not provide a valid UIDVALIDITY; cannot create a safe reference")
    validity = _number(int(value), "UIDVALIDITY")
    if expected is not None and validity != expected:
        raise LookupError("The referenced folder was recreated (UIDVALIDITY changed); run a new live search")
    return validity


def _quoted(value: str) -> bytes:
    return ('"' + _text(value, "Search value").replace("\\", "\\\\").replace('"', '\\"') + '"').encode("utf-8")


def _search_uids(connection, criteria: list, deadline: float) -> list[int]:
    _deadline(deadline)
    nonascii = any(isinstance(p, bytes) and not p.isascii() for p in criteria)
    args = ["CHARSET", "UTF-8"] if nonascii else [None]
    with _literal_limit(connection, 0):
        data = imap._check(connection.uid("SEARCH", *args, "UNDELETED", *criteria), "UID SEARCH")
    tokens = b" ".join(p for p in data if isinstance(p, bytes)).split()
    if len(tokens) > 200_000:
        raise ValueError("Live search matched too many messages; add a folder or date filter")
    if any(not token.isdigit() or not 1 <= int(token) <= 2**32 - 1 for token in tokens):
        raise RuntimeError("IMAP server returned invalid search UIDs")
    return sorted({int(token) for token in tokens}, reverse=True)


def _fetch_literal(connection, uid: int, section: str, byte_limit: int, deadline: float) -> tuple[bytes, bytes]:
    _deadline(deadline)
    with _literal_limit(connection, byte_limit):
        data = imap._check(connection.uid("FETCH", str(uid),
            f"(UID RFC822.SIZE FLAGS BODY.PEEK[{section}]<0.{byte_limit}>)"), "UID FETCH")
    matches = []
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2 or not isinstance(item[0], bytes):
            continue
        found = _UID.search(item[0])
        if found and int(found[1]) == uid and isinstance(item[1], bytes):
            if len(item[1]) > byte_limit:
                raise RuntimeError("IMAP server exceeded the requested fetch limit")
            matches.append((item[0], item[1]))
    if not matches:
        raise LookupError("The referenced message is no longer in this folder; run a new live search")
    if len(matches) != 1:
        raise RuntimeError("IMAP server returned an ambiguous message response")
    return matches[0]


def _raw(connection, uid: int, deadline: float, *, max_bytes: int = MAX_RAW_BYTES) -> bytes:
    _deadline(deadline)
    with _literal_limit(connection, 0):
        data = imap._check(connection.uid("FETCH", str(uid), "(UID RFC822.SIZE)"), "UID size FETCH")
    sizes = []
    for item in data:
        header = item[0] if isinstance(item, tuple) else item
        if not isinstance(header, bytes):
            continue
        found, size = _UID.search(header), _SIZE.search(header)
        if found and int(found[1]) == uid and size:
            sizes.append(int(size[1]))
    if not sizes:
        raise LookupError("The referenced message is no longer in this folder; run a new live search")
    if len(sizes) != 1:
        raise RuntimeError("IMAP server returned ambiguous message sizes")
    if sizes[0] > max_bytes:
        raise ValueError("Message exceeds the remaining live read/export byte limit; read individual messages or use Thunderbird's local copy")
    header, raw = _fetch_literal(connection, uid, "", sizes[0] + 1, deadline)
    fetched_size = _SIZE.search(header)
    if len(raw) != sizes[0] or (fetched_size and int(fetched_size[1]) != sizes[0]):
        raise RuntimeError("IMAP returned incomplete or inconsistent MIME data; no message was exported")
    return raw


def _summary(msg, locator: Locator, *, raw_header: bytes | None = None) -> dict:
    mid = index.normalize_message_id(index._header(msg, "message-id"))
    timestamp = index._date_ts(msg)
    result = {
        "reference": locator.reference, "uri": locator.reference, "id": None,
        "source": "imap", "account": locator.account, "account_id": locator.account,
        "folder": locator.folder, "folder_path": locator.folder,
        "uid": locator.uid, "uidvalidity": locator.uidvalidity,
        "from": index._header(msg, "from"), "to": index._header(msg, "to"),
        "cc": index._header(msg, "cc"), "reply_to": index._header(msg, "reply-to"),
        "subject": index._header(msg, "subject") or "(no subject)", "message_id": mid,
        "in_reply_to": index.normalize_message_id(index._header(msg, "in-reply-to")),
        "references": index.message_ids(index._header(msg, "references")),
        "date": dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat() if timestamp else "",
        "thunderbird_uri": "mid:" + quote(mid.strip("<>"), safe="@") if mid else None,
        "reference_scope": "account_folder_uidvalidity",
    }
    if raw_header is not None:
        flags = imaplib.ParseFlags(raw_header)
        result["unread"] = b"\\seen" not in {flag.lower() for flag in flags}
        size = _SIZE.search(raw_header)
        result["size"] = int(size[1]) if size else None
    return result


def _header(connection, locator: Locator, deadline: float) -> dict:
    metadata, raw = _fetch_literal(connection, locator.uid, "HEADER", MAX_HEADER_BYTES, deadline)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    result = _summary(msg, locator, raw_header=metadata)
    result.update(headers_complete=len(raw) < MAX_HEADER_BYTES, snippet="", attachment_count=None)
    return result


def _calendar(value: str, name: str) -> dt.date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"Live {name} accepts YYYY-MM-DD calendar dates, not timestamps")
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {name} date; use YYYY-MM-DD") from exc


def _imap_date(value: dt.date) -> str:
    return f"{value.day:02d}-{_MONTHS[value.month - 1]}-{value.year:04d}"


def search_email(query: str = "", limit: int = 20, *, folder: str | None = None,
                 account: str | None = None, sender: str | None = None,
                 recipient: str | None = None, subject: str | None = None,
                 since: str | None = None, until: str | None = None,
                 unread: bool | None = None) -> dict:
    """Search one live folder, returning headers without marking messages read.

    Words/quoted phrases are literal IMAP TEXT matches joined with AND. Supports
    from:, to:, folder:, after:, before:; before is exclusive, until inclusive.
    Date filters use the Date header's calendar day. No semantic ranking occurs.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    _text(query, "Query", empty=True)
    if unread is not None and not isinstance(unread, bool):
        raise ValueError("unread must be true, false, or omitted")
    from .search import _parse
    text, filters = _parse(query)
    if set(filters) - {"from", "to", "folder", "after", "before"}:
        raise ValueError("Live search does not support attachment filters; use local search")
    for name, value in (("from", sender), ("to", recipient), ("folder", folder), ("after", since)):
        if value is not None:
            filters[name] = _text(value, name)
    folder = filters.get("folder", "INBOX")
    _text(folder, "Folder", limit=1024)
    lower = _calendar(filters["after"], "since") if "after" in filters else None
    upper = _calendar(filters["before"], "before") if "before" in filters else None
    if until is not None:
        try:
            upper = _calendar(until, "until") + dt.timedelta(days=1)
        except OverflowError as exc:
            raise ValueError("until must be earlier than 9999-12-31") from exc
    if lower is not None and upper is not None and lower >= upper:
        raise ValueError("The lower date bound must be earlier than the upper date bound")
    try:
        words = shlex.split(text)
    except ValueError as exc:
        raise ValueError("Unclosed quote in live search query") from exc
    if len(words) > 30:
        raise ValueError("Live search accepts at most 30 words or quoted phrases")
    criteria = []
    for word in words:
        criteria.extend(("TEXT", _quoted(word)))
    if "from" in filters:
        criteria.extend(("FROM", _quoted(filters["from"])))
    if "to" in filters:
        recipient_value = _quoted(filters["to"])
        criteria.extend(("OR", "TO", recipient_value, "CC", recipient_value))
    if subject is not None:
        criteria.extend(("SUBJECT", _quoted(subject)))
    if lower is not None:
        criteria.extend(("SENTSINCE", _imap_date(lower)))
    if upper is not None:
        criteria.extend(("SENTBEFORE", _imap_date(upper)))
    if unread is not None:
        criteria.append("UNSEEN" if unread else "SEEN")
    config = outgoing.account_config(account)
    deadline = time.monotonic() + OPERATION_SECONDS
    warnings, results = [], []
    with _session(config) as connection:
        validity = _select(connection, folder)
        uids = _search_uids(connection, criteria, deadline)
        for uid in uids[:limit]:
            locator = Locator(config["account_id"], _fingerprint(config), folder, validity, uid)
            try:
                results.append(_header(connection, locator, deadline))
            except LookupError:
                warnings.append("A matching message left this folder during the search; search again if needed")
    return {"source": "imap", "account": config["account_id"], "folder": folder,
            "count": len(results), "matched": len(uids), "truncated": len(uids) > limit,
            "order": "newest_uid_first", "results": results,
            "diagnostics": {"semantic": "disabled", "headers_only": True,
                            "warnings": list(dict.fromkeys(warnings))}}


def get_email(reference: str, *, max_body_chars: int = index.MAX_BODY_CHARS) -> dict:
    if (isinstance(max_body_chars, bool) or not isinstance(max_body_chars, int)
            or not 0 <= max_body_chars <= index.MAX_BODY_CHARS):
        raise ValueError(f"max_body_chars must be between 0 and {index.MAX_BODY_CHARS}")
    locator = parse_reference(reference)
    config = _config(locator)
    with _session(config) as connection:
        _select(connection, locator.folder, locator.uidvalidity)
        raw = _raw(connection, locator.uid, time.monotonic() + OPERATION_SECONDS)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    result = _summary(msg, locator)
    body = index._extract_body(msg) or ""
    result.update(body=body[:max_body_chars], body_truncated=len(body) > max_body_chars,
                  headers_complete=True, size=len(raw), copies=[locator.folder],
                  attachment_count=sum(1 for p in msg.walk() if not p.is_multipart()
                      and (p.get_content_disposition() == "attachment" or p.get_filename())))
    return result


def reference_email(reference: str) -> dict:
    locator = parse_reference(reference)
    with _session(_config(locator)) as connection:
        _select(connection, locator.folder, locator.uidvalidity)
        result = _header(connection, locator, time.monotonic() + OPERATION_SECONDS)
    label = f"{result['subject']} — {result['from']} ({result['date'][:10] or 'undated'})"
    label = re.sub(r"[\r\n]+", " ", label).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    result["citation"] = f"[{label}]({result['reference']})"
    result["reference_note"] = "This reference expires if the message moves, the folder is recreated, or the account changes"
    return result


def _links(message: dict) -> set[str]:
    return set(index.message_ids(message["message_id"]) + index.message_ids(message["in_reply_to"])
               + message["references"])


def get_thread(reference: str, *, limit: int = 50, include_bodies: bool = False) -> dict:
    """Follow exact Message-ID links within this folder, subject to explicit caps."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Live thread limit must be between 1 and 100")
    locator = parse_reference(reference)
    config, deadline = _config(locator), time.monotonic() + OPERATION_SECONDS
    capped, searches = False, 0
    with _session(config) as connection:
        _select(connection, locator.folder, locator.uidvalidity)
        anchor = _header(connection, locator, deadline)
        capped = not anchor["headers_complete"]
        found, inspected, visited = {locator.uid: anchor}, {locator.uid}, set()
        pending = _links(anchor)
        while pending and searches < MAX_THREAD_SEARCHES and len(inspected) < MAX_THREAD_MESSAGES:
            mid = sorted(pending)[0]
            pending.remove(mid)
            if mid in visited:
                continue
            visited.add(mid)
            if len(mid) > 998:
                capped = True
                continue
            value = _quoted(mid)
            criteria = ["OR", "HEADER", "Message-ID", value, "OR", "HEADER", "In-Reply-To", value,
                        "HEADER", "References", value]
            candidates = _search_uids(connection, criteria, deadline)
            searches += 1
            for uid in candidates:
                if uid in inspected:
                    continue
                if len(inspected) >= MAX_THREAD_MESSAGES:
                    capped = True
                    break
                inspected.add(uid)
                candidate = Locator(locator.account, locator.fingerprint, locator.folder, locator.uidvalidity, uid)
                try:
                    message = _header(connection, candidate, deadline)
                except LookupError:
                    capped = True
                    continue
                # IMAP HEADER is a substring search, not an identity lookup.
                links = _links(message)
                if mid in links:
                    found[uid] = message
                    capped = capped or not message["headers_complete"]
                    pending.update(links - visited)
        capped = capped or bool(pending)
        ordered = sorted(found.values(), key=lambda m: (m["date"], m["uid"]))[:limit]
        if include_bodies:
            remaining_bytes = MAX_RAW_BYTES
            for message in ordered:
                raw = _raw(connection, message["uid"], deadline, max_bytes=remaining_bytes)
                remaining_bytes -= len(raw)
                if remaining_bytes < 0:
                    raise ValueError("Thread exceeds the 25 MiB live body limit; read individual messages instead")
                parsed = email.message_from_bytes(raw, policy=email.policy.default)
                body = index._extract_body(parsed) or ""
                message.update(body=body[:index.MAX_BODY_CHARS], body_truncated=len(body) > index.MAX_BODY_CHARS)
    return {"reference": locator.reference, "source": "imap", "account": locator.account,
            "folder": locator.folder, "scope": "this_folder_only", "count": len(ordered),
            "total": len(found), "truncated": capped or len(found) > limit,
            "match_method": "message_headers", "messages": ordered}


def _original(reference: str) -> tuple[Locator, bytes]:
    locator = parse_reference(reference)
    with _session(_config(locator)) as connection:
        _select(connection, locator.folder, locator.uidvalidity)
        raw = _raw(connection, locator.uid, time.monotonic() + OPERATION_SECONDS)
    return locator, raw


def list_attachments(reference: str) -> dict:
    from .messages import _attachments
    locator, raw = _original(reference)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    attachments = [meta for meta, _ in _attachments(msg)]
    return {"reference": locator.reference, "count": len(attachments), "attachments": attachments}


def get_attachment_text(reference: str, attachment_id: int | str, max_chars: int = 25_000) -> dict:
    from . import attachments
    from .messages import _attachment_id, _attachment_text
    attachment_id = _attachment_id(attachment_id)
    if (isinstance(max_chars, bool) or not isinstance(max_chars, int)
            or not 0 <= max_chars <= attachments.MAX_ATTACHMENT_CHARS):
        raise ValueError(f"max_chars must be between 0 and {attachments.MAX_ATTACHMENT_CHARS}")
    locator, raw = _original(reference)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return {"reference": locator.reference, **_attachment_text(msg, attachment_id, max_chars)}


def _export(destination, data: bytes, db_path) -> dict:
    from .messages import _write_export
    path = Path(db_path or index.default_db_path())
    connection = index.connect(path, read_only=True) if path.is_file() else None
    try:
        return _write_export(connection, destination, data, db_path)
    finally:
        if connection is not None:
            connection.close()


def export_email(reference: str, destination: str | Path, *, db_path: Path | None = None) -> dict:
    locator, raw = _original(reference)
    return {"reference": locator.reference, **_export(destination, raw, db_path)}


def export_attachment(reference: str, attachment_id: int | str, destination: str | Path, *,
                      db_path: Path | None = None) -> dict:
    from .messages import _attachments, _attachment_id
    attachment_id = _attachment_id(attachment_id)
    locator, raw = _original(reference)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    for meta, payload in _attachments(msg):
        if meta["id"] == attachment_id:
            return {"reference": locator.reference, "attachment": meta,
                    **_export(destination, payload, db_path)}
    raise LookupError("No attachment with that ID; use list_attachments first")
