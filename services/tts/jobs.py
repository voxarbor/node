"""Synthesis jobs with history. One job runs at a time; each segment goes through the shared model worker on its
own (so direct /v1/audio/speech calls interleave), is saved as soon as it is ready (live playback over SSE) and the
final file is assembled at the end (also after cancel/error, from the finished part). Jobs left unfinished by a
restart resume from their saved segments.

Layout: TTS_JOBS_DIR/<id>/meta.json, text.txt, seg-00000.flac (24-bit; removed SEG_KEEP_S after the job ends),
audio.<ext>. Retention: TTS_JOBS_KEEP_DAYS and TTS_JOBS_MAX_GB, oldest finished jobs first."""

from __future__ import annotations

import asyncio
import io
import json
import os
import secrets
import shutil
import time
from datetime import datetime, timezone

ROOT = os.environ.get("TTS_JOBS_DIR", "/jobs")
KEEP_DAYS = float(os.environ.get("TTS_JOBS_KEEP_DAYS", "30"))
MAX_BYTES = float(os.environ.get("TTS_JOBS_MAX_GB", "5")) * 1024 ** 3
SEG_KEEP_S = 3600
FINAL = ("done", "error", "canceled")
EXT = {"mp3": "mp3", "opus": "ogg", "flac": "flac", "wav": "wav"}
SAMPLE_RATE = 24000


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _title(text: str) -> str:
    first = " ".join(text.split())[:70]
    return first if len(first) < 70 else first[:first.rfind(" ")].rstrip(",.;:") + "…"


