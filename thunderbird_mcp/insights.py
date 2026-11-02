"""Bounded evidence tools; headers are evidence, never a trust verdict."""
from __future__ import annotations

import email
import email.policy
import json
import re
import time
from email.utils import getaddresses
from pathlib import Path
from . import index, live, messages, outgoing, policy


def original(reference: str, db_path: Path | None = None) -> bytes:
    from . import providers
    if providers.is_reference(reference):
        return providers.original(reference)[1]
    if live.is_reference(reference):
        locator = live.parse_reference(reference)
        policy.require("read", account=locator.account)
        return live._original(reference)[1]
    conn = index.connect(db_path, read_only=True)
    try:
        row = messages._resolve(conn, reference)
        policy.require("read", account=outgoing.infer_account(row["folder"]))
        return messages._source_bytes(conn, row)
    finally:
        conn.close()


def authentication_evidence(reference: str, *, db_path: Path | None = None) -> dict:
    policy.require("read", tool="authentication_evidence")
    raw = original(reference, db_path)
    headers = re.split(br"\r?\n\r?\n", raw, maxsplit=1)[0]
    truncated = len(headers) > 65536
    msg = email.message_from_bytes(headers[:65536], policy=email.policy.default)
    evidence = []
    signals = {key: [] for key in ("spf", "dkim", "dmarc")}
    fields = [(key, str(value)) for key, value in msg.items()
              if key.lower() in {"authentication-results", "received-spf", "arc-authentication-results"}]
    for position, (key, value) in enumerate(fields[:20]):
        bounded = " ".join(value.split())[:2048]
        # Comments and quoted strings are excluded from mechanism parsing so a
        # reason/comment mentioning dkim=pass cannot become a reported result.
        clean = re.sub(r'"(?:\\.|[^"\\])*"', '', bounded)
        for _ in range(8):
            replaced = re.sub(r"\([^()]*\)", "", clean)
            if replaced == clean:
                break
            clean = replaced
        if any(char in clean for char in '()"'):
            clean = ""  # Malformed/deep comments or quoting cannot assert results.
        parsed = []
        if key.lower() == "received-spf":
            match = re.match(r"\s*([a-z]+)\b", clean, re.I)
            pairs = [("spf", match[1].lower())] if match else []
            authserv = None
        else:
            authserv = clean.split(";", 1)[0].strip()[:256]
            pairs = re.findall(r"(?:^|;)\s*(spf|dkim|dmarc)(?:/[0-9]+)?\s*=\s*([a-z]+)\b", clean, re.I)
        for method, result in pairs:
            method, result = method.lower(), result.lower()
            if result not in {"pass", "fail", "softfail", "neutral", "none", "temperror", "permerror", "policy"}:
                result = "unknown"
            parsed.append({"method": method, "result": result})
            # ARC evidence belongs to previous hops and is not merged with the
            # current receiver's Authentication-Results.
            if key.lower() != "arc-authentication-results":
                signals[method].append(result)
        evidence.append({"header": key, "position": position, "authserv_id": authserv,
                         "value": bounded, "results": parsed,
                         "provenance": "unverified_message_header"})
    summary = {method: ("unknown" if not values else values[0] if len(set(values)) == 1 else "conflicting")
               for method, values in signals.items()}
    return {"reference": reference, "summary": summary, "evidence": evidence,
            "dkim_signature_present": bool(msg.get_all("DKIM-Signature")),
            "truncated": truncated or len(fields) > 20 or any(len(v) > 2048 for _, v in fields[:20]),
            "independently_verified": False,
            "limitations": "These are reported header assertions, which can be forged outside a receiver's trust boundary. No cryptographic/DNS verification or message trust verdict is performed."}


