"""Interactive BYO-client OAuth enrollment using a loopback PKCE callback.

This is a terminal flow. Authorization codes, PKCE verifiers and tokens are
never returned as MCP data, logged, or placed in configuration files.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import ssl
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPSHandler, Request, build_opener

from . import oauth, policy


_GMAIL = "https://www.googleapis.com/auth/gmail."
SCOPES = {
    "gmail": {"read-only": [_GMAIL + "readonly"], "draft-only": [_GMAIL + "readonly", _GMAIL + "compose"],
              "send-only": [_GMAIL + "send"], "full": [_GMAIL + "modify"]},
    "graph": {"read-only": ["https://graph.microsoft.com/Mail.Read"],
              "draft-only": ["https://graph.microsoft.com/Mail.ReadWrite"],
              "send-only": ["https://graph.microsoft.com/Mail.Send"],
              "full": ["https://graph.microsoft.com/Mail.ReadWrite", "https://graph.microsoft.com/Mail.Send"]},
}


def _provider(value: str) -> str:
    value = {"google": "gmail", "microsoft": "graph", "outlook": "graph"}.get(value, value)
    if value not in SCOPES:
        raise ValueError("provider must be gmail or graph")
    return value


def _settings(provider: str, protocol: str | None, mode: str, tenant: str) -> dict:
    provider = _provider(provider)
    protocol = protocol or provider
    if protocol not in (provider, "imap", "smtp"):
        raise ValueError("protocol must be the selected provider API, imap, or smtp")
    if mode not in SCOPES[provider]:
        raise ValueError("mode must be read-only, draft-only, send-only, or full")
    if not isinstance(tenant, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,200}", tenant):
        raise ValueError("Invalid Microsoft tenant")
    if protocol in ("imap", "smtp"):
        if provider == "gmail":
            scopes = ["https://mail.google.com/"]
        else:
            scopes = ["https://outlook.office.com/IMAP.AccessAsUser.All" if protocol == "imap"
                      else "https://outlook.office.com/SMTP.Send"]
    else:
        scopes = SCOPES[provider][mode][:]
    if provider == "graph":
        scopes.append("offline_access")
    return {"provider": provider, "protocol": protocol, "mode": mode, "scopes": scopes,
            "authorization_endpoint": ("https://accounts.google.com/o/oauth2/v2/auth" if provider == "gmail"
                else f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"),
            "token_endpoint": ("https://oauth2.googleapis.com/token" if provider == "gmail"
                else f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"), "tenant": tenant}


def guide(provider: str, *, protocol: str | None = None, mode: str = "read-only", tenant: str = "common") -> dict:
    """Explain required registration and requested scopes without using secrets."""
    settings = _settings(provider, protocol, mode, tenant)
    google = settings["provider"] == "gmail"
    return {**settings, "pkce": "S256", "callback_binding": "127.0.0.1 only",
            "registration_url": "https://console.cloud.google.com/apis/credentials" if google
                else "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
            "registration_type": "Desktop app" if google else "Public client / mobile and desktop application",
            "steps": (["Create your own Desktop OAuth client and enable the Gmail API for API transport.",
                       "Configure the consent screen and test users, or complete the provider's required verification."]
                      if google else ["Register your own public client and configure delegated permissions.",
                                      "Register the exact http://127.0.0.1:<port>/oauth/callback redirect URI (or the provider's supported loopback registration).",
                                      "Tenant administrators may need to approve requested permissions."]),
            "notes": ["The wizard cannot bypass provider consent, app verification, or tenant policy.",
                      "IMAP/SMTP permissions are separate from native API grants.",
                      "Gmail compose permission can send; draft-only enforcement is provided by TB_MCP_PROFILE."]}


@dataclass
class _Pending:
    settings: dict
    client_id: str
    redirect_uri: str
    state: str = field(repr=False)
    verifier: str = field(repr=False)
    client_secret: str | None = field(default=None, repr=False)

    def authorization_url(self) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode("ascii")).digest()).rstrip(b"=").decode()
        params = {"client_id": self.client_id, "response_type": "code", "redirect_uri": self.redirect_uri,
                  "scope": " ".join(self.settings["scopes"]), "state": self.state,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        if self.settings["provider"] == "gmail":
            params.update(access_type="offline", prompt="consent")
        else:
            params["response_mode"] = "query"
        return self.settings["authorization_endpoint"] + "?" + urlencode(params)


def _pending(settings, client_id, redirect_uri, client_secret=None):
    oauth._text(client_id, "client_id")
    if client_secret is not None:
        oauth._text(client_secret, "client_secret")
    return _Pending(settings, client_id, redirect_uri, secrets.token_urlsafe(32),
                    secrets.token_urlsafe(64), client_secret)


def _exchange(pending: _Pending, code: str, *, timeout: float = 15) -> dict:
    oauth._text(code, "authorization code")
    payload = {"grant_type": "authorization_code", "client_id": pending.client_id,
               "code": code, "redirect_uri": pending.redirect_uri, "code_verifier": pending.verifier}
    if pending.client_secret:
        payload["client_secret"] = pending.client_secret
    request = Request(pending.settings["token_endpoint"], urlencode(payload).encode("ascii"),
                      {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, method="POST")
    opener = build_opener(oauth._NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(oauth._MAX_RESPONSE + 1)
        if len(raw) > oauth._MAX_RESPONSE:
            raise ValueError("Oversized token response")
        received = json.loads(raw)
        if not isinstance(received, dict) or not received.get("access_token"):
            raise ValueError("Missing access token")
        token = {key: received[key] for key in ("access_token", "refresh_token", "expires_in", "token_type", "scope") if key in received}
        if "expires_in" not in token:
            raise ValueError("Missing token expiry")
        token.update(client_id=pending.client_id, token_url=pending.settings["token_endpoint"])
        token.setdefault("scope", " ".join(pending.settings["scopes"]))
        if pending.client_secret:
            token["client_secret"] = pending.client_secret
        return oauth._validated(token)
    except Exception as exc:
        raise RuntimeError(f"OAuth enrollment failed ({type(exc).__name__}); check app registration, consent and redirect URI") from None


def _callback_handler(pending: _Pending, result: dict):
    class Callback(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Request URLs contain an authorization code.

        def do_GET(self):
            parsed = urlsplit(self.path)
            try:
                fields = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
            except ValueError:
                fields = {}
            state = fields.get("state", [])
            valid = (self.headers.get("Host") == urlsplit(pending.redirect_uri).netloc
                     and parsed.path == "/oauth/callback" and len(state) == 1
                     and hmac.compare_digest(state[0].encode(), pending.state.encode()))
            if not valid:
                status, text = 400, "Invalid OAuth callback. Return to the authorization window."
            elif "error" in fields:
                result["error"] = "Authorization was declined or blocked by provider policy"
                status, text = 400, result["error"]
            elif len(fields.get("code", [])) != 1 or not fields["code"][0]:
                status, text = 400, "The callback did not contain an authorization code."
            else:
                result["code"] = fields["code"][0]
                status, text = 200, "Authorization received. Return to the terminal to finish."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.send_header("Content-Length", str(len(text.encode())))
            self.end_headers()
            self.wfile.write(text.encode())
    return Callback


def enroll(*, provider: str, client_id: str, account: str | None = None,
           protocol: str | None = None, mode: str = "read-only", tenant: str = "common",
           client_secret: str | None = None, port: int = 0, timeout: float = 180,
           open_browser: bool = True, on_ready=None) -> dict:
    """Authorize and store a grant through a temporary local browser callback.

    ``on_ready(url, redirect_uri)`` lets a CLI print the consent URL when no
    browser is available. It must never print the callback URL received later.
    """
    from . import outgoing, providers
    settings = _settings(provider, protocol, mode, tenant)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if not isinstance(timeout, (int, float)) or not 10 <= timeout <= 600:
        raise ValueError("timeout must be between 10 and 600 seconds")
    config = providers.config(settings["provider"], account=account, tenant=tenant) if settings["protocol"] in SCOPES else outgoing.account_config(account)
    if settings["protocol"] in ("imap", "smtp"):
        endpoint = oauth._default_endpoint(config, settings["protocol"])
        expected_host = "oauth2.googleapis.com" if settings["provider"] == "gmail" else "login.microsoftonline.com"
        if endpoint is None or urlsplit(endpoint).hostname != expected_host:
            raise ValueError("The selected account endpoint does not match this OAuth provider")
    policy.require("configure", tool="enroll_account", account=config["account_id"])
    result = {}
    with HTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler) as server:
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/oauth/callback"
        pending = _pending(settings, client_id, redirect, client_secret)
        server.RequestHandlerClass = _callback_handler(pending, result)
        url = pending.authorization_url()
        if on_ready:
            on_ready(url, redirect)
        if open_browser:
            webbrowser.open(url, new=1)
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if "error" in result:
        raise RuntimeError(result["error"])
    if not result:
        raise TimeoutError("OAuth consent timed out; run enrollment again")
    token = _exchange(pending, result["code"])
    policy.require("configure", tool="enroll_account", account=config["account_id"])
    stored = oauth.store_token(config, settings["protocol"], token)
    return {**stored, "provider": settings["provider"], "mode": mode,
            "requested_scopes": settings["scopes"], "granted_scopes": token.get("scope", "").split(),
            "note": "Credentials stored in the account/provider keyring. Restart clients after changing account transport settings."}