class Jobs:
    def __init__(self, synth, encode):
        """synth(spoken_text, params) -> (float32 pcm bytes, info) awaitable; encode(pcm, fmt) -> bytes."""
        self.synth, self.encode = synth, encode
        self.meta: dict[str, dict] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.stop: set[str] = set()       # cancel requested
        self.drop: set[str] = set()       # delete requested while running
        self.version = 0
        self.changed = asyncio.Condition()
        self._saved: dict[str, float] = {}

    # ---- persistence ----------------------------------------------------------------------------------------
    def _dir(self, jid: str) -> str:
        return os.path.join(ROOT, jid)

    def _save(self, jid: str, force: bool = True) -> None:
        if not force and time.monotonic() - self._saved.get(jid, 0) < 3:
            return
        m = self.meta.get(jid)
        if m is None:
            return
        tmp = os.path.join(self._dir(jid), "meta.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(self._dir(jid), "meta.json"))
        self._saved[jid] = time.monotonic()

    async def _notify(self) -> None:
        async with self.changed:
            self.version += 1
            self.changed.notify_all()

    def start(self) -> None:
        os.makedirs(ROOT, exist_ok=True)
        pending = []
        for jid in sorted(os.listdir(ROOT)):
            try:
                with open(os.path.join(self._dir(jid), "meta.json"), encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            self.meta[jid] = m
            if m["status"] in ("queued", "running"):
                m["status"] = "queued"
                pending.append(jid)
        for jid in pending:  # resume after a restart, oldest first
            self.queue.put_nowait(jid)
        asyncio.create_task(self._runner())
        asyncio.create_task(self._sweeper())

    # ---- public view --------------------------------------------------------------------------------------------
    def public(self, m: dict, full: bool = False) -> dict:
        out = {k: m[k] for k in ("id", "title", "status", "created", "updated", "chars", "params", "duration_s",
                                 "elapsed_s", "error")}
        segs = m["segments"]
        done_chars = sum(len(s["text"]) for s in segs if s["done"])
        all_chars = sum(len(s["text"]) for s in segs) or 1
        frac = done_chars / all_chars
        eta = None  # remaining seconds, from the speed so far (after the first segments, when it is meaningful)
        if m["status"] == "running" and 0.02 <= frac < 1 and m.get("elapsed_s"):
            eta = round(m["elapsed_s"] * (1 - frac) / frac)
        out["progress"] = {"done": sum(s["done"] for s in segs), "total": len(segs),
                           "percent": 100 if m["status"] == "done" else round(100 * frac, 1), "eta_s": eta}
        has_audio = m.get("audio") is not None
        out["audio_url"] = f"/v1/jobs/{m['id']}/audio" if has_audio else None
        out["subtitles_url"] = f"/v1/jobs/{m['id']}/subtitles" if has_audio else None
        if full:
            try:
                with open(os.path.join(self._dir(m["id"]), "text.txt"), encoding="utf-8") as f:
                    out["text"] = f.read()
            except OSError:
                out["text"] = ""
            out["segments"] = [{"index": i, "text": s["text"], "spoken": s["spoken"], "start_s": s.get("start_s"),
                                "duration_s": s.get("duration_s"), "done": s["done"]} for i, s in enumerate(segs)]
        return out

    def list(self, limit: int, offset: int, q: str = "") -> dict:
        """Newest first; q matches the title, voice, engine or any sentence of the text (case-insensitive)."""
        q = q.strip().lower()

        def hit(m: dict) -> bool:
            return (q in m["title"].lower() or q in str(m["params"].get("voice") or "").lower()
                    or q in m["params"]["engine"] or any(q in s["text"].lower() for s in m["segments"]))

        ids = sorted((j for j, m in self.meta.items() if not q or hit(m)), reverse=True)
        return {"jobs": [self.public(self.meta[j]) for j in ids[offset:offset + limit]], "total": len(ids)}

    def counts(self) -> dict:
        st = [m["status"] for m in self.meta.values()]
        return {"queued": st.count("queued"), "running": st.count("running")}

    # ---- commands -------------------------------------------------------------------------------------------
    async def create(self, text: str, segments: list[dict], params: dict, title: str | None) -> dict:
        jid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        os.makedirs(self._dir(jid))
        with open(os.path.join(self._dir(jid), "text.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        now = _now()
        self.meta[jid] = {
            "id": jid, "title": (title or "").strip()[:200] or _title(segments[0]["text"] if segments else text),
            "status": "queued", "created": now, "updated": now, "chars": len(text), "params": params,
            "duration_s": 0.0, "elapsed_s": 0.0, "error": None, "audio": None, "ended": None,
            "segments": [{"text": s["text"], "spoken": s["spoken"], "pause_after_ms": s["pause_after_ms"],
                          "done": False} for s in segments],
        }
        self._save(jid)
        await self.queue.put(jid)
        await self._notify()
        return self.public(self.meta[jid])

    async def rename(self, jid: str, title: str) -> dict:
        m = self.meta[jid]
        m["title"], m["updated"] = title.strip()[:200] or m["title"], _now()
        self._save(jid)
        await self._notify()
        return self.public(m)

    async def cancel(self, jid: str) -> dict:
        m = self.meta[jid]
        if m["status"] == "queued":
            m["status"], m["updated"], m["ended"] = "canceled", _now(), time.time()
            self._save(jid)
        elif m["status"] == "running":
            self.stop.add(jid)
        await self._notify()
        return self.public(m)

    async def delete(self, jid: str) -> None:
        m = self.meta[jid]
        if m["status"] == "running":
            self.stop.add(jid)
            self.drop.add(jid)  # the runner removes it after the current segment
        else:
            self.meta.pop(jid)
            await asyncio.to_thread(shutil.rmtree, self._dir(jid), True)
        await self._notify()

    # ---- files --------------------------------------------------------------------------------------------------
    def seg_path(self, jid: str, i: int) -> str:
        return os.path.join(self._dir(jid), f"seg-{i:05d}.flac")

    def segment_wav(self, jid: str, i: int) -> bytes | None:
        import soundfile as sf

        path = self.seg_path(jid, i)
        if not os.path.isfile(path):
            return None
        data, sr = sf.read(path, dtype="float32")
        buf = io.BytesIO()
        sf.write(buf, data, sr, format="WAV", subtype="PCM_16")
        return buf.getvalue()

    def audio_path(self, jid: str) -> str | None:
        m = self.meta.get(jid)
        return os.path.join(self._dir(jid), m["audio"]) if m and m.get("audio") else None

    def subtitles(self, jid: str, fmt: str) -> str:
        def ts(t: float) -> str:
            ms = int(round(t * 1000))
            h, ms = divmod(ms, 3600_000)
            mi, ms = divmod(ms, 60_000)
            s, ms = divmod(ms, 1000)
            return f"{h:02d}:{mi:02d}:{s:02d}{',' if fmt == 'srt' else '.'}{ms:03d}"

        cues = [s for s in self.meta[jid]["segments"] if s["done"] and s.get("duration_s")]
        out = [] if fmt == "srt" else ["WEBVTT", ""]
        for n, s in enumerate(cues, 1):
            if fmt == "srt":
                out.append(str(n))
            out += [f"{ts(s['start_s'])} --> {ts(s['start_s'] + s['duration_s'])}", s["text"], ""]
        return "\n".join(out)

    # ---- worker ---------------------------------------------------------------------------------------------
    async def _runner(self) -> None:
        while True:
            jid = await self.queue.get()
            try:
                await self._process(jid)
            except Exception as exc:  # keep the runner alive; the job shows the error
                m = self.meta.get(jid)
                if m is not None:
                    m["status"], m["error"], m["updated"], m["ended"] = "error", f"{type(exc).__name__}: {exc}", _now(), time.time()
                    self._save(jid)
                await self._notify()

    async def _process(self, jid: str) -> None:
        import soundfile as sf
        import numpy as np

        m = self.meta.get(jid)
        if m is None or m["status"] != "queued":
            return
        m["status"], m["updated"] = "running", _now()
        self._save(jid)
        await self._notify()
        started = time.monotonic() - m.get("elapsed_s", 0.0)
        cursor, error = 0.0, None
        for i, seg in enumerate(m["segments"]):
            if seg["done"] and os.path.isfile(self.seg_path(jid, i)):  # resumed job
                cursor = seg["start_s"] + seg["duration_s"] + (seg["pause_after_ms"] / 1000 if seg["duration_s"] else 0)
                continue
            if jid in self.stop:
                break
            try:
                pcm, _ = await self.synth(seg["spoken"], m["params"])
            except ValueError:  # nothing speakable in this segment
                pcm = b""
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                break
            audio = np.frombuffer(pcm, dtype=np.float32)
            if len(audio):
                await asyncio.to_thread(sf.write, self.seg_path(jid, i), audio, SAMPLE_RATE, subtype="PCM_24", format="FLAC")
            seg.update(done=True, start_s=round(cursor, 3), duration_s=round(len(audio) / SAMPLE_RATE, 3))
            if len(audio):
                cursor += seg["duration_s"] + seg["pause_after_ms"] / 1000
            m["duration_s"], m["elapsed_s"], m["updated"] = round(cursor, 2), round(time.monotonic() - started, 1), _now()
            self._save(jid, force=False)
            await self._notify()
        if jid in self.drop:
            self.stop.discard(jid)
            self.drop.discard(jid)
            self.meta.pop(jid, None)
            await asyncio.to_thread(shutil.rmtree, self._dir(jid), True)
            await self._notify()
            return
        canceled = jid in self.stop
        self.stop.discard(jid)
        if any(s["done"] and s["duration_s"] for s in m["segments"]):
            m["audio"] = await asyncio.to_thread(self._assemble, jid)
        m["duration_s"] = round(sum(s["duration_s"] + (s["pause_after_ms"] / 1000 if s is not m["segments"][-1] else 0)
                                    for s in m["segments"] if s["done"] and s.get("duration_s")), 2)
        m["status"] = "error" if error else "canceled" if canceled else "done"
        m["error"], m["elapsed_s"], m["updated"], m["ended"] = error, round(time.monotonic() - started, 1), _now(), time.time()
        self._save(jid)
        await self._notify()

    def _assemble(self, jid: str) -> str:
        """Finished segments + their pauses -> one file in the job's format."""
        import numpy as np
        import soundfile as sf

        m = self.meta[jid]
        parts, last = [], None
        for i, s in enumerate(m["segments"]):
            if not (s["done"] and s.get("duration_s")) or not os.path.isfile(self.seg_path(jid, i)):
                continue
            if last is not None:
                parts.append(np.zeros(int(SAMPLE_RATE * last["pause_after_ms"] / 1000), dtype=np.float32))
            parts.append(sf.read(self.seg_path(jid, i), dtype="float32")[0])
            last = s
        fmt = m["params"].get("response_format", "mp3")
        name = f"audio.{EXT.get(fmt, fmt)}"
        data = self.encode(np.concatenate(parts).tobytes(), fmt)
        with open(os.path.join(self._dir(jid), name), "wb") as f:
            f.write(data)
        return name

    async def _sweeper(self) -> None:
        while True:
            try:
                snapshot = {j: dict(m) for j, m in self.meta.items()}
                for jid in await asyncio.to_thread(self._sweep, snapshot):
                    self.meta.pop(jid, None)
                    await asyncio.to_thread(shutil.rmtree, self._dir(jid), True)
            except Exception as exc:
                print(f"jobs sweep failed: {exc!r}", flush=True)
            await asyncio.sleep(600)

    def _sweep(self, meta: dict[str, dict]) -> list[str]:
        """Runs in a thread on a snapshot: removes stale segment files, returns the jobs to delete."""
        now = time.time()
        finished = sorted(j for j, m in meta.items() if m["status"] in FINAL)
        for jid in finished:  # segment files are only needed for live playback
            m = meta[jid]
            if m.get("ended") and now - m["ended"] > SEG_KEEP_S:
                for f in os.listdir(self._dir(jid)):
                    if f.startswith("seg-"):
                        os.remove(os.path.join(self._dir(jid), f))

        def size(j: str) -> int:
            d = self._dir(j)
            return sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d)) if os.path.isdir(d) else 0

        sizes = {j: size(j) for j in meta}
        total, drop = sum(sizes.values()), []
        for jid in finished:  # oldest first
            m = meta[jid]
            old = m.get("ended") and now - m["ended"] > KEEP_DAYS * 86400
            if old or total > MAX_BYTES:
                total -= sizes.get(jid, 0)
                drop.append(jid)
        return drop
