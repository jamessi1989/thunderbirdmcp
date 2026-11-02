"""Durable, opt-in mail events and explicitly approved scheduled delivery.

Nothing starts at import or MCP startup. Callers run bounded watcher/scheduler
ticks from an explicitly configured service. All delivery uses the outbox's
atomic claim; interrupted or ambiguous attempts are never automatically retried.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import select
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import imap, index, live, operations, outgoing, policy

_SCHEDULE_ID = re.compile(r"sched_[0-9a-f]{32}\Z")
_MAX_EVENTS_PER_TICK = 500


def default_db_path() -> Path:
    return Path(os.environ.get("TB_AUTOMATION_DB", str(index.default_db_path().parent / "mail-automation.sqlite"))).expanduser()


@contextmanager
def _db():
    path = default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, revision INTEGER NOT NULL,
            account TEXT NOT NULL, principal TEXT, created_at REAL NOT NULL,
            due_at REAL NOT NULL, timezone TEXT NOT NULL, status TEXT NOT NULL,
            cancel_requested INTEGER NOT NULL DEFAULT 0, operation_id TEXT NOT NULL,
            policy TEXT NOT NULL, result TEXT, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS schedules_due ON schedules(status,due_at,id);
        CREATE TABLE IF NOT EXISTS mail_watchers (
            owner TEXT NOT NULL, account TEXT NOT NULL, fingerprint TEXT NOT NULL,
            folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL, last_uid INTEGER NOT NULL,
            updated_at REAL NOT NULL, PRIMARY KEY(owner,account,fingerprint,folder)
        );
        CREATE TABLE IF NOT EXISTS mail_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
            account TEXT NOT NULL, fingerprint TEXT NOT NULL, folder TEXT NOT NULL,
            uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL, kind TEXT NOT NULL,
            reference TEXT, created_at REAL NOT NULL,
            UNIQUE(owner,account,fingerprint,folder,uidvalidity,uid,kind)
        );
    """)
    conn.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(schedules)")}
    if "transport" not in columns:
        conn.execute("ALTER TABLE schedules ADD COLUMN transport TEXT NOT NULL DEFAULT 'smtp'")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _limit(value, name, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")


def _scope_account(account, tool):
    access = policy.current()
    if account is None and access.accounts is not None and len(access.accounts) == 1:
        account = next(iter(access.accounts))
    selected = outgoing.account_config(account)["account_id"] if account is not None else None
    policy.require("read", tool=tool, account=selected)
    return selected


def _due_time(value: str, timezone: str | None) -> tuple[float, str]:
    if not isinstance(value, str):
        raise ValueError("due_at must be an ISO date and time with an offset or explicit timezone")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("due_at must be an ISO date and time") from None
    if "T" not in value and " " not in value:
        raise ValueError("due_at must include a time, not only a calendar date")
    try:
        zone = ZoneInfo(timezone) if timezone else None
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("timezone must be a supported IANA zone such as Europe/Madrid") from None
    if parsed.tzinfo is None:
        if zone is None:
            raise ValueError("A timezone or UTC offset is required; the machine's local timezone is never inferred")
        first, second = parsed.replace(tzinfo=zone, fold=0), parsed.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("The local time is ambiguous or nonexistent at a daylight-saving transition; provide an explicit UTC offset")
        if first.astimezone(dt.timezone.utc).astimezone(zone).replace(tzinfo=None) != parsed:
            raise ValueError("The local time does not exist in the selected timezone")
        parsed = first
    elif zone is not None and parsed.utcoffset() != parsed.astimezone(zone).utcoffset():
        raise ValueError("The explicit UTC offset does not match the selected timezone at that instant")
    return parsed.timestamp(), timezone or str(parsed.tzinfo)


def _saved_policy() -> dict:
    current = policy.current()
    return {"profile": current.profile,
            "tools": sorted(current.tools) if current.tools is not None else None,
            "accounts": sorted(current.accounts) if current.accounts is not None else None,
            "file_roots": [str(path) for path in current.file_roots] if current.file_roots is not None else None,
            "principal": current.principal}


def _job_policy(row) -> policy.Policy:
    saved = json.loads(row["policy"])
    return policy.Policy(**saved)


def _schedule_row(conn, schedule_id):
    if not isinstance(schedule_id, str) or not _SCHEDULE_ID.fullmatch(schedule_id):
        raise ValueError("Invalid schedule ID")
    row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    if row is None:
        raise LookupError("Schedule not found")
    access = policy.current()
    if ((access.principal is not None and row["principal"] != access.principal) or
            (access.accounts is not None and row["account"] not in access.accounts)):
        raise policy.PolicyError("Schedule is outside the current session/account scope")
    return row


def _public_schedule(row):
    return {"schedule_id": row["id"], "draft_id": row["draft_id"], "revision": row["revision"],
            "account_id": row["account"], "due_at": dt.datetime.fromtimestamp(row["due_at"], dt.timezone.utc).isoformat(),
            "timezone": row["timezone"], "status": row["status"], "cancel_requested": bool(row["cancel_requested"]),
            "transport": row["transport"],
            "operation_id": row["operation_id"], "created_at": row["created_at"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "note": "Running or uncertain jobs are never automatically retried; cancellation cannot reverse accepted delivery."}


def schedule_draft(draft_id: str, revision: int, due_at: str, approved: bool = False,
                   timezone: str | None = None, transport: str = "smtp") -> dict:
    """Persist explicit approval of one exact draft revision at one due time."""
    if approved is not True:
        raise ValueError("Scheduling requires approved=True after the user has reviewed this saved draft revision and due time")
    if transport not in {"smtp", "gmail", "graph"}:
        raise ValueError("transport must be smtp, gmail, or graph")
    timestamp, zone = _due_time(due_at, timezone)
    if timestamp <= time.time():
        raise ValueError("The scheduled time must be in the future")
    outgoing._validate_id(draft_id)
    with outgoing._db() as outbox:
        draft = outbox.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if draft is None:
            raise ValueError("Draft does not exist")
        config = json.loads(draft["config"])
        policy.require("send", tool="schedule_draft", account=config["account_id"])
        outgoing._revision(draft, revision)
        if draft["status"] != "prepared" or draft["server_link"]:
            raise ValueError("Only an unsent prepared local draft can be scheduled")
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("SELECT * FROM schedules WHERE draft_id=? AND status IN ('queued','running')", (draft_id,)).fetchone()
        if previous is not None:
            _schedule_row(conn, previous["id"])
            if previous["revision"] != revision or previous["due_at"] != timestamp or previous["transport"] != transport:
                raise ValueError("This draft already has pending delivery; cancel that schedule before changing its time, revision or transport")
            return {**_public_schedule(previous), "duplicate_prevented": True}
        schedule_id = "sched_" + uuid.uuid4().hex
        operation = operations.create_operation("schedule_draft", account=config["account_id"], total=1,
            metadata={"schedule_id": schedule_id, "draft_id": draft_id, "revision": revision,
                      "due_at": timestamp, "transport": transport,
                      "mime_sha256": hashlib.sha256(bytes(draft["message"])).hexdigest()},
            idempotency_key=schedule_id)
        now = time.time()
        conn.execute("INSERT INTO schedules(id,draft_id,revision,account,principal,created_at,due_at,timezone,status,"
                     "cancel_requested,operation_id,policy,result,updated_at,transport) VALUES(?,?,?,?,?,?,?,?,?,0,?,?,NULL,?,?)",
                     (schedule_id, draft_id, revision, config["account_id"], policy.current().principal,
                      now, timestamp, zone, "queued", operation["operation_id"], json.dumps(_saved_policy()), now, transport))
        conn.commit()
        return _public_schedule(_schedule_row(conn, schedule_id))


def get_schedule(schedule_id: str) -> dict:
    with _db() as conn:
        row = _schedule_row(conn, schedule_id)
        policy.require("read", tool="get_schedule", account=row["account"])
        return _public_schedule(row)


def list_schedules(limit: int = 50, account: str | None = None) -> dict:
    _limit(limit, "limit", 100)
    selected = _scope_account(account, "list_schedules")
    access = policy.current()
    clauses, values = [], []
    if selected is not None:
        clauses.append("account=?")
        values.append(selected)
    if access.principal is not None:
        clauses.append("principal=?")
        values.append(access.principal)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with _db() as conn:
        rows = conn.execute(f"SELECT * FROM schedules {where} ORDER BY created_at DESC,id DESC LIMIT ?", (*values, limit)).fetchall()
    return {"schedules": [_public_schedule(row) for row in rows], "count": len(rows)}


def cancel_schedule(schedule_id: str) -> dict:
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _schedule_row(conn, schedule_id)
        policy.require("send", tool="cancel_schedule", account=row["account"])
        if row["status"] in {"queued", "running"}:
            status = "cancelled" if row["status"] == "queued" else "running"
            conn.execute("UPDATE schedules SET status=?,cancel_requested=1,updated_at=? WHERE id=?",
                         (status, time.time(), schedule_id))
            conn.commit()
            if status == "cancelled":
                operations.finish_operation(row["operation_id"], status="cancelled", result={"sent": False})
        result = _public_schedule(_schedule_row(conn, schedule_id))
        result["effects_reversed"] = False
        return result


def _finish_schedule(schedule_id, status, result):
    with _db() as conn:
        conn.execute("UPDATE schedules SET status=?,result=?,updated_at=? WHERE id=?",
                     (status, json.dumps(result), time.time(), schedule_id))
        conn.commit()
        return _public_schedule(_schedule_row(conn, schedule_id))


def run_scheduled(limit: int = 10) -> dict:
    """Run a bounded due-job tick; no service is started implicitly.

    Current process policy and the policy captured at approval both apply.
    A narrower/revoked policy leaves jobs queued. Claimed jobs remain running
    after an abrupt process exit and are never reclaimed automatically.
    """
    _limit(limit, "limit", 100)
    access = policy.current()
    clauses, values = ["status='queued'", "due_at<=?"], [time.time()]
    if access.principal is not None:
        clauses.append("principal=?")
        values.append(access.principal)
    if access.accounts is not None:
        clauses.append("account IN (" + ",".join("?" for _ in access.accounts) + ")")
        values.extend(sorted(access.accounts))
    with _db() as conn:
        rows = conn.execute("SELECT * FROM schedules WHERE " + " AND ".join(clauses) + " ORDER BY due_at,id LIMIT ?",
                            (*values, limit)).fetchall()
    outcomes = []
    for candidate in rows:
        try:
            with policy.context(_job_policy(candidate)):
                policy.require("send", tool="run_scheduled", account=candidate["account"])
                # Check future dispatch before claiming so revoked send access
                # leaves the approved job pending rather than losing it.
                delivery_tool = "send_draft" if candidate["transport"] == "smtp" else "send_draft_via_provider"
                policy.require("send", tool=delivery_tool, account=candidate["account"])
                policy.require("send", tool="schedule_draft", account=candidate["account"])
                with _db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    row = _schedule_row(conn, candidate["id"])
                    if row["status"] != "queued":
                        continue
                    if operations.cancellation_requested(row["operation_id"]):
                        conn.execute("UPDATE schedules SET status='cancelled',cancel_requested=1 WHERE id=?", (row["id"],))
                        conn.commit()
                        outcomes.append(_public_schedule(_schedule_row(conn, row["id"])))
                        continue
                    conn.execute("UPDATE schedules SET status='running',updated_at=? WHERE id=?", (time.time(), row["id"]))
                    conn.commit()
                claimed = operations.claim_operation(row["operation_id"])
                if not claimed["claimed"]:
                    outcomes.append(_finish_schedule(row["id"], "cancelled" if claimed["status"] == "cancelled" else "uncertain",
                                                     {"sent": False if claimed["status"] == "cancelled" else None,
                                                      "reason": "operation_not_claimable"}))
                    continue
                with _db() as conn:
                    latest = _schedule_row(conn, row["id"])
                    cancelled = latest["cancel_requested"] or operations.cancellation_requested(row["operation_id"])
                if cancelled:
                    result, state = {"sent": False, "cancelled": True}, "cancelled"
                else:
                    try:
                        if row["transport"] == "smtp":
                            result = outgoing.send_draft(row["draft_id"], revision=row["revision"])
                        else:
                            from . import providers
                            result = providers.send_local_draft(row["draft_id"], row["transport"], revision=row["revision"])
                        state = ("completed" if result.get("status") == "sent" else
                                 "partial" if result.get("status") == "partially_sent" else
                                 "cancelled" if result.get("status") == "cancelled" else
                                 "uncertain" if result.get("delivery_uncertain") or result.get("status") == "sending" else "failed")
                    except Exception as exc:
                        # No retry even for a known validation failure: changed
                        # revisions require fresh review and explicit approval.
                        result, state = {"sent": False, "error": outgoing._error(exc)}, "failed"
                operations.finish_operation(row["operation_id"], status=state, result=result)
                outcomes.append(_finish_schedule(row["id"], state, result))
        except policy.PolicyError:
            outcomes.append({"schedule_id": candidate["id"], "status": "blocked_by_policy", "sent": False})
    return {"schedules": outcomes, "count": len(outcomes)}


def _idle_wait(connection, seconds: int) -> str:
    """Bounded RFC 2177 IDLE for Python versions predating IMAP4.idle()."""
    capabilities = imap._check(connection.capability(), "CAPABILITY")
    if b"IDLE" not in b" ".join(part.upper() for part in capabilities if isinstance(part, bytes)).split():
        return "poll"
    # imaplib's parser retains unsolicited responses for the subsequent SEARCH.
    # The private primitives are present in the supported CPython 3.10–3.12;
    # fail clearly rather than silently issuing an unbounded protocol command.
    for name in ("_new_tag", "_get_response", "send", "sock"):
        if not hasattr(connection, name):
            raise RuntimeError("This IMAP transport does not support bounded IDLE; use polling")
    tag = connection._new_tag()
    previous_timeout = connection.sock.gettimeout()
    connection.sock.settimeout(5)
    started = False
    try:
        connection.send(tag + b" IDLE\r\n")
        deadline = time.monotonic() + 5
        while connection._get_response() is not None:
            if connection.tagged_commands.get(tag) is not None:
                raise RuntimeError("IMAP server rejected IDLE")
            if time.monotonic() >= deadline:
                raise TimeoutError("IMAP IDLE continuation timed out")
        started = True
        # A response may already be buffered by the continuation read. Polling
        # SEARCH after DONE remains authoritative, so an undetected buffer only
        # delays this tick up to its explicit wait bound.
        readable, _, _ = select.select([connection.sock], [], [], seconds)
        if readable:
            connection._get_response()
    finally:
        try:
            if started:
                connection.send(b"DONE\r\n")
                deadline = time.monotonic() + 5
                connection.sock.settimeout(5)
                while connection.tagged_commands.get(tag) is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("IMAP IDLE completion timed out")
                    connection._get_response()
                status, _ = connection.tagged_commands.pop(tag)
                if status != "OK":
                    raise RuntimeError("IMAP IDLE completion failed")
        finally:
            connection.sock.settimeout(previous_timeout)
    return "idle"


def watch_mail(account: str | None = None, folder: str = "INBOX", wait_seconds: int = 0,
               include_existing: bool = False) -> dict:
    """Record one bounded poll/IDLE tick with durable UID identities and cursors."""
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= 50:
        raise ValueError("wait_seconds must be between 0 and 50")
    if not isinstance(include_existing, bool):
        raise ValueError("include_existing must be a boolean")
    live._text(folder, "Folder", limit=1024)
    config = outgoing.account_config(account)
    policy.require("read", tool="watch_mail", account=config["account_id"])
    owner, fingerprint = policy.current().principal or "", live._fingerprint(config)
    key = (owner, config["account_id"], fingerprint, folder)
    with _db() as conn:
        old = conn.execute("SELECT * FROM mail_watchers WHERE owner=? AND account=? AND fingerprint=? AND folder=?", key).fetchone()
    mode = "poll"
    with live._session(config) as connection:
        validity = live._select(connection, folder)
        # Baseline immediately; IDLE waits apply only once the watch exists.
        if wait_seconds and old is not None and old["uidvalidity"] == validity:
            mode = _idle_wait(connection, wait_seconds)
            validity = live._select(connection, folder, validity)
        criteria = []
        if old is not None and old["uidvalidity"] == validity:
            # UID ranges can include their upper endpoint when reversed, so
            # checkpoint filtering below is still required after server search.
            criteria = ["UID", f"{min(old['last_uid'] + 1, 2**32 - 1)}:*"]
        uids = sorted(live._search_uids(connection, criteria, time.monotonic() + live.OPERATION_SECONDS))
    policy.require("read", tool="watch_mail", account=config["account_id"])
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Re-read under the claim lock: simultaneous ticks deduplicate events.
        current = conn.execute("SELECT * FROM mail_watchers WHERE owner=? AND account=? AND fingerprint=? AND folder=?", key).fetchone()
        reset = current is not None and current["uidvalidity"] != validity
        baseline = current is None and not include_existing or reset
        if reset:
            conn.execute("INSERT OR IGNORE INTO mail_events(owner,account,fingerprint,folder,uidvalidity,uid,kind,reference,created_at) VALUES(?,?,?,?,?,0,'folder_reset',NULL,?)",
                         (*key, validity, time.time()))
        last_uid = 0 if current is None or reset else current["last_uid"]
        candidates = [] if baseline else [uid for uid in uids if uid > last_uid]
        page = candidates[:_MAX_EVENTS_PER_TICK]
        for uid in page:
            reference = live.Locator(config["account_id"], fingerprint, folder, validity, uid).reference
            conn.execute("INSERT OR IGNORE INTO mail_events(owner,account,fingerprint,folder,uidvalidity,uid,kind,reference,created_at) VALUES(?,?,?,?,?,?,'new_mail',?,?)",
                         (*key, validity, uid, reference, time.time()))
        checkpoint = max(uids, default=0) if baseline else max([last_uid, *page])
        conn.execute("INSERT INTO mail_watchers VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner,account,fingerprint,folder) DO UPDATE SET "
                     "uidvalidity=excluded.uidvalidity,last_uid=excluded.last_uid,updated_at=excluded.updated_at",
                     (*key, validity, checkpoint, time.time()))
        cursor = conn.execute("SELECT coalesce(max(id),0) FROM mail_events WHERE owner=? AND account=?", (owner, config["account_id"])).fetchone()[0]
        conn.commit()
    return {"account_id": config["account_id"], "folder": folder, "uidvalidity": validity,
            "mode": mode, "baseline": baseline, "folder_reset": reset, "recorded": len(page),
            "more_pending": len(candidates) > len(page), "cursor": cursor,
            "note": "Events identify observed UID arrivals, not a guarantee of delivery time; moved/copied mail can also appear new."}


def list_mail_events(after: int = 0, limit: int = 100, account: str | None = None) -> dict:
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise ValueError("after must be a non-negative event cursor")
    _limit(limit, "limit", 500)
    selected = _scope_account(account, "list_mail_events")
    owner = policy.current().principal or ""
    clauses, values = ["owner=?", "id>?"], [owner, after]
    if selected is not None:
        clauses.append("account=?")
        values.append(selected)
    with _db() as conn:
        rows = conn.execute("SELECT * FROM mail_events WHERE " + " AND ".join(clauses) + " ORDER BY id LIMIT ?", (*values, limit + 1)).fetchall()
    page = rows[:limit]
    events = [{"cursor": row["id"], "type": row["kind"], "account_id": row["account"], "folder": row["folder"],
               "uidvalidity": row["uidvalidity"], "uid": row["uid"] or None, "reference": row["reference"],
               "observed_at": row["created_at"]} for row in page]
    return {"events": events, "count": len(events), "cursor": page[-1]["id"] if page else after,
            "more": len(rows) > limit}
