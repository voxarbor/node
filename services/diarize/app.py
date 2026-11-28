"""Speaker diarization API: who speaks when, and each speaker's gender (median pitch). sherpa-onnx (pyannote
segmentation-3.0 + speaker embeddings, ONNX, CPU) in a worker process started on demand and stopped after IDLE_TTL.

POST /v1/diarize (multipart): file (anything ffmpeg reads), optional offset/duration (seconds) for a window,
session (keeps the decoded audio and links speakers across windows by embedding similarity, so "S1" stays the same
person through the video; later windows of a session need no file), num_speakers | min_speakers/max_speakers,
threshold. -> {speakers [{id, gender, total_s, f0_hz}], turns [{start, end, speaker}], window, elapsed_ms}."""

import asyncio
import multiprocessing
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import worker

API_KEY = os.environ["API_KEY"]
IDLE_TTL = int(os.environ.get("IDLE_TTL", "600"))
QUEUE_MAX = int(os.environ.get("QUEUE_MAX", "4"))
LINK_SIM = float(os.environ.get("DIARIZE_LINK_SIM", "0.5"))  # cosine similarity to reuse a session speaker
SESSION_TTL = int(os.environ.get("DIARIZE_SESSION_TTL", "7200"))
CONTEXT_S = float(os.environ.get("DIARIZE_CONTEXT_S", "60"))  # windows re-read this much of the previous one
MIN_OVERLAP_S = 2.0  # shared speech needed to carry a speaker id over from the previous window
MAX_UPLOAD = 1024 * 1024 * 1024
WORK = os.environ.get("DIARIZE_WORK", "/tmp/diarize")
# audit #28: decoded audio lives in a RAM-backed tmpfs: at most this much audio per upload (16 kHz float: 230 MB/h),
# ffmpeg at most DECODE_TIMEOUT s, one decode at a time, and the slot is taken before the upload is copied
MAX_AUDIO_S = float(os.environ.get("DIARIZE_MAX_AUDIO_S", str(4 * 3600)))
DECODE_TIMEOUT = float(os.environ.get("DIARIZE_DECODE_TIMEOUT", "900"))
_decode_one = asyncio.Semaphore(1)
_admitted = 0

