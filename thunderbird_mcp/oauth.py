"""Account-scoped OAuth tokens for SASL XOAUTH2, stored only in the keyring.

This module imports an already authorized token response; it does not borrow
Thunderbird's OAuth client registration or prompt an LLM for secrets. Refresh
uses the standard OAuth refresh-token grant over verified HTTPS.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import ssl
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import keyring

SERVICE = "thunderbird-mcp-oauth2"
_MAX_RESPONSE = 64 * 1024
_LOCK = threading.RLock()
_FIELDS = ("access_token", "refresh_token", "client_id", "client_secret", "token_url", "scope", "expires_at")


def _key(config: dict, protocol: str) -> str:
    from .outgoing import credential_key
    if protocol not in ("imap", "smtp"):
        raise ValueError("Protocol must be imap or smtp")
    return credential_key(config, protocol)


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 32768 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"OAuth {field} must be nonempty text without control characters")
    return value


def _endpoint(value: str) -> str:
    _text(value, "token_url")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("OAuth token_url must be an HTTPS URL without credentials, query, or fragment")
    return value


def _default_endpoint(config: dict, protocol: str) -> str | None:
    host = config[f"{protocol}_host"].lower()
    if host in {"imap.gmail.com", "smtp.gmail.com", "imap.googlemail.com", "smtp.googlemail.com"}:
        return "https://oauth2.googleapis.com/token"
    if host in {"outlook.office365.com", "smtp.office365.com", "imap-mail.outlook.com", "smtp-mail.outlook.com"}:
        return "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    return None


def _validated(token: dict) -> dict:
    if not isinstance(token, dict):
        raise ValueError("OAuth token import must be a JSON object")
    allowed = {*_FIELDS, "expires_in", "token_type"}
    if set(token) - allowed:
        raise ValueError("OAuth token import contains unsupported fields")
    token_type = token.get("token_type", "Bearer")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise ValueError("Only OAuth Bearer tokens are supported")
    result = {}
    for field in _FIELDS:
        if field not in token or token[field] is None:
            continue
        if field == "expires_at":
            try:
                expiry = float(token[field])
            except (TypeError, ValueError):
                raise ValueError("OAuth expires_at must be a Unix timestamp") from None
            if not math.isfinite(expiry) or expiry < 0:
                raise ValueError("OAuth expires_at must be a finite nonnegative Unix timestamp")
            result[field] = expiry
        elif field == "token_url":
            result[field] = _endpoint(token[field])
        else:
            result[field] = _text(token[field], field)
    if "expires_in" in token:
        try:
            duration = float(token["expires_in"])
        except (TypeError, ValueError):
            raise ValueError("OAuth expires_in must be seconds") from None
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("OAuth expires_in must be positive finite seconds")
        result["expires_at"] = time.time() + duration
    if not (result.get("access_token") or result.get("refresh_token")):
        raise ValueError("OAuth token import needs an access_token or refresh_token")
    return result


def _stored(config: dict, protocol: str) -> dict:
    try:
        value = keyring.get_password(SERVICE, _key(config, protocol))
    except Exception as exc:
        raise RuntimeError(f"Cannot access OAuth keyring ({type(exc).__name__})") from None
    if not value:
        return {}
    try:
        decoded = json.loads(value)
        source = decoded.pop("_environment_fingerprint", None) if isinstance(decoded, dict) else None
        result = _validated(decoded)
        if source is not None:
            result["_environment_fingerprint"] = source
        return result
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError("Stored OAuth credentials are invalid; import them again") from None


def _load(config: dict, protocol: str) -> dict:
    from .outgoing import credential_environment
    # Explicit environment credentials work even if a desktop keyring is locked.
    values = {field: credential_environment(config, protocol, "OAUTH_" + field.upper()) for field in _FIELDS}
    values = {field: value for field, value in values.items() if value is not None}
    if values.get("access_token") or values.get("refresh_token"):
        result = _validated(values)
        source = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            cached = _stored(config, protocol)
        except RuntimeError:
            cached = {}
        if cached.get("_environment_fingerprint") == source:
            result = cached
        else:
            result["_environment_fingerprint"] = source
    else:
        result = _stored(config, protocol)
        result.update(values)
        if result:
            result.pop("_environment_fingerprint", None)
            result = _validated(result)
    if result and not result.get("token_url"):
        endpoint = _default_endpoint(config, protocol)
        if endpoint:
            result["token_url"] = endpoint
    return result


def _summary(config: dict, protocol: str, token: dict) -> dict:
    expires_at = token.get("expires_at")
    access_available = bool(token.get("access_token")) and (expires_at is None or expires_at > time.time() + 60)
    refresh_available = bool(token.get("refresh_token") and token.get("client_id") and token.get("token_url"))
    return {"account_id": config.get("account_id", "environment"), "protocol": protocol,
            "auth": config.get(f"{protocol}_auth", "password"),
            "credential_available": access_available or refresh_available,
            "access_token_available": access_available, "refresh_available": refresh_available,
            "expires_at": expires_at}


def token_status(config: dict, protocol: str = "imap") -> dict:
    """Return redacted credential availability without any network request."""
    return _summary(config, protocol, _load(config, protocol))


def _save(config: dict, protocol: str, token: dict) -> None:
    try:
        keyring.set_password(SERVICE, _key(config, protocol), json.dumps(token, separators=(",", ":")))
    except Exception as exc:
        raise RuntimeError(f"Cannot store OAuth credentials in keyring ({type(exc).__name__})") from None


def store_token(config: dict, protocol: str, token: dict) -> dict:
    """Import a token response, optionally with refresh settings, into keyring.

    Accepted keys: access_token, refresh_token, expires_in/expires_at,
    client_id, client_secret, token_url, scope, and token_type (Bearer only).
    This function is for a terminal command receiving a private JSON file/stdin.
    """
    _key(config, protocol)
    if not config.get(f"{protocol}_host") or not config.get(f"{protocol}_user"):
        raise ValueError("Configure the selected mail account before importing OAuth credentials")
    value = _validated(token)
    if not value.get("token_url"):
        endpoint = _default_endpoint(config, protocol)
        if endpoint:
            value["token_url"] = endpoint
    with _refresh_lock(config, protocol):
        _save(config, protocol, value)
    return {**_summary(config, protocol, value), "stored": True,
            "note": "Select oauth2 authentication for this account if it is not already configured."}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never forward a refresh token or client secret.
        raise HTTPError(req.full_url, code, "OAuth token endpoint redirect refused", headers, fp)


def _refresh(config: dict, token: dict) -> dict:
    if not token.get("refresh_token") or not token.get("client_id") or not token.get("token_url"):
        raise RuntimeError("OAuth token expired or missing; import an access token or refresh token with client_id and token_url")
    endpoint = _endpoint(token["token_url"])
    payload = {"grant_type": "refresh_token", "refresh_token": token["refresh_token"], "client_id": token["client_id"]}
    for field in ("client_secret", "scope"):
        if token.get(field):
            payload[field] = token[field]
    request = Request(endpoint, urlencode(payload).encode("ascii"),
                      {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, method="POST")
    opener = build_opener(_NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=config.get("timeout", 15)) as response:
            raw = response.read(_MAX_RESPONSE + 1)
            if len(raw) > _MAX_RESPONSE:
                raise ValueError("Oversized response")
        received = json.loads(raw)
        if not isinstance(received, dict) or not received.get("access_token"):
            raise ValueError("Missing access token")
        # Providers may return extra fields. Retain only the grant fields and
        # preserve the old refresh token when the provider does not rotate it.
        changes = {key: received[key] for key in ("access_token", "refresh_token", "expires_in", "token_type", "scope") if key in received}
        merged = {**token, **changes}
        merged.pop("expires_at", None)
        if "expires_in" not in changes:
            # Missing expiry must not turn a refreshed token into an immortal one.
            raise ValueError("Missing expiry")
        source = merged.pop("_environment_fingerprint", None)
        result = _validated(merged)
        if source is not None:
            result["_environment_fingerprint"] = source
        return result
    except Exception as exc:
        # HTTP bodies, exception messages and token values are deliberately absent.
        raise RuntimeError(f"OAuth refresh failed ({type(exc).__name__}); reauthorize or check client settings") from None


@contextmanager
def _refresh_lock(config: dict, protocol: str):
    """Serialize token rotation across simultaneous Codex/Claude/OpenCode calls."""
    directory = Path(os.environ.get("TB_OAUTH_LOCK_DIR", str(Path.home() / ".cache" / "thunderbird-mcp" / "oauth-locks"))).expanduser()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (_key(config, protocol).replace(":", "-") + ".lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    with _LOCK:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "r+b") as lock:
            if os.name == "nt":
                import msvcrt
                if os.fstat(lock.fileno()).st_size == 0:
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)


def access_token(config: dict, protocol: str) -> str:
    """Get a usable access token and refresh it once if expiry is known."""
    with _refresh_lock(config, protocol):
        token = _load(config, protocol)
        if _summary(config, protocol, token)["access_token_available"]:
            return token["access_token"]
        updated = _refresh(config, token)
        _save(config, protocol, updated)
        return updated["access_token"]


def xoauth2(user: str, token: str) -> str:
    """Unencoded SASL payload; imaplib/smtplib apply the protocol base64 layer."""
    _text(user, "username")
    _text(token, "access_token")
    return f"user={user}\x01auth=Bearer {token}\x01\x01"
