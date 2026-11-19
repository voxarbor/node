"""Node capabilities for a coordinator or any client: GET /capabilities (Bearer = the node token) gathers what this
node runs and can do from its services, plus host resources and current load. Stdlib only (tiny image).
Schema is versioned (`schema`); fields are only ever added. GET /health is open."""

from __future__ import annotations

import glob
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
TAG = os.environ.get("VOXARBOR_NODE_VERSION", "")  # the tag the node pins (VOXARBOR_NODE_TAG)
RELEASE = os.environ.get("VOXARBOR_NODE_RELEASE", "dev")  # baked into the image by CI
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


_HW: dict = {}
VENDORS = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}


def hardware() -> dict:
    """general facts about the machine for benchmarks; no identifiers (serials, MAC, UUIDs, host or user names, paths,
    processes): see docs/security.md «Які дані про ваш комп'ютер бачить координатор»"""
    if _HW:
        return _HW
    cpu = None
    try:
        for line in open("/proc/cpuinfo"):
            if line.lower().startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    osname = None
    try:  # the host's os-release, mounted read-only by compose.yaml (the container's own would say Debian)
        rel = dict(l.rstrip("\n").split("=", 1) for l in open("/host/os-release") if "=" in l)
        osname = rel.get("PRETTY_NAME", "").strip('"') or None
    except (OSError, ValueError):
        pass
    try:
        kernel = open("/proc/sys/kernel/osrelease").read().strip()
    except OSError:
        kernel = ""
    if "microsoft" in kernel.lower():
        osname = f"{osname or 'Linux'} · WSL2"
    drm = []  # GPUs Linux sees through DRM (native Linux; WSL has none): vendor + PCI device id only
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        try:
            v = open(f"{card}/device/vendor").read().strip()
            d = open(f"{card}/device/device").read().strip()
        except OSError:
            continue
        name = None
        try:
            name = open(f"{card}/device/product_name").read().strip() or None  # amdgpu exposes it on some boards
        except OSError:
            pass
        drm.append((VENDORS.get(v, "other"), name or f"{VENDORS.get(v, 'unknown').upper()} GPU (PCI {v}:{d})"))
    _HW.update(cpu_model=cpu, cpu_threads=os.cpu_count(), os=osname, kernel=kernel or None, drm_gpus=drm)
    return _HW


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
    hw = hardware()
    out.update(cpu_model=hw["cpu_model"], cpu_threads=hw["cpu_threads"], os=hw["os"])
    if hw["drm_gpus"]:  # native Linux: the first discrete-looking GPU (NVIDIA/AMD before Intel)
        vendor, name = sorted(hw["drm_gpus"], key=lambda g: ["nvidia", "amd", "intel", "other"].index(g[0]))[0]
        out.update(gpu_vendor=vendor, gpu_name=name)
    elif os.path.exists("/dev/dxg"):
        out["gpu_vendor"] = "unknown"  # WSL: a GPU is passed through, Linux cannot tell which (nvidia-smi below can)
    else:
        out["gpu_vendor"] = "none"
    if shutil.which("nvidia-smi"):  # node-info sees the GPU only with compose.gpu.yaml
        try:
            line = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                                   "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                  timeout=5).stdout.strip().splitlines()[0]
            name, total, used, util = [x.strip() for x in line.split(",")]
            out.update(gpu=True, gpu_vendor="nvidia", gpu_name=name, vram_gb=round(float(total) / 1024, 1),
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


def _stt_revision() -> str | None:
    """the Whisper model's HF commit, read from the shared hf-cache volume (mounted read-only)"""
    ref = os.path.join(os.environ.get("HF_CACHE_DIR", "/hf"), "models--" + STT_MODEL.replace("/", "--"), "refs", "main")
    try:
        return open(ref).read().strip()[:12] or None
    except OSError:
        return None


def updater() -> dict:
    """node-updater's status (volume updater-ctl, read-only here): {"updater": {...}} or nothing without an updater"""
    try:
        with open(os.path.join(os.environ.get("UPDATER_CTL", "/ctl"), "status.json")) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {}
    keys = ("version", "mode", "state", "desired", "available", "error", "seq", "last_update", "warnings")
    return {"updater": {k: st.get(k) for k in keys}}


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
                mi = h.get("model_info") or {}
                s.update(engine="holos", model="HolosTTS (patriotyk/HolosTTS), PyTorch FP32"
                         + (f", holos-tts {mi['holos_tts']}" if mi.get("holos_tts") else ""),
                         model_revision=(mi.get("holos_revision") or "")[:12] or None,
                         stress_model=(f"accentuk {mi.get('accentuk')} ({mi.get('accentuk_mode')})"
                                       if mi.get("stress") == "accentuk" else mi.get("stress")),
                         verbalizer=mi.get("verbalizer"))
                s["features"] = h.get("features", [])
                s["perf"] = h.get("perf")   # audio seconds synthesized per second of work, last hour
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
                s["perf"] = h.get("perf")   # sentences translated per second of work, last hour
                s.update(engine="opus", model="Helsinki-NLP Opus-MT tc-big (en-zle and other pairs -> uk), CTranslate2 int8",
                         model_revision=f"opus-models-{h.get('opus_models_version')}" if h.get("opus_models_version") else None,
                         punctuation_revision=(h.get("punctuation_revision") or "")[:12] or None)
                s.update(opus_pairs=h.get("opus_pairs"), opus_models_version=h.get("opus_models_version"), fallback_model=h.get("fallback_model"),
                         punctuation_model="1-800-BAD-CODE/xlm-roberta_punctuation_fullstop_truecase",
                         **st.get("translate", {}))
                s["endpoints"] = ["/v1/translate", "/v1/punctuate", "/v1/languages"]
            elif name == "stt":
                s.update(model=STT_MODEL, endpoints=["/v1/audio/transcriptions"], engine="faster-whisper (speaches)",
                         model_revision=_stt_revision())
            elif name == "fetch":
                s["features"] = h.get("features", [])   # "list": POST /fetch/v1/list (0.13.5+)
                s["resting_s"] = h.get("resting_s")       # > 0: cooling down after a YouTube bot check
                s.update(engine="yt-dlp", model="yt-dlp + deno (YouTube JS challenges)",
                         model_revision=h.get("ytdlp_version"))
                s.update(ytdlp_version=h.get("ytdlp_version"), deno_version=h.get("deno_version"),
                         last_bot_check=h.get("last_bot_check"), last_ok=h.get("last_ok"),
                         endpoints=["/v1/info", "/v1/subs", "/v1/audio", "/v1/list"])
            elif name == "diarize":
                s.update(engine=h.get("engine", "sherpa-onnx"), gender_model=h.get("gender_model", "tts-voices-0"),
                         model=h.get("model") or "pyannote segmentation-3.0 + wespeaker_en_voxceleb_resnet34_LM (sherpa-onnx)",
                         model_revision=h.get("model_revision"),
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
    try:  # STT speed is measured by the connector on the coordinator's real requests
        services.get("stt", {})["perf"] = get(os.environ.get("CONNECTOR_URL", "http://connector:8000") + "/health",
                                              timeout=3).get("stt_perf")
    except Exception:
        pass
    node["mode"] = os.environ.get("NODE_MODE", "normal")   # normal | gentle
    node["gpu_used"] = any(s.get("device") == "cuda" for s in services.values())
    return {"schema": 1, "product": "voxarbor-node", "product_version": VERSION,
            "voxarbor_node_version": VERSION, "voxarbor_node_tag": TAG or None, "voxarbor_node_release": RELEASE,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "node": node, "services": services, **updater(),
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
