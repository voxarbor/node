"""Ukrainian TTS API (engines StyleTTS2-ukrainian and HolosTTS, CPU) + the web UI on a second port (see API.md).
Models are loaded in a worker process on the first request and the process is stopped after IDLE_TTL seconds
without requests, so the idle service holds only this API process. Long texts are split into sentence groups
(speech.py) that are synthesized one by one: streamed (/v1/audio/speech/stream) or as jobs with history (jobs.py)."""

import asyncio
import io
import multiprocessing
import os
import secrets
import subprocess
import json
import logging
import re
import time
import uuid
from urllib.parse import quote
from concurrent.futures import ProcessPoolExecutor

import uvicorn
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import jobs
import lexicon
import phonemize
import speech as speechtext
import worker

log = logging.getLogger("tts")

API_KEY = os.environ["API_KEY"]
IDLE_TTL = int(os.environ.get("IDLE_TTL", "600"))
ENABLE_UI = os.environ.get("ENABLE_UI", "true").lower() == "true"
FEATURES = ["lexicon_off", "model_settings"]  # optional request features a coordinator can rely on (in /capabilities)
MAX_CHARS = int(os.environ.get("TTS_MAX_CHARS", "5000"))  # one-shot /v1/audio/speech
LONG_MAX_CHARS = int(os.environ.get("TTS_LONG_MAX_CHARS", "300000"))  # stream and jobs
FORMATS = {"wav": "audio/wav", "mp3": "audio/mpeg", "opus": "audio/ogg", "flac": "audio/flac"}

app = FastAPI(title="tts")
bearer = HTTPBearer(auto_error=False)
_pool: ProcessPoolExecutor | None = None
_last_used = 0.0
_busy = 0  # jobs submitted and not finished
_lock = asyncio.Lock()
QUEUE_MAX = int(os.environ.get("QUEUE_MAX", "8"))


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


async def run(fn, *args, request: Request | None = None):
    """Run fn(*args) in the model worker process (started on demand). At most QUEUE_MAX jobs wait or run (429
    above that); a job still waiting when its HTTP client disconnects is dropped instead of computed."""
    global _pool, _last_used, _busy
    async with _lock:
        if _busy >= QUEUE_MAX:
            raise HTTPException(429, f"busy: {QUEUE_MAX} jobs queued, retry later")
        if _pool is None:
            _pool = ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"), initializer=worker.init)
        token = uuid.uuid4().hex
        future = _pool.submit(worker.call, token, fn, *args)
        _busy += 1
    try:
        waiter = asyncio.wrap_future(future)
        while request is not None:
            done, _ = await asyncio.wait([waiter], timeout=1)
            if done:
                break
            if await request.is_disconnected():
                if not future.cancel():  # already running in the worker: ask it to stop between sentence parts
                    os.makedirs(worker.CANCEL_DIR, exist_ok=True)
                    open(os.path.join(worker.CANCEL_DIR, token), "w").close()
                waiter.add_done_callback(lambda f: f.cancelled() or f.exception())  # its Cancelled is expected
                raise HTTPException(499, "client disconnected")
        return await waiter
    finally:
        _busy -= 1
        _last_used = time.monotonic()


def encode(pcm: bytes, fmt: str, sample_rate: int | None) -> bytes:
    """Model output is float32 mono at 24 kHz. wav/flac: 24-bit at the native rate (lossless). mp3: 320k, which
    needs >= 32 kHz, so it is resampled to 48 kHz (soxr, high precision). opus: 48 kHz (its native rate), 160k."""
    import numpy as np
    import soundfile as sf

    audio = np.frombuffer(pcm, dtype=np.float32)
    buf = io.BytesIO()
    if fmt == "wav" and not sample_rate:
        sf.write(buf, audio, worker.SAMPLE_RATE, format="WAV", subtype="PCM_24")
        return buf.getvalue()
    sf.write(buf, audio, worker.SAMPLE_RATE, format="WAV", subtype="FLOAT")  # no quantization before ffmpeg
    rate = sample_rate or {"mp3": 48000, "opus": 48000}.get(fmt)
    args = {"wav": ["-c:a", "pcm_s24le", "-f", "wav"],
            "flac": ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24", "-f", "flac"],
            "mp3": ["-c:a", "libmp3lame", "-b:a", "320k", "-f", "mp3"],
            "opus": ["-c:a", "libopus", "-b:a", "160k", "-f", "ogg"]}[fmt]
    resample = ["-af", "aresample=resampler=soxr:precision=28", "-ar", str(rate)] if rate else []
    return subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", *resample, *args, "pipe:1"],
                          input=buf.getvalue(), capture_output=True, check=True).stdout


