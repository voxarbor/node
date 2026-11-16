"""fetch: YouTube metadata, subtitles and audio fetched by this node (its own IP), for a coordinator whose IP is
blocked. Optional service (compose profile `fetch`). yt-dlp runs as a subprocess per request (nothing heavy stays
in memory while idle). Only YouTube URLs are accepted.

  POST /v1/info  {"url"}                          -> yt-dlp info dict without formats / signed URLs
  POST /v1/subs  {"url", "lang", "kind": manual|auto} -> the json3 track as is
  POST /v1/audio {"url"}                          -> best audio stream as is (no re-encode), header X-Audio-Ext
Errors: 400 bad URL, 422 video unavailable/private, 429 {"error": "bot_check"} when YouTube asks to sign in
(the coordinator should use another node and let this one rest), 502 other yt-dlp failures."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

API_KEY = os.environ["API_KEY"]
QUEUE_MAX = int(os.environ.get("QUEUE_MAX", "4"))
TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "120"))          # info / subs
AUDIO_TIMEOUT = float(os.environ.get("FETCH_AUDIO_TIMEOUT", "1800"))
COOKIES = os.environ.get("YTDLP_COOKIES", "/config/cookies.txt")  # optional Netscape cookies file
PROXY = os.environ.get("YTDLP_PROXY", "")                        # optional outbound proxy for yt-dlp
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
         "www.youtube-nocookie.com", "youtube-nocookie.com"}
DROP = {"formats", "requested_formats", "requested_downloads", "requested_subtitles", "url", "manifest_url",
        "http_headers", "fragments", "fragment_base_url", "_format_sort_fields", "format", "format_id", "ext",
        "protocol", "downloader_options", "cookies"}
BOT = re.compile(r"Sign in to confirm|not a bot|HTTP Error 429|Too Many Requests|rate-limit", re.I)
GONE = re.compile(r"Video unavailable|Private video|This video is (?:private|unavailable|not available)|"
                  r"members-only|removed by the uploader|account associated with this video has been terminated|"
                  r"Sign in to confirm your age|age-restricted|Premieres in|This live event will begin|"
                  r"not available in your country|blocked it in your country", re.I)

app = FastAPI(title="VoxArbor Node fetch", docs_url=None, redoc_url=None)
_bearer = HTTPBearer(auto_error=False)
_busy = 0
_state = {"last_bot_check": None, "last_ok": None}
_version: dict = {}


def auth(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    if cred is None or not secrets.compare_digest(cred.credentials, API_KEY):
        raise HTTPException(401, "invalid token")


def youtube_url(url) -> str:
    if not isinstance(url, str) or len(url) > 500:
        raise HTTPException(400, "url required")
    u = urlsplit(url.strip())
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in HOSTS or u.username or u.port:
        raise HTTPException(400, "only youtube.com / youtu.be URLs are accepted")
    return url.strip()


def base_args() -> list[str]:
    args = ["yt-dlp", "--no-playlist", "--no-warnings", "--ignore-config", "--socket-timeout", "30"]
    if PROXY:
        args += ["--proxy", PROXY]
    return args


def cookies_args(tmp: str) -> list[str]:
    """yt-dlp rewrites its cookies file, so it gets a private copy (the mounted one may be read-only)."""
    if COOKIES and os.path.isfile(COOKIES):
        dst = os.path.join(tmp, "cookies.txt")
        shutil.copyfile(COOKIES, dst)
        return ["--cookies", dst]
    return []


def fail(stderr: str) -> HTTPException:
    msg = stderr.strip().splitlines()[-1][:300] if stderr.strip() else "yt-dlp failed"
    if BOT.search(stderr):
        _state["last_bot_check"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return HTTPException(429, {"error": "bot_check", "detail": msg})
    if GONE.search(stderr):
        return HTTPException(422, {"error": "unavailable", "detail": msg})
    return HTTPException(502, {"error": "fetch_failed", "detail": msg})


class Slot:
    async def __aenter__(self):
        global _busy
        if _busy >= QUEUE_MAX:
            raise HTTPException(429, {"error": "busy", "detail": f"{QUEUE_MAX} fetches running, retry later"})
        _busy += 1

    async def __aexit__(self, *exc):
        global _busy
        _busy -= 1


async def run(args: list[str], timeout: float) -> tuple[int, bytes, str]:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        raise HTTPException(504, {"error": "timeout", "detail": f"yt-dlp took over {timeout:.0f} s"})
    return p.returncode, out, err.decode(errors="replace")


def strip_tracks(tracks) -> dict:
    return {lang: [{k: t.get(k) for k in ("ext", "name") if t.get(k) is not None} for t in items]
            for lang, items in (tracks or {}).items()}


def clean_info(info: dict) -> dict:
    out = {k: v for k, v in info.items() if k not in DROP}
    out["subtitles"] = strip_tracks(info.get("subtitles"))
    out["automatic_captions"] = strip_tracks(info.get("automatic_captions"))
    return out


async def versions() -> dict:
    if not _version:
        for name, cmd in (("ytdlp_version", ["yt-dlp", "--version"]), ("deno_version", ["deno", "--version"])):
            try:
                _, out, _ = await run(cmd, 20)
                _version[name] = out.decode().split()[1 if name == "deno_version" else 0]
            except Exception:
                _version[name] = None
    return _version


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "loaded": None, "busy": _busy, "queue_max": QUEUE_MAX, **await versions(),
            "last_bot_check": _state["last_bot_check"], "last_ok": _state["last_ok"],
            "cookies": bool(COOKIES and os.path.isfile(COOKIES)), "proxy": bool(PROXY)}


def ok() -> None:
    _state["last_ok"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@app.post("/v1/info", dependencies=[Depends(auth)])
async def info(request: Request) -> JSONResponse:
    url = youtube_url((await request.json()).get("url"))
    async with Slot():
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = await run(base_args() + cookies_args(tmp) + ["-J", "--skip-download", "--", url], TIMEOUT)
    if code != 0:
        raise fail(err)
    ok()
    return JSONResponse(clean_info(json.loads(out)))


@app.post("/v1/subs", dependencies=[Depends(auth)])
async def subs(request: Request) -> JSONResponse:
    body = await request.json()
    url, lang, kind = youtube_url(body.get("url")), body.get("lang"), body.get("kind", "manual")
    if not isinstance(lang, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", lang) or kind not in ("manual", "auto"):
        raise HTTPException(400, "lang (e.g. en, en-US) and kind manual|auto required")
    async with Slot():
        with tempfile.TemporaryDirectory() as tmp:
            args = base_args() + cookies_args(tmp) + [
                "--skip-download", "--write-auto-subs" if kind == "auto" else "--write-subs", "--sub-langs", lang,
                "--sub-format", "json3", "-P", tmp, "-o", "track.%(ext)s", "--", url]
            code, _, err = await run(args, TIMEOUT)
            if code != 0:
                raise fail(err)
            files = [f for f in os.listdir(tmp) if f.startswith("track.") and f.endswith(".json3")]
            if not files:
                raise HTTPException(404, {"error": "no_track", "detail": f"no {kind} json3 track for {lang}"})
            data = json.load(open(os.path.join(tmp, files[0]), encoding="utf-8"))
    ok()
    return JSONResponse(data)


@app.post("/v1/audio", dependencies=[Depends(auth)])
async def audio(request: Request) -> StreamingResponse:
    url = youtube_url((await request.json()).get("url"))
    slot = Slot()
    await slot.__aenter__()
    tmp = tempfile.mkdtemp()
    extf = os.path.join(tmp, "ext")
    args = base_args() + cookies_args(tmp) + ["-f", "bestaudio", "--print-to-file", "before_dl:%(ext)s", extf,
                                             "-o", "-", "--", url]
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                             limit=1 << 20)

    async def cleanup() -> None:
        if p.returncode is None:
            p.kill()
        await p.wait()
        shutil.rmtree(tmp, ignore_errors=True)
        await slot.__aexit__(None, None, None)

    try:
        first = await asyncio.wait_for(p.stdout.read(65536), TIMEOUT)
    except asyncio.TimeoutError:
        await cleanup()
        raise HTTPException(504, {"error": "timeout", "detail": "no audio data from yt-dlp"})
    if not first:
        err = (await p.stderr.read()).decode(errors="replace")
        await cleanup()
        raise fail(err)
    ext = open(extf).read().strip() if os.path.isfile(extf) else "bin"

    async def body():
        try:
            yield first
            deadline = time.monotonic() + AUDIO_TIMEOUT
            while chunk := await asyncio.wait_for(p.stdout.read(1 << 16), max(1, deadline - time.monotonic())):
                yield chunk
            await p.wait()
            if p.returncode == 0:
                ok()
        finally:
            await cleanup()

    media = {"m4a": "audio/mp4", "webm": "audio/webm", "mp3": "audio/mpeg", "opus": "audio/ogg"}.get(ext, "application/octet-stream")
    return StreamingResponse(body(), media_type=media, headers={"X-Audio-Ext": ext})


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
    return JSONResponse(detail, exc.status_code, headers=exc.headers)
