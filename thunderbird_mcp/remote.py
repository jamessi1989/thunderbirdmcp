"""Authenticated, stateless Streamable HTTP for explicitly scoped mail access.

Every request authenticates independently; protocol sessions cannot cross
principals because this transport issues no session IDs. Persistent search/job
records retain principal ownership separately. This is provisioned bearer auth,
not an OAuth authorization server or automatic public deployment.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat

from . import policy


def load_auth(path: str | Path) -> dict:
    path = Path(path).expanduser()
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ValueError("HTTP auth configuration must be a regular private file")
    if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
        raise ValueError("HTTP auth configuration must be owned by this user and mode 0600")
    if info.st_size > 65536:
        raise ValueError("HTTP auth configuration exceeds 64 KiB")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("principals"), list) or not 1 <= len(value["principals"]) <= 100:
        raise ValueError("Configure 1–100 authenticated principals")
    names, hashes = set(), set()
    for item in value["principals"]:
        if not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", item.get("name", "")):
            raise ValueError("Invalid principal name")
        digest = item.get("token_sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or digest in hashes or item["name"] in names:
            raise ValueError("Each principal needs a unique SHA256 token digest and name")
        accounts = item.get("accounts")
        if not isinstance(accounts, list) or not accounts or any(not isinstance(v, str) or not v for v in accounts):
            raise ValueError("Every HTTP principal needs explicit permitted account IDs")
        roots = item.get("file_roots", [])
        if not isinstance(roots, list) or any(not isinstance(v, str) or not Path(v).expanduser().is_absolute() for v in roots):
            raise ValueError("File roots must be explicit absolute directories")
        item["policy"] = policy.Policy(profile=item.get("profile", "read-only"),
            tools=frozenset(item["tools"]) if "tools" in item else None,
            accounts=frozenset(accounts), file_roots=tuple(Path(v) for v in roots), principal=item["name"])
        names.add(item["name"]); hashes.add(digest)
    value.setdefault("allowed_hosts", ["127.0.0.1", "localhost", "[::1]"])
    value.setdefault("allowed_origins", [])
    if any(not isinstance(value[key], list) for key in ("allowed_hosts", "allowed_origins")):
        raise ValueError("Allowed hosts and origins must be lists")
    if any(not isinstance(v, str) or not v or "*" in v for key in ("allowed_hosts", "allowed_origins") for v in value[key]):
        raise ValueError("Allowed hosts and origins must be explicit strings without wildcards")
    return value


class AuthenticatedHTTP:
    def __init__(self, app, auth: dict):
        self.app, self.auth = app, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return
        headers = {}
        for key, value in scope.get("headers", []):
            key = key.lower()
            if key in headers and key in {b"authorization", b"host", b"origin"}:
                return await self._deny(send, 400, "Duplicate security header")
            headers[key] = value.decode("latin-1")
        host = headers.get(b"host", "")
        hostname = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
        if host not in self.auth["allowed_hosts"] and hostname not in self.auth["allowed_hosts"]:
            return await self._deny(send, 403, "Host is not permitted")
        origin = headers.get(b"origin")
        if origin is not None and origin not in self.auth["allowed_origins"]:
            return await self._deny(send, 403, "Origin is not permitted")
        if b"mcp-session-id" in headers:
            return await self._deny(send, 400, "This endpoint uses independently authenticated stateless requests")
        supplied = headers.get(b"authorization", "")
        token = supplied[7:] if supplied.startswith("Bearer ") else ""
        digest = hashlib.sha256(token.encode()).hexdigest() if 32 <= len(token) <= 512 else ""
        selected = None
        for item in self.auth["principals"]:
            if hmac.compare_digest(digest, item["token_sha256"]):
                selected = item["policy"]
        if selected is None:
            return await self._deny(send, 401, "A provisioned bearer token is required")
        length = headers.get(b"content-length")
        if length is not None:
            try:
                if not 0 <= int(length) <= 1024 * 1024:
                    return await self._deny(send, 413, "MCP request exceeds 1 MiB")
            except ValueError:
                return await self._deny(send, 400, "Invalid Content-Length")
        # Buffer only a bounded MCP JSON request before SDK dispatch; chunked
        # bodies cannot evade the same limit. SSE responses remain streaming.
        body, more = bytearray(), True
        while more:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > 1024 * 1024:
                return await self._deny(send, 413, "MCP request exceeds 1 MiB")
            more = event.get("more_body", False)
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        with policy.context(selected):
            await self.app(scope, replay, send)

    @staticmethod
    async def _deny(send, status, detail):
        body = json.dumps({"error": detail}).encode()
        headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
        if status == 401:
            headers.append((b"www-authenticate", b'Bearer realm="thunderbird-mcp"'))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def create_app(auth_file: str | Path):
    from .server import mcp
    from mcp.server.transport_security import TransportSecuritySettings
    auth = load_auth(auth_file)
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    # Our outer middleware validates exact hosts/origins including configured
    # reverse-proxy hosts; avoid a second inconsistent SDK allowlist.
    mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if mcp._session_manager is not None:
        raise RuntimeError("Create the HTTP app once per process, before serving any MCP transport")
    return AuthenticatedHTTP(mcp.streamable_http_app(), auth)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Authenticated Streamable HTTP mail MCP; loopback by default")
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ssl-certfile")
    parser.add_argument("--ssl-keyfile")
    parser.add_argument("--init-auth", action="store_true", help="Create a private hash-only auth file and print the new token once")
    parser.add_argument("--account", help="Account ID required with --init-auth")
    parser.add_argument("--principal", default="terminal")
    args = parser.parse_args(argv)
    if args.init_auth:
        if not args.account:
            parser.error("--init-auth requires --account")
        token = secrets.token_urlsafe(32)
        value = {"principals": [{"name": args.principal, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                                 "accounts": [args.account], "profile": "read-only", "file_roots": []}]}
        fd = os.open(Path(args.auth_file).expanduser(), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
        print(json.dumps({"token": token, "auth_file": str(args.auth_file), "profile": "read-only"}))
        return 0
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host == "localhost"
    if not loopback and not (args.ssl_certfile and args.ssl_keyfile):
        parser.error("Non-loopback binding requires --ssl-certfile and --ssl-keyfile; a reverse proxy can use the loopback endpoint")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    import uvicorn
    uvicorn.run(create_app(args.auth_file), host=args.host, port=args.port,
                ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile,
                access_log=False, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
