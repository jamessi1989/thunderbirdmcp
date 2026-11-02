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
from email.parser import BytesParser, Parser
from email.utils import formataddr, formatdate, getaddresses, make_msgid
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


def _account_ids(prefs: dict) -> list[str]:
    ids = str(prefs.get("mail.accountmanager.accounts", "")).split(",")
    ids += [key[len("mail.account."):-len(".server")] for key in prefs
            if key.startswith("mail.account.") and key.endswith(".server")]
    return list(dict.fromkeys(item.strip() for item in ids if item.strip()))


def _default_account(prefs: dict) -> str:
    return os.environ.get("TB_ACCOUNT") or prefs.get("mail.accountmanager.defaultaccount") or "environment"


def _account_id(account: str | None, prefs: dict) -> str:
    selected = account if account is not None else _default_account(prefs)
    if not isinstance(selected, str) or not selected.strip():
        raise ValueError("Account must be a Thunderbird account id or unique sender email")
    selected = selected.strip()
    ids = _account_ids(prefs)
    if selected in ids or (selected == "environment" and not ids):
        return selected
    matches = []
    for candidate in ids:
        identity = str(prefs.get(f"mail.account.{candidate}.identities", "")).split(",")[0].strip()
        if str(prefs.get(f"mail.identity.{identity}.useremail", "")).casefold() == selected.casefold():
            matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("Sender email matches multiple accounts; select the Thunderbird account id")
    raise ValueError("Unknown Thunderbird account; use list_accounts to find its account id")


def _environment_prefix(account: str) -> str:
    # Thunderbird account ids are normally account1, account2, etc. Reject
    # unusual identifiers rather than merging distinct ids into one env name.
    if not re.fullmatch(r"[A-Za-z0-9_]+", account):
        raise ValueError("Unsupported Thunderbird account id")
    return f"TB_ACCOUNT_{account.upper()}_"


def account_config(account: str | None = None, identity: str | None = None) -> dict:
    """Return public settings for one account, never passwords or OAuth tokens.

    TB_* overrides apply only to the selected default account. An explicit
    secondary account uses its Thunderbird settings and TB_ACCOUNT_<ID>_* vars.
    """
    prefs = _prefs()
    account = _account_id(account, prefs)
    environment_prefix = _environment_prefix(account)
    use_global = account == _default_account(prefs)
    explicit_identity = identity is not None
    def setting(name: str, fallback=None):
        # Selecting a Thunderbird identity also selects its SMTP server. Global
        # default-account overrides must not silently substitute another sender's
        # endpoint or authentication mode for that explicit selection.
        if explicit_identity and name in {"SMTP_HOST", "SMTP_USER", "SMTP_PORT", "SMTP_TLS", "SMTP_AUTH", "SENT_FOLDER"}:
            return fallback
        if environment_prefix + name in os.environ:
            return os.environ[environment_prefix + name]
        return os.environ.get("TB_" + name, fallback) if use_global else fallback
    server = prefs.get(f"mail.account.{account}.server", "")
    server_type = prefs.get(f"mail.server.{server}.type", "imap")
    if server_type != "imap":
        raise ValueError("Selected account is not an IMAP account")
    identities = [item.strip() for item in prefs.get(f"mail.account.{account}.identities", "").split(",") if item.strip()]
    if explicit_identity:
        matches = [item for item in identities if identity == item or str(prefs.get(
            f"mail.identity.{item}.useremail", "")).casefold() == str(identity).casefold()]
        if len(matches) != 1:
            raise ValueError("Select one of this account's configured identities using list_identities")
        identity = matches[0]
    else:
        identity = identities[0] if identities else ""
    imap_prefix = f"mail.server.{server}."
    identity_prefix = f"mail.identity.{identity}."
    smtp = prefs.get(identity_prefix + "smtpServer") or prefs.get("mail.smtp.defaultserver", "")
    smtp_prefix = f"mail.smtpserver.{smtp}."
    host = setting("IMAP_HOST", prefs.get(imap_prefix + "hostname", ""))
    user = setting("IMAP_USER", prefs.get(imap_prefix + "userName", ""))
    port = int(setting("IMAP_PORT", prefs.get(imap_prefix + "port", 993)))
    smtp_host = setting("SMTP_HOST", prefs.get(smtp_prefix + "hostname", host))
    smtp_user = setting("SMTP_USER", prefs.get(smtp_prefix + "username", user))
    smtp_port = int(setting("SMTP_PORT", prefs.get(smtp_prefix + "port", 465)))
    configured_sender = formataddr((prefs.get(identity_prefix + "fullName", ""),
                                    prefs.get(identity_prefix + "useremail", "")))
    sender = configured_sender if explicit_identity else setting("FROM", configured_sender)
    displayed, mailboxes = _addresses([sender], "From") if sender else ([], [])
    aliases = []
    for item in identities:
        alias = prefs.get(f"mail.identity.{item}.useremail")
        if alias:
            try:
                aliases.extend(_addresses([alias], "Identity")[1])
            except ValueError:
                # An unused malformed identity must not break another working one.
                continue
    sent_url = urlparse(prefs.get(identity_prefix + "fcc_folder", ""))
    sent_folder = setting("SENT_FOLDER", unquote(sent_url.path.lstrip("/")) or "Sent")
    for value, name in ((host, "IMAP host"), (smtp_host, "SMTP host"), (user, "IMAP user"),
                        (smtp_user, "SMTP user"), (sent_folder, "Sent folder")):
        _header(value, name)
    if not 1 <= port <= 65535 or not 1 <= smtp_port <= 65535:
        raise ValueError("Mail ports must be between 1 and 65535")
    timeout = float(setting("NETWORK_TIMEOUT", "15"))
    if not 0 < timeout <= 60:
        raise ValueError("TB_NETWORK_TIMEOUT must be greater than 0 and at most 60 seconds")
    directory = prefs.get(imap_prefix + "directory-rel") or prefs.get(imap_prefix + "directory", "")
    local_dir = unquote(str(directory).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]) if directory else host
    account_prefix = setting("IMAP_ACCOUNT_DIR", local_dir).rstrip("/") + "/"
    auth = {}
    for protocol, prefix, auth_key in (("imap", imap_prefix, "authMethod"), ("smtp", smtp_prefix, "authMethod")):
        method = str(setting(f"{protocol.upper()}_AUTH", "oauth2" if str(prefs.get(prefix + auth_key, "")) == "10" else "password")).lower()
        if method not in ("password", "oauth2"):
            raise ValueError(f"{protocol.upper()} authentication must be password or oauth2")
        auth[f"{protocol}_auth"] = method
    return {
        "account_id": account, "identity_id": identity or None, "identity_selected": explicit_identity,
        "name": prefs.get(imap_prefix + "name", account),
        "from": displayed[0] if displayed else "", "from_email": mailboxes[0] if mailboxes else "",
        "aliases": list(dict.fromkeys(aliases)),
        "imap_host": host, "imap_user": user, "imap_port": port,
        "imap_tls": _tls(setting("IMAP_TLS", prefs.get(imap_prefix + "socketType")), port, "TB_IMAP_TLS"),
        "smtp_host": smtp_host, "smtp_user": smtp_user, "smtp_port": smtp_port,
        "smtp_tls": _tls(setting("SMTP_TLS", prefs.get(smtp_prefix + "try_ssl")), smtp_port, "TB_SMTP_TLS"),
        "sent_folder": sent_folder, "timeout": timeout,
        "account_prefix": account_prefix, "account_prefixes": [account_prefix],
        "environment_prefix": environment_prefix, "use_global_environment": use_global,
        "imap_keyring_service": setting("IMAP_KEYRING_SERVICE", "thunderbird-mcp"),
        "smtp_keyring_service": setting("SMTP_KEYRING_SERVICE", "thunderbird-mcp"),
        **auth,
    }


