"""Private, durable journal for bounded jobs and cooperative cancellation.

The journal records progress; it does not retry or undo effects. In particular a
running/uncertain delivery is never made claimable again by process restart.
Callers check cancellation between work items and record each committed effect.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import index, policy


STATES = frozenset({"queued", "running", "cancel_requested", "cancelled", "completed", "partial", "failed", "uncertain"})
TERMINAL = frozenset({"cancelled", "completed", "partial", "failed", "uncertain"})
MAX_JSON_BYTES = 128 * 1024
_ID = re.compile(r"op_[0-9a-f]{32}\Z")
_SECRET_KEYS = frozenset({"password", "token", "access_token", "refresh_token", "client_secret",
                          "authorization", "credentials", "credential", "mime", "raw_message",
                          "message_bytes", "body", "body_html"})


def default_db_path() -> Path:
    parent = (Path(os.environ["TB_OUTBOX_DB"]).expanduser().parent if os.environ.get("TB_OUTBOX_DB")
              else index.default_db_path().parent)
    return Path(os.environ.get("TB_OPERATIONS_DB", str(parent / "mail-operations.sqlite"))).expanduser()


@contextmanager
def _db(database=None):
    path = Path(database or default_db_path()).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("""CREATE TABLE IF NOT EXISTS operations (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, account TEXT, principal TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, status TEXT NOT NULL,
        completed INTEGER NOT NULL DEFAULT 0, total INTEGER,
        cancellation_requested INTEGER NOT NULL DEFAULT 0,
        metadata TEXT NOT NULL, result TEXT, error TEXT, idempotency_key TEXT
    )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS operations_key ON operations(kind, ifnull(account,''), ifnull(principal,''), idempotency_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS operations_recent ON operations(created_at DESC, id DESC)")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _json(value) -> str:
    def clean(item, depth=0):
        if depth > 16:
            raise ValueError("Operation metadata nesting exceeds the limit")
        if isinstance(item, dict):
            return {str(key): "[redacted]" if str(key).lower().replace("-", "_") in _SECRET_KEYS
                    else clean(child, depth + 1) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(child, depth + 1) for child in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise ValueError("Operation metadata must be JSON-compatible; raw bytes are not accepted")
    result = json.dumps(clean(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(result.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("Operation metadata exceeds the 128 KiB limit")
    return result


def _error(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, BaseException):
        result = {"type": type(value).__name__}
    elif isinstance(value, dict):
        # Provider response strings may contain credentials or message contents.
        result = {"type": str(value.get("type", "OperationError"))[:80]}
        for key in ("code", "smtp_code", "http_status"):
            if isinstance(value.get(key), int):
                result[key] = value[key]
    else:
        result = {"type": "OperationError"}
    return _json(result)


def _check_count(value, name):
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{name} must be a non-negative integer")


def _row(conn, operation_id):
    if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
        raise ValueError("Invalid operation ID")
    row = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
    if row is None:
        raise LookupError("Operation not found")
    access = policy.current()
    if access.principal is not None and row["principal"] != access.principal:
        raise policy.PolicyError("Operation not found in the current session scope")
    if access.accounts is not None and row["account"] not in access.accounts:
        raise policy.PolicyError("Operation not found in the current account scope")
    return row


def _public(row):
    return {
        "operation_id": row["id"], "kind": row["kind"], "account_id": row["account"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "status": row["status"], "completed": row["completed"], "total": row["total"],
        "cancellation_requested": bool(row["cancellation_requested"]),
        "metadata": json.loads(row["metadata"]),
        "result": json.loads(row["result"]) if row["result"] is not None else None,
        "error": json.loads(row["error"]) if row["error"] is not None else None,
    }


def _authorize_work(row):
    policy.require(policy.TOOL_CAPABILITIES.get(row["kind"], "mail-write"),
                   tool=row["kind"], account=row["account"])


def create_operation(kind: str, *, account: str | None = None, total: int | None = None,
                     metadata: dict | None = None, idempotency_key: str | None = None,
                     database=None) -> dict:
    """Create queued work, or return the same scoped idempotent request."""
    if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", kind):
        raise ValueError("Invalid operation kind")
    _check_count(total, "total")
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200):
        raise ValueError("idempotency_key must contain 1 to 200 characters")
    policy.require(policy.TOOL_CAPABILITIES.get(kind, "mail-write"), tool=kind, account=account)
    principal = policy.current().principal
    encoded = _json(metadata or {})
    with _db(database) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if idempotency_key is not None:
            old = conn.execute("SELECT * FROM operations WHERE kind=? AND account IS ? AND principal IS ? AND idempotency_key=?",
                               (kind, account, principal, idempotency_key)).fetchone()
            if old is not None:
                if old["metadata"] != encoded or old["total"] != total:
                    raise ValueError("Idempotency key already belongs to a different operation plan")
                return _public(old)
        operation_id = "op_" + uuid.uuid4().hex
        now = time.time()
        conn.execute("INSERT INTO operations(id,kind,account,principal,created_at,updated_at,status,total,metadata,idempotency_key) VALUES(?,?,?,?,?,?,'queued',?,?,?)",
                     (operation_id, kind, account, principal, now, now, total, encoded, idempotency_key))
        conn.commit()
        return _public(_row(conn, operation_id))


def claim_operation(operation_id: str, *, database=None) -> dict:
    """Atomically claim queued work once. Running/uncertain work is not retried."""
    with _db(database) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn, operation_id)
        _authorize_work(row)
        claimed = row["status"] == "queued"
        if claimed:
            conn.execute("UPDATE operations SET status='running',updated_at=? WHERE id=?", (time.time(), operation_id))
        conn.commit()
        return {**_public(_row(conn, operation_id)), "claimed": claimed}


def update_operation(operation_id: str, *, completed: int | None = None, total: int | None = None,
                     result=None, error=None, database=None) -> dict:
    """Checkpoint committed outcomes between items; progress never moves back."""
    _check_count(completed, "completed")
    _check_count(total, "total")
    encoded_result, encoded_error = (_json(result) if result is not None else None), _error(error)
    with _db(database) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn, operation_id)
        _authorize_work(row)
        if row["status"] not in {"running", "cancel_requested"}:
            raise ValueError("Only a running operation can record progress")
        done = row["completed"] if completed is None else completed
        count = row["total"] if total is None else total
        if done < row["completed"] or (count is not None and done > count):
            raise ValueError("Operation progress must be monotonic and cannot exceed total")
        conn.execute("UPDATE operations SET completed=?,total=?,result=coalesce(?,result),error=coalesce(?,error),updated_at=? WHERE id=?",
                     (done, count, encoded_result, encoded_error, time.time(), operation_id))
        conn.commit()
        return _public(_row(conn, operation_id))


def finish_operation(operation_id: str, *, status: str = "completed", result=None,
                     error=None, database=None) -> dict:
    """Record a terminal outcome. Cancellation does not reverse earlier effects."""
    if status not in TERMINAL:
        raise ValueError("A terminal operation status is required")
    encoded_result, encoded_error = (_json(result) if result is not None else None), _error(error)
    with _db(database) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn, operation_id)
        # Finishing is allowed after policy revocation to retain the true outcome.
        if row["status"] in TERMINAL:
            if row["status"] != status:
                raise ValueError("An operation's terminal outcome cannot be changed")
            return _public(row)
        if row["status"] == "queued" and status not in {"cancelled", "failed"}:
            raise ValueError("Claim an operation before recording executed effects")
        conn.execute("UPDATE operations SET status=?,result=coalesce(?,result),error=coalesce(?,error),updated_at=? WHERE id=?",
                     (status, encoded_result, encoded_error, time.time(), operation_id))
        conn.commit()
        return _public(_row(conn, operation_id))


def request_cancel(operation_id: str, *, database=None) -> dict:
    """Cancel queued work or ask a running worker to stop between effects."""
    with _db(database) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn, operation_id)
        policy.require("mail-write", tool="cancel_operation", account=row["account"])
        if row["status"] not in TERMINAL:
            status = "cancelled" if row["status"] == "queued" else "cancel_requested"
            conn.execute("UPDATE operations SET status=?,cancellation_requested=1,updated_at=? WHERE id=?",
                         (status, time.time(), operation_id))
            conn.commit()
        result = _public(_row(conn, operation_id))
        result["effects_reversed"] = False
        return result


def cancellation_requested(operation_id: str, *, database=None) -> bool:
    with _db(database) as conn:
        return bool(_row(conn, operation_id)["cancellation_requested"])


def get_operation(operation_id: str, *, database=None) -> dict:
    with _db(database) as conn:
        row = _row(conn, operation_id)
        policy.require("read", tool="get_operation", account=row["account"])
        return _public(row)


def list_operations(*, limit: int = 20, account: str | None = None, status: str | None = None,
                    before: str | None = None, database=None) -> dict:
    """List bounded history with a stable creation-time/ID continuation cursor."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if status is not None and status not in STATES:
        raise ValueError("Unknown operation status")
    access = policy.current()
    if account is None and access.accounts is not None and len(access.accounts) == 1:
        account = next(iter(access.accounts))
    policy.require("read", tool="list_operations", account=account)
    clauses, values = [], []
    if account is not None:
        clauses.append("account=?")
        values.append(account)
    if access.principal is not None:
        clauses.append("principal=?")
        values.append(access.principal)
    if status is not None:
        clauses.append("status=?")
        values.append(status)
    if before is not None:
        try:
            if len(before) > 512:
                raise ValueError
            created, operation_id = json.loads(base64.urlsafe_b64decode(before.encode()).decode())
            if not isinstance(created, (int, float)) or not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
                raise ValueError
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("Invalid operation cursor") from exc
        clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
        values.extend((created, created, operation_id))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with _db(database) as conn:
        rows = conn.execute(f"SELECT * FROM operations {where} ORDER BY created_at DESC,id DESC LIMIT ?", (*values, limit + 1)).fetchall()
        page = rows[:limit]
        cursor = None
        if len(rows) > limit:
            last = page[-1]
            cursor = base64.urlsafe_b64encode(json.dumps([last["created_at"], last["id"]]).encode()).decode()
        return {"operations": [_public(row) for row in page], "count": len(page), "next_cursor": cursor}
