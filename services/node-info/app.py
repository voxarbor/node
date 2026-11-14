"""Node capabilities for a coordinator or any client: GET /capabilities (Bearer = the node token) gathers what this
node runs and can do from its services, plus host resources and current load. Stdlib only (tiny image).
Schema is versioned (`schema`); fields are only ever added. GET /health is open."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["API_KEY"]
VERSION = os.environ.get("VOICE_CORE_VERSION", "dev")
SERVICES = {  # name -> base URL inside the node's compose network
    "stt": os.environ.get("STT_URL", "http://stt:8000"),
    "tts": os.environ.get("TTS_URL", "http://tts:8000"),
    "translate": os.environ.get("TRANSLATE_URL", "http://translate:8000"),
    "diarize": os.environ.get("DIARIZE_URL", "http://diarize:8000"),
}
IMAGES = {k: os.environ.get(f"{k.upper()}_IMAGE", "") for k in SERVICES}
STT_MODEL = os.environ.get("STT_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
_cache: dict = {"at": 0.0, "static": None}


def get(url: str, auth: bool = True, timeout: float = 5):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"} if auth else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    try:
        return json.loads(body)
    except ValueError:  # speaches answers /health with plain "OK"
        return {"status": "ok" if body.strip().upper() == b"OK" else body.decode(errors="replace")[:100]}


def host() -> dict:
    mem = {}
    try:
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    try:
        load = [float(x) for x in open("/proc/loadavg").read().split()[:3]]
    except OSError:
        load = None
    gpu = any(os.path.exists(p) for p in ("/dev/nvidia0", "/dev/nvidiactl"))
    return {"cpu_cores": os.cpu_count(), "ram_total_gb": round(mem.get("MemTotal", 0) / 1024 ** 3, 1),
            "ram_available_gb": round(mem.get("MemAvailable", 0) / 1024 ** 3, 1), "load_avg": load,
            "gpu": gpu, "gpu_used": False}  # services here are CPU-only builds


def static_info() -> dict:
    """Things that change only with a redeploy (voices, language lists): cached for 10 minutes."""
    if _cache["static"] and time.time() - _cache["at"] < 600:
        return _cache["static"]
    info: dict = {}
    try:
        v = get(f"{SERVICES['tts']}/v1/voices")
        info["tts"] = {"default_engine": v["default_engine"], "engines": {
            e: {"default": d["default"], "favorites": d["favorites"], "voices": d["voices"],
                "genders": {n: m["gender"] for n, m in d.get("meta", {}).items()},
                "labels": {n: m["label"] for n, m in d.get("meta", {}).items()}} for e, d in v["engines"].items()}}
    except Exception as exc:
        info["tts"] = {"error": type(exc).__name__}
    try:
        info["translate"] = {"languages": sorted(get(f"{SERVICES['translate']}/v1/languages")["languages"])}
    except Exception as exc:
        info["translate"] = {"error": type(exc).__name__}
    _cache.update(at=time.time(), static=info)
    return info


def capabilities() -> dict:
    services, st = {}, static_info()
    for name, url in SERVICES.items():
        s = {"image": IMAGES[name] or None}
        try:
            h = get(f"{url}/health", timeout=3)  # speaches wants the token even on /health
            s.update(available=True, healthy=h.get("status", "ok") == "ok", loaded=h.get("loaded"),
                     busy=h.get("busy"), queue_max=h.get("queue_max"))
            if name == "tts":
                s.update(jobs=h.get("jobs"), **{k: v for k, v in st.get("tts", {}).items()})
                try:  # lets a coordinator check cheaply that this node's stress lexicon matches its source
                    lex = get(f"{url}/v1/lexicon", timeout=5)["text"]
                    s["lexicon_sha256"] = hashlib.sha256(lex.encode("utf-8")).hexdigest()
                    s["lexicon_rules"] = sum(1 for l in lex.splitlines() if l.strip() and not l.strip().startswith("#"))
                except Exception:
                    s["lexicon_sha256"] = None
                s["endpoints"] = ["/v1/audio/speech", "/v1/audio/speech/stream", "/v1/jobs", "/v1/voices",
                                  "/v1/text/prepare", "/v1/text/phonemize", "/v1/warmup"]
            elif name == "translate":
                s.update(opus_pairs=h.get("opus_pairs"), fallback_model=h.get("fallback_model"),
                         punctuation_model="1-800-BAD-CODE/xlm-roberta_punctuation_fullstop_truecase",
                         **st.get("translate", {}))
                s["endpoints"] = ["/v1/translate", "/v1/punctuate", "/v1/languages"]
            elif name == "stt":
                s.update(model=STT_MODEL, endpoints=["/v1/audio/transcriptions"])
            elif name == "diarize":
                s.update(model="pyannote segmentation-3.0 + wespeaker_en_voxceleb_resnet34_LM (sherpa-onnx)",
                         sessions=h.get("sessions"), endpoints=["/v1/diarize"])
        except Exception as exc:
            s.update(available=False, healthy=False, error=type(exc).__name__)
        services[name] = s
    return {"schema": 1, "voice_core_version": VERSION, "local_ai_version": VERSION, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "node": host(), "services": services,
            "gateway_paths": {k: f"/{k}/" for k in SERVICES}}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        if self.path.split("?")[0] != "/capabilities":
            return self._send(404, {"detail": "not found"})
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(token, TOKEN):
            return self._send(401, {"detail": "invalid token"})
        self._send(200, capabilities())

    def log_message(self, *args) -> None:  # quiet
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