def follow_up_candidates(*, folders: list[str], account: str | None = None,
                         mode: str = "awaiting_reply", min_age_days: int = 2,
                         limit: int = 20, own_addresses: list[str] | None = None,
                         db_path: Path | None = None, now: int | None = None) -> dict:
    if mode not in {"awaiting_reply", "needs_reply"}:
        raise ValueError("mode must be awaiting_reply or needs_reply")
    if not isinstance(folders, list) or not 1 <= len(folders) <= 50 or any(not isinstance(v, str) or not v for v in folders):
        raise ValueError("Specify 1–50 indexed folders for the candidate scan")
    if not 0 <= min_age_days <= 3650 or not 1 <= limit <= 100:
        raise ValueError("min_age_days must be 0–3650 and limit 1–100")
    config = outgoing.account_config(account) if account or own_addresses is None else None
    selected_account = config["account_id"] if config else account
    policy.require("read", tool="follow_up_candidates", account=selected_account)
    own = {v.lower() for _, v in getaddresses(own_addresses or [config["from_email"], *config.get("aliases", [])]) if v}
    if not own:
        raise ValueError("At least one own email address is required")
    conn = index.connect(db_path, read_only=True)
    try:
        prefix = config["account_prefix"] if config else None
        if prefix and any(not folder.startswith(prefix) for folder in folders):
            raise ValueError("Selected folders must belong to the selected account")
        sql = "SELECT * FROM emails"
        rows = conn.execute(sql + (" WHERE substr(folder,1,?)=?" if prefix else "") +
                            " ORDER BY date_ts DESC,id DESC LIMIT 20001",
                            (len(prefix), prefix) if prefix else ()).fetchall()
        coverage_truncated = len(rows) > 20000
        rows = rows[:20000]
        replies = {}
        for row in rows:
            senders = {v.lower() for _, v in getaddresses([row["sender"] or ""]) if v}
            links = index.message_ids(row["in_reply_to"]) + json.loads(row["references_json"] or "[]")
            for key in links:
                replies.setdefault(key, []).append((row, senders))
        cutoff = (int(time.time()) if now is None else now) - min_age_days * 86400
        candidates, exclusions, seen = [], {}, set()
        def excluded(reason):
            exclusions[reason] = exclusions.get(reason, 0) + 1
        for row in rows:
            if row["folder"] not in folders:
                continue
            key = messages.key_for_row(row)
            if key in seen:
                continue
            seen.add(key)
            if not row["message_id"]:
                excluded("missing_message_id"); continue
            if not row["date_ts"]:
                excluded("unknown_date"); continue
            if row["date_ts"] > cutoff:
                excluded("too_recent"); continue
            senders = {v.lower() for _, v in getaddresses([row["sender"] or ""]) if v}
            recipients = {v.lower() for _, v in getaddresses([row["to_header"] or row["recipients"] or "", row["cc"] or ""]) if v}
            is_ours = bool(senders & own)
            if not senders or is_ours != (mode == "awaiting_reply"):
                excluded("direction"); continue
            if mode == "awaiting_reply" and not recipients - own:
                excluded("no_external_recipient"); continue
            if mode == "needs_reply" and not recipients & own:
                excluded("not_addressed_to_selected_identity"); continue
            responses = [reply for reply, addresses in replies.get(row["message_id"], [])
                         if reply["date_ts"] >= row["date_ts"] and addresses
                         and bool(addresses & own) != is_ours]
            if responses:
                excluded("has_header_linked_response"); continue
            value = messages._summary(row)
            value.update(reason="No later response from the other side links this Message-ID in References or In-Reply-To",
                         uncertainty="Replies without threading headers or outside the indexed coverage may be missing; no reminder has been sent")
            candidates.append(value)
        return {"mode": mode, "count": min(len(candidates), limit), "candidates": candidates[:limit],
                "omitted_candidates": max(0, len(candidates) - limit), "exclusions": exclusions,
                "coverage": {"source": "local_index", "scanned_messages": len(rows), "truncated": coverage_truncated,
                             "candidate_folders": folders, "response_scope": "selected_account" if prefix else "indexed_mail"},
                "match_method": "message_headers", "automatic_actions": False}
    finally:
        conn.close()
