"""Authenticated mail delivery with durable, reviewable, single-send drafts.

Credentials are read only when connecting; they are never stored in drafts.
SMTP acceptance and saving a Sent copy are separate operations: an archive
failure must never cause a second SMTP delivery.
"""

from __future__ import annotations

import hashlib
import imaplib
import json
import mimetypes
import os
import re
import smtplib
import sqlite3
import ssl
import time
import uuid
from contextlib import contextmanager
from email import policy
from email.message import EmailMessage
from email.parser import Parser
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from urllib.parse import unquote, urlparse

import keyring

from . import index as _index
from .profile import find_profile

_PREF = re.compile(r'^user_pref\(("(?:[^"\\]|\\.)*"),\s*(.+)\);$')
_DRAFT_ID = re.compile(r"^[a-f0-9]{32}$")
_MESSAGE_ID = re.compile(r"^<[^\s<>@]+@[^\s<>@]+>$")
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def _prefs() -> dict:
    try:
        path = find_profile() / "prefs.js"
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    out = {}
    for line in contents.splitlines():
        match = _PREF.match(line.strip())
        if match:
            try:
                out[json.loads(match[1])] = json.loads(match[2])
            except (ValueError, TypeError):
                continue
    return out


def _header(value: str, name: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} must be text without control characters")
    return value.strip()


def _addresses(values: list[str] | None, name: str) -> tuple[list[str], list[str]]:
    if values is None:
        return [], []
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a list of individual email addresses")
    display, envelope = [], []
    for value in values:
        value = _header(value, name)
        parsed = Parser(policy=policy.default).parsestr(f"To: {value}\n\n")["To"]
        if parsed is None or parsed.defects or len(parsed.addresses) != 1:
            raise ValueError(f"Each {name} entry must be one valid email address")
        address = parsed.addresses[0]
        if not address.username or not address.domain or any(group.display_name for group in parsed.groups):
            raise ValueError(f"Each {name} entry must contain a mailbox and domain")
        # International display names work; SMTPUTF8 mailboxes need explicit
        # server negotiation and are deliberately rejected rather than mangled.
        try:
            address.addr_spec.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("Non-ASCII mailbox addresses are not supported") from None
        if address.addr_spec.casefold() not in {item.casefold() for item in envelope}:
            display.append(str(address))
            envelope.append(address.addr_spec)
    return display, envelope


def _tls(value, port: int, name: str) -> str:
    value = str(value).lower() if value is not None else ("ssl" if port in (465, 993) else "starttls")
    value = {"3": "ssl", "2": "starttls", "tls": "starttls"}.get(value, value)
    if value not in ("ssl", "starttls"):
        raise ValueError(f"{name} must be ssl or starttls; plaintext authentication is disabled")
    return value


def account_config() -> dict:
    """Public connection settings from Thunderbird, overridden by TB_* vars."""
    prefs = _prefs()
    account = os.environ.get("TB_ACCOUNT", prefs.get("mail.accountmanager.defaultaccount", ""))
    server = prefs.get(f"mail.account.{account}.server", "")
    identity = prefs.get(f"mail.account.{account}.identities", "").split(",")[0]
    imap_prefix = f"mail.server.{server}."
    identity_prefix = f"mail.identity.{identity}."
    smtp = prefs.get(identity_prefix + "smtpServer") or prefs.get("mail.smtp.defaultserver", "")
    smtp_prefix = f"mail.smtpserver.{smtp}."
    host = os.environ.get("TB_IMAP_HOST", prefs.get(imap_prefix + "hostname", ""))
    user = os.environ.get("TB_IMAP_USER", prefs.get(imap_prefix + "userName", ""))
    port = int(os.environ.get("TB_IMAP_PORT", prefs.get(imap_prefix + "port", 993)))
    smtp_host = os.environ.get("TB_SMTP_HOST", prefs.get(smtp_prefix + "hostname", host))
    smtp_user = os.environ.get("TB_SMTP_USER", prefs.get(smtp_prefix + "username", user))
    smtp_port = int(os.environ.get("TB_SMTP_PORT", prefs.get(smtp_prefix + "port", 465)))
    sender = os.environ.get("TB_FROM", formataddr((prefs.get(identity_prefix + "fullName", ""),
                                                  prefs.get(identity_prefix + "useremail", ""))))
    displayed, mailboxes = _addresses([sender], "From") if sender else ([], [])
    sent_url = urlparse(prefs.get(identity_prefix + "fcc_folder", ""))
    sent_folder = os.environ.get("TB_SENT_FOLDER", unquote(sent_url.path.lstrip("/")) or "Sent")
    for value, name in ((host, "IMAP host"), (smtp_host, "SMTP host"), (user, "IMAP user"),
                        (smtp_user, "SMTP user"), (sent_folder, "Sent folder")):
        _header(value, name)
    if not 1 <= port <= 65535 or not 1 <= smtp_port <= 65535:
        raise ValueError("Mail ports must be between 1 and 65535")
    timeout = float(os.environ.get("TB_NETWORK_TIMEOUT", "15"))
    if not 0 < timeout <= 60:
        raise ValueError("TB_NETWORK_TIMEOUT must be greater than 0 and at most 60 seconds")
    return {
        "from": displayed[0] if displayed else "", "from_email": mailboxes[0] if mailboxes else "",
        "imap_host": host, "imap_user": user, "imap_port": port,
        "imap_tls": _tls(os.environ.get("TB_IMAP_TLS", prefs.get(imap_prefix + "socketType")), port, "TB_IMAP_TLS"),
        "smtp_host": smtp_host, "smtp_user": smtp_user, "smtp_port": smtp_port,
        "smtp_tls": _tls(os.environ.get("TB_SMTP_TLS", prefs.get(smtp_prefix + "try_ssl")), smtp_port, "TB_SMTP_TLS"),
        "sent_folder": sent_folder, "timeout": timeout,
        "account_prefix": os.environ.get("TB_IMAP_ACCOUNT_DIR", host) + "/",
        "imap_keyring_service": os.environ.get("TB_IMAP_KEYRING_SERVICE", "thunderbird-mcp"),
        "smtp_keyring_service": os.environ.get("TB_SMTP_KEYRING_SERVICE", "thunderbird-mcp"),
    }


