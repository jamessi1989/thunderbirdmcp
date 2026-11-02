"""Hybrid mail search: FTS5 keywords + local multilingual embeddings, fused by rank.

Embeddings are keyed by Message-ID (or a content hash) rather than row id,
because reindexing a folder deletes and re-inserts its rows with new ids, and
the same message often lives in several folders.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

from . import index as _index
from . import messages as _messages

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_CHARS = 600  # the model truncates at 128 tokens anyway
RRF_K = 60
RECENCY_HALF_LIFE_DAYS = 365

EMB_TABLE = "embeddings"


def _schema(table: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError("Invalid embedding table name")
    return f"CREATE TABLE IF NOT EXISTS {table} (key TEXT PRIMARY KEY, vec BLOB NOT NULL, doc_hash TEXT, model TEXT)"
_FILTER = re.compile(r'(?<!\S)(from|to|folder|after|before|has|attachment):("[^"]*"|\S+)', re.I)

RERANK_MODEL = "jinaai/jina-reranker-v2-base-multilingual"
RERANK_TOP = 20
RERANK_CHARS = 400
RERANK_WEIGHT = 1.5

_BOILER = [
    re.compile(r"<URL: https?://\S+ >"),
    re.compile(r"^.*Request \d+ was acted upon\..*?Requestors?:[^\n]*", re.S | re.M),
    re.compile(r"^>.*$", re.M),                      # quoted replies
    re.compile(r"^On .{5,80} wrote:\s*$", re.M),
    re.compile(r"^El .{5,80} escribió:\s*$", re.M),
    re.compile(r"\n-- ?\n.*", re.S),                 # signature and below
    re.compile(r"-{20,}.*", re.S),                     # RT/helpdesk signature rulers
]


def clean_body(body: str) -> str:
    """Strip ticket boilerplate, quoted history and signatures; keep what the message says."""
    for rx in _BOILER:
        body = rx.sub(" ", body)
    return " ".join(body.split())

_model = None
_reranker = None
_model_lock = threading.Lock()
_logger = logging.getLogger(__name__)


def _threads() -> int:
    try:
        return max(1, min(8, int(os.environ.get("TB_EMBED_THREADS", "2"))))
    except ValueError:
        return 2


def _cross_encoder():
    global _reranker
    if _reranker is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        _reranker = TextCrossEncoder(RERANK_MODEL, threads=_threads())
    return _reranker


def _embedder():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from fastembed import TextEmbedding
                _model = TextEmbedding(MODEL, threads=_threads())
    return _model


def _key(message_id: str | None, sender: str, subject: str, date_ts: int) -> str:
    if message_id:
        return message_id
    return "h:" + hashlib.sha1(f"{sender}|{subject}|{date_ts}".encode()).hexdigest()


def _doc(subject: str, sender: str, body: str, attachment_text: str = "", attachment_names: str = "") -> str:
    text = clean_body(body or "")
    if attachment_text or attachment_names:
        text = text[:400] + "\nAttachments: " + clean_body(attachment_names + " " + attachment_text)[:200]
    return f"{subject or ''}\n{sender or ''}\n{text[:EMBED_CHARS]}"


def _embedding_schema(conn, table):
    conn.execute(_schema(table))
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for column in ("doc_hash", "model"):
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    conn.execute("INSERT OR IGNORE INTO index_meta(key,value) VALUES (?,0)", (f"{table}_revision",))
    for suffix, event in (("ai", "INSERT"), ("au", "UPDATE"), ("ad", "DELETE")):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_revision_{suffix} AFTER {event} ON {table} BEGIN "
                     f"UPDATE index_meta SET value=value+1 WHERE key='{table}_revision'; END")
    conn.execute(f"DROP TRIGGER IF EXISTS {table}_invalidate")
    conn.execute(f"CREATE TRIGGER {table}_invalidate AFTER UPDATE OF subject,sender,body,attachment_text,attachment_names ON emails "
                 "WHEN old.subject IS NOT new.subject OR old.sender IS NOT new.sender OR old.body IS NOT new.body "
                 "OR old.attachment_text IS NOT new.attachment_text OR old.attachment_names IS NOT new.attachment_names BEGIN "
                 f"DELETE FROM {table} WHERE key IN (old.stable_key,old.message_id); END")
    conn.commit()


def embed_missing(progress=print, table: str | None = None, *, db_path: Path | None = None) -> int:
    """Embed every indexed message that has no stored vector yet."""
    table = table or EMB_TABLE
    conn = _index.connect(db_path)
    try:
        _embedding_schema(conn, table)
        have = {r[0]: (r[1], r[2]) for r in conn.execute(f"SELECT key,doc_hash,model FROM {table}")}
        todo: dict[str, tuple[str, str]] = {}
        for r in conn.execute("SELECT * FROM emails"):
            k = _messages.key_for_row(r)
            doc = _doc(r["subject"], r["sender"], r["body"], r["attachment_text"], r["attachment_names"])
            digest = hashlib.sha256(doc.encode()).hexdigest()
            if have.get(k) != (digest, MODEL) and k not in todo:
                todo[k] = (doc, digest)
        if not todo:
            return 0
        keys = list(todo)
        done, t0 = 0, time.time()
        for i in range(0, len(keys), 512):
            batch = keys[i:i + 512]
            vecs = list(_embedder().embed([todo[k][0] for k in batch], batch_size=64))
            if len(vecs) != len(batch):
                raise RuntimeError("Embedding model returned an incomplete batch")
            conn.executemany(
                f"INSERT OR REPLACE INTO {table}(key,vec,doc_hash,model) VALUES (?,?,?,?)",
                [(k, np.asarray(v, dtype=np.float32).tobytes(), todo[k][1], MODEL) for k, v in zip(batch, vecs)])
            conn.executemany(f"DELETE FROM {table} WHERE key=?", [(k[4:],) for k in batch if k.startswith("mid:")])
            conn.commit()
            done += len(vecs)
            if progress:
                progress(f"embedded {done}/{len(todo)} ({done / max(time.time() - t0, .001):.0f}/s)")
        conn.execute(f"DELETE FROM {table} WHERE NOT EXISTS (SELECT 1 FROM emails WHERE stable_key={table}.key OR message_id={table}.key)")
        conn.commit()
        return done
    finally:
        conn.close()


class _VectorCache:
    """All embeddings in memory (~40k x 384 floats = 60 MB); reloaded when the table grows."""

    def __init__(self):
        self.lock = threading.Lock()
        self.token = None
        self.keys: list[str] = []
        self.matrix = np.zeros((0, 384), dtype=np.float32)

    def get(self, conn: sqlite3.Connection):
        with self.lock:
            return self._get(conn)

    def _get(self, conn: sqlite3.Connection):
        try:
            _schema(EMB_TABLE)
            token = _database_token(conn, EMB_TABLE)
            rows = None
            if token != self.token:
                rows = conn.execute(f"SELECT key,vec FROM {EMB_TABLE}").fetchall()
        except sqlite3.OperationalError:
            return [], np.zeros((0, 0), dtype=np.float32)
        if rows is not None:
            valid = [(r[0], np.frombuffer(r[1], dtype=np.float32)) for r in rows if len(r[1]) and len(r[1]) % 4 == 0]
            dimensions = max((len(v) for _, v in valid), default=0)
            valid = [(k, v) for k, v in valid if len(v) == dimensions and np.isfinite(v).all() and np.linalg.norm(v) > 0]
            self.keys = [k for k, _ in valid]
            m = np.stack([v for _, v in valid]) if valid else np.zeros((0, 0), dtype=np.float32)
            self.matrix = m / np.linalg.norm(m, axis=1, keepdims=True) if valid else m
            self.token = token
        return self.keys, self.matrix


def _database_token(conn, table="emails"):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    # File identity + WAL modification time catch same-count edits from other
    # processes, and do not confuse two databases that happen to have equal size.
    stamps = []
    for name in (path, path + "-wal"):
        try:
            stat = os.stat(name)
            stamps.append((stat.st_ino, stat.st_mtime_ns, stat.st_size))
        except OSError:
            stamps.append(None)
    revision = conn.execute("SELECT value FROM index_meta WHERE key=?", ("revision" if table == "emails" else f"{table}_revision",)).fetchone()
    return path, table, tuple(stamps), revision[0] if revision else None, conn.total_changes


_cache = _VectorCache()


def _parse(raw: str) -> tuple[str, dict]:
    filters = {}
    for m in _FILTER.finditer(raw):
        quoted = m.group(2)
        if quoted.startswith('"') and not quoted.endswith('"'):
            raise ValueError(f"Unclosed quote in {m.group(1)}: filter")
        name, value = m.group(1).lower(), quoted.strip('"')
        if not value or '"' in value:
            raise ValueError(f"Invalid {name}: filter")
        if name in filters:
            raise ValueError(f"Duplicate {name}: filter")
        filters[name] = value
    text = _FILTER.sub(" ", raw).strip()
    if re.search(r"(?<!\S)(from|to|folder|after|before|has|attachment):", text, re.I):
        raise ValueError("A mail filter is missing a value or closing quote")
    return text, filters


def _epoch(value: str) -> int:
    try:
        dt = _dt.datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid date {value!r}; use YYYY-MM-DD or an ISO timestamp") from exc


# Function words in the languages this mailbox uses (en/es/ca). As OR-ed prefix
# terms they match nearly every message and drown out the words that matter.
_STOP = set("""
a an and are as at be by can for from has have how i in is it me my of on or our please re
that the this to was we what when where which who with you your about after any been did do
does get got not some there they will would
de del el la los las un una y o en que por para con se su al lo como mas pero sus le ya
es son esta este hay sobre sin
els les i amb per dels una uns es pel
""".split())

_vocab_cache: dict = {"token": None, "terms": [], "doc": {}}


def _vocab(conn) -> tuple[list[str], dict[str, int]]:
    token = _database_token(conn)
    if token != _vocab_cache["token"]:
        rows = conn.execute(
            "SELECT term, doc FROM emails_vocab WHERE doc >= 3 AND length(term) BETWEEN 4 AND 24"
            " AND term NOT GLOB '*[0-9]*'").fetchall()
        _vocab_cache.update(token=token, terms=[r[0] for r in rows], doc={r[0]: r[1] for r in rows})
    return _vocab_cache["terms"], _vocab_cache["doc"]


def _correct(word: str, conn) -> str | None:
    """Closest common indexed term for a word that looks misspelt, else None."""
    if len(word) < 5 or any(c.isdigit() for c in word):
        return None
    terms, doc = _vocab(conn)
    if doc.get(word, 0) >= 3 or conn.execute(
            "SELECT 1 FROM emails_vocab WHERE term = ?", (word,)).fetchone():
        return None
    from rapidfuzz import process
    from rapidfuzz.distance import DamerauLevenshtein
    hits = process.extract(word, terms, scorer=DamerauLevenshtein.distance,
                           score_cutoff=1 if len(word) < 8 else 2, limit=5)
    return max(hits, key=lambda h: (-h[1], doc[h[0]]))[0] if hits else None


def _fts_expr(text: str, conn, op: str = "OR") -> str:
    # OR of terms: bm25 rewards matching more of them, and nothing is dropped
    # just because one word is missing. Prefix-match only longer words, so
    # "vm" does not expand to every word starting with "vm".
    parts = []
    for w in re.findall(r"\w+", text.lower(), re.UNICODE):
        if w in _STOP or len(w) < 2:
            continue
        alts = [f'"{w}"*' if len(w) >= 4 else f'"{w}"']
        if (fix := _correct(w, conn)):
            alts.append(f'"{fix}"')
        parts.append("(" + " OR ".join(alts) + ")")
    return f" {op} ".join(parts)


class SearchResults(list):
    """List-compatible results with optional health details for MCP callers."""
    def __init__(self, values=(), *, diagnostics=None):
        super().__init__(values)
        self.diagnostics = diagnostics or {}


def search(raw: str, limit: int = 40, use: tuple = ("fts", "vec", "rerank"), *,
           db_path: Path | None = None, folder: str | None = None, sender: str | None = None,
           recipient: str | None = None, since: str | None = None, until: str | None = None,
           has_attachment: bool | None = None, attachment: str | None = None,
           account_prefix: str | None = None,
           deduplicate_threads: bool = True) -> list[dict]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if not isinstance(raw, str) or len(raw) > 10_000:
        raise ValueError("query must be a string of at most 10000 characters")
    text, f = _parse(raw)
    for name, value in (("folder", folder), ("from", sender), ("to", recipient), ("after", since), ("attachment", attachment)):
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} filter must not be empty")
            f[name] = value
    if until is not None:
        hi = _epoch(until)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", until):
            hi += 86400
        f["before"] = _dt.datetime.fromtimestamp(hi, _dt.timezone.utc).isoformat()
    where, params = [], []
    for name, column in (("from", "sender"), ("to", "recipients"), ("folder", "folder"), ("attachment", "attachment_names")):
        if name in f:
            where.append(f"e.{column} LIKE ? ESCAPE '\\'")
            value = f[name].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{value}%")
    if "has" in f:
        if f["has"].lower() != "attachment":
            raise ValueError("Supported has: filter is has:attachment")
        if has_attachment is False:
            raise ValueError("has:attachment conflicts with has_attachment=False")
        has_attachment = True
    if has_attachment is not None:
        if not isinstance(has_attachment, bool):
            raise ValueError("has_attachment must be true or false")
        where.append("e.attachment_count > 0" if has_attachment else "COALESCE(e.attachment_count,0) = 0")
    if account_prefix is not None:
        if not isinstance(account_prefix, str) or not account_prefix.strip("/"):
            raise ValueError("account_prefix must not be empty")
        prefix = account_prefix.rstrip("/").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("e.folder LIKE ? ESCAPE '\\'")
        params.append(prefix + "/%")
    lo = _epoch(f["after"]) if "after" in f else None
    hi = _epoch(f["before"]) if "before" in f else None
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError("The lower date bound must be earlier than the upper date bound")
    if lo is not None:
        where.append("e.date_ts >= ?")
        params.append(lo)
    if hi is not None:
        where.append("e.date_ts < ?")
        params.append(hi)
    filt = (" AND " + " AND ".join(where)) if where else ""

    diagnostics = {"semantic": "disabled" if "vec" not in use else "not_indexed", "warnings": []}
    conn = _index.connect(db_path, read_only=True)
    try:
        ranks: dict[str, float] = {}
        rows: dict[str, sqlite3.Row] = {}

        def add(ranked_rows):
            seen_in_list = set()
            for rank, r in enumerate(ranked_rows):
                k = _messages.key_for_row(r)
                if k in seen_in_list:
                    continue
                seen_in_list.add(k)
                ranks[k] = ranks.get(k, 0.0) + 1.0 / (RRF_K + rank)
                # Prefer the copy in a real folder over Thunderbird's stale '-1' dirs.
                if k not in rows or re.search(r"-\d+$", rows[k]["folder"]):
                    rows[k] = r

        cols = "e.*"
        if text:
            # Two keyword lists: messages with every term (precise; lets a rare word
            # like a project code win) and with any term (recall).
            for op in (("AND", "OR") if "fts" in use else ()):
                expr = _fts_expr(text, conn, op)
                if expr:
                    add(conn.execute(
                        f"SELECT {cols}, snippet(emails_fts, -1, '\x02', '\x03', ' … ', 18) AS snip, "
                        "(instr(highlight(emails_fts,4,'\x02','\x03'),'\x02')>0 OR "
                        "instr(highlight(emails_fts,5,'\x02','\x03'),'\x02')>0) AS attachment_hit, "
                        "(instr(highlight(emails_fts,0,'\x02','\x03'),'\x02')>0 OR "
                        "instr(highlight(emails_fts,1,'\x02','\x03'),'\x02')>0 OR "
                        "instr(highlight(emails_fts,2,'\x02','\x03'),'\x02')>0 OR "
                        "instr(highlight(emails_fts,3,'\x02','\x03'),'\x02')>0) AS message_hit "
                        f"FROM emails_fts JOIN emails e ON e.id = emails_fts.rowid "
                        f"WHERE emails_fts MATCH ?{filt} ORDER BY bm25(emails_fts, 10, 5, 1, 1, 1, 2) LIMIT 200",
                        [expr, *params]).fetchall())

            try:
                keys, matrix = _cache.get(conn) if "vec" in use else ([], None)
                if keys:
                    q = np.asarray(next(iter(_embedder().embed([text]))), dtype=np.float32)
                    if not np.isfinite(q).all() or np.linalg.norm(q) == 0:
                        raise ValueError("Invalid query embedding")
                    scores = matrix @ (q / np.linalg.norm(q))
                    ranked = [keys[i] for i in np.argsort(-scores)]
                    if filt:
                        eligible = set()
                        for r in conn.execute(f"SELECT stable_key,message_id FROM emails e WHERE 1=1{filt}", params):
                            eligible.update((r["stable_key"], r["message_id"]))
                        ranked = [key for key in ranked if key in eligible]
                    top = ranked[:400]
                    if top:
                        ph = ",".join("?" for _ in top)
                        by_key = {}
                        for r in conn.execute(
                            f"SELECT {cols},NULL AS snip FROM emails e WHERE "
                            f"(e.stable_key IN ({ph}) OR e.message_id IN ({ph})){filt}", [*top, *top, *params]):
                            by_key.setdefault(_messages.key_for_row(r), r)
                            by_key.setdefault(r["message_id"], r)
                        add([by_key[k] for k in top if k in by_key])
                    diagnostics["semantic"] = "used"
            except Exception:
                diagnostics["semantic"] = "unavailable"
                diagnostics["warnings"].append("Semantic search is unavailable; keyword results are shown. Check local model installation or rebuild embeddings.")
                _logger.warning("Semantic search failed; using keyword results", exc_info=False)
        else:
            # Filters only: newest first.
            add(conn.execute(
                f"SELECT {cols}, NULL AS snip FROM emails e WHERE 1=1{filt} "
                "ORDER BY e.date_ts DESC LIMIT 200", params).fetchall())

        now = time.time()
        def score(k):
            age_days = max(0.0, (now - (rows[k]["date_ts"] or 0)) / 86400)
            return ranks[k] * (0.6 + 0.4 * 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS))

        order = sorted(ranks, key=score, reverse=True)
        if text and order and "rerank" in use:
            try:
                order = _rerank(conn, text, order, rows)
            except Exception:
                diagnostics["warnings"].append("Optional reranking is unavailable; keyword and semantic ranking retained.")
        out, seen = [], set()
        for k in order:
            r = rows[k]
            # One result per conversation: RT tickets and lists produce dozens of
            # messages per thread that would otherwise fill the whole page.
            refs = json.loads(r["references_json"] or "[]")
            sig = refs[0] if refs else r["in_reply_to"] or r["message_id"] or k
            if deduplicate_threads and sig in seen:
                continue
            seen.add(sig)
            out.append(_result(r))
            if len(out) == limit:
                break
        return SearchResults(out, diagnostics=diagnostics)
    finally:
        conn.close()


def _rerank(conn, text: str, order: list[str], rows: dict) -> list[str]:
    """Re-score the head of the fused list with a cross-encoder reading query + message together."""
    head, seen = [], set()
    for k in order:  # one candidate per thread, so the budget covers distinct conversations
        t = thread_key(rows[k]["subject"]) or k
        if t not in seen:
            seen.add(t)
            head.append(k)
        if len(head) == RERANK_TOP:
            break
    bodies = {r["id"]: r["body"] for r in conn.execute(
        f"SELECT id, body FROM emails WHERE id IN ({','.join('?' * len(head))})",
        [rows[k]["id"] for k in head])}
    docs = [f"{rows[k]['subject'] or ''}\n{rows[k]['sender'] or ''}\n"
            f"{clean_body((rows[k]['snip'] or '') + ' ' + (bodies.get(rows[k]['id']) or ''))[:RERANK_CHARS]}" for k in head]
    scores = list(_cross_encoder().rerank(text, docs, batch_size=RERANK_TOP))
    # Blend, don't replace: the cross-encoder is strong on meaning but blind to
    # typos and opaque codes that the keyword lists already got right.
    by_ce = {k: i for i, (_, k) in enumerate(sorted(zip(scores, head), key=lambda p: -p[0]))}
    fused = sorted(head, key=lambda k: -(RERANK_WEIGHT / (10 + by_ce[k]) + 1 / (10 + head.index(k))))
    return fused + [k for k in order if k not in set(head)]


def thread_key(subject: str | None) -> str:
    s = (subject or "").lower()
    if (m := re.search(r"\[((?:bsc)?rt\.bsc\.es|eudat\.eu) #(\d+)\]", s)):
        return f"{m.group(1)}#{m.group(2)}"
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"^\s*((re|fwd?|rv|aw|tr|sv)\s*:\s*)+", "", s.strip())
    return re.sub(r"\W+", " ", s).strip()


def _result(r) -> dict:
    snip = r["snip"] or ""
    keys = r.keys()
    attachment_hit = "attachment_hit" in keys and bool(r["attachment_hit"])
    message_hit = "message_hit" in keys and bool(r["message_hit"])
    match_source = ("message_and_attachment" if message_hit else "attachment") if attachment_hit else "message"
    return {
        "id": r["id"],
        "date": _dt.datetime.fromtimestamp(r["date_ts"], _dt.timezone.utc).strftime("%Y-%m-%d") if r["date_ts"] else "",
        "folder": r["folder"].split("/", 1)[-1],
        "folder_path": r["folder"],
        "from": r["sender"] or "",
        "subject": r["subject"] or "(no subject)",
        "snippet": snip,
        "match_source": match_source,
        "attachment_count": r["attachment_count"] or 0,
        "attachments_indexed": bool(r["attachments_indexed"]),
        "attachments": json.loads(r["attachments_json"] or "[]"),
        "message_id": r["message_id"],
        "reference": _messages.reference_for(_messages.key_for_row(r)),
        "uri": _messages.reference_for(_messages.key_for_row(r)),
    }


def cli() -> None:
    n = embed_missing()
    print(f"done: {n} new embeddings")