def list_identities(account: str | None = None) -> dict:
    """List only identities configured for the selected Thunderbird account."""
    from . import policy as access
    config = account_config(account)
    access.require("read", tool="list_identities", account=config["account_id"])
    prefs = _prefs()
    ids = [item.strip() for item in str(prefs.get(
        f"mail.account.{config['account_id']}.identities", "")).split(",") if item.strip()]
    identities = []
    for item in ids:
        try:
            selected = account_config(config["account_id"], identity=item)
        except ValueError:
            continue
        identities.append({key: selected[key] for key in ("account_id", "identity_id", "from", "from_email")})
    if not identities and config["from_email"]:
        identities.append({key: config[key] for key in ("account_id", "identity_id", "from", "from_email")})
    return {"account_id": config["account_id"], "identities": identities}


def list_accounts() -> dict:
    """List configured IMAP account ids and public settings without connecting."""
    prefs = _prefs()
    accounts = []
    for account in _account_ids(prefs) or ["environment"]:
        server = prefs.get(f"mail.account.{account}.server", "")
        if prefs.get(f"mail.server.{server}.type", "imap") != "imap":
            continue
        try:
            config = account_config(account)
            accounts.append({**config, "default": account == _default_account(prefs)})
        except ValueError as exc:
            accounts.append({"account_id": account, "default": account == _default_account(prefs),
                             "configured": False, "error": str(exc)})
    return {"accounts": accounts, "default_account": _default_account(prefs)}


def infer_account(folder: str) -> str | None:
    """Find the unique account for a local index folder; never guess on overlap."""
    matches = [account["account_id"] for account in list_accounts()["accounts"]
               if any(folder.startswith(prefix) for prefix in account.get("account_prefixes", []))]
    return matches[0] if len(matches) == 1 else None


def credential_environment(config: dict, protocol: str, suffix: str) -> str | None:
    """Read credentials only from env bindings that still identify this account."""
    if protocol not in ("imap", "smtp"):
        raise ValueError("Protocol must be imap or smtp")
    key = f"{protocol.upper()}_{suffix}"
    prefix = config.get("environment_prefix")
    # Snapshotted drafts must not acquire the credentials of a newly selected
    # default account or a changed account endpoint.
    if "account_id" in config:
        try:
            current = account_config(config["account_id"], identity=config.get("identity_id") if config.get("identity_selected") else None)
        except (ValueError, OSError):
            return None
        if any(current.get(f"{protocol}_{item}") != config.get(f"{protocol}_{item}") for item in ("host", "port", "user")):
            return None
        if prefix and prefix + key in os.environ:
            return os.environ[prefix + key]
        if not (current.get("use_global_environment") and config.get("use_global_environment")):
            return None
    return os.environ.get("TB_" + key)


def credential_key(config: dict, protocol: str) -> str:
    if protocol not in ("imap", "smtp"):
        raise ValueError("Protocol must be imap or smtp")
    binding = [config.get("account_id", "environment"), config[f"{protocol}_host"],
               config.get(f"{protocol}_port"), config[f"{protocol}_user"], protocol]
    return "account:" + hashlib.sha256(json.dumps(binding, separators=(",", ":")).encode()).hexdigest()


def _legacy_password_allowed(config: dict, protocol: str) -> bool:
    """Use an old service/username secret only when its mailbox is unambiguous."""
    try:
        current = account_config()
        if config.get("account_id", current["account_id"]) != current["account_id"]:
            return False
        fields = (f"{protocol}_host", f"{protocol}_port", f"{protocol}_user")
        endpoint = tuple(config.get(field) for field in fields)
        if tuple(current.get(field) for field in fields) != endpoint:
            return False
        service, username = config[f"{protocol}_keyring_service"], config[f"{protocol}_user"]
        for candidate in list_accounts()["accounts"]:
            if candidate.get(f"{protocol}_keyring_service") == service and candidate.get(f"{protocol}_user") == username:
                if tuple(candidate.get(field) for field in fields) != endpoint:
                    return False
        return True
    except (ValueError, OSError):
        return False


def store_password(config: dict, protocol: str, secret: str) -> dict:
    """Store a password under an account/server/protocol-bound keyring key.

    Call only from a terminal password prompt or private stdin/file import.
    """
    key = credential_key(config, protocol)
    _require_config(config, protocol)
    if not isinstance(secret, str) or not secret or len(secret) > 65536 or any(char in secret for char in "\r\n\x00"):
        raise ValueError("Password must be nonempty text without CR, LF, or NUL characters")
    try:
        keyring.set_password(config[f"{protocol}_keyring_service"], key, secret)
    except Exception as exc:
        raise RuntimeError(f"Cannot store {protocol.upper()} password in keyring ({type(exc).__name__})") from None
    return {"stored": True, "account_id": config.get("account_id", "environment"),
            "protocol": protocol, "auth": config.get(f"{protocol}_auth", "password"),
            "credential_available": True}