app = FastAPI(title="diarize")
bearer = HTTPBearer(auto_error=False)
_pool: ProcessPoolExecutor | None = None
_last_used = 0.0
_busy = 0
_lock = asyncio.Lock()
_sessions: dict[str, dict] = {}  # id -> {path, audio_s, speakers [{id, centroid, weight, f0, total_s}], used}
_SESSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def auth(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
    if not cred or not secrets.compare_digest(cred.credentials, API_KEY):
        raise HTTPException(401, "invalid api key")


async def _reaper() -> None:
    global _pool
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for sid, s in list(_sessions.items()):  # forget idle sessions and their audio
            if now - s["used"] > SESSION_TTL:
                _sessions.pop(sid, None)
                shutil.rmtree(os.path.dirname(s["path"]), ignore_errors=True)
        async with _lock:
            if not _pool or _busy or time.monotonic() - _last_used <= IDLE_TTL:
                continue
            pool, _pool = _pool, None
        await asyncio.to_thread(pool.shutdown)


@app.on_event("startup")
async def _start() -> None:
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    asyncio.create_task(_reaper())


async def _run(fn, *args):
    global _pool, _last_used, _busy
    async with _lock:
        if _busy >= QUEUE_MAX:
            raise HTTPException(429, f"busy: {QUEUE_MAX} jobs queued, retry later")
        if _pool is None:
            _pool = ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"), initializer=worker.init)
        future = _pool.submit(fn, *args)
        _busy += 1
    try:
        return await asyncio.wrap_future(future)
    finally:
        _busy -= 1
        _last_used = time.monotonic()


@app.get("/health")
async def health() -> dict:
    try:  # the device the worker really uses (known once it started), else what was asked for
        dev = open("/tmp/diarize-device").read().strip()
    except OSError:
        dev = os.environ.get("DIARIZE_DEVICE", "cpu") if worker.ENGINE == "pyannote" else "cpu"
    return {"status": "ok", "loaded": _pool is not None, "idle_ttl": IDLE_TTL, "sessions": len(_sessions),
            "busy": _busy, "queue_max": QUEUE_MAX, "engine": worker.ENGINE, "device": dev,
            "gender_model": worker.GENDER_MODEL,
            # pyannote: HF commit of community-1; sherpa-onnx: sha256 of the segmentation + embedding ONNX files
            "model_revision": (os.environ.get("PYANNOTE_REVISION", "")[:12] if worker.ENGINE == "pyannote"
                               else "seg 24615ee884c8 + emb e9848563da86"),
            "model": ("pyannote/speaker-diarization-community-1 (pyannote.audio 4)" if worker.ENGINE == "pyannote"
                      else "pyannote segmentation-3.0 + wespeaker_en_voxceleb_resnet34_LM (sherpa-onnx)")}


async def _decode(upload: UploadFile) -> tuple[str, float]:
    """Upload -> 16 kHz mono float WAV in its own temp dir (ffmpeg reads any audio/video)."""
    d = tempfile.mkdtemp(dir=WORK)
    src, dst = os.path.join(d, "input"), os.path.join(d, "audio.wav")
    size = 0
    try:
        with open(src, "wb") as f:
            while chunk := await upload.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "file larger than 1 GB")
                f.write(chunk)
        async with _decode_one:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-v", "error", "-nostdin", "-i", src, "-vn", "-ac", "1", "-ar", str(worker.SR),
                "-t", str(MAX_AUDIO_S), "-c:a", "pcm_f32le", dst, stderr=asyncio.subprocess.PIPE)
            try:
                _, err = await asyncio.wait_for(proc.communicate(), DECODE_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise HTTPException(504, f"decoding took over {DECODE_TIMEOUT:.0f} s") from None
        os.remove(src)
        if proc.returncode != 0:
            raise HTTPException(422, f"cannot decode audio: {err.decode(errors='replace')[-300:]}")
    except BaseException:  # any failure (also a full tmpfs) leaves no folder behind
        shutil.rmtree(d, ignore_errors=True)
        raise
    import soundfile as sf

    return dst, sf.info(dst).frames / worker.SR


def _by_overlap(session: dict, turns: list, ctx_start: float, ctx_end: float) -> dict[int, str]:
    """Local speakers -> session ids by shared speech in the re-read context (who was talking then, by the previous
    window's labels): the reliable link; similar voices can have similar embeddings."""
    score: dict[tuple[int, str], float] = {}
    for a, b, lid in turns:
        a, b = max(a, ctx_start), min(b, ctx_end)
        if b <= a:
            continue
        for t in session["turns"]:
            o = min(b, t["end"]) - max(a, t["start"])
            if o > 0:
                score[(lid, t["speaker"])] = score.get((lid, t["speaker"]), 0.0) + o
    mapping, used = {}, set()
    for (lid, gid), o in sorted(score.items(), key=lambda kv: -kv[1]):
        if o >= MIN_OVERLAP_S and lid not in mapping and gid not in used:
            mapping[lid] = gid
            used.add(gid)
    return mapping


def _link(session: dict, local: dict, premap: dict[int, str] | None = None) -> dict[int, str]:
    """Local speakers of one window -> session speaker ids: premap (context overlap) first, then cosine similarity
    of embeddings to the remaining session speakers (greedy, one-to-one); the rest become new ids. Centroids and
    pitch accumulate."""
    known = session["speakers"]
    mapping = dict(premap or {})
    taken = set(mapping.values())
    pairs = []
    for lid, d in local.items():
        if lid in mapping:
            continue
        e = np.array(d["embedding"], dtype=np.float32)
        for k, g in enumerate(known):
            if g["id"] in taken:
                continue
            c = g["centroid"] / (np.linalg.norm(g["centroid"]) + 1e-9)
            pairs.append((float(e @ c), lid, k))
    used = set()
    for sim, lid, k in sorted(pairs, reverse=True):
        if sim >= LINK_SIM and lid not in mapping and k not in used:
            mapping[lid] = known[k]["id"]
            used.add(k)
    for lid in sorted(local, key=lambda i: -local[i]["total_s"]):
        if lid not in mapping:
            known.append({"id": f"S{len(known) + 1}", "centroid": np.zeros(len(local[lid]["embedding"]), np.float32),
                          "weight": 0.0, "f0": [], "total_s": 0.0, "pf": 0.0, "pf_w": 0.0})
            mapping[lid] = known[-1]["id"]
    by_id = {g["id"]: g for g in known}
    for lid, d in local.items():
        g = by_id[mapping[lid]]
        w = min(d["total_s"], 30.0)
        g["centroid"] = g["centroid"] + w * np.array(d["embedding"], dtype=np.float32)
        g["weight"] += w
        g["total_s"] = round(g["total_s"] + d["total_s"], 2)
        g["f0"] = (g["f0"] + d["f0"])[-6000:]
        g["pf"] += w * d["p_female"]  # session gender: talk-time weighted P(female)
        g["pf_w"] += w
    return mapping


@app.post("/v1/diarize", dependencies=[Depends(auth)])
async def diarize(request: Request, file: UploadFile | None = File(None), session: str | None = Form(None),
                  offset: float = Form(0.0), duration: float | None = Form(None), num_speakers: int = Form(0),
                  min_speakers: int = Form(0), max_speakers: int = Form(0),
                  threshold: float | None = Form(None)) -> dict:
    started = time.monotonic()
    if session is not None and not _SESSION_ID.match(session):
        raise HTTPException(422, "session: 1-64 of A-Z a-z 0-9 _ . -")
    if offset < 0 or (duration is not None and duration <= 0):
        raise HTTPException(422, "offset >= 0, duration > 0")
    s = _sessions.get(session) if session else None
    global _admitted
    if _admitted >= QUEUE_MAX:  # before copying and decoding the upload (#28)
        raise HTTPException(429, f"busy: {QUEUE_MAX} jobs queued, retry later")
    _admitted += 1
    try:
        return await _diarize(s, file, session, offset, duration, num_speakers, min_speakers, max_speakers,
                              threshold, started)
    finally:
        _admitted -= 1


async def _diarize(s, file, session, offset, duration, num_speakers, min_speakers, max_speakers, threshold, started):
    if file is not None:
        path, audio_s = await _decode(file)
        if s:  # a new file for an existing session replaces its audio and speakers
            shutil.rmtree(os.path.dirname(s["path"]), ignore_errors=True)
        s = {"path": path, "audio_s": audio_s, "speakers": [], "turns": [], "used": time.time()}
        if session:
            _sessions[session] = s
    elif s is None:
        raise HTTPException(422, "send a file (or a session that already has one)")
    s["used"] = time.time()
    # a later window re-reads the end of the previous one, so its speakers can be matched by who talked there
    ctx = min(CONTEXT_S, offset) if session and s["turns"] and offset > 0 else 0.0
    try:
        out = await _run(worker.diarize, s["path"], offset - ctx, None if duration is None else duration + ctx,
                         num_speakers, min_speakers, max_speakers, threshold)
    finally:
        if not session:
            shutil.rmtree(os.path.dirname(s["path"]), ignore_errors=True)
    premap = _by_overlap(s, out["turns"], offset - ctx, offset) if ctx else {}
    mapping = _link(s, out["speakers"], premap)
    turns = [{"start": max(a, offset), "end": b, "speaker": mapping[sp]} for a, b, sp in out["turns"] if b > offset]
    if session:
        s["turns"] = [t for t in s["turns"] if t["end"] > offset - 2 * CONTEXT_S] + turns  # only recent ones matter
    speakers = []
    for g in s["speakers"]:
        pf = g["pf"] / g["pf_w"] if g["pf_w"] else None
        gender, conf = worker.gender(pf, g["f0"])
        speakers.append({"id": g["id"], "gender": gender, "gender_confidence": conf,
                         "p_female": round(pf, 3) if pf is not None else None, "total_s": g["total_s"],
                         "f0_hz": round(float(np.median(g["f0"])), 1) if g["f0"] else None})
    end = out["audio_s"] if duration is None else min(out["audio_s"], offset + duration)
    return {"speakers": speakers, "turns": turns, "session": session,
            "window": {"offset": offset, "end": round(end, 2), "audio_s": round(out["audio_s"], 2),
                       "done": end >= out["audio_s"] - 0.01},
            "elapsed_ms": round((time.monotonic() - started) * 1000)}


@app.delete("/v1/diarize/{session}", dependencies=[Depends(auth)])
async def forget(session: str) -> dict:
    s = _sessions.pop(session, None)
    if s:
        shutil.rmtree(os.path.dirname(s["path"]), ignore_errors=True)
    return {"deleted": bool(s)}