def password(config: dict, protocol: str) -> str:
    """Get a secret without including its value in errors or public results."""
    value = os.environ.get(f"TB_{protocol.upper()}_PASSWORD")
    if not value and protocol == "smtp" and config["smtp_user"] == config["imap_user"] and config["smtp_host"] == config["imap_host"]:
        value = os.environ.get("TB_IMAP_PASSWORD")
    if not value:
        try:
            value = keyring.get_password(config[f"{protocol}_keyring_service"], config[f"{protocol}_user"])
        except Exception as exc:
            raise RuntimeError(f"Cannot access {protocol.upper()} keyring ({type(exc).__name__})") from None
    if not value:
        raise RuntimeError(f"No {protocol.upper()} password configured; use the system keyring or TB_{protocol.upper()}_PASSWORD")
    return value


def _require_config(config: dict, protocol: str) -> None:
    if not config[f"{protocol}_host"] or not config[f"{protocol}_user"]:
        raise ValueError(f"No {protocol.upper()} account configured; select TB_PROFILE/TB_ACCOUNT or set TB_{protocol.upper()}_HOST and TB_{protocol.upper()}_USER")


@contextmanager
def smtp_session(config: dict):
    _require_config(config, "smtp")
    secret = password(config, "smtp")
    context = ssl.create_default_context()
    smtp = None
    try:
        if config["smtp_tls"] == "ssl":
            smtp = smtplib.SMTP_SSL(config["smtp_host"], config["smtp_port"], timeout=config["timeout"], context=context)
        else:
            smtp = smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=config["timeout"])
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(config["smtp_user"], secret)
        yield smtp
    finally:
        if smtp is not None:
            # QUIT errors cannot turn an accepted message into an ambiguous send.
            try:
                smtp.quit()
            except Exception:
                try:
                    smtp.close()
                except Exception:
                    pass


def _error(exc: Exception) -> dict:
    result = {"type": type(exc).__name__}
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int):
        result["smtp_code"] = code
    # Server response strings can contain message content or credentials.
    return result


def health_check(check_network: bool = False) -> dict:
    """Read-only credential, TLS, login, and Sent-folder availability checks."""
    config = account_config()
    result = {"config": config, "network_checked": check_network}
    for protocol in ("imap", "smtp"):
        entry = {"configured": bool(config[f"{protocol}_host"] and config[f"{protocol}_user"])}
        try:
            _require_config(config, protocol)
            password(config, protocol)
            entry["credential_available"] = True
        except Exception as exc:
            entry.update(credential_available=False, error=_error(exc))
        result[protocol] = entry
    if check_network:
        if result["imap"].get("credential_available"):
            try:
                from . import imap as _imap
                with _imap.session(config=config, require_move=False) as connection:
                    result["imap"].update(authenticated=True, sent_folder_exists=config["sent_folder"] in _imap.list_folders(connection))
            except Exception as exc:
                result["imap"].update(authenticated=False, error=_error(exc))
        if result["smtp"].get("credential_available"):
            try:
                with smtp_session(config) as connection:
                    code, _ = connection.noop()
                    if code != 250:
                        raise smtplib.SMTPResponseException(code, b"NOOP rejected")
                    result["smtp"]["authenticated"] = True
            except Exception as exc:
                result["smtp"].update(authenticated=False, error=_error(exc))
    result["ready"] = all(result[p].get("authenticated" if check_network else "credential_available", False) for p in ("imap", "smtp"))
    return result


