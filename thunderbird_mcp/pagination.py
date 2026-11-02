"""Private expiring search snapshots with stable cursors and bounded fan-out."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from . import index, live, outgoing, policy, search

TTL_SECONDS = 3600
MAX_SNAPSHOT_RESULTS = 5000


@contextmanager
def _db():
    path = Path(os.environ.get("TB_SEARCH_SNAPSHOTS_DB", str(index.default_db_path().parent / "search-snapshots.sqlite")))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, created REAL, principal TEXT, accounts TEXT, source TEXT, results TEXT, metadata TEXT)")
    conn.execute("DELETE FROM snapshots WHERE created<?", (time.time() - TTL_SECONDS,))
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def search_page(query: str = "", *, source: str = "local", limit: int = 20,
                cursor: str | None = None, account: str | None = None,
                scopes: list[dict] | None = None, filters: dict | None = None,
                semantic: bool = False, deduplicate_threads: bool = True) -> dict:
    if isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if source not in {"local", "live"}:
        raise ValueError("source must be local or live")
    current = policy.current()
    if cursor is not None:
        if not isinstance(cursor, str) or not re.fullmatch(r"[0-9a-f]{32}:[0-9]{1,5}", cursor):
            raise ValueError("Invalid search continuation cursor")
        snapshot_id, offset_text = cursor.split(":")
        offset = int(offset_text)
        with _db() as conn:
            row = conn.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise LookupError("Search snapshot expired; run the search again")
        if row["principal"] != current.principal:
            raise PermissionError("Search snapshot belongs to another authenticated principal")
        accounts = json.loads(row["accounts"])
        if current.accounts is not None and not accounts:
            raise PermissionError("An unscoped search snapshot cannot be reused inside an account restriction; run a new scoped search")
        for selected in accounts or [None]:
            policy.require("read", tool="search_page", account=selected)
        results, metadata, source = json.loads(row["results"]), json.loads(row["metadata"]), row["source"]
        created = row["created"]
        if offset > len(results):
            raise ValueError("Continuation offset is outside the search snapshot")
    else:
        filters = filters or {}
        if not isinstance(filters, dict):
            raise ValueError("filters must be an object")
        allowed = {"folder", "sender", "recipient", "since", "until", "has_attachment", "attachment"} if source == "local" else {"sender", "recipient", "subject", "since", "until", "unread"}
        if set(filters) - allowed:
            raise ValueError("Unsupported filters: " + ", ".join(sorted(set(filters) - allowed)))
        results, accounts, errors = [], [], []
        known_matches = 0
        diagnostics = {}
        if source == "local":
            config = outgoing.account_config(account) if account else None
            selected = config["account_id"] if config else None
            policy.require("read", tool="search_page", account=selected)
            if selected:
                accounts.append(selected)
            values = search.search(query, limit=200, use=("fts", "vec") if semantic else ("fts",),
                                   account_prefix=config["account_prefix"] if config else None,
                                   deduplicate_threads=deduplicate_threads, _candidate_limit=MAX_SNAPSHOT_RESULTS,
                                   _return_all=True, **filters)
            results = list(values)[:MAX_SNAPSHOT_RESULTS]
            diagnostics = values.diagnostics
            known_matches = diagnostics["keyword_matched"]
        else:
            scopes = scopes or [{"account": account, "folder": "INBOX"}]
            if not isinstance(scopes, list) or not 1 <= len(scopes) <= 10:
                raise ValueError("Live search accepts 1–10 account/folder scopes")
            deadline = time.monotonic() + live.OPERATION_SECONDS
            unique = set()
            for scope in scopes:
                if not isinstance(scope, dict) or set(scope) - {"account", "folder"}:
                    raise ValueError("Each scope accepts account and folder")
                config = outgoing.account_config(scope.get("account"))
                selected = config["account_id"]
                policy.require("read", tool="search_page", account=selected)
                if selected not in accounts:
                    accounts.append(selected)
                folder = scope.get("folder", "INBOX")
                if (selected, folder) in unique:
                    continue
                unique.add((selected, folder))
                try:
                    if len(results) >= MAX_SNAPSHOT_RESULTS or time.monotonic() >= deadline:
                        raise RuntimeError("Fan-out snapshot limit reached; narrow the scopes")
                    page = live.search_email(query, account=selected, folder=folder,
                                             _snapshot_limit=MAX_SNAPSHOT_RESULTS - len(results), **filters)
                    results.extend(page["results"])
                    known_matches += page["matched"]
                except (ValueError, LookupError, RuntimeError, OSError) as exc:
                    errors.append({"account_id": selected, "folder": folder, "error": type(exc).__name__})
            diagnostics = {"semantic": "disabled", "supported_filters": sorted(allowed),
                           "order": "scope_order_then_newest_uid", "coverage": "explicit_scopes_only"}
        metadata = {"diagnostics": diagnostics, "scope_errors": errors,
                    "known_matching_messages": known_matches,
                    "snapshot_results": len(results), "omitted_before_snapshot": max(0, known_matches - len(results)),
                    "coverage_complete": not errors and known_matches <= len(results) and not diagnostics.get("candidate_coverage_limited", False)}
        snapshot_id, offset, created = uuid.uuid4().hex, 0, time.time()
        with _db() as conn:
            conn.execute("DELETE FROM snapshots WHERE id IN (SELECT id FROM snapshots ORDER BY created DESC LIMIT -1 OFFSET 127)")
            conn.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)", (snapshot_id, created, current.principal,
                         json.dumps(accounts), source, json.dumps(results), json.dumps(metadata)))
            conn.commit()
    page_results = results[offset:offset + limit]
    page_errors = []
    if source == "live":
        hydrated = []
        deadline = time.monotonic() + live.OPERATION_SECONDS
        for item in page_results:
            locator = live.parse_reference(item["reference"])
            try:
                live._deadline(deadline)
                with live._session(live._config(locator)) as connection:
                    live._select(connection, locator.folder, locator.uidvalidity)
                    hydrated.append(live._header(connection, locator, deadline))
            except (ValueError, LookupError, RuntimeError, OSError) as exc:
                page_errors.append({"reference": locator.reference, "error": type(exc).__name__})
        page_results = hydrated
    next_offset = min(len(results), offset + limit)
    return {**metadata, "source": source, "count": len(page_results), "results": page_results,
            "page_errors": page_errors, "snapshot_created_at": created,
            "expires_at": created + TTL_SECONDS, "remaining_in_snapshot": len(results) - next_offset,
            "next_cursor": f"{snapshot_id}:{next_offset}" if next_offset < len(results) else None}
