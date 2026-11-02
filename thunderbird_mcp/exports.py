"""Reviewed, resumable exports of frozen bytes, never bulk model-visible base64."""
from __future__ import annotations

import email
import email.policy
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from . import index, insights, live, messages, operations, outgoing, policy

MAX_BYTES = 64 * 1024 * 1024


@contextmanager
def _db():
    path = Path(os.environ.get("TB_EXPORTS_DB", str(index.default_db_path().parent / "export-plans.sqlite")))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, principal TEXT, account TEXT, directory TEXT, created REAL, running INTEGER DEFAULT 0, operation_id TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS files(plan TEXT, ordinal INTEGER, reference TEXT, attachment_id INTEGER, path TEXT, checksum TEXT, data BLOB, status TEXT DEFAULT 'planned', PRIMARY KEY(plan,ordinal))")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _load(conn, plan_id):
    if not isinstance(plan_id, str) or not re.fullmatch(r"export_[0-9a-f]{32}", plan_id):
        raise ValueError("Invalid export plan ID")
    row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise LookupError("Export plan not found")
    if row["principal"] != policy.current().principal:
        raise PermissionError("Export plan belongs to another principal")
    policy.require("file-write", account=row["account"], paths=[row["directory"]])
    return row


def _public(conn, plan_id):
    row = _load(conn, plan_id)
    files = [dict(v) for v in conn.execute("SELECT ordinal,reference,attachment_id,path,checksum AS sha256,length(data) AS bytes,status FROM files WHERE plan=? ORDER BY ordinal", (plan_id,))]
    return {"plan_id": plan_id, "directory": row["directory"], "operation_id": row["operation_id"],
            "created_at": row["created"], "files": files, "count": len(files),
            "bytes": sum(v["bytes"] for v in files), "running": bool(row["running"])}


def plan_export(items: list[dict], directory: str, account: str | None = None) -> dict:
    """Each item is {reference, attachment_id?}; omission exports original MIME."""
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ValueError("An export plan accepts 1–50 items")
    directory_path = Path(directory).expanduser().resolve()
    selected = outgoing.account_config(account)["account_id"] if account else None
    policy.require("file-write", tool="plan_export", account=selected, paths=[directory_path])
    prepared, total, names, actual_accounts = [], 0, set(), set()
    for ordinal, item in enumerate(items):
        if not isinstance(item, dict) or set(item) - {"reference", "attachment_id"} or not isinstance(item.get("reference"), str):
            raise ValueError("Each item requires reference and optionally attachment_id")
        reference = item["reference"]
        from . import providers
        if live.is_reference(reference):
            actual_account = live.parse_reference(reference).account
        elif providers.is_reference(reference):
            actual_account = providers.parse_reference(reference).account
        else:
            conn = index.connect(read_only=True)
            try:
                source = messages._resolve(conn, reference)
                actual_account = outgoing.infer_account(source["folder"])
            finally:
                conn.close()
        if selected is not None and actual_account != selected:
            raise ValueError("Export item does not belong to the selected account")
        actual_accounts.add(actual_account)
        if len(actual_accounts) > 1:
            raise ValueError("Export plans must contain one account; split mixed-account exports")
        raw = insights.original(reference)
        attachment_id = item.get("attachment_id")
        if attachment_id is None:
            data, suffix = raw, ".eml"
        else:
            attachment_id = messages._attachment_id(attachment_id)
            parts = list(messages._attachments(email.message_from_bytes(raw, policy=email.policy.default)))
            match = next(((meta, value) for meta, value in parts if meta["id"] == attachment_id), None)
            if match is None:
                raise LookupError("Attachment ID not found; refresh list_attachments")
            meta, data = match
            suffix = "-" + re.sub(r"[^A-Za-z0-9_.-]", "_", meta["filename"] or "attachment")[:120]
        total += len(data)
        if total > MAX_BYTES:
            raise ValueError("Export plan exceeds the 64 MiB frozen-byte limit; split the batch")
        stem = hashlib.sha256((reference + ":" + str(attachment_id)).encode()).hexdigest()[:16]
        name = stem + suffix
        collision = 0
        while name in names or (directory_path / name).exists():
            collision += 1
            if collision > 1000:
                raise ValueError("Too many destination name collisions")
            name = stem + f"-{collision}" + suffix
        names.add(name)
        path = directory_path / name
        prepared.append((ordinal, reference, attachment_id, str(path), hashlib.sha256(data).hexdigest(), data))
    plan_id = "export_" + uuid.uuid4().hex
    if selected is None:
        selected = next(iter(actual_accounts))
    with _db() as conn:
        conn.execute("INSERT INTO plans(id,principal,account,directory,created) VALUES(?,?,?,?,?)",
                     (plan_id, policy.current().principal, selected, str(directory_path), time.time()))
        conn.executemany("INSERT INTO files(plan,ordinal,reference,attachment_id,path,checksum,data) VALUES(?,?,?,?,?,?,?)", [(plan_id, *values) for values in prepared])
        conn.commit()
        return _public(conn, plan_id)


