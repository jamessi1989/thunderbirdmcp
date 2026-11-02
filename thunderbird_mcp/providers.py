"""Native Gmail/Graph adapters with provider-scoped credentials and references.

Only fixed public provider endpoints are supported. Requests never follow
redirects, and mailbox URLs name the configured user instead of assuming that
an imported token belongs to ``me``. Synthetic tests cover request contracts;
real consent and tenant/provider behavior still require configured accounts.
"""

from __future__ import annotations

import base64
import datetime as dt
import email
import email.policy
import hashlib
import json
import os
import re
import ssl
import time
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import HTTPSHandler, Request, build_opener

from . import index, oauth, outgoing, policy


MAX_MIME_BYTES = 25 * 1024 * 1024
MAX_GRAPH_MIME_BYTES = 3 * 1024 * 1024
MAX_RESPONSE_BYTES = 36 * 1024 * 1024
_HOSTS = {"gmail": "gmail.googleapis.com", "graph": "graph.microsoft.com"}


def config(provider: str, *, account: str | None = None, tenant: str | None = None, frozen: dict | None = None) -> dict:
    if provider not in _HOSTS:
        raise ValueError("provider must be gmail or graph")
    source = dict(frozen if frozen is not None else outgoing.account_config(account))
    if tenant is None:
        tenant = os.environ.get(source.get("environment_prefix", "") + "GRAPH_TENANT")
        if tenant is None and source.get("use_global_environment"):
            tenant = os.environ.get("TB_GRAPH_TENANT")
        tenant = tenant or source.get("graph_tenant", "common")
    if not isinstance(tenant, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,200}", tenant):
        raise ValueError("Invalid Microsoft tenant")
    # A selected send-as identity is not a different mailbox account.
    mailbox = source.get("imap_user", "")
    if "@" not in mailbox:
        mailbox = source.get("from_email", "")
    if not isinstance(mailbox, str) or not re.fullmatch(r"[^\s/@]+@[^\s/@]+", mailbox):
        raise ValueError("Native API access requires a configured mailbox email address")
    source.update({f"{provider}_host": _HOSTS[provider], f"{provider}_user": mailbox,
                   f"{provider}_auth": "oauth2", "provider": provider, "graph_tenant": tenant})
    return source


def _fingerprint(settings: dict) -> str:
    provider = settings["provider"]
    identity = [provider, settings["account_id"], settings[f"{provider}_user"].casefold(),
                settings.get("graph_tenant") if provider == "graph" else None]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Locator:
    provider: str
    account: str
    fingerprint: str
    message_id: str

    @property
    def reference(self):
        return f"thunderbird://{self.provider}/" + "/".join(quote(value, safe="") for value in
            (self.account, self.fingerprint, self.message_id))


def is_reference(value) -> bool:
    return isinstance(value, str) and value.startswith(("thunderbird://gmail/", "thunderbird://graph/"))


def parse_reference(reference: str) -> Locator:
    if not is_reference(reference) or len(reference) > 8192 or re.search(r"%(?![0-9A-Fa-f]{2})", reference):
        raise ValueError("Use an account-bound Gmail or Graph reference from provider search")
    parsed = urlsplit(reference)
    parts = parsed.path.lstrip("/").split("/")
    if parsed.query or parsed.fragment or len(parts) != 3:
        raise ValueError("Invalid provider message reference")
    try:
        account, fingerprint, identifier = [unquote(value, errors="strict") for value in parts]
    except UnicodeError as exc:
        raise ValueError("Invalid provider message reference") from exc
    if (not re.fullmatch(r"[0-9a-f]{24}", fingerprint) or not account or not identifier
            or any(ord(c) < 32 or ord(c) == 127 for c in account + identifier)
            or len(account) > 256 or len(identifier) > 2048):
        raise ValueError("Invalid provider message reference")
    return Locator(parsed.netloc, account, fingerprint, identifier)


def _config(locator: Locator) -> dict:
    settings = config(locator.provider, account=locator.account)
    if _fingerprint(settings) != locator.fingerprint:
        raise LookupError("The referenced provider mailbox changed; run a new search")
    return settings


