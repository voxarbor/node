"""Web UI / PWA «Озвучка» (port 8001): a static SPA (services/tts/web) plus a cookie login, and /api/* forwarded
in-process (ASGI) to the API app with the bearer key injected, so the key never reaches the browser.
Login: user TTS_UI_USER (default "voice"), password TTS_UI_PASSWORD, falling back to API_KEY.
No ML imports here: this module lives in the light API process."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

USER = os.environ.get("TTS_UI_USER", "voice")
COOKIE = "ozv_session"
SESSION_DAYS = 30
WEB_DIR = Path(__file__).resolve().parent / "web"

THEME, BG = "#1d4ed8", "#0f172a"
MANIFEST = {
    "name": "Озвучка", "short_name": "Озвучка", "description": "Українська озвучка тексту, локально",
    "lang": "uk", "id": "/", "start_url": "/", "scope": "/", "display": "standalone",
    "theme_color": THEME, "background_color": BG,
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
              {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
}
# Blue tile with yellow sound-wave bars, pre-rendered PNGs (no Pillow in the image any more)
ICONS = ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png", "favicon.png")
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy",
     b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
     b"connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
     b"form-action 'self'; manifest-src 'self'"),
]


# ---------------------------------------------------------------- sessions

class Sessions:
    """Stateless signed cookie "<expiry>.<nonce>.<sig>". The HMAC key is derived from the password, so changing
    the password invalidates every session."""

    def __init__(self, password: str):
        self.key = hmac.new(b"ozvuchka-session-v1", password.encode(), hashlib.sha256).digest()

    def _sig(self, payload: str) -> str:
        mac = hmac.new(self.key, payload.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def issue(self) -> str:
        payload = f"{int(time.time()) + SESSION_DAYS * 86400}.{secrets.token_urlsafe(12)}"
        return f"{payload}.{self._sig(payload)}"

    def expiry(self, token: str | None) -> int | None:
        """Expiry timestamp of a valid token, else None."""
        if not token or token.count(".") != 2:
            return None
        exp, nonce, sig = token.split(".")
        if not hmac.compare_digest(sig, self._sig(f"{exp}.{nonce}")):
            return None
        try:
            exp_i = int(exp)
        except ValueError:
            return None
        return exp_i if exp_i > time.time() else None


class Throttle:
    """Failed logins per client: after 3 failures every next attempt waits 2, 4, 8 ... up to 300 s."""

    def __init__(self):
        self.fails: dict[str, tuple[int, float]] = {}  # client -> (failures, earliest next attempt)

    def wait(self, client: str) -> float:
        fails, until = self.fails.get(client, (0, 0.0))
        return max(0.0, until - time.monotonic())

    def failed(self, client: str) -> float:
        now = time.monotonic()
        if len(self.fails) > 10000:  # drop stale entries
            self.fails = {k: v for k, v in self.fails.items() if v[1] > now - 3600}
        fails = self.fails.get(client, (0, 0.0))[0] + 1
        delay = 0.0 if fails < 3 else float(min(2 ** (fails - 2), 300))
        self.fails[client] = (fails, now + delay)
        return delay

    def ok(self, client: str) -> None:
        self.fails.pop(client, None)


def _client(scope) -> str:
    """Peer address plus the address Caddy appended to X-Forwarded-For (the peer is Caddy behind the proxy)."""
    headers = dict(scope.get("headers") or [])
    xff = headers.get(b"x-forwarded-for", b"").decode("latin-1").split(",")[-1].strip()
    peer = (scope.get("client") or ("?", 0))[0]
    return f"{peer}|{xff}"


def _is_https(scope) -> bool:
    headers = dict(scope.get("headers") or [])
    proto = headers.get(b"x-forwarded-proto", b"").decode("latin-1").split(",")[0].strip().lower()
    return proto == "https" or scope.get("scheme") == "https"


def _cookie_token(scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v
    return None


def _set_cookie(resp: Response, request: Request, token: str | None) -> None:
    secure = _is_https(request.scope)
    if token is None:
        resp.delete_cookie(COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
    else:
        resp.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, path="/", secure=secure, httponly=True,
                        samesite="lax")


async def _json_error(send, status: int, detail: str, extra: list | None = None) -> None:
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store"), *(extra or [])]})
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------- app

def build(api_app, password: str, api_key: str):
    sessions, throttle = Sessions(password), Throttle()
    auth_header = (b"authorization", f"Bearer {api_key}".encode())

    async def login(request: Request) -> Response:
        client = _client(request.scope)
        wait = throttle.wait(client)
        if wait > 0:
            return JSONResponse({"detail": f"Забагато невдалих спроб. Зачекайте {int(wait) + 1} с.",
                                 "retry_after": int(wait) + 1}, 429, headers={"Retry-After": str(int(wait) + 1)})
        try:
            data = await request.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return JSONResponse({"detail": "Очікується JSON {username, password}"}, 400)
        user_ok = hmac.compare_digest(str(data.get("username", "")).strip().encode(), USER.encode())
        pass_ok = hmac.compare_digest(str(data.get("password", "")).encode(), password.encode())
        if not (user_ok and pass_ok):
            delay = throttle.failed(client)
            msg = "Невірний логін або пароль."
            if delay:
                msg += f" Наступна спроба через {int(delay)} с."
            return JSONResponse({"detail": msg, "retry_after": int(delay)}, 401)
        throttle.ok(client)
        resp = JSONResponse({"user": USER})
        _set_cookie(resp, request, sessions.issue())
        return resp

    async def logout(request: Request) -> Response:
        resp = JSONResponse({"ok": True})
        _set_cookie(resp, request, None)
        return resp

    async def me(request: Request) -> Response:
        exp = sessions.expiry(_cookie_token(request.scope))
        if exp is None:
            return JSONResponse({"user": None}, headers={"Cache-Control": "no-store"})
        resp = JSONResponse({"user": USER}, headers={"Cache-Control": "no-store"})
        if exp - time.time() < (SESSION_DAYS - 1) * 86400:  # sliding session: refresh at most once a day
            _set_cookie(resp, request, sessions.issue())
        return resp

    async def manifest(request: Request) -> Response:
        return Response(json.dumps(MANIFEST, ensure_ascii=False), media_type="application/manifest+json",
                        headers={"Cache-Control": "public, max-age=3600"})

    def icon_route(name):
        data = (WEB_DIR / "icons" / name).read_bytes() if (WEB_DIR / "icons" / name).is_file() else b""

        async def handler(request: Request) -> Response:
            if not data:
                return Response(status_code=404)
            return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
        return handler

    routes = [Route("/auth/login", login, methods=["POST"]),
              Route("/auth/logout", logout, methods=["POST"]),
              Route("/auth/me", me, methods=["GET"]),
              Route("/manifest.json", manifest, methods=["GET", "HEAD"]),
              *(Route(f"/{name}", icon_route(name), methods=["GET", "HEAD"]) for name in ICONS)]
    if WEB_DIR.is_dir():
        routes.append(Mount("/", StaticFiles(directory=WEB_DIR, html=True)))
    else:  # image built without web/: keep the API up, explain on the page
        routes.append(Route("/", lambda r: PlainTextResponse("web UI files are missing (services/tts/web)", 503)))
    site = Starlette(routes=routes)

    async def forward(scope, receive, send) -> None:
        """/api/<path> -> API app /<path>, in-process; responses (SSE, audio) stream through unbuffered."""
        path = scope["path"][4:] or "/"
        headers = [(k, v) for k, v in scope["headers"] if k not in (b"authorization", b"cookie", b"host")]
        headers += [auth_header, (b"host", b"tts-api")]
        inner = dict(scope, path=path, raw_path=path.encode(), root_path="", headers=headers)
        await api_app(inner, receive, send)

    async def app(scope, receive, send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "lifespan":
                return await site(scope, receive, send)
            return
        path = scope["path"]
        if path == "/api" or path.startswith("/api/"):
            if sessions.expiry(_cookie_token(scope)) is None:
                return await _json_error(send, 401, "not logged in")
            if scope["method"] not in ("GET", "HEAD", "OPTIONS"):
                # CSRF: SameSite=Lax already blocks cross-site POSTs; also require the SPA's custom header
                if (b"x-requested-with", b"ozvuchka") not in scope["headers"]:
                    return await _json_error(send, 403, "missing X-Requested-With")
            return await forward(scope, receive, send)

        async def send_secure(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers += SECURITY_HEADERS
                if path == "/" or path.endswith((".html", ".js", ".css")):
                    headers = [h for h in headers if h[0] != b"cache-control"] + [(b"cache-control", b"no-cache")]
                message = dict(message, headers=headers)
            await send(message)

        await site(scope, receive, send_secure)

    return app
