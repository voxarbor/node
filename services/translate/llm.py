"""EN->UK with EuroLLM-9B-Instruct-2512 (Apache-2.0, GGUF Q4_K_M) through a llama-server subprocess on the GPU.

Only in the node-translate-cuda image with TRANSLATE_ENGINE=llm (compose.gpu.yaml + TRANSLATE_GPU=1). Stdlib only:
the API process stays light. The model file is downloaded once into the translate volume (sha256 pinned); it sits in
VRAM only while translating: llama-server starts on demand when enough VRAM is free and stops after LLM_IDLE_S idle.
When the GPU is too small or busy (STT/TTS of a live viewer have priority), translate() returns None and the caller
uses Opus-MT on the CPU. Sentences whose output looks wrong (placeholders N0/N1 lost or duplicated, empty, runaway
length) are returned as None one by one, so the caller re-translates just those with Opus-MT.
"""
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("translate.llm")

ENABLED = os.environ.get("TRANSLATE_ENGINE", "") == "llm"
SERVER = os.environ.get("LLM_SERVER", "/app/llama-server")
REPO = "mradermacher/EuroLLM-9B-Instruct-2512-GGUF"
REVISION = "5138603fccb9dc1f35c25cfd1a4981a78617e9ff"
FILE = "EuroLLM-9B-Instruct-2512.Q4_K_M.gguf"
SHA256 = "fe9b50d4ba67eb0131f1ceeaab2e56ea8fb6b2bbc37391d3219c791c3b140a28"
SIZE = 5582838912
MODEL_NAME = "eurollm-9b-instruct-2512-q4_k_m"
DIR = os.environ.get("LLM_DIR", "/cache/llm")
PATH = os.path.join(DIR, FILE)
PORT = int(os.environ.get("LLM_PORT", "8081"))
IDLE_S = float(os.environ.get("LLM_IDLE_S", "60"))
NEED_GB = float(os.environ.get("LLM_VRAM_GB", "5.6"))        # weights + KV cache for 4 slots x 1024 tokens
MIN_TOTAL_GB = float(os.environ.get("LLM_MIN_VRAM_TOTAL_GB", "11.5"))  # "12 GB" cards report ~11.9
WAIT_S = float(os.environ.get("LLM_WAIT_S", "30"))           # wait this long for free VRAM, then Opus-MT
SLOTS = int(os.environ.get("LLM_SLOTS", "4"))
PROMPT = ("<|im_start|>user\nTranslate the following English source text to Ukrainian:\nEnglish: {t} \n"
          "Ukrainian: <|im_end|>\n<|im_start|>assistant\n")
STOP = ["<|im_end|>", "</s>", "\n\n"]
PH = re.compile(r"\bN\d+\b")

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_last_used = 0.0
_busy = 0
_state = {"downloaded": os.path.isfile(PATH), "download_error": None, "gpu_total_gb": None, "last_fallback": None}


def gpu_mem() -> tuple[float, float] | None:
    """(total, free) GB of the first GPU, None without nvidia-smi / GPU"""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
        t, f = (float(x) / 1024 for x in out.split(","))
        return t, f
    except Exception:  # noqa: BLE001
        return None


def usable() -> bool:
    """engine llm on a GPU of >= 12 GB with the model downloaded"""
    if not ENABLED or not _state["downloaded"]:
        return False
    if _state["gpu_total_gb"] is None:
        g = gpu_mem()
        _state["gpu_total_gb"] = round(g[0], 1) if g else 0
    return _state["gpu_total_gb"] >= MIN_TOTAL_GB


def download() -> None:
    """once, in a background thread at startup: the GGUF into the volume, sha256 checked"""
    if not ENABLED or os.path.isfile(PATH):
        return
    os.makedirs(DIR, exist_ok=True)
    url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{FILE}"
    part = PATH + ".part"
    for attempt in range(1, 6):
        try:
            h, have = hashlib.sha256(), 0
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    have += len(chunk)
            if have != SIZE or h.hexdigest() != SHA256:
                raise ValueError(f"size {have} / sha256 mismatch")
            os.replace(part, PATH)
            _state.update(downloaded=True, download_error=None)
            log.info("EuroLLM model downloaded (%.1f GB)", SIZE / 1e9)
            return
        except Exception as exc:  # noqa: BLE001
            _state["download_error"] = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("model download attempt %d failed: %s", attempt, exc)
            time.sleep(30 * attempt)


def _alive() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _start() -> bool:
    """llama-server with the model in VRAM, only if enough VRAM is free (waits up to WAIT_S)"""
    global _proc
    if _proc and _proc.poll() is None:
        return True
    deadline = time.monotonic() + WAIT_S
    while True:
        g = gpu_mem()
        if g and g[1] >= NEED_GB:
            break
        if time.monotonic() > deadline:
            _state["last_fallback"] = f"VRAM free {g[1]:.1f} GB < {NEED_GB} GB" if g else "no GPU"
            return False
        time.sleep(2)
    _proc = subprocess.Popen([SERVER, "-m", PATH, "-ngl", "99", "-c", str(1024 * SLOTS), "-np", str(SLOTS),
                              "--host", "127.0.0.1", "--port", str(PORT), "--no-webui"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 180:
        if _proc.poll() is not None:
            _state["last_fallback"] = f"llama-server exited {_proc.returncode}"
            _proc = None
            return False
        if _alive():
            log.info("EuroLLM loaded in %.1f s", time.monotonic() - t0)
            return True
        time.sleep(0.5)
    stop()
    _state["last_fallback"] = "llama-server did not start in 180 s"
    return False


def stop() -> None:
    global _proc
    p, _proc = _proc, None
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill()


def reaper() -> None:
    """background thread: the model leaves VRAM after IDLE_S without work"""
    while True:
        time.sleep(5)
        with _lock:
            if _proc and not _busy and time.monotonic() - _last_used > IDLE_S:
                log.info("EuroLLM unloaded after %.0f s idle", IDLE_S)
                stop()


def _one(text: str) -> str | None:
    body = json.dumps({"prompt": PROMPT.format(t=text), "n_predict": min(512, 32 + 4 * len(text)),
                       "temperature": 0, "stop": STOP, "cache_prompt": True}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/completion", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())["content"].strip()
    ok = out and sorted(PH.findall(out)) == sorted(PH.findall(text)) and len(out) <= 3 * len(text) + 40
    return out if ok else None


def translate(texts: list[str]) -> list[str | None] | None:
    """None: the LLM is not available now (use Opus-MT for all); otherwise one result per text, None where the
    output failed the checks (use Opus-MT for those)"""
    global _busy, _last_used
    if not usable():
        return None
    with _lock:
        _busy += 1
    try:
        with _lock:
            if not _start():
                return None
        with ThreadPoolExecutor(SLOTS) as ex:
            out = list(ex.map(lambda t: _safe(_one, t), texts))
        return out
    finally:
        with _lock:
            _busy -= 1
            _last_used = time.monotonic()


def _safe(fn, t):
    try:
        return fn(t)
    except Exception:  # noqa: BLE001
        return None


def health() -> dict:
    return {"engine": "llm" if usable() else "opus", "llm_model": MODEL_NAME if ENABLED else None,
            "llm_loaded": bool(_proc and _proc.poll() is None), "llm_downloaded": _state["downloaded"],
            "llm_download_error": _state["download_error"], "llm_gpu_total_gb": _state["gpu_total_gb"],
            "llm_last_fallback": _state["last_fallback"]}
