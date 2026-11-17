"""Text translation API (any language -> Ukrainian by default): Opus-MT tc-big or NLLB-200 on CTranslate2 (see worker).

The model runs in a worker process started on the first request and stopped after IDLE_TTL seconds idle.
POST /v1/translate {text | segments[], source: "auto"|ISO|NLLB code, target: "uk"} keeps segment order.
"""

import asyncio
import multiprocessing
import os
import re
import secrets
import time
from concurrent.futures import ProcessPoolExecutor

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, model_validator

import llm
import worker

API_KEY = os.environ["API_KEY"]
IDLE_TTL = int(os.environ.get("IDLE_TTL", "600"))
MODEL = os.environ.get("TRANSLATE_MODEL", "nllb-600m")
MAX_CHARS = 50_000

app = FastAPI(title="translate")
bearer = HTTPBearer(auto_error=False)
_pool: ProcessPoolExecutor | None = None
_last_used = 0.0
_busy = 0  # jobs submitted and not finished
_lock = asyncio.Lock()
QUEUE_MAX = int(os.environ.get("QUEUE_MAX", "8"))
# Sentence split for free text: after .!?… (plus closing quotes/brackets) followed by space; newlines kept apart.
SENTENCE = re.compile(r"(?<=[.!?…])[\"'»”)\]]*\s+")


def auth(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
    if not cred or not secrets.compare_digest(cred.credentials, API_KEY):
        raise HTTPException(401, "invalid api key")


async def _reaper() -> None:
    global _pool
    while True:
        await asyncio.sleep(30)
        async with _lock:
            if not _pool or _busy or time.monotonic() - _last_used <= IDLE_TTL:
                continue
            pool, _pool = _pool, None
        await asyncio.to_thread(pool.shutdown)  # off the event loop: the API stays responsive meanwhile


@app.on_event("startup")
async def _start() -> None:
    asyncio.create_task(_reaper())
    if llm.ENABLED:  # GPU node with >= 12 GB VRAM: EuroLLM for EN->UK (llm.py); model file once into the volume
        import threading
        threading.Thread(target=llm.download, daemon=True).start()
        threading.Thread(target=llm.reaper, daemon=True).start()


def _opus_version() -> str | None:
    """The Opus-MT models version copied into the volume by init-opus (None: not copied yet, or an older copy)."""
    try:
        return open(os.path.join(worker.OPUS_DIR, ".models-version")).read().strip() or None
    except OSError:
        return None


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "fallback_model": MODEL if worker.nllb_enabled() else None, "loaded": _pool is not None, "idle_ttl": IDLE_TTL,
            "busy": _busy, "queue_max": QUEUE_MAX, **llm.health(),
            "opus_models_version": _opus_version(),
            "opus_pairs": sorted(d for d in (os.listdir(worker.OPUS_DIR) if os.path.isdir(worker.OPUS_DIR) else [])
                                 if os.path.isfile(os.path.join(worker.OPUS_DIR, d, "model.bin")))}


@app.get("/v1/languages", dependencies=[Depends(auth)])
async def languages() -> dict:
    return {"languages": worker.LANGS, "note": "any FLORES-200 code (e.g. ukr_Cyrl) is accepted too"}


class TranslateRequest(BaseModel):
    text: str | None = None
    segments: list[str] | None = None
    source: str = "auto"
    target: str = "uk"
    beam_size: int = Field(2, ge=1, le=5)
    punctuate: bool = Field(False, description="Restore punctuation first (auto-captions, raw STT text)")

    @model_validator(mode="after")
    def _one_input(self):
        if (self.text is None) == (self.segments is None):
            raise ValueError("send exactly one of 'text' or 'segments'")
        size = len(self.text or "") + sum(len(s) for s in self.segments or [])
        if size > MAX_CHARS:
            raise ValueError(f"input longer than {MAX_CHARS} characters")
        return self


async def _run(fn, *args, request: Request | None = None):
    """Run fn(*args) in the model worker process (started on demand). At most QUEUE_MAX jobs wait or run (429
    above that); a job still waiting when its HTTP client disconnects is dropped instead of computed."""
    global _pool, _last_used, _busy
    async with _lock:
        if _busy >= QUEUE_MAX:
            raise HTTPException(429, f"busy: {QUEUE_MAX} jobs queued, retry later")
        if _pool is None:
            _pool = ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"), initializer=worker.init)
        future = _pool.submit(fn, *args)
        _busy += 1
    try:
        waiter = asyncio.wrap_future(future)
        while request is not None:
            done, _ = await asyncio.wait([waiter], timeout=1)
            if done:
                break
            if await request.is_disconnected():
                future.cancel()  # no effect once the worker has started it
                raise HTTPException(499, "client disconnected")
        return await waiter
    finally:
        _busy -= 1
        _last_used = time.monotonic()


