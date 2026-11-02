"""Read-only diagnostics for the mailbox, local index, and account connection."""

from __future__ import annotations

import datetime as dt
import importlib.metadata
import sqlite3
from pathlib import Path

from . import index
from .profile import discover_folders, find_profile


def _iso(timestamp: int | float | None) -> str | None:
    if timestamp is None or timestamp == 0:
        return None
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat()


def mail_status(check_network: bool = False, account: str | None = None) -> dict:
    """Inspect local health; optionally authenticate to IMAP/SMTP without sending."""
    result: dict = {"ok": True, "transport": "stdio", "warnings": []}
    try:
        result["version"] = importlib.metadata.version("thunderbird-mcp")
    except importlib.metadata.PackageNotFoundError:
        result["version"] = "development"

    folders = None
    try:
        profile = find_profile()
        folders = discover_folders(profile)
        result["profile"] = {"path": str(profile), "local_folders": len(folders)}
    except (OSError, ValueError) as exc:
        result["profile"] = {"error": str(exc)}
        result["warnings"].append("Thunderbird's local mail store is unavailable.")

    path = index.default_db_path()
    result["index"] = {"path": str(path), "exists": path.is_file()}
    if path.is_file():
        conn = None
        try:
            conn = index.connect(read_only=True)
            aggregate = conn.execute(
                "SELECT count(*) AS messages, count(DISTINCT folder) AS folders, "
                "max(date_ts) AS newest FROM emails"
            ).fetchone()
            indexed = {r["folder"]: r for r in conn.execute("SELECT * FROM folders")}
            changed = []
            missing = []
            if folders is not None:
                for folder in folders:
                    prior = indexed.get(folder.name)
                    if (prior is None or prior["size"] != folder.size
                            or abs((prior["mtime"] or 0) - folder.mtime) >= 0.001
                            or prior["schema_version"] != index.SCHEMA_VERSION):
                        changed.append(folder.name)
                missing = sorted(set(indexed) - {f.name for f in folders})
            result["index"].update({
                "messages": aggregate["messages"],
                "folders": aggregate["folders"],
                "tracked_local_folders": len(indexed),
                "newest_message": _iso(aggregate["newest"]),
                "last_folder_indexed": _iso(max(
                    (r["indexed_at"] or 0 for r in indexed.values()), default=0)),
                "changed_local_folders": changed,
                "missing_local_folders": missing,
                "refresh_needed": bool(changed or missing),
                "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
            })
            if changed or missing:
                result["warnings"].append("The index needs a refresh from Thunderbird's local mail.")
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'"
            ).fetchone()
            vectors = conn.execute("SELECT count(*) FROM embeddings").fetchone()[0] if exists else 0
            result["search"] = {"keyword": True, "stored_embeddings": vectors,
                                "semantic_index_available": bool(vectors)}
            columns = {row[1] for row in conn.execute("PRAGMA table_info(emails)")}
            if "attachments_indexed" in columns:
                extracted = conn.execute(
                    "SELECT sum(attachment_count) AS attachments, "
                    "sum(CASE WHEN attachments_indexed=1 THEN 1 ELSE 0 END) AS messages_processed, "
                    "sum(CASE WHEN length(attachment_text)>0 THEN 1 ELSE 0 END) AS messages_with_text, "
                    "sum(CASE WHEN attachments_indexed=0 AND attachment_count>0 THEN 1 ELSE 0 END) AS pending "
                    "FROM emails"
                ).fetchone()
                result["attachments"] = {key: extracted[key] or 0 for key in extracted.keys()}
                result["attachments"]["extraction_statuses"] = {
                    row["status"]: row["count"] for row in conn.execute(
                        "SELECT json_extract(a.value,'$.status') AS status, count(*) AS count "
                        "FROM emails e, json_each(e.attachments_json) a GROUP BY status")
                }
        except (sqlite3.Error, OSError) as exc:
            result["ok"] = False
            result["index"]["error"] = str(exc)
        finally:
            if conn is not None:
                conn.close()
    else:
        result["ok"] = False
        result["warnings"].append("Build the index with tbmail reindex.")

    from . import outgoing
    try:
        result["account"] = outgoing.account_config(account)
        result["connection"] = outgoing.health_check(check_network=check_network, account=account)
        if not result["connection"].get("ready", False):
            result["ok"] = False
    except (RuntimeError, OSError, ValueError) as exc:
        result["account"] = {"error": str(exc)}
        result["ok"] = False
    result["scope"] = ("Local search covers downloaded mail and extracted attachments. "
                       "Live search reads the selected IMAP folder directly. "
                       "IMAP/SMTP use the selected account; drafts retain their original account.")
    return result