class SpeechRequest(BaseModel):
    text: str | None = Field(None, description="Ukrainian text; '+' after a vowel marks stress")
    input: str | None = Field(None, description="Alias of text (OpenAI /v1/audio/speech)")
    engine: str | None = Field(None, description="'styletts2' or 'holos' (default TTS_DEFAULT_ENGINE)")
    voice: str | None = Field(None, description="Preset name, a favorite alias (male, male2, male3, female, female2) or custom:<id> (Holos); omitted = male")
    speed: float = Field(1.0, ge=0.5, le=2.0)
    model: str = Field("multi", description="a model id from /capabilities models (holos, styletts2, omnivoice) picks "
                       "the engine; styletts2 only: 'multi' (31 voices) or 'single' (one male voice)")
    settings: dict | None = Field(None, description="the model's settings, by the JSON Schema in /capabilities models "
                                                     "(omnivoice: num_step, guidance_scale, instructions, seed)")
    verbalize: bool = Field(False, description="Spell out numbers/abbreviations first (seq2seq model)")
    lexicon: bool = Field(True, description="false: read without any lexicon rule (stress, name, replace, regex), "
                                            "e.g. to hear a word as the automatic stress says it")
    pause_ms: int = Field(0, ge=0, le=3000, description="Silence inserted between sentences")
    response_format: str = Field("wav", description="wav (24-bit) | flac (24-bit) | mp3 (320k, 48 kHz) | opus")
    sample_rate: int | None = Field(None, description="Resample output (native 24000; soxr)")
    english: bool = Field(True, description="Read Latin words (English names, CamelCase, acronyms) the English way")
    clean: bool = Field(True, description="Strip Markdown and shorten URLs before reading")
    brackets: str = Field("aside", description="Text in brackets: aside (comma pauses) | skip | announce "
                                               "('у дужках: …') | voice (read by brackets_voice)")
    initials: str = Field("letters", description="Initials next to a surname: letters ('те ге Шевченко') | skip; "
                                                 "full names come from lexicon replace rules")
    brackets_announce: bool = Field(False, description="Say 'у дужках:' before bracket text (with aside or voice)")
    brackets_voice: str | None = Field(None, description="brackets=voice: the second voice (same engine); "
                                                          "default female, or male when the main voice is female")
    brackets_gain_db: float = Field(-3.0, ge=-20, le=6, description="brackets=voice: volume of the aside")
    target_duration: float | None = Field(None, gt=0.2, le=600, description="/v1/audio/speech: make the audio last "
                                          "exactly this many seconds (speed adjusted within fit_min/max_speed)")
    fit_max_speed: float = Field(1.35, ge=1.0, le=2.0, description="target_duration: fastest allowed (x speed)")
    fit_min_speed: float = Field(0.85, ge=0.5, le=1.0, description="target_duration: slowest allowed (x speed)")
    fit_pad: str = Field("end", pattern="^(end|none)$", description="target_duration: shorter speech is padded "
                                                                     "with silence at the end (end) or left (none)")


class LongSpeechRequest(SpeechRequest):
    """Stream / job defaults: numbers spelled out, short pauses between sentences, mp3."""
    verbalize: bool = True
    pause_ms: int = Field(150, ge=0, le=3000)
    response_format: str = "mp3"


class JobRequest(LongSpeechRequest):
    title: str | None = Field(None, max_length=300)


def _validate(req: SpeechRequest, max_chars: int = MAX_CHARS, formats=FORMATS) -> tuple[str, str, str, str | None]:
    text = (req.text or req.input or "").strip()
    if not text:
        raise HTTPException(422, "text is required")
    if len(text) > max_chars:
        raise HTTPException(413, f"text longer than {max_chars} characters")
    engine = (req.engine or worker.DEFAULT_ENGINE).lower()
    if req.model not in ("multi", "single"):  # models system: "model" names the model, which is the engine here
        if req.model not in worker.ENGINES:
            raise HTTPException(422, f"model: one of {list(worker.ENGINES)} on this node")
        engine = req.model
    if engine not in worker.ENGINES:
        raise HTTPException(422, f"engine: one of {list(worker.ENGINES)}")
    _check_settings(engine, req.settings)
    model = "single" if engine == "styletts2" and req.model == "single" else "multi"
    voice = None
    if not (engine == "styletts2" and model == "single"):
        try:
            voice = worker.resolve_voice(engine, req.voice)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    if req.response_format not in formats:
        raise HTTPException(422, f"response_format: one of {sorted(formats)}")
    if req.initials not in speechtext.INITIALS_MODES:
        raise HTTPException(422, f"initials: one of {list(speechtext.INITIALS_MODES)}")
    if req.brackets not in speechtext.BRACKET_MODES:
        raise HTTPException(422, f"brackets: one of {list(speechtext.BRACKET_MODES)}")
    return text, engine, model, voice


def model_settings(engine: str) -> dict:
    """JSON Schema of the settings a model accepts (published in /capabilities models)"""
    return worker.OMNI_SETTINGS if engine == "omnivoice" else {"type": "object", "properties": {},
                                                                 "additionalProperties": False}


def _check_settings(engine: str, settings: dict | None) -> None:
    """settings against the model's schema: known keys only, types and ranges"""
    props = model_settings(engine)["properties"]
    for k, v in (settings or {}).items():
        spec = props.get(k)
        if spec is None:
            raise HTTPException(422, f"settings.{k}: not a setting of model {engine} ({sorted(props) or 'none'})")
        kind = spec["type"]
        ok = (isinstance(v, int) and not isinstance(v, bool)) if kind == "integer" else \
             (isinstance(v, (int, float)) and not isinstance(v, bool)) if kind == "number" else isinstance(v, str)
        if not ok:
            raise HTTPException(422, f"settings.{k}: {kind} expected")
        if kind != "string" and not (spec.get("minimum", v) <= v <= spec.get("maximum", v)):
            raise HTTPException(422, f"settings.{k}: {spec.get('minimum')}..{spec.get('maximum')}")
        if kind == "string" and len(v) > spec.get("maxLength", 10 ** 6):
            raise HTTPException(422, f"settings.{k}: at most {spec['maxLength']} characters")


def _aside_voice(req: SpeechRequest, engine: str, voice: str | None) -> str | None:
    if req.brackets != "voice":
        return None
    try:
        if req.brackets_voice:
            return worker.resolve_voice(engine, req.brackets_voice)
        female = worker.resolve_voice(engine, "female")
        return worker.resolve_voice(engine, "male") if voice == female else female
    except ValueError as exc:
        raise HTTPException(422, f"brackets_voice: {exc}")


async def _synth(spoken: str, p: dict, request: Request | None = None):
    """One segment through the worker; text in brackets with brackets=voice is synthesized by the second voice
    (a bit quieter, short pauses around it) and stitched in."""
    runs = speechtext.aside_runs(spoken) if speechtext.ASIDE_START in spoken else None
    if not runs:
        return await _synth_one(spoken.replace(speechtext.ASIDE_END, ""), p, request)
    import numpy as np

    gap = np.zeros(int(worker.SAMPLE_RATE * 0.18), dtype=np.float32)
    parts, info = [], None
    for text, aside in runs:
        q = dict(p, voice=p.get("brackets_voice") or p["voice"]) if aside else p
        try:
            pcm, info = await _synth_one(text, q, request)
        except ValueError:  # nothing speakable in this run
            continue
        audio = np.frombuffer(pcm, dtype=np.float32)
        if aside:
            audio = audio * (10 ** (p.get("brackets_gain_db", -3.0) / 20))
        if parts:
            parts.append(gap)
        parts.append(audio.astype(np.float32))
    if not parts:
        raise ValueError("nothing to synthesize")
    return np.concatenate(parts).tobytes(), info