def outbox_path() -> Path:
    return Path(os.environ.get("TB_OUTBOX_DB", str(_index.default_db_path().parent / "mail-outbox.sqlite"))).expanduser()


@contextmanager
def _db():
    path = outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("""CREATE TABLE IF NOT EXISTS drafts (
        id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        status TEXT NOT NULL, idempotency_key TEXT UNIQUE, fingerprint TEXT NOT NULL,
        preview TEXT NOT NULL, envelope TEXT NOT NULL, config TEXT NOT NULL,
        message BLOB NOT NULL, result TEXT
    )""")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _validate_id(draft_id: str) -> None:
    if not isinstance(draft_id, str) or not _DRAFT_ID.fullmatch(draft_id):
        raise ValueError("Invalid draft id")


def _public(row, *, include_body: bool = True) -> dict:
    preview = json.loads(row["preview"])
    if not include_body:
        preview.pop("body", None)
    return {"draft_id": row["id"], "status": row["status"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], **preview,
            **({"delivery": json.loads(row["result"])} if row["result"] else {})}


def prepare_draft(to: list[str], subject: str, body: str, cc: list[str] | None = None,
                  bcc: list[str] | None = None, attachments: list[str] | None = None,
                  in_reply_to: str | None = None, references: list[str] | None = None,
                  idempotency_key: str | None = None) -> dict:
    """Validate and persist the exact MIME bytes and envelope to be sent later."""
    config = account_config()
    if not config["from_email"]:
        raise ValueError("No sender configured; set TB_FROM or select a Thunderbird identity")
    to_display, to_envelope = _addresses(to, "To")
    cc_display, cc_envelope = _addresses(cc, "Cc")
    bcc_display, bcc_envelope = _addresses(bcc, "Bcc")
    unique_recipients = {}
    for recipient in to_envelope + cc_envelope + bcc_envelope:
        unique_recipients.setdefault(recipient.casefold(), recipient)
    recipients = list(unique_recipients.values())
    if not recipients:
        raise ValueError("At least one recipient is required")
    subject = _header(subject, "Subject")
    if not isinstance(body, str) or "\x00" in body:
        raise ValueError("Body must be text without NUL characters")
    if in_reply_to and not _MESSAGE_ID.fullmatch(in_reply_to):
        raise ValueError("Invalid In-Reply-To message id")
    if references is not None and not isinstance(references, list):
        raise ValueError("References must be a list of message ids")
    refs = list(dict.fromkeys((references or []) + ([in_reply_to] if in_reply_to else [])))
    if any(not isinstance(item, str) or not _MESSAGE_ID.fullmatch(item) for item in refs):
        raise ValueError("Invalid References message id")
    if idempotency_key is not None:
        idempotency_key = _header(idempotency_key, "Idempotency key")
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("Idempotency key must contain 1 to 200 characters")
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = config["from"]
    if to_display:
        msg["To"] = ", ".join(to_display)
    if cc_display:
        msg["Cc"] = ", ".join(cc_display)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=config["from_email"].rsplit("@", 1)[1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if refs:
        msg["References"] = " ".join(refs)
    msg.set_content(body)
    attachment_info = []
    total_bytes = 0
    if attachments is not None and not isinstance(attachments, list):
        raise ValueError("Attachments must be a list of local file paths")
    for attachment in attachments or []:
        path = Path(attachment).expanduser()
        if not path.is_file():
            raise ValueError(f"Attachment is not a regular file: {path}")
        filename = _header(path.name, "Attachment filename")
        if total_bytes + path.stat().st_size > _MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachments exceed the total 25 MiB limit")
        with path.open("rb") as source:
            data = source.read(_MAX_ATTACHMENT_BYTES - total_bytes + 1)
        total_bytes += len(data)
        if total_bytes > _MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachments exceed the total 25 MiB limit")
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        maintype, subtype = content_type.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        attachment_info.append({"filename": filename, "content_type": content_type, "size": len(data),
                                "sha256": hashlib.sha256(data).hexdigest()})
    preview = {"from": config["from"], "to": to_display, "cc": cc_display, "bcc": bcc_display,
               "subject": subject, "body": body, "message_id": str(msg["Message-ID"]),
               "in_reply_to": in_reply_to, "references": refs, "attachments": attachment_info}
    fingerprint_data = {key: value for key, value in preview.items() if key != "message_id"}
    fingerprint_data["account"] = config
    fingerprint = hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True).encode()).hexdigest()
    draft_id = uuid.uuid4().hex
    now = int(time.time())
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            existing = conn.execute("SELECT * FROM drafts WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("Idempotency key already belongs to a different draft")
                return _public(existing)
        conn.execute("INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                     (draft_id, now, now, "prepared", idempotency_key, fingerprint, json.dumps(preview),
                      json.dumps(recipients), json.dumps(config), msg.as_bytes()))
        conn.commit()
    return get_draft(draft_id)