def _split(text: str) -> list[tuple[str, str]]:
    """Free text -> [(sentence, separator after it)], so the translation keeps paragraphs and line breaks."""
    parts = []
    for line, nl in re.findall(r"([^\n]*)(\n*)", text):
        if not line and not nl:
            continue
        pieces = [p for p in SENTENCE.split(line)]
        for i, p in enumerate(pieces):
            parts.append((p.strip(), (" " if i < len(pieces) - 1 else "") + (nl if i == len(pieces) - 1 else "")))
    return parts


class PunctuateRequest(BaseModel):
    text: str | None = None
    segments: list[str] | None = None

    @model_validator(mode="after")
    def _one_input(self):
        if (self.text is None) == (self.segments is None):
            raise ValueError("send exactly one of 'text' or 'segments'")
        if len(self.text or "") + sum(len(s) for s in self.segments or []) > MAX_CHARS:
            raise ValueError(f"input longer than {MAX_CHARS} characters")
        return self


@app.post("/v1/punctuate", dependencies=[Depends(auth)])
async def punctuate(req: PunctuateRequest, request: Request) -> dict:
    """Text without punctuation (YouTube auto-captions, raw STT) -> punctuation, capitals, sentence boundaries.
    text -> {text, sentences}; segments (caption lines) -> {segments}: the same lines, punctuated with the context
    of their neighbours, word counts kept so timings stay aligned. 47 languages (uk, ru, en, pl, de, ...)."""
    started = time.monotonic()
    out = await _run(worker.punctuate, req.text, req.segments, request=request)
    body = {"segments": out} if req.segments is not None else {"text": " ".join(out), "sentences": out}
    return {**body, "elapsed_ms": round((time.monotonic() - started) * 1000)}


@app.post("/v1/translate", dependencies=[Depends(auth)])
@app.post("/translate", dependencies=[Depends(auth)], include_in_schema=False)
async def translate(req: TranslateRequest, request: Request) -> dict:
    started = time.monotonic()
    if req.punctuate:
        if req.segments is not None:
            req.segments = await _run(worker.punctuate, None, req.segments, request=request)
        else:
            req.text = " ".join(await _run(worker.punctuate, req.text, None, request=request))
    if req.segments is not None:
        units = [(s.strip(), "") for s in req.segments]
    else:
        units = _split(req.text)
    sentences = [u for u, _ in units if u]
    try:
        target = worker.nllb_code(req.target)
        if req.source == "auto":
            detected = worker.detect(" ".join(sentences)[:2000]) if sentences else req.target
            source = worker.nllb_code(detected)
        else:
            detected, source = None, worker.nllb_code(req.source)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if not sentences or source == target:
        translated, model = sentences, None  # nothing to do, keep the input
    else:
        got = None
        if llm.ENABLED and source == "eng_Latn" and target == "ukr_Cyrl":
            got = await asyncio.to_thread(llm.translate, sentences)
        try:
            if got is None:
                translated, model = await _run(worker.translate, sentences, source, target, req.beam_size,
                                                request=request)
            else:
                translated, model = got, "llm:" + llm.MODEL_NAME
                bad = [i for i, t in enumerate(got) if t is None]
                if bad:  # outputs that failed the checks (placeholders, length): Opus-MT for just those
                    fixed, opus = await _run(worker.translate, [sentences[i] for i in bad], source, target,
                                             req.beam_size, request=request)
                    for i, t in zip(bad, fixed):
                        translated[i] = t
                    model += f"+{opus}({len(bad)})"
        except ValueError as exc:  # e.g. a pair that needs the opt-in NLLB
            raise HTTPException(422, str(exc))
    it = iter(translated)
    outs = [next(it) if u else "" for u, _ in units]
    body = {"source": source, "detected": detected, "target": target, "model": model,
            "elapsed_ms": round((time.monotonic() - started) * 1000)}
    if req.segments is not None:
        body["segments"] = outs
    else:
        body["text"] = "".join(o + sep for o, (_, sep) in zip(outs, units)).strip()
    return body