async def _synth_one(spoken: str, p: dict, request: Request | None = None):
    """Waits instead of failing while the worker queue is full."""
    while True:
        try:
            t0 = time.monotonic()
            out = await run(worker.synthesize, spoken, p["voice"], p["speed"], p["model"], p["pause_ms"],
                            p["verbalize"], p["engine"], p.get("lexicon", True), p.get("settings"), request=request)
            PERF.add(len(out[0]) / 4 / worker.SAMPLE_RATE, time.monotonic() - t0)
            return out
        except HTTPException as exc:
            if exc.status_code != 429:
                raise
            await asyncio.sleep(2)


def _params(req: SpeechRequest, engine: str, model: str, voice: str | None) -> dict:
    return {"engine": engine, "voice": voice, "model": model, "speed": req.speed, "pause_ms": req.pause_ms,
            "verbalize": req.verbalize, "english": req.english, "clean": req.clean,
            "response_format": req.response_format, "brackets": req.brackets,
            "brackets_announce": req.brackets_announce or req.brackets == "announce", "initials": req.initials,
            "brackets_voice": _aside_voice(req, engine, voice), "brackets_gain_db": req.brackets_gain_db,
            "lexicon": req.lexicon, "settings": req.settings}


async def _plan(text: str, req: SpeechRequest) -> list[dict]:
    segs, _ = await asyncio.to_thread(speechtext.plan, text, clean_text=req.clean, use_english=req.english,
                                      pause_ms=req.pause_ms, brackets=req.brackets,
                                      announce=req.brackets_announce, initials_mode=req.initials,
                                      use_lexicon=req.lexicon)
    if not segs:
        raise HTTPException(422, "nothing to read after cleanup")
    return segs



class _Perf:
    """speed from real work over the last hour: units done (audio seconds / sentences) per second of work"""
    def __init__(self, window: float = 3600):
        import collections
        self.window, self.items = window, collections.deque()

    def add(self, units: float, seconds: float) -> None:
        import time as _t
        if units > 0 and seconds > 0:
            self.items.append((_t.monotonic(), units, seconds))

    def summary(self) -> dict:
        import time as _t
        cut = _t.monotonic() - self.window
        while self.items and self.items[0][0] < cut:
            self.items.popleft()
        u, s = sum(x[1] for x in self.items), sum(x[2] for x in self.items)
        return {"jobs": len(self.items), "units": round(u, 1), "seconds": round(s, 1),
                "per_second": round(u / s, 2) if s else None, "window_s": int(self.window)}


PERF = _Perf()

@app.get("/health")
async def health() -> dict:
    loading = _warming is not None and not _warming.done()  # models are being loaded by /v1/warmup
    return {"status": "ok", "loaded": _pool is not None and not loading, "loading": loading, "idle_ttl": IDLE_TTL,
            "ui": ENABLE_UI, "jobs": JOBS.counts(), "busy": _busy, "queue_max": QUEUE_MAX, "device": _device(),
            "features": FEATURES, "model_info": _model_info(), "perf": PERF.summary(),
            **({"omnivoice": o} if (o := await asyncio.to_thread(_omni_health)) else {})}


def _model_info() -> dict:
    """the models this image carries (pinned at build time) and how it uses them: for /capabilities"""
    import importlib.metadata as md

    def ver(pkg):
        try:
            return md.version(pkg)
        except md.PackageNotFoundError:
            return None
    return {"holos_revision": os.environ.get("HOLOS_REVISION"), "holos_tts": ver("holos-tts"),
            "stress": worker.STRESS, "accentuk": ver("accentuk"), "accentuk_mode": worker.ACCENTUK_MODE,
            "verbalizer": worker.VERBALIZER}


OMNI_REVISION = "Serveurperso/OmniVoice-GGUF@0170941"  # = services/tts-omnivoice/fetch-weights.sh
_omni_state = {"at": 0.0, "ok": False}


def _omni_health() -> dict | None:
    """tts-omnivoice up? (cached 10 s; /health is polled by node-info every 30 s)"""
    if "omnivoice" not in worker.ENGINES:
        return None
    if time.monotonic() - _omni_state["at"] > 10:
        try:
            worker._omni_call("/health", method="GET", timeout=2)
            ok = True
        except Exception:
            ok = False
        _omni_state.update(at=time.monotonic(), ok=ok)
    return {"available": _omni_state["ok"], "device": os.environ.get("OMNIVOICE_DEVICE_KIND", "cuda"),
            "revision": f"{OMNI_REVISION} {os.environ.get('OMNIVOICE_QUANT', 'Q8_0')}",
            "quant": os.environ.get("OMNIVOICE_QUANT", "Q8_0"), "license": "cc-by-nc-4.0",
            "settings": worker.OMNI_SETTINGS}


def _device() -> str:
    """cpu | cuda: what the model worker uses (known once it started), else what was asked for."""
    try:
        return open(worker.DEVICE_FILE).read().strip() or "cpu"
    except OSError:
        return os.environ.get("TTS_DEVICE", "cpu")


# Voice gender, from the median pitch of a test phrase measured for every preset (2026-11-13): Holos female voices are
# 190-240 Hz, male 95-146 Hz, nothing in between. StyleTTS2 presets are named after their speakers.
HOLOS_FEMALE = {"Speaker_3", "Speaker_12", "Speaker_17", "Speaker_26", "Speaker_30", "Speaker_37", "Speaker_43",
                "Speaker_59", "Speaker_67", "Speaker_78", "Гаська Шиян"}
STYLETTS2_FEMALE = {"Марина Панас", "Гаська Шиян", "Анастасія Павленко", "Вероніка Дорош", "Вікторія Левченко",
                    "Влада Муравець", "Інна Гелевера", "Катерина Потапенко", "Людмила Чиркова", "Марися Нікітюк",
                    "Марічка Штирбулова", "Марта Мольфар", "Олена Шверк", "Поліна Еккерт", "Слава Красовська",
                    "Тетяна Гончарова", "Тетяна Лукинюк"}