def password(config: dict, protocol: str) -> str:
    """Get a secret without including its value in errors or public results."""
    value = credential_environment(config, protocol, "PASSWORD")
    if not value and protocol == "smtp" and config["smtp_user"] == config["imap_user"] and config["smtp_host"] == config["imap_host"]:
        value = credential_environment(config, "imap", "PASSWORD")
    if not value:
        try:
            value = keyring.get_password(config[f"{protocol}_keyring_service"], credential_key(config, protocol))
            if not value and _legacy_password_allowed(config, protocol):
                value = keyring.get_password(config[f"{protocol}_keyring_service"], config[f"{protocol}_user"])
        except Exception as exc:
            raise RuntimeError(f"Cannot access {protocol.upper()} keyring ({type(exc).__name__})") from None
    if not value:
        raise RuntimeError(f"No {protocol.upper()} password configured for this account; use its scoped environment variable or system keyring")
    return value


def _require_config(config: dict, protocol: str) -> None:
    if not config[f"{protocol}_host"] or not config[f"{protocol}_user"]:
        raise ValueError(f"No {protocol.upper()} account configured; select TB_PROFILE/TB_ACCOUNT or set TB_{protocol.upper()}_HOST and TB_{protocol.upper()}_USER")


@contextmanager
def smtp_session(config: dict):
    _require_config(config, "smtp")
    oauth2 = config.get("smtp_auth", "password") == "oauth2"
    if oauth2:
        from . import oauth
        secret = oauth.access_token(config, "smtp")
    else:
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
        try:
            if oauth2:
                smtp.ehlo_or_helo_if_needed()
                payload = oauth.xoauth2(config["smtp_user"], secret)
                smtp.auth("XOAUTH2", lambda challenge=None: payload if challenge is None else "")
            else:
                smtp.login(config["smtp_user"], secret)
        except smtplib.SMTPAuthenticationError as exc:
            raise smtplib.SMTPAuthenticationError(exc.smtp_code, b"Authentication failed") from None
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


