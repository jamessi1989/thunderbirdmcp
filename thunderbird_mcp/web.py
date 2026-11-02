"""Local search web app over the mail index. Everything runs on this machine."""

from __future__ import annotations

import base64
import datetime as _dt
import os
import secrets
import subprocess
from pathlib import Path

import keyring
import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import index as _index
from . import search as _search
from . import messages as _messages

HOST = os.environ.get("MAIL_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("MAIL_WEB_PORT", "3458"))
KEYRING_SERVICE, KEYRING_USER = "thunderbird-mail-web", "web"
PAGE = Path(__file__).with_name("web.html")


def _password() -> str:
    pw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    if not pw:
        pw = secrets.token_urlsafe(12)
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, pw)
    return pw


class BasicAuth(BaseHTTPMiddleware):
    def __init__(self, app, password: str):
        super().__init__(app)
        self.expected = "Basic " + base64.b64encode(f"mail:{password}".encode()).decode()

    async def dispatch(self, request, call_next):
        if not secrets.compare_digest(request.headers.get("authorization", ""), self.expected):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="mail"'})
        response = await call_next(request)
        # Mail bodies are rendered as text, but belt and braces: no remote loads, no framing.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


async def page(request):
    return HTMLResponse(PAGE.read_text())


async def api_search(request):
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse({"results": []})
    # deep=1 adds the cross-encoder pass (~1-3 s on CPU); the page asks for the
    # fast list while typing and the deep one once typing pauses.
    use = ("fts", "vec", "rerank") if request.query_params.get("deep") == "1" else ("fts", "vec")
    try:
        return JSONResponse({"results": await run_in_threadpool(_search.search, q, 40, use)})
    except ValueError as exc:
        return JSONResponse({"results": [], "error": str(exc)}, status_code=400)


async def api_email(request):
    try:
        result = await run_in_threadpool(_messages.get_email, int(request.path_params["id"]))
        return JSONResponse(result, status_code=404 if "error" in result else 200)
    except (ValueError, LookupError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


async def api_open(request):
    """Open the message in the running Thunderbird on this laptop."""
    mid = (await request.json()).get("message_id", "")
    if not mid.startswith("<") or not mid.endswith(">") or any(c in mid for c in "\r\n"):
        return JSONResponse({"error": "bad message id"}, 400)
    subprocess.Popen(["thunderbird", f"mid:{mid[1:-1]}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return JSONResponse({"ok": True})


app = Starlette(
    routes=[
        Route("/", page),
        Route("/api/search", api_search),
        Route("/api/email/{id:int}", api_email),
        Route("/api/open", api_open, methods=["POST"]),
    ],
    middleware=[Middleware(BasicAuth, password=_password())],
)


def main() -> None:
    if _index.default_db_path().is_file():
        _index.connect().close()
    # Load models before the first query, not during it.
    _search._embedder()
    _search._cross_encoder()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