CHILD = {"Поліна Еккерт(хлопчик)"}
GENDER_WORD = {"male": "Чоловічий", "female": "Жіночий", "child": "Дитячий"}
# Display names of the Holos presets (owner's pick 2026-11-14): a name plus what the voice sounds like (pitch,
# liveliness and tempo measured on one phrase). Ids stay Speaker_N; requests and aliases are unchanged.
HOLOS_NAMES = {
    "Speaker_11": ("Богдан", "бас"), "Speaker_36": ("Тарас", "низький спокійний"), "Speaker_24": ("Остап", "жвавий"),
    "Speaker_53": ("Ярослав", "оповідач"), "Speaker_84": ("Назар", "диктор"), "Speaker_1": ("Андрій", "основний"),
    "Speaker_0": ("Іван", "рівний"), "Speaker_34": ("Максим", "бадьорий"), "Speaker_56": ("Данило", "виразний"),
    "Speaker_42": ("Роман", "спокійний"), "Speaker_16": ("Олесь", "ведучий"), "Speaker_69": ("Мирослав", "неспішний"),
    "Speaker_21": ("Захар", "тенор"), "Speaker_18": ("Ілля", "високий"), "Speaker_29": ("Орест", "молодий"),
    "Speaker_35": ("Юрко", "юнак"),
    "Speaker_30": ("Оксана", "основна"), "Speaker_78": ("Соломія", "неспішна"), "Speaker_43": ("Леся", "спокійна"),
    "Speaker_37": ("Христина", "виразна"), "Speaker_67": ("Ярина", "рівна"), "Speaker_17": ("Дарина", "ведуча"),
    "Speaker_12": ("Марічка", "жвава"), "Speaker_26": ("Зоряна", "казкарка"), "Speaker_3": ("Олеся", "дикторка"),
    "Speaker_59": ("Уляна", "висока"),
}


def voice_meta(engine: str, names: list[str]) -> dict:
    """{voice: {gender, label[, name, description]}}: Holos presets get a name and a description ("Ярослав — оповідач",
    HOLOS_NAMES; an unknown Speaker_N falls back to a number per gender, "Чоловічий 3"); named voices keep their name. Labels are for display only; requests still use the voice ids and aliases."""
    female = HOLOS_FEMALE if engine == "holos" else STYLETTS2_FEMALE
    meta, counters = {}, {}

    def key(n: str):
        return int(n[8:]) if n.startswith("Speaker_") and n[8:].isdigit() else -1

    for n in sorted(names, key=key):
        gender = "child" if n in CHILD else "female" if n in female else "male"
        extra = {}
        if engine == "holos" and n in HOLOS_NAMES:
            name, about = HOLOS_NAMES[n]
            label, extra = f"{name} — {about}", {"name": name, "description": about}
        elif n.startswith("Speaker_"):
            counters[gender] = counters.get(gender, 0) + 1
            label = f"{GENDER_WORD[gender]} {counters[gender]}"
        else:
            label = n.replace("(хлопчик)", " (хлопчик)")
        meta[n] = {"gender": gender, "label": label, **extra}
    return meta


# What a phrase editor may offer as buttons (published per engine in /v1/voices and /capabilities). kind: inline =
# insert at the cursor ("insert"), wrap = put around the selection ("open"/"close"), replace = rewrite the selected
# word as it should sound, param = not markup but a request parameter ("param").
MARKUP = [
    {"id": "stress", "title": "Наголос", "kind": "inline", "insert": "+",
     "syntax": "+ одразу після наголошеного голосного", "example": "за+мок"},
    {"id": "stress_long", "title": "Наголос із протяжним голосним", "kind": "inline", "insert": "++",
     "syntax": "++ одразу після наголошеного голосного", "example": "за++мок"},
    {"id": "respell", "title": "Вимова слова", "kind": "replace",
     "syntax": "слово кирилицею так, як має звучати, з + на наголосі", "example": "Ма+йнкрафт",
     "note": "для всіх текстів одразу: правило словника (Minecraft => Ма+йнкрафт)"},
    {"id": "pause", "title": "Пауза", "kind": "inline", "insert": "{пауза 0.8}",
     "syntax": "{пауза N}: N секунд тиші, 0.1-5 (кома або крапка)", "example": "Зачекайте {пауза 1,5} і слухайте.",
     "min_s": 0.1, "max_s": 5.0,
     "note": "у субтитрах не видно; поза межами обрізається до 0.1 / 5 с; на початку чи в кінці довгого тексту (jobs, stream) ігнорується"},
    {"id": "aside", "title": "Вставка в дужках", "kind": "wrap", "open": "(", "close": ")",
     "example": "Він (як завжди) запізнився.", "note": "як читати, задає параметр brackets"},
    {"id": "paragraph", "title": "Пауза абзацу", "kind": "inline", "insert": "\n\n",
     "syntax": "порожній рядок між абзацами", "example": "Перше.\n\nДруге.", "note": "+400 мс до звичайної паузи"},
    {"id": "punctuation", "title": "Природні паузи", "kind": "inline", "syntax": ", . … — ? !",
     "example": "Так… Ні — ще ні."},
    {"id": "brackets", "title": "Як читати дужки", "kind": "param", "param": "brackets",
     "values": ["aside", "skip", "announce", "voice"], "default": "aside"},
    {"id": "sentence_pause", "title": "Пауза між реченнями", "kind": "param", "param": "pause_ms",
     "min": 0, "max": 3000, "default": 150, "note": "мс; /v1/audio/speech: 0 за замовчуванням"},
    {"id": "speed", "title": "Темп", "kind": "param", "param": "speed", "min": 0.5, "max": 2.0, "default": 1.0},
]


