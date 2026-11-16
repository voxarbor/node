"""Node capabilities for a coordinator or any client: GET /capabilities (Bearer = the node token) gathers what this
node runs and can do from its services, plus host resources and current load. Stdlib only (tiny image).
Schema is versioned (`schema`); fields are only ever added. GET /health is open."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["API_KEY"]
TAG = os.environ.get("VOICE_CORE_VERSION", "")  # the tag the node pins (VOICE_CORE_TAG): v0.7.4, latest, ...
RELEASE = os.environ.get("VOICE_CORE_RELEASE", "dev")  # baked into the image by CI
VERSION = TAG if TAG.startswith("v") and TAG[1:2].isdigit() else RELEASE
SERVICES = {  # name -> base URL inside the node's compose network
    "stt": os.environ.get("STT_URL", "http://stt:8000"),
    "tts": os.environ.get("TTS_URL", "http://tts:8000"),
    "translate": os.environ.get("TRANSLATE_URL", "http://translate:8000"),
    "diarize": os.environ.get("DIARIZE_URL", "http://diarize:8000"),
    "fetch": os.environ.get("FETCH_URL", "http://fetch:8000"),  # optional (compose profile fetch)
}
IMAGES = {k: os.environ.get(f"{k.upper()}_IMAGE", "") for k in SERVICES}
DEVICES = {k: os.environ.get(f"{k.upper()}_DEVICE", "") for k in SERVICES}  # set by compose.gpu.yaml for stt
STT_MODEL = os.environ.get("STT_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
STATIC_TTL = float(os.environ.get("STATIC_TTL", "60"))
_cache: dict = {"at": 0.0, "static": None}
_last: dict[str, tuple[float, dict]] = {}  # service -> (time, its last good entry)
STALE_S = float(os.environ.get("STALE_S", "120"))  # a busy service answering /health slowly keeps its last state


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
    out = {"cpu_cores": os.cpu_count(), "ram_total_gb": round(mem.get("MemTotal", 0) / 1024 ** 3, 1),
           "ram_available_gb": round(mem.get("MemAvailable", 0) / 1024 ** 3, 1), "load_avg": load,
           "gpu": gpu, "gpu_used": False, "gpu_name": None, "vram_gb": None}
    if shutil.which("nvidia-smi"):  # node-info sees the GPU only with compose.gpu.yaml
        try:
            line = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                                   "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                  timeout=5).stdout.strip().splitlines()[0]
            name, total, used, util = [x.strip() for x in line.split(",")]
            out.update(gpu=True, gpu_name=name, vram_gb=round(float(total) / 1024, 1),
                       vram_used_gb=round(float(used) / 1024, 1), gpu_util_pct=int(float(util)))
        except Exception:
            pass
    return out


def static_info() -> dict:
    """Voices and language lists: cached briefly (a service restarted with other engines/voices shows up within
    STATIC_TTL seconds; was 10 min, which kept engines that had just been switched off)."""
    if _cache["static"] and time.time() - _cache["at"] < STATIC_TTL:
        return _cache["static"]
    info: dict = {}
    try:
        v = get(f"{SERVICES['tts']}/v1/voices")
        info["tts"] = {"default_engine": v["default_engine"], "engines": {
            e: {"default": d["default"], "favorites": d["favorites"], "voices": d["voices"],
                "genders": {n: m["gender"] for n, m in d.get("meta", {}).items()},
                "labels": {n: m["label"] for n, m in d.get("meta", {}).items()},
                "markup": d.get("markup", [])} for e, d in v["engines"].items()}}
    except Exception as exc:
        info["tts"] = {"error": type(exc).__name__}
    try:
        info["translate"] = {"languages": sorted(get(f"{SERVICES['translate']}/v1/languages")["languages"])}
    except Exception as exc:
        info["translate"] = {"error": type(exc).__name__}
    if not any("error" in v for v in info.values()):  # retry next time while a service is still starting
        _cache.update(at=time.time(), static=info)
    return info


def capabilities() -> dict:
    services, st = {}, static_info()
    for name, url in SERVICES.items():
        s = {"image": IMAGES[name] or None, "device": "cpu"}
        try:
            h = get(f"{url}/health", timeout=3)  # speaches wants the token even on /health
            s.update(available=True, healthy=h.get("status", "ok") == "ok", loaded=h.get("loaded"),
                     busy=h.get("busy"), queue_max=h.get("queue_max"))
            s["device"] = DEVICES.get(name) or h.get("device") or "cpu"  # stt: speaches says nothing, so env
            if name == "tts":
                s["features"] = h.get("features", [])
                s.update(jobs=h.get("jobs"), **{k: v for k, v in st.get("tts", {}).items()})
                try:  # lets a coordinator check cheaply that this node's stress lexicon matches its source
                    lex = get(f"{url}/v1/lexicon", timeout=5)["text"]
                    s["lexicon_sha256"] = hashlib.sha256(lex.encode("utf-8")).hexdigest()
                    s["lexicon_rules"] = sum(1 for l in lex.splitlines() if l.strip() and not l.strip().startswith("#"))
                except Exception:
                    s["lexicon_sha256"] = None
                try:  # custom (zero-shot) voices: the coordinator syncs them by sha256, like the lexicon
                    cv = get(f"{url}/v1/voices/custom", timeout=5)
                    s["voice_create"] = {"zero_shot": cv.get("zero_shot", True), "finetune": cv.get("finetune", False)}
                    s["custom_voices"] = {k: v.get("sha256") for k, v in cv.get("voices", {}).items()}
                except Exception:  # an older TTS image
                    s["voice_create"] = {"zero_shot": False, "finetune": False}
                    s["custom_voices"] = {}
                s["endpoints"] = ["/v1/audio/speech", "/v1/audio/speech/stream", "/v1/jobs", "/v1/voices",
                                  "/v1/text/prepare", "/v1/text/phonemize", "/v1/warmup", "/v1/voices/encode",
                                  "/v1/voices/custom"]
            elif name == "translate":
                s.update(opus_pairs=h.get("opus_pairs"), fallback_model=h.get("fallback_model"),
                         punctuation_model="1-800-BAD-CODE/xlm-roberta_punctuation_fullstop_truecase",
                         **st.get("translate", {}))
                s["endpoints"] = ["/v1/translate", "/v1/punctuate", "/v1/languages"]
            elif name == "stt":
                s.update(model=STT_MODEL, endpoints=["/v1/audio/transcriptions"])
            elif name == "fetch":
                s.update(ytdlp_version=h.get("ytdlp_version"), deno_version=h.get("deno_version"),
                         last_bot_check=h.get("last_bot_check"), last_ok=h.get("last_ok"),
                         endpoints=["/v1/info", "/v1/subs", "/v1/audio"])
            elif name == "diarize":
                s.update(engine=h.get("engine", "sherpa-onnx"),
                         model=h.get("model") or "pyannote segmentation-3.0 + wespeaker_en_voxceleb_resnet34_LM (sherpa-onnx)",
                         sessions=h.get("sessions"), endpoints=["/v1/diarize"])
            _last[name] = (time.time(), dict(s))
        except Exception as exc:
            seen = _last.get(name)
            slow = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
            if seen and slow and time.time() - seen[0] < STALE_S:
                # /health was slow (a busy worker), not gone: keep the last known state, marked as stale
                s = dict(seen[1], stale=True, stale_s=round(time.time() - seen[0]), error=type(exc).__name__)
            else:
                s.update(available=False, healthy=False, error=type(exc).__name__)
        services[name] = s
    node = host()
    node["gpu_used"] = any(s.get("device") == "cuda" for s in services.values())
    return {"schema": 1, "product": "voxarbor-node", "product_version": VERSION,
            "voice_core_version": VERSION, "local_ai_version": VERSION,
            "voice_core_tag": TAG or None, "voice_core_release": RELEASE, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "node": node, "services": services,
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