def get_draft(draft_id: str) -> dict:
    _validate_id(draft_id)
    with _db() as conn:
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise ValueError("Draft does not exist")
        return _public(row)


def list_drafts(limit: int = 20) -> dict:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM drafts ORDER BY created_at DESC, rowid DESC LIMIT ?",
                            (max(1, min(limit, 100)),)).fetchall()
        return {"drafts": [_public(row, include_body=False) for row in rows], "count": len(rows)}


def _record(draft_id: str, status: str, result: dict) -> dict:
    result = {"draft_id": draft_id, "status": status, **result}
    with _db() as conn:
        conn.execute("UPDATE drafts SET status=?, result=?, updated_at=? WHERE id=?",
                     (status, json.dumps(result), int(time.time()), draft_id))
        conn.commit()
    return result


def _append_sent(config: dict, message: bytes) -> None:
    from . import imap as _imap
    with _imap.session(config=config, require_move=False) as connection:
        _imap._check(connection.append(_imap._q(config["sent_folder"]), r"(\Seen)",
                                       imaplib.Time2Internaldate(time.time()), message), "APPEND Sent")


def send_draft(draft_id: str, retry_failed: bool = False) -> dict:
    """Send a prepared draft once. Unknown SMTP outcomes are never retried."""
    _validate_id(draft_id)
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise ValueError("Draft does not exist")
        if row["status"] != "prepared" and not (row["status"] == "failed" and retry_failed):
            previous = json.loads(row["result"]) if row["result"] else {
                "sent": None, "delivery_uncertain": True,
                "note": "An attempt is active or was interrupted; verify delivery before creating another draft.",
            }
            return {**previous, "draft_id": draft_id, "status": row["status"], "duplicate_prevented": True}
        conn.execute("UPDATE drafts SET status='sending', updated_at=?, result=NULL WHERE id=?", (int(time.time()), draft_id))
        conn.commit()
    config, recipients, preview = json.loads(row["config"]), json.loads(row["envelope"]), json.loads(row["preview"])
    message = bytes(row["message"])
    attempted = False
    try:
        with smtp_session(config) as smtp:
            attempted = True
            refusals = smtp.sendmail(config["from_email"], recipients, message)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
        result = {"sent": False, "safe_to_retry": True, "error": _error(exc), "message_id": preview["message_id"]}
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            result["refused_recipients"] = {address: {"smtp_code": info[0]} for address, info in exc.recipients.items()}
        return _record(draft_id, "failed", result)
    except Exception as exc:
        return _record(draft_id, "uncertain" if attempted else "failed", {
            "sent": None if attempted else False, "delivery_uncertain": attempted,
            "safe_to_retry": not attempted, "error": _error(exc), "message_id": preview["message_id"],
            "note": "Check Sent and recipient delivery before sending again." if attempted else "Delivery did not begin; fix the connection or credentials before retrying.",
        })
    refused = {address: {"smtp_code": info[0]} for address, info in refusals.items()}
    accepted = [recipient for recipient in recipients if recipient not in refusals]
    result = {"sent": bool(accepted), "message_id": preview["message_id"], "accepted_recipients": accepted,
              "refused_recipients": refused, "delivery_complete": not refused, "sent_copy_saved": False}
    status = "partially_sent" if refused else "sent"
    # Commit SMTP success before attempting a separate operation that may fail.
    _record(draft_id, status, result)
    try:
        _append_sent(config, message)
        result["sent_copy_saved"] = True
    except Exception as exc:
        result["sent_copy_error"] = _error(exc)
        result["note"] = "SMTP accepted the message. Do not resend; only the Sent copy could not be confirmed."
    return _record(draft_id, status, result)


def send_email(to: list[str], subject: str, body: str, cc: list[str] | None = None,
               dry_run: bool = True, bcc: list[str] | None = None,
               attachments: list[str] | None = None, idempotency_key: str | None = None) -> dict:
    draft = prepare_draft(to, subject, body, cc=cc, bcc=bcc, attachments=attachments,
                          idempotency_key=idempotency_key)
    if dry_run:
        return {"dry_run": True, **draft}
    return send_draft(draft["draft_id"])