def voices_info() -> dict:
    engines = {}
    for engine in worker.ENGINES:
        names = worker.voice_names(engine)
        aliases = {k: v for k, v in worker.ALIASES[engine].items() if v in names}
        favorites = list(dict.fromkeys(aliases.values()))  # in alias order, default first
        engines[engine] = {"default": aliases.get("male") or (favorites or names)[0], "aliases": aliases,
                           "favorites": favorites, "voices": favorites + [n for n in names if n not in favorites],
                           "meta": voice_meta(engine, names), "markup": MARKUP}
    if "omnivoice" in engines:  # voice design presets; no stress marks or respelling (it reads plain text)
        engines["omnivoice"]["meta"] = {
            "male": {"gender": "male", "label": "Чоловічий (опис голосу)"},
            "female": {"gender": "female", "label": "Жіночий (опис голосу)"},
            "auto": {"gender": "male", "label": "Автоматичний"}}
        engines["omnivoice"]["markup"] = [m for m in MARKUP if m["id"] not in ("stress", "stress_long", "respell")]
        engines["omnivoice"]["settings"] = worker.OMNI_SETTINGS
    for vid, v in worker.custom_voices().items():  # custom voices after the presets of their model
        m = v.get("model", "holos")
        if m in engines:
            engines[m]["voices"].append(f"custom:{vid}")
            engines[m]["meta"][f"custom:{vid}"] = {"gender": v["gender"], "label": v["name"], "name": v["name"],
                                                 "custom": True, "sha256": v["sha256"]}
    if "styletts2" in engines:
        engines["styletts2"]["models"] = {"multi": "the voices above", "single": "one male narrator (voice ignored)"}
    return {"default_engine": worker.DEFAULT_ENGINE, "default_voice": engines[worker.DEFAULT_ENGINE]["default"],
            "stress": worker.STRESS, "engines": engines}


@app.get("/v1/voices", dependencies=[Depends(auth)])
@app.get("/voices", dependencies=[Depends(auth)], include_in_schema=False)
async def voices() -> dict:
    return voices_info()


# --- custom voices (zero-shot Holos): encode a reference recording; install / delete a voice by id ----------------
VOICE_ID = r"^[a-z0-9][a-z0-9_-]{0,47}$"
ENCODE_MAX_BYTES = 50 * 1024 * 1024
CUSTOM_MAX = int(os.environ.get("TTS_CUSTOM_MAX", "500"))


def vector_sha256(vector: list[float]) -> str:
    """sha256 of the vector as 256 little-endian float32 (what the coordinator stores and compares)."""
    import hashlib
    import struct

    return hashlib.sha256(struct.pack(f"<{len(vector)}f", *vector)).hexdigest()