def _base(settings: dict) -> str:
    provider = settings["provider"]
    user = quote(settings[f"{provider}_user"], safe="")
    return (f"https://gmail.googleapis.com/gmail/v1/users/{user}" if provider == "gmail"
            else f"https://graph.microsoft.com/v1.0/users/{user}")


class ProviderError(RuntimeError):
    def __init__(self, *, status: int | None = None, uncertain: bool = False):
        self.http_status, self.uncertain = status, uncertain
        super().__init__("Provider request failed" + (f" (HTTP {status})" if status else "")
                         + "; check consent, scopes and account connectivity")


class DeliveryCancelled(RuntimeError):
    """Cancellation was observed before opening a mutating provider request."""


def _request(settings: dict, route: str, *, method: str = "GET", data=None,
             raw: bool = False, mime: bool = False, capability: str = "read", tool: str = "provider_search",
             operation_id: str | None = None):
    base = _base(settings)
    url = route if route.startswith("https://") else base + route
    # Continuation URLs are untrusted provider data: never send the token to
    # another host, API version, account, or endpoint family.
    if not url.startswith(base + "/") or len(url) > 16384 or urlsplit(url).fragment:
        raise ValueError("Provider continuation URL is outside the configured mailbox")
    path = urlsplit(url).path
    for _ in range(3):
        path = unquote(path, errors="strict")
        if any(segment in (".", "..") for segment in path.split("/")) or "\\" in path:
            raise ValueError("Provider continuation URL contains an unsafe path")
    policy.require(capability, tool=tool, account=settings["account_id"])
    token = oauth.access_token(settings, settings["provider"])
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if settings["provider"] == "graph":
        headers["Prefer"] = 'IdType="ImmutableId"'
    if data is None:
        payload = None
    elif mime:
        payload = base64.b64encode(data)
        headers["Content-Type"] = "text/plain"
    else:
        payload = json.dumps(data, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    request = Request(url, payload, headers, method=method)
    opener = build_opener(oauth._NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    policy.require(capability, tool=tool, account=settings["account_id"])
    if operation_id:
        from . import operations
        if operations.cancellation_requested(operation_id):
            raise DeliveryCancelled("Cancelled before the provider request")
    try:
        with opener.open(request, timeout=20) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("Provider response exceeded the byte limit")
            status = getattr(response, "status", 200)
        if raw:
            if len(body) > MAX_MIME_BYTES:
                raise ValueError("Message exceeds the MIME byte limit")
            return body
        if not body:
            return {"http_status": status}
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("Provider response was not an object")
        return value
    except HTTPError as exc:
        # A definitive 4xx rejection can be reviewed and explicitly retried.
        # Timeouts, redirects and server failures remain uncertain after writes.
        known_rejection = 400 <= exc.code < 500 and exc.code not in (408, 425)
        raise ProviderError(status=exc.code, uncertain=method != "GET" and not known_rejection) from None
    except Exception:
        raise ProviderError(uncertain=method != "GET") from None


def _decode_mime(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > (MAX_MIME_BYTES * 4 // 3 + 8):
        raise ValueError("Provider MIME exceeds the size limit")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError as exc:
        raise ValueError("Invalid provider MIME encoding") from exc
    if len(raw) > MAX_MIME_BYTES:
        raise ValueError("Provider MIME exceeds the size limit")
    return raw


def _locator(settings, identifier) -> Locator:
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("Provider returned an invalid message ID")
    return parse_reference(Locator(settings["provider"], settings["account_id"], _fingerprint(settings), identifier).reference)


def _summary(message, locator: Locator) -> dict:
    timestamp = index._date_ts(message)
    mid = index.normalize_message_id(index._header(message, "message-id"))
    return {"reference": locator.reference, "uri": locator.reference, "id": None,
            "source": locator.provider, "account": locator.account, "account_id": locator.account,
            "provider_message_id": locator.message_id, "reference_scope": "account_provider_message",
            "from": index._header(message, "from"), "to": index._header(message, "to"),
            "cc": index._header(message, "cc"), "reply_to": index._header(message, "reply-to"),
            "subject": index._header(message, "subject") or "(no subject)", "message_id": mid,
            "in_reply_to": index.normalize_message_id(index._header(message, "in-reply-to")),
            "references": index.message_ids(index._header(message, "references")),
            "date": dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat() if timestamp else "",
            "thunderbird_uri": "mid:" + quote(mid.strip("<>"), safe="@") if mid else None,
            "headers_complete": True}


def original(reference: str) -> tuple[Locator, bytes]:
    locator = parse_reference(reference)
    settings = _config(locator)
    route = "/messages/" + quote(locator.message_id, safe="")
    if locator.provider == "gmail":
        value = _request(settings, route + "?format=raw", tool="get_email")
        if value.get("id") != locator.message_id:
            raise LookupError("Provider returned a different message")
        return locator, _decode_mime(value.get("raw"))
    return locator, _request(settings, route + "/$value", raw=True, tool="get_email")


def get_email(reference: str, *, max_body_chars: int = 50_000) -> dict:
    if isinstance(max_body_chars, bool) or not isinstance(max_body_chars, int) or not 0 <= max_body_chars <= 50_000:
        raise ValueError("max_body_chars must be between 0 and 50000")
    locator, raw = original(reference)
    message = email.message_from_bytes(raw, policy=email.policy.default)
    body = index._extract_body(message) or ""
    return {**_summary(message, locator), "body": body[:max_body_chars], "body_truncated": len(body) > max_body_chars,
            "size": len(raw), "attachment_count": sum(1 for part in message.walk()
                if not part.is_multipart() and (part.get_content_disposition() == "attachment" or part.get_filename()))}


def get_thread(reference: str, *, limit: int = 50, include_bodies: bool = False) -> dict:
    """Read a bounded provider-defined conversation with reusable references.

    Provider conversation grouping can differ from RFC References threading; the
    result reports that distinction instead of claiming subject matches prove a
    header relationship. Mail is never marked read by this workflow.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    locator = parse_reference(reference)
    settings = _config(locator)
    route = "/messages/" + quote(locator.message_id, safe="")
    messages, errors = [], []
    deadline = time.monotonic() + 90
    if locator.provider == "gmail":
        first = _request(settings, route + "?format=metadata", tool="get_thread")
        thread_id = first.get("threadId")
        if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 2048:
            raise LookupError("Provider did not supply the message's conversation ID")
        result = _request(settings, "/threads/" + quote(thread_id, safe="") + "?format=metadata", tool="get_thread")
        items = result.get("messages", [])
        if not isinstance(items, list):
            raise ValueError("Provider returned an invalid conversation")
        total, truncated = len(items), len(items) > limit
        for item in items[:limit]:
            child = _locator(settings, item.get("id"))
            if time.monotonic() >= deadline:
                errors.append({"reference": child.reference, "error": "thread_time_limit"})
                continue
            try:
                if include_bodies:
                    message = get_email(child.reference)
                else:
                    parsed = email.message.EmailMessage()
                    for header in item.get("payload", {}).get("headers", []):
                        parsed[header["name"]] = header["value"]
                    message = _summary(parsed, child)
                messages.append(message)
            except (ProviderError, ValueError, LookupError):
                errors.append({"reference": child.reference, "error": "message_unavailable"})
    else:
        first = _request(settings, route + "?$select=conversationId", tool="get_thread")
        conversation = first.get("conversationId")
        if not isinstance(conversation, str) or not conversation or len(conversation) > 2048:
            raise LookupError("Provider did not supply the message's conversation ID")
        params = {"$filter": "conversationId eq '" + conversation.replace("'", "''") + "'", "$top": limit,
                  "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,internetMessageId,internetMessageHeaders"}
        result = _request(settings, "/messages?" + urlencode(params), tool="get_thread")
        items = result.get("value", [])
        if not isinstance(items, list) or len(items) > limit:
            raise ValueError("Provider returned an invalid or oversized conversation page")
        total, truncated = None, bool(result.get("@odata.nextLink"))
        for item in items:
            child = _locator(settings, item.get("id"))
            if time.monotonic() >= deadline:
                errors.append({"reference": child.reference, "error": "thread_time_limit"})
                continue
            try:
                if include_bodies:
                    message = get_email(child.reference)
                else:
                    parsed = email.message.EmailMessage()
                    for header in item.get("internetMessageHeaders", []):
                        parsed[header["name"]] = header["value"]
                    message = _summary(parsed, child)
                    message["headers_complete"] = bool(item.get("internetMessageHeaders"))
                    message["subject"] = item.get("subject", message["subject"])
                    message["message_id"] = item.get("internetMessageId", message["message_id"])
                    message["date"] = item.get("receivedDateTime", message["date"])
                messages.append(message)
            except (ProviderError, ValueError, LookupError):
                errors.append({"reference": child.reference, "error": "message_unavailable"})
    return {"reference": reference, "source": locator.provider, "count": len(messages),
            "total": total, "truncated": truncated or bool(errors), "messages": messages,
            "partial_errors": errors, "match_method": "provider_conversation",
            "note": "Conversation membership follows the provider; it may differ from References/In-Reply-To threading."}


def _cursor(settings, query: str, continuation: str) -> str:
    value = [_fingerprint(settings), hashlib.sha256(query.encode()).hexdigest(), continuation]
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


def _continuation(settings, query: str, cursor: str) -> str:
    try:
        if len(cursor) > 24000:
            raise ValueError
        fingerprint, query_hash, continuation = json.loads(base64.urlsafe_b64decode(cursor))
        if fingerprint != _fingerprint(settings) or query_hash != hashlib.sha256(query.encode()).hexdigest() or not isinstance(continuation, str):
            raise ValueError
        return continuation
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("Cursor belongs to a different provider/account/query or is invalid") from exc


def search_email(provider: str, query: str = "", *, account: str | None = None,
                 limit: int = 20, cursor: str | None = None) -> dict:
    """Bounded provider-native search. Query syntax and matching remain native."""
    if not isinstance(query, str) or len(query) > 4096 or any(ord(c) < 32 for c in query):
        raise ValueError("query must be text of at most 4096 characters without controls")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    settings = config(provider, account=account)
    next_value = _continuation(settings, query, cursor) if cursor is not None else None
    partial_errors, results = [], []
    deadline = time.monotonic() + 90
    if provider == "gmail":
        params = {"maxResults": limit, "q": query}
        if next_value:
            params["pageToken"] = next_value
        value = _request(settings, "/messages?" + urlencode(params))
        items = value.get("messages", [])
        if not isinstance(items, list) or len(items) > limit:
            raise ValueError("Provider returned an invalid or oversized page")
        for item in items:
            locator = _locator(settings, item["id"])
            if time.monotonic() >= deadline:
                partial_errors.append({"reference": locator.reference, "error": "search_time_limit"})
                continue
            try:
                details = _request(settings, "/messages/" + quote(locator.message_id, safe="") + "?format=metadata")
                if details.get("id") != locator.message_id:
                    raise ValueError("Provider returned a different message")
                message = email.message.EmailMessage()
                for header in details.get("payload", {}).get("headers", []):
                    if isinstance(header, dict) and isinstance(header.get("name"), str) and isinstance(header.get("value"), str):
                        message[header["name"]] = header["value"]
                results.append({**_summary(message, locator), "labels": details.get("labelIds", []),
                                "unread": "UNREAD" in details.get("labelIds", []), "snippet": details.get("snippet", "")[:500]})
            except (ProviderError, ValueError):
                partial_errors.append({"reference": locator.reference, "error": "message_unavailable"})
        following = value.get("nextPageToken")
        estimate = value.get("resultSizeEstimate")
    else:
        params = {"$top": limit, "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,internetMessageId,isRead,hasAttachments,bodyPreview,parentFolderId"}
        if query:
            params["$search"] = json.dumps(query)
        value = _request(settings, next_value or "/messages?" + urlencode(params))
        items = value.get("value", [])
        if not isinstance(items, list) or len(items) > limit:
            raise ValueError("Provider returned an invalid or oversized page")
        for item in items:
            locator = _locator(settings, item["id"])
            address = lambda obj: obj.get("emailAddress", {}).get("address", "")
            results.append({"reference": locator.reference, "uri": locator.reference, "id": None,
                "source": provider, "account": locator.account, "account_id": locator.account,
                "provider_message_id": locator.message_id, "subject": item.get("subject", ""),
                "from": address(item.get("from") or {}), "to": ", ".join(address(o) for o in item.get("toRecipients", [])),
                "cc": ", ".join(address(o) for o in item.get("ccRecipients", [])),
                "date": item.get("receivedDateTime", ""), "message_id": item.get("internetMessageId"),
                "unread": not item.get("isRead", False), "has_attachment": item.get("hasAttachments"),
                "snippet": item.get("bodyPreview", "")[:500], "folder": item.get("parentFolderId"),
                "headers_complete": False})
        following = value.get("@odata.nextLink")
        estimate = value.get("@odata.count")
    return {"source": provider, "account_id": settings["account_id"], "results": results,
            "count": len(results), "matched_estimate": estimate, "partial_errors": partial_errors,
            "next_cursor": _cursor(settings, query, following) if following else None,
            "diagnostics": {"query_syntax": provider + " native", "semantic": "disabled",
                            "pagination": "provider continuation; mailbox changes may affect later pages"}}


def list_labels(provider: str, *, account: str | None = None) -> dict:
    settings = config(provider, account=account)
    if provider == "gmail":
        result = _request(settings, "/labels", tool="provider_labels")
        return {"provider": provider, "labels": result.get("labels", [])}
    result = _request(settings, "/mailFolders?$top=100", tool="provider_labels")
    return {"provider": provider, "folders": result.get("value", []),
            "truncated": bool(result.get("@odata.nextLink")), "note": "Graph uses folders and message categories; it has no Gmail labels."}


def update_labels(reference: str, *, add: list[str] | None = None, remove: list[str] | None = None,
                  unread: bool | None = None, flagged: bool | None = None, categories: list[str] | None = None) -> dict:
    locator = parse_reference(reference)
    settings = _config(locator)
    for values in (add, remove, categories):
        if values is not None and (not isinstance(values, list) or len(values) > 100 or any(
                not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value) for value in values)):
            raise ValueError("Labels/categories must be a bounded list of nonempty strings")
    if unread is not None and not isinstance(unread, bool) or flagged is not None and not isinstance(flagged, bool):
        raise ValueError("unread and flagged must be booleans")
    route = "/messages/" + quote(locator.message_id, safe="")
    if locator.provider == "gmail":
        if categories is not None:
            raise ValueError("Use Gmail label IDs instead of Graph categories")
        additions, removals = set(add or []), set(remove or [])
        for state, label in ((unread, "UNREAD"), (flagged, "STARRED")):
            if state is not None:
                (additions if state else removals).add(label)
        if additions & removals or not additions and not removals:
            raise ValueError("Specify non-conflicting Gmail label changes")
        result = _request(settings, route + "/modify", method="POST",
                          data={"addLabelIds": sorted(additions), "removeLabelIds": sorted(removals)},
                          capability="mail-write", tool="provider_update_labels")
    else:
        if add or remove:
            raise ValueError("Graph requires categories or explicit read/flag state, not Gmail label IDs")
        changes = {}
        if unread is not None:
            changes["isRead"] = not unread
        if flagged is not None:
            changes["flag"] = {"flagStatus": "flagged" if flagged else "notFlagged"}
        if categories is not None:
            changes["categories"] = list(dict.fromkeys(categories))
        if not changes:
            raise ValueError("Specify a Graph message state change")
        result = _request(settings, route, method="PATCH", data=changes,
                          capability="mail-write", tool="provider_update_labels")
    return {"reference": reference, "updated": True, "provider": locator.provider,
            "labels": result.get("labelIds"), "categories": result.get("categories")}


def _prepared(draft_id: str, provider: str, revision: int, *, handoff: bool, retry_failed: bool):
    outgoing._validate_id(draft_id)
    tool = "save_draft_to_provider" if handoff else "send_draft_via_provider"
    capability = "draft" if handoff else "send"
    with outgoing._db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise ValueError("Draft does not exist")
        frozen, preview = json.loads(row["config"]), json.loads(row["preview"])
        settings = config(provider, frozen=frozen)
        policy.require(capability, tool=tool, account=settings["account_id"])
        if row["server_link"] or (row["status"] != "prepared" and not (row["status"] == "failed" and retry_failed and not handoff)):
            previous = json.loads(row["result"]) if row["result"] else {"sent": None, "delivery_uncertain": True}
            return None, None, None, {**previous, "draft_id": draft_id, "status": row["status"], "duplicate_prevented": True}
        outgoing._revision(row, revision)
        raw = bytes(row["message"])
        # SMTP uses a separate envelope. API delivery requires Bcc in MIME;
        # restore exactly the addresses already present in the reviewed preview.
        if preview.get("bcc"):
            message = email.message_from_bytes(raw, policy=email.policy.SMTP)
            if "Bcc" in message:
                del message["Bcc"]
            message["Bcc"] = ", ".join(preview["bcc"])
            raw = message.as_bytes()
        maximum = MAX_GRAPH_MIME_BYTES if provider == "graph" else MAX_MIME_BYTES
        if len(raw) > maximum:
            raise ValueError(f"The {provider} MIME path supports at most {maximum} bytes")
        settings["delivery_operation_id"] = outgoing._claim_operation(conn, row, tool)
        conn.execute("UPDATE drafts SET status=?,updated_at=?,result=NULL WHERE id=?",
                     ("saving_to_provider" if handoff else "sending", int(time.time()), draft_id))
        conn.commit()
        return settings, raw, preview, None


def _submit(draft_id: str, provider: str, revision: int, *, handoff: bool, retry_failed: bool = False) -> dict:
    settings, raw, preview, duplicate = _prepared(draft_id, provider, revision, handoff=handoff, retry_failed=retry_failed)
    if duplicate is not None:
        return duplicate
    tool = "save_draft_to_provider" if handoff else "send_draft_via_provider"
    capability = "draft" if handoff else "send"
    accepted = False
    try:
        if provider == "gmail":
            encoded = base64.urlsafe_b64encode(raw).decode()
            route = "/drafts" if handoff else "/messages/send"
            data = {"message": {"raw": encoded}} if handoff else {"raw": encoded}
            result = _request(settings, route, method="POST", data=data, capability=capability, tool=tool,
                              operation_id=settings["delivery_operation_id"])
        else:
            result = _request(settings, "/messages" if handoff else "/sendMail", method="POST",
                              data=raw, mime=True, capability=capability, tool=tool,
                              operation_id=settings["delivery_operation_id"])
        accepted = True
        outcome = {"provider": provider, "operation": tool, "message_id": preview["message_id"],
                   "provider_id": result.get("id"), "sent": False if handoff else True,
                   "saved": handoff, "delivery_complete": not handoff,
                   "accepted": not handoff, "safe_to_retry": False}
        if handoff:
            outcome["note"] = "Saved unsent to the provider. Continue editing/sending there; the local snapshot is locked."
        else:
            outcome["note"] = "Provider accepted the request; final recipient delivery is not confirmed. Do not resend."
        return outgoing._record(draft_id, "saved_to_provider" if handoff else "sent", outcome)
    except Exception as exc:
        if accepted:
            # A local persistence failure after provider acceptance cannot make
            # this message retryable. The existing sending/handoff claim stays.
            raise
        if isinstance(exc, DeliveryCancelled):
            return outgoing._record(draft_id, "cancelled", {"provider": provider, "operation": tool,
                "sent": False, "saved": False, "cancelled": True, "safe_to_retry": False,
                "note": "Cancelled before the provider request. Edit and review a new revision before another attempt."})
        uncertain = isinstance(exc, ProviderError) and exc.uncertain
        state = ("provider_save_uncertain" if handoff else "uncertain") if uncertain else ("prepared" if handoff else "failed")
        outcome = {"provider": provider, "operation": tool, "sent": None if uncertain and not handoff else False,
                   "saved": None if uncertain and handoff else False, "safe_to_retry": not uncertain,
                   "delivery_uncertain": uncertain and not handoff, "save_uncertain": uncertain and handoff,
                   "error": {"type": type(exc).__name__}, "message_id": preview["message_id"]}
        if isinstance(exc, ProviderError) and exc.http_status:
            outcome["error"]["http_status"] = exc.http_status
        return outgoing._record(draft_id, state, outcome)


def save_local_draft(draft_id: str, provider: str, revision: int) -> dict:
    """Hand off a reviewed snapshot to an API Drafts store, without sending."""
    return _submit(draft_id, provider, revision, handoff=True)


def send_local_draft(draft_id: str, provider: str, revision: int, retry_failed: bool = False) -> dict:
    """Submit the reviewed MIME once, sharing the SMTP outbox claim and states."""
    return _submit(draft_id, provider, revision, handoff=False, retry_failed=retry_failed)