def health_check(check_network: bool = False, account: str | None = None) -> dict:
    """Read-only credential, TLS, login, and Sent-folder availability checks."""
    config = account_config(account)
    result = {"config": config, "network_checked": check_network}
    for protocol in ("imap", "smtp"):
        entry = {"configured": bool(config[f"{protocol}_host"] and config[f"{protocol}_user"])}
        try:
            _require_config(config, protocol)
            if config.get(f"{protocol}_auth") == "oauth2":
                from . import oauth
                entry.update(oauth.token_status(config, protocol))
            else:
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
    conn.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(drafts)")}
    if "revision" not in columns:
        conn.execute("ALTER TABLE drafts ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    if "server_link" not in columns:
        conn.execute("ALTER TABLE drafts ADD COLUMN server_link TEXT")
    if "operation_id" not in columns:
        conn.execute("ALTER TABLE drafts ADD COLUMN operation_id TEXT")
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
        preview.pop("html_body", None)
    result = json.loads(row["result"]) if row["result"] else None
    result_key = "thunderbird_draft" if result and result.get("operation") == "save_draft_to_thunderbird" else "delivery"
    return {"draft_id": row["id"], "status": row["status"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "revision": row["revision"], **preview,
            **({"operation_id": row["operation_id"]} if row["operation_id"] else {}),
            **({"server_draft": json.loads(row["server_link"])} if row["server_link"] else {}),
            **({result_key: result} if result else {})}


def prepare_draft(to: list[str], subject: str, body: str, cc: list[str] | None = None,
                  bcc: list[str] | None = None, attachments: list[str] | None = None,
                  in_reply_to: str | None = None, references: list[str] | None = None,
                  idempotency_key: str | None = None, account: str | None = None,
                  html_body: str | None = None, inline_attachments: list[dict] | None = None,
                  identity: str | None = None) -> dict:
    """Validate and persist the exact MIME bytes and envelope to be sent later."""
    from . import policy as access
    config = account_config(account, identity=identity)
    access.require("draft", tool="draft_email", account=config["account_id"],
                   paths=[*(attachments or []), *[item.get("path", "") for item in inline_attachments or [] if isinstance(item, dict)]])
    records = _attachment_records(attachments, inline_attachments)
    preview, recipients, message, fingerprint = _compose(config, to, subject, body, cc=cc, bcc=bcc,
        in_reply_to=in_reply_to, references=references, html_body=html_body, attachment_records=records)
    if idempotency_key is not None:
        idempotency_key = _header(idempotency_key, "Idempotency key")
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("Idempotency key must contain 1 to 200 characters")
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
        conn.execute("INSERT INTO drafts (id,created_at,updated_at,status,idempotency_key,fingerprint,preview,envelope,config,message) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (draft_id, now, now, "prepared", idempotency_key, fingerprint, json.dumps(preview),
                      json.dumps(recipients), json.dumps(config), message))
        conn.commit()
        return _public(conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())


def _attachment_records(attachments, inline_attachments) -> list[dict]:
    """Read all paths once; later edits and sends use captured bytes only."""
    if attachments is not None and not isinstance(attachments, list):
        raise ValueError("Attachments must be a list of local file paths")
    if inline_attachments is not None and not isinstance(inline_attachments, list):
        raise ValueError("Inline attachments must be a list of objects with path and cid")
    records, total, cids = [], 0, set()
    for entry in [*[{"path": path} for path in attachments or []], *(inline_attachments or [])]:
        if not isinstance(entry, dict) or set(entry) - {"path", "cid"} or not isinstance(entry.get("path"), str):
            raise ValueError("Each attachment must specify a local file path; inline attachments also need cid")
        cid = entry.get("cid")
        if entry in (inline_attachments or []) and (not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,200}", cid)):
            raise ValueError("Inline cid must contain 1 to 200 letters, digits, dots, underscores, @ or hyphens")
        if cid in cids:
            raise ValueError("Inline content IDs must be unique")
        if cid:
            cids.add(cid)
        path = Path(entry["path"]).expanduser()
        if not path.is_file():
            raise ValueError(f"Attachment is not a regular file: {path}")
        if total + path.stat().st_size > _MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachments exceed the total 25 MiB limit")
        with path.open("rb") as source:
            data = source.read(_MAX_ATTACHMENT_BYTES - total + 1)
        total += len(data)
        if total > _MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachments exceed the total 25 MiB limit")
        filename = _header(path.name, "Attachment filename")
        records.append({"filename": filename, "content_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                        "data": data, "cid": cid})
    return records


def _compose(config, to, subject, body, *, cc=None, bcc=None, in_reply_to=None,
             references=None, html_body=None, attachment_records=(), message_id=None):
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
    if html_body is not None and (not isinstance(html_body, str) or "\x00" in html_body):
        raise ValueError("HTML body must be text without NUL characters")
    if in_reply_to and not _MESSAGE_ID.fullmatch(in_reply_to):
        raise ValueError("Invalid In-Reply-To message id")
    if references is not None and not isinstance(references, list):
        raise ValueError("References must be a list of message ids")
    refs = list(dict.fromkeys((references or []) + ([in_reply_to] if in_reply_to else [])))
    if any(not isinstance(item, str) or not _MESSAGE_ID.fullmatch(item) for item in refs):
        raise ValueError("Invalid References message id")
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = config["from"]
    if to_display:
        msg["To"] = ", ".join(to_display)
    if cc_display:
        msg["Cc"] = ", ".join(cc_display)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id or make_msgid(domain=config["from_email"].rsplit("@", 1)[1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if refs:
        msg["References"] = " ".join(refs)
    msg.set_content(body)
    if html_body is not None:
        msg.add_alternative(html_body, subtype="html")
    attachment_info = []
    total_bytes = 0
    for record in attachment_records:
        filename, data, content_type = record["filename"], record["data"], record["content_type"]
        total_bytes += len(data)
        if total_bytes > _MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachments exceed the total 25 MiB limit")
        maintype, subtype = content_type.split("/", 1)
        cid = record.get("cid")
        if cid:
            if html_body is None:
                raise ValueError("Inline attachments require an HTML body with a plain-text alternative")
            html_part = msg.get_body(preferencelist=("html",))
            # get_body returns the HTML child after the first add_related.
            parent = next((part for part in msg.walk() if part.get_content_type() == "multipart/related"), html_part)
            parent.add_related(data, maintype=maintype, subtype=subtype, filename=filename,
                               disposition="inline", cid=f"<{cid}>")
        else:
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        attachment_info.append({"filename": filename, "content_type": content_type, "size": len(data),
                                "sha256": hashlib.sha256(data).hexdigest(),
                                **({"cid": cid, "disposition": "inline"} if cid else {})})
    preview = {"account_id": config.get("account_id"), "identity_id": config.get("identity_id"),
               "from": config["from"], "to": to_display, "cc": cc_display, "bcc": bcc_display,
               "subject": subject, "body": body, "message_id": str(msg["Message-ID"]),
               "in_reply_to": in_reply_to, "references": refs, "attachments": attachment_info}
    if html_body is not None:
        preview["html_body"] = html_body
    fingerprint_data = {key: value for key, value in preview.items() if key != "message_id"}
    fingerprint_data["account"] = config
    fingerprint = hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True).encode()).hexdigest()
    return preview, recipients, msg.as_bytes(), fingerprint


def get_draft(draft_id: str) -> dict:
    from . import policy as access
    _validate_id(draft_id)
    with _db() as conn:
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise ValueError("Draft does not exist")
        access.require("read", tool="get_draft", account=json.loads(row["config"])["account_id"])
        return _public(row)


def list_drafts(limit: int = 20, account: str | None = None) -> dict:
    from . import policy as access
    selected = account_config(account)["account_id"] if account is not None else None
    access.require("read", tool="list_drafts", account=selected)
    with _db() as conn:
        if selected is None:
            rows = conn.execute("SELECT * FROM drafts ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                (max(1, min(limit, 100)),)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM drafts WHERE json_extract(config, '$.account_id') = ? "
                                "ORDER BY created_at DESC, rowid DESC LIMIT ?", (selected, max(1, min(limit, 100)))).fetchall()
        return {"drafts": [_public(row, include_body=False) for row in rows], "count": len(rows)}


def _revision(row, revision: int | None, *, required: bool = True) -> None:
    if revision is None and not required and row["revision"] == 1:
        return
    if isinstance(revision, bool) or not isinstance(revision, int) or revision != row["revision"]:
        raise ValueError("Draft revision changed or was not supplied; read get_draft and use its current revision")


def _captured_attachments(message: bytes) -> list[dict]:
    msg = BytesParser(policy=policy.default).parsebytes(message)
    records = []
    for part in msg.walk():
        if part.is_multipart():
            if part.get_content_disposition() == "attachment":
                raise ValueError("Editing this multipart attachment is unsupported; edit it in Thunderbird")
            continue
        if not (part.get_filename() or part.get_content_disposition() in {"inline", "attachment"} or part.get("Content-ID")):
            continue
        cid = str(part.get("Content-ID", "")).strip("<>") or None
        records.append({"filename": part.get_filename() or f"attachment-{len(records) + 1}",
                        "content_type": part.get_content_type(), "data": part.get_payload(decode=True) or b"",
                        "cid": cid if part.get_content_disposition() != "attachment" else None})
    return records


def edit_draft(draft_id: str, revision: int, changes: dict) -> dict:
    """Edit an unsent snapshot using optimistic revision checks.

    Omitted fields, captured attachments and reply headers are retained. Use
    attachments=[] or inline_attachments=[] to remove that attachment class.
    Server-linked drafts require reconciliation before their first local edit.
    """
    from . import policy as access
    _validate_id(draft_id)
    editable = {"to", "cc", "bcc", "subject", "body", "html_body", "attachments", "inline_attachments", "identity"}
    if not isinstance(changes, dict) or not changes or set(changes) - editable:
        raise ValueError("changes must contain only to, cc, bcc, subject, body, html_body, attachments, inline_attachments or identity")
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise ValueError("Draft does not exist")
        config = json.loads(row["config"])
        access.require("draft", tool="edit_draft", account=config["account_id"],
                       paths=[*(changes.get("attachments") or []),
                              *[item.get("path", "") for item in changes.get("inline_attachments") or [] if isinstance(item, dict)]])
        _revision(row, revision)
        if row["status"] not in {"prepared", "failed", "cancelled", "reconciled", "edited_for_thunderbird"}:
            raise ValueError("Only unsent local drafts or explicitly reconciled server drafts can be edited")
        if "identity" in changes:
            config = account_config(config["account_id"], identity=changes["identity"])
        preview = json.loads(row["preview"])
        records = _captured_attachments(bytes(row["message"]))
        records = [record for record in records if not (("inline_attachments" if record.get("cid") else "attachments") in changes)]
        records.extend(_attachment_records(changes.get("attachments"), changes.get("inline_attachments")))
        values = {key: changes.get(key, preview.get(key)) for key in ("to", "cc", "bcc", "subject", "body", "html_body")}
        updated, recipients, message, fingerprint = _compose(config, **values,
            in_reply_to=preview.get("in_reply_to"), references=preview.get("references"),
            attachment_records=records, message_id=preview["message_id"])
        if "forwarded_reference" in preview:
            updated["forwarded_reference"] = preview["forwarded_reference"]
        status = "edited_for_thunderbird" if row["server_link"] else "prepared"
        conn.execute("UPDATE drafts SET status=?,updated_at=?,revision=revision+1,preview=?,envelope=?,config=?,"
                     "message=?,fingerprint=?,result=NULL WHERE id=?",
                     (status, int(time.time()), json.dumps(updated), json.dumps(recipients), json.dumps(config),
                      message, fingerprint, draft_id))
        conn.commit()
        return _public(conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())


def discard_draft(draft_id: str, revision: int) -> dict:
    """Discard a local unsent snapshot; a handed-off Thunderbird copy is retained."""
    from . import policy as access
    _validate_id(draft_id)
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise ValueError("Draft does not exist")
        access.require("draft", tool="discard_draft", account=json.loads(row["config"])["account_id"])
        _revision(row, revision)
        if row["status"] not in {"prepared", "failed", "cancelled", "reconciled", "edited_for_thunderbird", "saved_to_thunderbird", "discarded"}:
            raise ValueError("Cannot discard a draft whose delivery or handoff is active, accepted, or uncertain")
        result = {"discarded": True, "sent": False, "server_draft_retained": bool(row["server_link"] or row["status"] == "saved_to_thunderbird")}
        conn.execute("UPDATE drafts SET status='discarded',result=?,updated_at=?,revision=revision+1 WHERE id=?",
                     (json.dumps(result), int(time.time()), draft_id))
        conn.commit()
        return _public(conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())


def draft_forward(reference: str, to: list[str], body: str = "", attachment_ids: list[int] | None = None,
                  cc: list[str] | None = None, bcc: list[str] | None = None, account: str | None = None,
                  identity: str | None = None, html_body: str | None = None) -> dict:
    """Prepare a new conversation with explicit recipients and selected attachments."""
    from . import live, messages, providers, policy as access
    if attachment_ids is not None and (not isinstance(attachment_ids, list) or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in attachment_ids)):
        raise ValueError("attachment_ids must contain the positive IDs from list_attachments")
    if live.is_reference(reference):
        locator = live.parse_reference(reference)
        access.require("read", tool="draft_forward", account=locator.account)
        _, raw = live._original(reference)
        selected = account or locator.account
    elif providers.is_reference(reference):
        locator = providers.parse_reference(reference)
        access.require("read", tool="draft_forward", account=locator.account)
        _, raw = providers.original(reference)
        selected = account or locator.account
    else:
        conn = _index.connect(read_only=True)
        try:
            row = messages._resolve(conn, reference)
            selected = account or infer_account(row["folder"])
            if selected is None and len([item for item in list_accounts()["accounts"] if not item.get("error")]) > 1:
                raise ValueError("The source account is ambiguous; select the forwarding account explicitly")
            access.require("read", tool="draft_forward", account=selected)
            raw = messages._source_bytes(conn, row)
        finally:
            conn.close()
    config = account_config(selected, identity=identity)
    access.require("draft", tool="draft_forward", account=config["account_id"])
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    selected_ids = set(attachment_ids or [])
    records = [{"filename": meta["filename"], "content_type": meta["content_type"], "data": data, "cid": None}
               for meta, data in messages._attachments(msg) if meta["id"] in selected_ids]
    available = {meta["id"] for meta, _ in messages._attachments(msg)}
    if selected_ids - available:
        raise ValueError("An attachment ID is missing; list the source attachments again")
    original_subject = _index._header(msg, "subject") or "(no subject)"
    subject = original_subject if original_subject.lower().lstrip().startswith(("fwd:", "fw:")) else "Fwd: " + original_subject
    quote = "\n\n---------- Forwarded message ----------\n" + "\n".join(
        f"{label}: {_index._header(msg, key)}" for label, key in (("From", "from"), ("Date", "date"), ("Subject", "subject"), ("To", "to")))
    quote += "\n\n" + _index._extract_body(msg)
    if not isinstance(body, str):
        raise ValueError("Body must be text")
    if html_body is not None:
        import html
        html_body += "<pre>" + html.escape(quote) + "</pre>"
    preview, recipients, message, fingerprint = _compose(config, to, subject, body + quote,
        cc=cc, bcc=bcc, html_body=html_body, attachment_records=records)
    preview["forwarded_reference"] = reference
    now, draft_id = int(time.time()), uuid.uuid4().hex
    with _db() as conn:
        conn.execute("INSERT INTO drafts (id,created_at,updated_at,status,fingerprint,preview,envelope,config,message) VALUES (?,?,?,?,?,?,?,?,?)",
                     (draft_id, now, now, "prepared", fingerprint, json.dumps(preview), json.dumps(recipients), json.dumps(config), message))
        conn.commit()
        return _public(conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())


def _claim_operation(conn, row, kind: str) -> str:
    """Create an operation inside the caller's shared outbox claim transaction.

    Provider adapters use this too. A claim failure occurs before transport;
    a later journal error cannot make an accepted message retryable.
    """
    from . import operations
    config = json.loads(row["config"])
    operation = operations.create_operation(kind, account=config["account_id"], total=1,
        metadata={"draft_id": row["id"], "revision": row["revision"],
                  "mime_sha256": hashlib.sha256(bytes(row["message"])).hexdigest()})
    operations.claim_operation(operation["operation_id"])
    conn.execute("UPDATE drafts SET operation_id=? WHERE id=?", (operation["operation_id"], row["id"]))
    return operation["operation_id"]


def _record(draft_id: str, status: str, result: dict, *, journal_final: bool = True) -> dict:
    result = {"draft_id": draft_id, "status": status, **result}
    with _db() as conn:
        row = conn.execute("SELECT operation_id FROM drafts WHERE id=?", (draft_id,)).fetchone()
        operation_id = row["operation_id"] if row is not None else None
        if operation_id:
            result["operation_id"] = operation_id
        conn.execute("UPDATE drafts SET status=?, result=?, updated_at=? WHERE id=?",
                     (status, json.dumps(result), int(time.time()), draft_id))
        conn.commit()
    if operation_id:
        from . import operations
        try:
            if journal_final:
                journal_state = ("completed" if status in {"sent", "saved_to_thunderbird", "saved_to_provider"} else
                                 "partial" if status == "partially_sent" else
                                 "cancelled" if status == "cancelled" else
                                 "uncertain" if "uncertain" in status else "failed")
                operations.finish_operation(operation_id, status=journal_state, result=result)
            else:
                operations.update_operation(operation_id, completed=1 if result.get("sent") or result.get("saved") else 0,
                                            result=result)
        except Exception as exc:
            # Delivery/handoff state already committed above is authoritative.
            # Never turn a journal failure into an invitation to send again.
            result["journal_error"] = _error(exc)
    return result


def _cancel_before_effect(draft_id: str, operation_id: str, *, handoff: bool = False) -> dict | None:
    from . import operations
    if not operations.cancellation_requested(operation_id):
        return None
    return _record(draft_id, "cancelled", {"sent": False, "cancelled": True, "safe_to_retry": False,
        **({"saved": False} if handoff else {}),
        "note": "Cancelled before the mail effect. Edit and review a new revision before requesting another attempt."})


def _append_sent(config: dict, message: bytes) -> None:
    from . import imap as _imap
    with _imap.session(config=config, require_move=False) as connection:
        _imap._check(connection.append(_imap._q(config["sent_folder"]), r"(\Seen)",
                                       imaplib.Time2Internaldate(time.time()), message), "APPEND Sent")


def _thunderbird_draft_destination(config: dict) -> tuple[str | None, str | None]:
    """Resolve the account's existing IMAP Drafts folder without crossing accounts."""
    current = account_config(config["account_id"])
    if any(current[key] != config[key] for key in ("imap_host", "imap_port", "imap_user")):
        raise ValueError("The draft's IMAP account has changed; prepare a new draft for the current account")
    prefs = _prefs()
    identities = [item.strip() for item in prefs.get(
        f"mail.account.{config['account_id']}.identities", "").split(",") if item.strip()]
    identity = next((item for item in identities if str(prefs.get(
        f"mail.identity.{item}.useremail", "")).casefold() == config["from_email"].casefold()), None)
    folder_identity = identity or (identities[0] if identities else "default")
    uri = prefs.get(f"mail.identity.{folder_identity}.draft_folder",
                    prefs.get("mail.identity.default.draft_folder", ""))
    folder = None
    if uri:
        parsed = urlparse(uri)
        if (parsed.scheme != "imap" or parsed.hostname != config["imap_host"].lower()
                or (parsed.username is not None and unquote(parsed.username) != config["imap_user"])
                or (parsed.port is not None and parsed.port != config["imap_port"])):
            raise ValueError("Thunderbird Drafts must be an IMAP folder in this draft's account; local or other-account folders are unsupported")
        folder = unquote(parsed.path.lstrip("/"))
    if uri and not folder:
        raise ValueError("Thunderbird has no configured Drafts folder")
    if folder:
        _header(folder, "Drafts folder")
    if identity:
        _header(identity, "Thunderbird identity")
    return folder, identity


def _resolve_drafts_folder(connection, configured: str | None) -> str:
    from . import imap as _imap
    folders = _imap.list_folders(connection)
    if configured:
        if configured not in folders:
            raise ValueError("The configured Thunderbird Drafts folder does not exist on the server")
        return configured
    special = []
    response = connection.list()
    if isinstance(response, tuple) and response[0] == "OK":
        for line in response[1]:
            match = _imap._LIST_RE.match(line.decode("utf-8", errors="replace")) if isinstance(line, bytes) else None
            if match and "\\drafts" in match["flags"].lower().split():
                name = match["name"]
                if name.startswith('"') and name.endswith('"'):
                    name = re.sub(r"\\(.)", r"\1", name[1:-1])
                special.append(_imap.decode_folder(name))
    if len(set(special)) == 1 and special[0] in folders:
        return special[0]
    if len(set(special)) > 1:
        raise ValueError("Multiple Drafts special-use folders exist; configure Thunderbird's draft_folder explicitly")
    if "Drafts" in folders:
        return "Drafts"
    raise ValueError("No Drafts folder found; configure Thunderbird's draft_folder or a server \\Drafts folder")


def _server_draft_bytes(connection, locator) -> bytes:
    """Read a still-unsent draft by UID; message content is immutable for that UID."""
    from . import live, imap as _imap
    live._select(connection, locator.folder, locator.uidvalidity)
    data = _imap._check(connection.uid("FETCH", str(locator.uid), "(UID FLAGS)"), "Draft FLAGS")
    flags = []
    for item in data:
        metadata = item[0] if isinstance(item, tuple) else item
        match = live._UID.search(metadata) if isinstance(metadata, bytes) else None
        if match and int(match[1]) == locator.uid:
            flags.append({flag.lower() for flag in imaplib.ParseFlags(metadata)})
    if len(flags) != 1 or b"\\draft" not in flags[0] or b"\\deleted" in flags[0]:
        raise ValueError("The selected server message is no longer an undeleted draft; reconcile its current reference")
    return live._raw(connection, locator.uid, time.monotonic() + live.OPERATION_SECONDS)


def _append_locator(connection, response, config, folder) -> str | None:
    from . import live
    candidates = []
    for part in response[1] if isinstance(response[1], list) else []:
        if isinstance(part, bytes):
            match = re.search(rb"\bAPPENDUID ([1-9][0-9]*) ([1-9][0-9]*)\b", part, re.I)
            if match:
                candidates.append((int(match[1]), int(match[2])))
    if not candidates:
        # imaplib commonly stores tagged response codes separately.
        value = connection.response("APPENDUID")
        if isinstance(value, tuple) and isinstance(value[1], list):
            for part in value[1]:
                match = re.fullmatch(rb"([1-9][0-9]*) ([1-9][0-9]*)", part) if isinstance(part, bytes) else None
                if match:
                    candidates.append((int(match[1]), int(match[2])))
    if len(set(candidates)) == 1:
        validity, uid = candidates[0]
        if 0 < validity < 2**32 and 0 < uid < 2**32:
            return live.Locator(config["account_id"], live._fingerprint(config), folder, validity, uid).reference
    return None


def reconcile_draft(draft_id: str, reference: str, revision: int) -> dict:
    """Import an explicit current Thunderbird draft into a new reviewed revision.

    No server mutation occurs. The account, Drafts folder, UIDVALIDITY, Draft flag
    and configured sender are validated. The local snapshot stays send-locked.
    """
    from . import live, imap as _imap, policy as access
    _validate_id(draft_id)
    locator = live.parse_reference(reference)
    with _db() as conn:
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise ValueError("Draft does not exist")
        config = json.loads(row["config"])
        access.require("draft", tool="reconcile_draft", account=config["account_id"])
        _revision(row, revision)
        if row["status"] not in {"saved_to_thunderbird", "thunderbird_save_uncertain", "reconciled", "edited_for_thunderbird"}:
            raise ValueError("Only drafts previously handed to Thunderbird can be reconciled")
    if locator.account != config["account_id"] or locator.fingerprint != live._fingerprint(config):
        raise ValueError("The server draft belongs to a different account or endpoint")
    folder, _ = _thunderbird_draft_destination(config)
    with _imap.session(config=config, require_move=False) as connection:
        if locator.folder != _resolve_drafts_folder(connection, folder):
            raise ValueError("The selected server message is outside this account's Drafts folder")
        raw = _server_draft_bytes(connection, locator)
    msg = BytesParser(policy=policy.SMTP).parsebytes(raw)
    sender_display, sender = _addresses([str(msg.get("From", ""))], "From")
    prefs = _prefs()
    identities = [item.strip() for item in str(prefs.get(f"mail.account.{config['account_id']}.identities", "")).split(",") if item.strip()]
    if sender[0].casefold() != config["from_email"].casefold():
        matches = [item for item in identities if str(prefs.get(f"mail.identity.{item}.useremail", "")).casefold() == sender[0].casefold()]
        if len(matches) != 1:
            raise ValueError("Server draft From is not a configured identity for this account")
        config = account_config(config["account_id"], identity=matches[0])
    config["from"], config["from_email"] = sender_display[0], sender[0]
    preview, envelope = {}, []
    for header in ("To", "Cc", "Bcc"):
        values = [formataddr((name, address)) for name, address in getaddresses([str(value) for value in msg.get_all(header, [])])]
        display, addresses = _addresses(values, header)
        preview[header.lower()] = display
        envelope.extend(addresses)
    if not envelope:
        raise ValueError("Server draft has no valid recipients; edit recipients in Thunderbird first")
    # Bcc remains in the preview/envelope and is restored for IMAP handoff only.
    for header in ("Bcc", "X-Identity-Key"):
        if header in msg:
            del msg[header]
    body = _index._extract_body(msg)
    html_part = msg.get_body(preferencelist=("html",))
    records = _captured_attachments(msg.as_bytes())
    preview.update(account_id=config["account_id"], identity_id=config.get("identity_id"), from_=config["from"],
        subject=_index._header(msg, "subject"), body=body,
        message_id=_index.normalize_message_id(_index._header(msg, "message-id")),
        in_reply_to=_index.normalize_message_id(_index._header(msg, "in-reply-to")) or None,
        references=_index.message_ids(_index._header(msg, "references")),
        attachments=[{"filename": item["filename"], "content_type": item["content_type"], "size": len(item["data"]),
                      "sha256": hashlib.sha256(item["data"]).hexdigest(), **({"cid": item["cid"], "disposition": "inline"} if item.get("cid") else {})}
                     for item in records])
    preview["from"] = preview.pop("from_")
    if not preview["message_id"]:
        raise ValueError("Server draft has no valid Message-ID")
    if html_part:
        preview["html_body"] = html_part.get_content()
    link = {"reference": reference, "sha256": hashlib.sha256(raw).hexdigest(), "folder": locator.folder,
            "note": "Sending remains in Thunderbird; reconcile after external edits before publishing local changes."}
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        _revision(current, revision)
        if current["status"] != row["status"]:
            raise ValueError("Draft state changed while reconciling; read its current state")
        conn.execute("UPDATE drafts SET status='reconciled',revision=revision+1,updated_at=?,preview=?,envelope=?,config=?,"
                     "message=?,fingerprint=?,server_link=?,result=NULL WHERE id=?",
                     (int(time.time()), json.dumps(preview), json.dumps(list(dict.fromkeys(envelope))), json.dumps(config),
                      msg.as_bytes(), hashlib.sha256(msg.as_bytes()).hexdigest(), json.dumps(link), draft_id))
        conn.commit()
        return _public(conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())


def save_draft_to_thunderbird(draft_id: str, revision: int | None = None) -> dict:
    """Hand a local draft to Thunderbird over IMAP, without SMTP or repeat appends.

    Editing/sending continues in Thunderbird after this handoff. The local
    snapshot cannot subsequently be sent, since Thunderbird edits are not synced
    back here. An interrupted APPEND is never retried automatically.
    """
    from . import imap as _imap, live, policy as access
    _validate_id(draft_id)
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise ValueError("Draft does not exist")
        config, preview = json.loads(row["config"]), json.loads(row["preview"])
        access.require("draft", tool="save_draft_to_thunderbird", account=config["account_id"])
        if row["status"] not in {"prepared", "edited_for_thunderbird"}:
            previous = json.loads(row["result"]) if row["result"] else {}
            saved = previous.get("saved") if previous.get("operation") == "save_draft_to_thunderbird" else False
            return {**previous, "draft_id": draft_id, "status": row["status"],
                    "saved": saved, "duplicate_prevented": True,
                    "note": previous.get("note", "Only prepared drafts can be saved. Check Thunderbird before creating another copy.")}
        _revision(row, revision, required=False)
        previous_status = row["status"]
        previous_link = json.loads(row["server_link"]) if row["server_link"] else None
        if previous_link and not previous_link.get("reference"):
            raise ValueError("Reconcile the current server draft reference before updating it")
        folder, identity = _thunderbird_draft_destination(config)
        message = BytesParser(policy=policy.SMTP).parsebytes(bytes(row["message"]))
        for header in ("Bcc", "X-Identity-Key"):
            if header in message:
                del message[header]
        if preview["bcc"]:
            message["Bcc"] = ", ".join(preview["bcc"])
        if identity:
            message["X-Identity-Key"] = identity
        message_bytes = message.as_bytes()
        operation_id = _claim_operation(conn, row, "save_draft_to_thunderbird")
        base = {"operation": "save_draft_to_thunderbird", "sent": False,
                "account_id": config["account_id"], "folder": folder,
                "message_id": preview["message_id"], "operation_id": operation_id}
        pending = {**base, "saved": None, "save_uncertain": True,
                   "note": "Save is active or interrupted; check Thunderbird before creating another copy."}
        conn.execute("UPDATE drafts SET status='saving_to_thunderbird', updated_at=?, result=? WHERE id=?",
                     (int(time.time()), json.dumps(pending), draft_id))
        conn.commit()
    attempted = False
    accepted = False
    try:
        with _imap.session(config=config, require_move=False) as connection:
            folder = _resolve_drafts_folder(connection, folder)
            base["folder"] = folder
            previous_locator = None
            if previous_link:
                previous_locator = live.parse_reference(previous_link["reference"])
                if (previous_locator.account != config["account_id"] or previous_locator.folder != folder or
                        previous_locator.fingerprint != live._fingerprint(config)):
                    raise ValueError("The previous draft reference no longer matches this account and Drafts folder")
                existing = _server_draft_bytes(connection, previous_locator)
                if hashlib.sha256(existing).hexdigest() != previous_link["sha256"]:
                    raise ValueError("The server draft changed; reconcile and review its new revision before replacing it")
            access.require("draft", tool="save_draft_to_thunderbird", account=config["account_id"])
            cancelled = _cancel_before_effect(draft_id, operation_id, handoff=True)
            if cancelled:
                return cancelled
            attempted = True
            response = connection.append(_imap._q(folder), r"(\Draft \Seen)",
                                         imaplib.Time2Internaldate(time.time()), message_bytes)
            if response[0] in ("NO", "BAD"):
                attempted = False  # A tagged rejection confirms that nothing was saved.
            _imap._check(response, "APPEND Drafts")
            accepted = True
            reference = _append_locator(connection, response, config, folder)
            link = {"reference": reference, "sha256": hashlib.sha256(message_bytes).hexdigest(), "folder": folder,
                    "note": "Reconcile the server draft before editing this snapshot; sending remains in Thunderbird."}
            with _db() as conn:
                conn.execute("UPDATE drafts SET server_link=? WHERE id=?", (json.dumps(link), draft_id))
                conn.commit()
            base["reference"] = reference
            # Record acceptance before retiring the old version. Never append
            # twice because later cleanup or logout could not be confirmed.
            result = _record(draft_id, "saved_to_thunderbird", {
                **base, "saved": True, "save_uncertain": False,
                "note": "Saved unsent. Open Thunderbird's Drafts folder to edit or send; the local snapshot is locked.",
            }, journal_final=previous_locator is None)
            if previous_locator:
                try:
                    # Re-check before deletion. Only the matched old UID is
                    # touched; never EXPUNGE unrelated deleted messages.
                    latest = _server_draft_bytes(connection, previous_locator)
                    if hashlib.sha256(latest).hexdigest() != previous_link["sha256"]:
                        raise ValueError("Previous server draft changed during replacement")
                    _imap._check(connection.select(_imap._q(folder), readonly=False), "Draft SELECT")
                    _, values = connection.response("UIDVALIDITY")
                    if not values or values[0] != str(previous_locator.uidvalidity).encode():
                        raise ValueError("Draft folder changed during replacement")
                    _imap._check(connection.uid("STORE", str(previous_locator.uid), "+FLAGS.SILENT", r"(\Deleted)"), "Retire previous draft")
                    result["previous_draft_retired"] = True
                except Exception as exc:
                    result.update(previous_draft_retired=False, cleanup_error=_error(exc),
                                  note="Updated draft saved, but the previous server copy may remain. Inspect Drafts; do not save again.")
                result = _record(draft_id, "saved_to_thunderbird", result)
        return result
    except Exception as exc:
        if accepted:
            # A logout failure must not turn an accepted APPEND into a retry.
            return _record(draft_id, "saved_to_thunderbird", {
                **base, "saved": True, "save_uncertain": False,
                "note": "Saved unsent. Edit or send in Thunderbird; the local snapshot is locked.",
            })
        return _record(draft_id, "thunderbird_save_uncertain" if attempted else previous_status, {
            **base, "saved": None if attempted else False, "save_uncertain": attempted,
            "safe_to_retry": not attempted, "error": _error(exc),
            **({"reason": "reconcile_server_draft_before_retry"} if previous_link and isinstance(exc, (ValueError, LookupError)) else {}),
            "note": ("The server may have saved the draft. Check Thunderbird before creating another copy."
                     if attempted else "Nothing was saved. Check the account and Drafts folder before retrying."),
        })


def send_draft(draft_id: str, retry_failed: bool = False, revision: int | None = None) -> dict:
    """Send a prepared draft once. Unknown SMTP outcomes are never retried."""
    from . import policy as access
    _validate_id(draft_id)
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise ValueError("Draft does not exist")
        access.require("send", tool="send_draft", account=json.loads(row["config"])["account_id"])
        if row["server_link"] or (row["status"] != "prepared" and not (row["status"] == "failed" and retry_failed)):
            previous = json.loads(row["result"]) if row["result"] else {
                "sent": None, "delivery_uncertain": True,
                "note": "An attempt is active or was interrupted; verify delivery before creating another draft.",
            }
            return {**previous, "draft_id": draft_id, "status": row["status"], "duplicate_prevented": True,
                    **({"operation_id": row["operation_id"]} if row["operation_id"] else {})}
        _revision(row, revision, required=False)
        operation_id = _claim_operation(conn, row, "send_draft")
        conn.execute("UPDATE drafts SET status='sending', updated_at=?, result=NULL WHERE id=?", (int(time.time()), draft_id))
        conn.commit()
    config, recipients, preview = json.loads(row["config"]), json.loads(row["envelope"]), json.loads(row["preview"])
    message = bytes(row["message"])
    attempted = False
    try:
        with smtp_session(config) as smtp:
            access.require("send", tool="send_draft", account=config["account_id"])
            cancelled = _cancel_before_effect(draft_id, operation_id)
            if cancelled:
                return cancelled
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
    _record(draft_id, status, result, journal_final=False)
    try:
        _append_sent(config, message)
        result["sent_copy_saved"] = True
    except Exception as exc:
        result["sent_copy_error"] = _error(exc)
        result["note"] = "SMTP accepted the message. Do not resend; only the Sent copy could not be confirmed."
    return _record(draft_id, status, result)


def send_email(to: list[str], subject: str, body: str, cc: list[str] | None = None,
               dry_run: bool = True, bcc: list[str] | None = None,
               attachments: list[str] | None = None, idempotency_key: str | None = None,
               account: str | None = None, html_body: str | None = None,
               inline_attachments: list[dict] | None = None, identity: str | None = None) -> dict:
    draft = prepare_draft(to, subject, body, cc=cc, bcc=bcc, attachments=attachments,
                          idempotency_key=idempotency_key, account=account, html_body=html_body,
                          inline_attachments=inline_attachments, identity=identity)
    if dry_run:
        return {"dry_run": True, **draft}
    return send_draft(draft["draft_id"])