@app.post("/v1/voices/encode", dependencies=[Depends(auth)])
async def voices_encode(request: Request, audio: UploadFile = File(...)) -> dict:
    """Reference recording (any format ffmpeg reads, 3 s .. 50 MB) -> {vector, sha256, model, duration_s, warnings}.
    Nothing is stored: the audio is deleted when the vector is ready, the vector lives only in the reply."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="enc-") as tmp:
        src, wav = os.path.join(tmp, "in"), os.path.join(tmp, "ref.wav")
        size = 0
        with open(src, "wb") as f:
            while chunk := await audio.read(1 << 20):
                size += len(chunk)
                if size > ENCODE_MAX_BYTES:
                    raise HTTPException(413, "audio larger than 50 MB")
                f.write(chunk)
        p = await asyncio.create_subprocess_exec("ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", src, "-t", "300",
                                                 "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", wav,
                                                 stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate()
        if p.returncode != 0 or not os.path.isfile(wav):
            raise HTTPException(422, f"not a readable audio file: {err.decode(errors='replace')[-200:]}")
        if os.path.getsize(wav) < 3 * 24000 * 2:
            raise HTTPException(422, "too short: give at least 3 s (15-30 s of continuous speech is best)")
        out = await run(worker.encode_voice, wav, request=request)
    out["sha256"] = vector_sha256(out["vector"])
    out["model"] = f"holos@{os.environ.get('HOLOS_REVISION', 'unknown')[:12]}"
    out["dim"] = len(out["vector"])
    return out


class CustomVoice(BaseModel):
    model: str = Field("holos", pattern=r"^(holos|omnivoice)$", description="the model the voice belongs to")
    name: str = Field(..., min_length=1, max_length=64, description="display name")
    gender: str = Field(..., pattern=r"^(male|female|child)$")
    vector: list[float] | None = Field(None, min_length=256, max_length=256,
                                       description="holos: from POST /v1/voices/encode")
    ref_audio: str | None = Field(None, max_length=14_000_000, description="omnivoice: base64 of a 3-10 s recording "
                                                                          "(wav, opus, any format ffmpeg reads)")
    ref_text: str | None = Field(None, min_length=1, max_length=1000, description="omnivoice: what the recording says")
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$",
                        description="holos: sha256 of the vector as 256 LE float32; omnivoice: of the ref_audio bytes")
    consent_ref: str = Field(..., min_length=1, max_length=200, description="the coordinator's consent record")


async def _omni_reference(voice_id: str, req: CustomVoice) -> None:
    """omnivoice: checks and stores the recording as <id>.wav (24 kHz mono 16-bit, at most 30 s)"""
    import base64
    import hashlib
    import tempfile

    if "omnivoice" not in worker.ENGINES:
        raise HTTPException(409, "model omnivoice is not enabled on this node")
    if not req.ref_audio or not req.ref_text:
        raise HTTPException(422, "model omnivoice: ref_audio (base64) and ref_text are required")
    try:
        raw = base64.b64decode(req.ref_audio, validate=True)
    except ValueError:
        raise HTTPException(422, "ref_audio: not base64") from None
    if hashlib.sha256(raw).hexdigest() != req.sha256:
        raise HTTPException(400, "sha256 does not match the ref_audio bytes")
    with tempfile.TemporaryDirectory(prefix="ref-") as tmp:
        src, wav = os.path.join(tmp, "in"), os.path.join(tmp, "ref.wav")
        open(src, "wb").write(raw)
        p = await asyncio.create_subprocess_exec("ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", src, "-t", "30",
                                                 "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", wav,
                                                 stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate()
        if p.returncode != 0 or not os.path.isfile(wav):
            raise HTTPException(422, f"ref_audio: not a readable audio file: {err.decode(errors='replace')[-200:]}")
        if os.path.getsize(wav) < 3 * 24000 * 2:
            raise HTTPException(422, "ref_audio: too short, give 3-10 s")
        os.makedirs(worker.CUSTOM_DIR, exist_ok=True)
        os.replace(wav, os.path.join(worker.CUSTOM_DIR, f"{voice_id}.wav"))


@app.put("/v1/voices/custom/{voice_id}", dependencies=[Depends(auth)])
async def voices_custom_put(voice_id: str, req: CustomVoice) -> dict:
    """Install (or replace) a custom voice on this node; requests then use voice="custom:<id>" (engine holos)."""
    import re as _re

    if not _re.fullmatch(VOICE_ID, voice_id):
        raise HTTPException(400, "id: 1-48 of a-z 0-9 _ - (starting with a letter or digit)")
    if req.model == "holos":
        if req.vector is None:
            raise HTTPException(422, "model holos: vector is required")
        if vector_sha256(req.vector) != req.sha256:
            raise HTTPException(400, "sha256 does not match the vector (256 little-endian float32)")
    os.makedirs(worker.CUSTOM_DIR, exist_ok=True)
    path = os.path.join(worker.CUSTOM_DIR, f"{voice_id}.json")
    existed = os.path.isfile(path)
    if not existed and len(worker.custom_voices()) >= CUSTOM_MAX:
        raise HTTPException(409, f"this node holds the maximum of {CUSTOM_MAX} custom voices")
    if req.model == "omnivoice":
        await _omni_reference(voice_id, req)
    elif os.path.isfile(os.path.join(worker.CUSTOM_DIR, f"{voice_id}.wav")):  # was an omnivoice voice before
        os.remove(os.path.join(worker.CUSTOM_DIR, f"{voice_id}.wav"))
    fields = req.model_dump(exclude={"ref_audio"} | ({"vector"} if req.model == "omnivoice" else {"ref_text"}))
    rec = {"id": voice_id, **fields, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, path)
    if req.model == "omnivoice":  # best effort now; a synthesis registers it again if tts-omnivoice lost it
        try:
            await asyncio.to_thread(worker.omni_register, voice_id)
        except Exception as exc:
            log.warning("omnivoice voice %s not registered yet: %s", voice_id, exc)
    return {"id": voice_id, "model": req.model, "voice": f"custom:{voice_id}", "sha256": req.sha256,
            "status": "replaced" if existed else "created"}


@app.delete("/v1/voices/custom/{voice_id}", dependencies=[Depends(auth)])
async def voices_custom_delete(voice_id: str) -> dict:
    path = os.path.join(worker.CUSTOM_DIR, f"{voice_id}.json")
    if "/" in voice_id or not os.path.isfile(path):
        raise HTTPException(404, "no such custom voice")
    os.remove(path)
    wav = os.path.join(worker.CUSTOM_DIR, f"{voice_id}.wav")
    if os.path.isfile(wav):
        os.remove(wav)
        if worker.OMNIVOICE_URL:
            await asyncio.to_thread(worker.omni_unregister, voice_id)
    return {"id": voice_id, "status": "deleted"}


@app.get("/v1/voices/custom", dependencies=[Depends(auth)])
async def voices_custom_list() -> dict:
    """{voices: {id: {name, gender, sha256, consent_ref, created}}} without the vectors (cheap: for /capabilities)."""
    return {"zero_shot": True, "finetune": False,
            "voices": {k: {**{f: v.get(f) for f in ("name", "gender", "sha256", "consent_ref", "created")},
                           "model": v.get("model", "holos")} for k, v in worker.custom_voices().items()}}


@app.post("/v1/audio/speech", dependencies=[Depends(auth)])
@app.post("/synthesize", dependencies=[Depends(auth)], include_in_schema=False)
async def speech(req: SpeechRequest, request: Request) -> Response:
    text, engine, model, voice = _validate(req)
    text, _ = await asyncio.to_thread(speechtext.prepare_text, text, clean_text=req.clean, use_english=req.english,
                                      brackets=req.brackets, announce=req.brackets_announce,
                                      initials_mode=req.initials, use_lexicon=req.lexicon)

    async def speak(speed: float) -> bytes:
        if speechtext.ASIDE_START in text:
            pcm, _ = await _synth(text, dict(_params(req, engine, model, voice), speed=speed), request)
        else:
            t1 = time.monotonic()
            pcm, _ = await run(worker.synthesize, text, voice, speed, model, req.pause_ms, req.verbalize, engine,
                               req.lexicon, req.settings,
                               request=request)
            PERF.add(len(pcm) / 4 / worker.SAMPLE_RATE, time.monotonic() - t1)
        return pcm

    t0 = time.monotonic()
    fit = None
    try:
        pcm = await speak(req.speed)
        if req.target_duration:
            pcm, fit = await _fit(pcm, req, speak)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    elapsed = time.monotonic() - t0
    dur = len(pcm) / 4 / worker.SAMPLE_RATE
    data = await asyncio.to_thread(encode, pcm, req.response_format, req.sample_rate)
    headers = {"X-Audio-Duration": str(round(dur, 2)), "X-Elapsed": str(round(elapsed, 2)),
               "X-RTF": str(round(elapsed / dur, 3) if dur else 0), "X-Engine": engine,
               "X-Voice": quote(voice or "single")}
    if fit:
        headers.update({"X-Target-Duration": str(req.target_duration), "X-Fit": fit["status"],
                        "X-Fit-Speed": str(fit["speed"]), "X-Speech-Duration": str(fit["speech_s"])})
    return Response(data, media_type=FORMATS[req.response_format], headers=headers)


async def _fit(pcm: bytes, req: SpeechRequest, speak) -> tuple[bytes, dict]:
    """Make the speech last req.target_duration seconds: re-synthesize once at the needed speed (natural), then
    time-stretch the small remainder, within [fit_min_speed, fit_max_speed] x the requested speed; shorter speech
    is padded with silence (fit_pad=end) or left as is (none). status: ok | overflow (does not fit even at the
    fastest allowed speed; returned at that speed)."""
    sr, target = worker.SAMPLE_RATE, req.target_duration
    lo, hi = req.fit_min_speed, req.fit_max_speed
    dur = len(pcm) / 4 / sr
    factor = 1.0  # speed applied relative to req.speed
    want = dur / target
    if abs(want - 1) > 0.04:
        factor = min(hi, max(lo, want))
        new_speed = min(2.0, max(0.5, req.speed * factor))
        factor = new_speed / req.speed
        pcm = await speak(new_speed)
        dur = len(pcm) / 4 / sr
    rest = dur / target
    if abs(rest - 1) > 0.005:  # fine-tune with a time-stretch (only a few percent here, inaudible)
        a = min(hi / factor, max(lo / factor, rest))
        if abs(a - 1) > 0.005:
            pcm = await asyncio.to_thread(speechtext.time_stretch, pcm, a)
            factor *= a
            dur = len(pcm) / 4 / sr
    speech_s = round(dur, 2)
    status = "overflow" if dur > target + 0.05 else "ok"
    if dur < target and req.fit_pad == "end":
        pcm += bytes(4 * int(round((target - dur) * sr)))
    elif status == "ok" and dur > target:
        pcm = pcm[:4 * int(target * sr)]  # within 50 ms: trailing silence
    return pcm, {"status": status, "speed": round(req.speed * factor, 3), "speech_s": speech_s}


_warming: asyncio.Task | None = None


@app.post("/v1/warmup", dependencies=[Depends(auth)])
async def warmup(engine: str | None = None) -> dict:
    """Start loading the models in the background (cold start ~25 s); returns at once."""
    global _warming
    engine = engine if engine in worker.ENGINES else worker.DEFAULT_ENGINE
    if _pool is None and (_warming is None or _warming.done()):
        _warming = asyncio.create_task(run(worker.warmup, engine))
    return {"loaded": _pool is not None and (_warming is None or _warming.done()), "engine": engine}


@app.post("/v1/audio/speech/stream", dependencies=[Depends(auth)])
async def speech_stream(req: LongSpeechRequest, request: Request) -> StreamingResponse:
    """One continuous audio stream; sound starts after the first sentence group. Not stored in history."""
    text, engine, model, voice = _validate(req, LONG_MAX_CHARS, speechtext.STREAM_FORMATS)
    segs = await _plan(text, req)
    p = _params(req, engine, model, voice)

    async def pcm():
        for s in segs:
            try:
                data, _ = await _synth(s["spoken"], p, request)
            except ValueError:  # nothing speakable in this group
                continue
            yield data
            if s["pause_after_ms"]:
                yield bytes(4 * int(worker.SAMPLE_RATE * s["pause_after_ms"] / 1000))  # float32 zeros

    return StreamingResponse(speechtext.encode_stream(pcm(), req.response_format),
                             media_type=speechtext.STREAM_FORMATS[req.response_format],
                             headers={"X-Segments": str(len(segs)), "Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


# ---- jobs (history) ------------------------------------------------------------------------------------------------

JOBS = jobs.Jobs(synth=_synth, encode=lambda pcm, fmt: encode(pcm, fmt, None))
_JOB_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


def _job(jid: str) -> dict:
    m = JOBS.meta.get(jid) if _JOB_ID.match(jid) else None
    if m is None:
        raise HTTPException(404, "job not found")
    return m


@app.post("/v1/jobs", status_code=202, dependencies=[Depends(auth)])
async def create_job(req: JobRequest) -> dict:
    text, engine, model, voice = _validate(req, LONG_MAX_CHARS)
    segs = await _plan(text, req)
    return await JOBS.create(text, segs, _params(req, engine, model, voice), req.title)


@app.get("/v1/jobs", dependencies=[Depends(auth)])
async def list_jobs(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                    q: str = Query("", max_length=200)) -> dict:
    return JOBS.list(limit, offset, q)


@app.get("/v1/jobs/{jid}", dependencies=[Depends(auth)])
async def get_job(jid: str) -> dict:
    return JOBS.public(_job(jid), full=True)


class JobPatch(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)


@app.patch("/v1/jobs/{jid}", dependencies=[Depends(auth)])
async def rename_job(jid: str, req: JobPatch) -> dict:
    _job(jid)
    return await JOBS.rename(jid, req.title)


@app.post("/v1/jobs/{jid}/cancel", dependencies=[Depends(auth)])
async def cancel_job(jid: str) -> dict:
    _job(jid)
    return await JOBS.cancel(jid)


@app.delete("/v1/jobs/{jid}", dependencies=[Depends(auth)])
async def delete_job(jid: str) -> dict:
    _job(jid)
    await JOBS.delete(jid)
    return {"deleted": jid}


@app.get("/v1/jobs/{jid}/segments/{index}", dependencies=[Depends(auth)])
async def job_segment(jid: str, index: int) -> Response:
    _job(jid)
    data = await asyncio.to_thread(JOBS.segment_wav, jid, index)
    if data is None:
        raise HTTPException(404, "segment not ready or already removed (use the final audio)")
    return Response(data, media_type="audio/wav", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/v1/jobs/{jid}/audio", dependencies=[Depends(auth)])
async def job_audio(jid: str, download: bool = False) -> FileResponse:
    m = _job(jid)
    path = JOBS.audio_path(jid)
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "no audio yet")
    fmt = m["params"]["response_format"]
    return FileResponse(path, media_type=FORMATS.get(fmt, "application/octet-stream"),
                        filename=f"ozvuchka-{jid}{os.path.splitext(path)[1]}" if download else None)


@app.get("/v1/jobs/{jid}/subtitles", dependencies=[Depends(auth)])
async def job_subtitles(jid: str, format: str = Query("srt", pattern="^(srt|vtt)$")) -> PlainTextResponse:
    _job(jid)
    return PlainTextResponse(JOBS.subtitles(jid, format),
                             media_type="text/vtt" if format == "vtt" else "application/x-subrip")


@app.get("/v1/jobs/{jid}/events", dependencies=[Depends(auth)])
async def job_events(jid: str, request: Request) -> StreamingResponse:
    """SSE: replays finished segments, then live `segment` and `job` events until the job ends."""
    _job(jid)

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def gen():
        sent: set[int] = set()
        last = None
        while True:
            version = JOBS.version
            m = JOBS.meta.get(jid)
            if m is None:
                yield sse("job", {"id": jid, "status": "deleted"})
                return
            for i, s in enumerate(m["segments"]):
                if s["done"] and i not in sent:
                    sent.add(i)
                    yield sse("segment", {"index": i, "text": s["text"], "start_s": s["start_s"],
                                          "duration_s": s["duration_s"],
                                          "audio_url": f"/v1/jobs/{jid}/segments/{i}" if s["duration_s"] else None})
            pub = JOBS.public(m)
            if pub != last:
                last = pub
                yield sse("job", pub)
            if m["status"] in jobs.FINAL or await request.is_disconnected():
                return
            timed_out = False
            async with JOBS.changed:
                try:
                    await asyncio.wait_for(JOBS.changed.wait_for(lambda: JOBS.version != version), 15)
                except asyncio.TimeoutError:
                    timed_out = True
            if timed_out:
                yield ": ping\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


class LexiconRule(BaseModel):
    rule: str = Field(..., description='e.g. "ваш замок => ваш за+мок" or "~ (?<=\\bваш\\s)замок\\b => за+мок"')


def _lexicon_info() -> dict:
    text = lexicon.read_text()
    rules = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    return {"path": lexicon.PATH, "rules": rules, "text": text}


def _write_lexicon(text: str) -> dict:
    try:
        lexicon.write_text(text)
    except lexicon.RuleError as exc:
        raise HTTPException(422, str(exc))
    return _lexicon_info()


@app.get("/v1/lexicon", dependencies=[Depends(auth)])
async def get_lexicon() -> dict:
    """The stress lexicon: rules (without comments) and the whole file text."""
    return _lexicon_info()


@app.put("/v1/lexicon", dependencies=[Depends(auth)])
async def put_lexicon(request: Request) -> dict:
    """Replace the whole file: text/plain body, or JSON {"text": "..."}. Every rule is validated first."""
    if request.headers.get("content-type", "").startswith("application/json"):
        text = str((await request.json()).get("text", ""))
    else:
        text = (await request.body()).decode("utf-8")
    return _write_lexicon(text)


@app.post("/v1/lexicon", dependencies=[Depends(auth)])
async def add_lexicon_rule(req: LexiconRule) -> dict:
    """Append one rule (validated)."""
    rule = req.rule.strip()
    text = lexicon.read_text()
    if rule in (line.strip() for line in text.splitlines()):
        return _lexicon_info()
    return _write_lexicon(text.rstrip("\n") + "\n" + rule + "\n")


@app.delete("/v1/lexicon", dependencies=[Depends(auth)])
async def remove_lexicon_rule(req: LexiconRule) -> dict:
    """Remove every line equal to the rule; 404 if it is not there."""
    lines = lexicon.read_text().splitlines()
    kept = [line for line in lines if line.strip() != req.rule.strip()]
    if len(kept) == len(lines):
        raise HTTPException(404, "rule not found (send it exactly as GET /v1/lexicon lists it)")
    return _write_lexicon("\n".join(kept) + "\n")


class TextRequest(BaseModel):
    text: str
    lexicon: bool = True  # false: no lexicon rule at all
    verbalize: bool = True
    english: bool = True
    clean: bool = True
    brackets: str = "aside"
    brackets_announce: bool = False
    initials: str = "letters"


@app.post("/v1/text/verbalize", dependencies=[Depends(auth)])
async def verbalize(req: TextRequest) -> dict:
    return {"text": await run(worker.verbalize, req.text[:MAX_CHARS])}


@app.post("/v1/text/prepare", dependencies=[Depends(auth)])
async def prepare(req: TextRequest) -> dict:
    """Exactly what the voice will read: cleanup, English names, numbers in words, stress marks."""
    mode = req.brackets if req.brackets in speechtext.BRACKET_MODES else "aside"
    text, names = await asyncio.to_thread(speechtext.prepare_text, req.text[:MAX_CHARS], clean_text=req.clean,
                                          use_english=req.english, brackets=mode, announce=req.brackets_announce,
                                          initials_mode=req.initials if req.initials in speechtext.INITIALS_MODES
                                          else "letters", use_lexicon=req.lexicon)
    out = await run(worker.prepare, text.replace(speechtext.ASIDE_START, "").replace(speechtext.ASIDE_END, ""),
                    req.verbalize, req.lexicon)
    return {**out, "names": [{"word": w, "uk": u} for w, u in names.items()]}


class PhonemizeRequest(BaseModel):
    text: str = Field(description="text with English names (or just the names)")
    lang: str = Field("en-us", pattern=r"^en(-[a-z]{2})?$", description="espeak-ng English voice: en-us, en-gb")


@app.post("/v1/text/phonemize", dependencies=[Depends(auth)])
async def phonemize_names(req: PhonemizeRequest) -> dict:
    """How the Ukrainian voices should say each English name in the text: espeak-ng IPA (with stress) mapped to
    Cyrillic with "+" after the stressed vowel. Paste "uk" into the text (or a lexicon rule) instead of the Latin word."""
    words = [w for w in phonemize.latin_words(req.text[:MAX_CHARS]) if not speechtext.is_roman(w)][:200]

    def one(w: str) -> dict:
        return {"word": w, "ipa": phonemize.ipa(w, req.lang), "uk": speechtext.say_name(w, req.lang)}

    return {"lang": req.lang, "items": await asyncio.to_thread(lambda: [one(w) for w in words])}


async def main() -> None:
    asyncio.create_task(_reaper())
    JOBS.start()
    servers = [uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8000, access_log=False))]
    if ENABLE_UI:
        import ui

        # the UI talks to this same API in-process under /api (its login session instead of the bearer key)
        ui_app = ui.build(app, os.environ.get("TTS_UI_PASSWORD") or API_KEY, API_KEY)
        servers.append(uvicorn.Server(uvicorn.Config(ui_app, host="0.0.0.0", port=8001, access_log=False)))
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