def execute_export(plan_id: str, *, resume: bool = False) -> dict:
    policy.require("file-write", tool="execute_export")
    with _db() as conn:
        row = _load(conn, plan_id)
        conn.execute("BEGIN IMMEDIATE")
        claimed = conn.execute("UPDATE plans SET running=1 WHERE id=? AND running=0", (plan_id,)).rowcount
        if not claimed:
            conn.rollback()
            raise RuntimeError("Export is already running or was interrupted; use recover_export to verify files first")
        files = conn.execute("SELECT * FROM files WHERE plan=? ORDER BY ordinal", (plan_id,)).fetchall()
        job = operations.create_operation("execute_export", account=row["account"], total=len(files))
        operation_id = job["operation_id"]
        operations.claim_operation(operation_id)
        conn.execute("UPDATE plans SET operation_id=? WHERE id=?", (operation_id, plan_id))
        conn.commit()
        errors = []
        try:
            for item in files:
                if operations.cancellation_requested(operation_id):
                    break
                path = Path(item["path"])
                policy.require("file-write", account=row["account"], paths=[path])
                try:
                    if item["status"] == "completed" or resume and path.exists():
                        if path.is_symlink() or not path.is_file() or path.stat().st_size != len(item["data"]) or hashlib.sha256(path.read_bytes()).hexdigest() != item["checksum"]:
                            raise ValueError("Existing export does not match the reviewed bytes")
                    else:
                        messages._write_export(None, path, item["data"], None)
                    conn.execute("UPDATE files SET status='completed' WHERE plan=? AND ordinal=?", (plan_id, item["ordinal"]))
                    conn.commit()
                except (OSError, ValueError) as exc:
                    errors.append({"ordinal": item["ordinal"], "error": type(exc).__name__})
                    conn.execute("UPDATE files SET status='failed' WHERE plan=? AND ordinal=?", (plan_id, item["ordinal"]))
                    conn.commit()
                operations.update_operation(operation_id, completed=item["ordinal"] + 1,
                                            result={"plan_id": plan_id, "errors": errors})
        finally:
            conn.execute("UPDATE plans SET running=0 WHERE id=?", (plan_id,))
            conn.commit()
        result = _public(conn, plan_id)
        result["errors"] = errors
        completed = sum(v["status"] == "completed" for v in result["files"])
        status = "completed" if completed == len(files) else "partial" if completed else "cancelled" if operations.cancellation_requested(operation_id) else "failed"
        operations.finish_operation(operation_id, status=status, result={"plan_id": plan_id, "exported": completed, "errors": errors})
        return result


def recover_export(plan_id: str) -> dict:
    """Recover an interrupted claim only after verifying every extant file."""
    policy.require("file-write", tool="recover_export")
    with _db() as conn:
        row = _load(conn, plan_id)
        if row["running"] and row["operation_id"]:
            job = operations.get_operation(row["operation_id"])
            if time.time() - job["updated_at"] < 300:
                raise RuntimeError("Export has recent activity; wait five minutes before recovery")
        for item in conn.execute("SELECT * FROM files WHERE plan=?", (plan_id,)):
            path = Path(item["path"])
            policy.require("file-write", account=row["account"], paths=[path])
            if path.exists():
                if path.is_symlink() or not path.is_file() or path.stat().st_size != len(item["data"]) or hashlib.sha256(path.read_bytes()).hexdigest() != item["checksum"]:
                    raise ValueError("A destination file differs from the reviewed export; resolve the collision manually")
                conn.execute("UPDATE files SET status='completed' WHERE plan=? AND ordinal=?", (plan_id, item["ordinal"]))
        conn.execute("UPDATE plans SET running=0 WHERE id=?", (plan_id,))
        conn.commit()
        return _public(conn, plan_id)
