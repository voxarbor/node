"""Node benchmark: what each service costs on this machine, alone and together, as a site table.

Runs on the node host against its gateway (stdlib only; docker CLI for per-container stats, nvidia-smi if present):
  NODE_TOKEN=... python3 tools/bench_node.py --url http://127.0.0.1:8790 --audio talk.wav --out bench.json
--audio: speech in any language Whisper knows, 16 kHz mono WAV, ≥ 3 min (two speakers make diarize meaningful).

Phases (each sampled every second: node CPU % (all its containers, % of the whole machine), host CPU %, RAM, per-container CPU/RAM, GPU util/VRAM/temp/power):
  idle       60 s with nothing requested (models may or may not be loaded: reported)
  stt        transcribe the clip (first call = cold if the model was unloaded) and again warm -> RTF
  translate  the transcript's sentences -> uk, cold and warm -> sentences/s
  diarize    the clip -> RTF, speakers
  tts        warmup (model load time), stream: time to first audio, then whole file -> RTF
  first      "time to the first spoken phrase": first 30 s of the clip -> stt -> translate -> tts stream first byte
  together   stt + diarize + translate + tts at the same time -> each one's time and RTF under load
  impact     what the owner's own programs get: a CPU probe (all cores, 10 s) and, on a GPU node, a CUDA probe
             (fp16 matmul, 10 s, in the node's own tts image) first with the node idle, then while the node runs the
             "together" load -> "programs keep N % of their CPU / GPU speed"
Result: JSON with every sample and a summary table (Markdown) printed at the end.
"""
import argparse, io, json, os, re, subprocess, threading, time, urllib.error, urllib.request, uuid, wave
from concurrent.futures import ThreadPoolExecutor

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8790")
ap.add_argument("--audio", required=True)
ap.add_argument("--clip", type=float, default=180, help="seconds of --audio used for stt/diarize")
ap.add_argument("--idle", type=float, default=60)
ap.add_argument("--voice", default="male")
ap.add_argument("--phases", default="idle,stt,translate,diarize,tts,first,together,impact")
ap.add_argument("--out", default="bench.json")
a = ap.parse_args()
TOKEN = os.environ["NODE_TOKEN"]
URL = a.url.rstrip("/")


# ---------------------------------------------------------------- http
def req(method, path, body=None, ctype=None, timeout=3600, stream_first=False):
    """-> (status, bytes, headers, seconds, first_byte_seconds, bytes_sent)"""
    h = {"Authorization": f"Bearer {TOKEN}"}
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h["Content-Type"] = ctype or "application/json"
    t0 = time.monotonic()
    r = urllib.request.Request(URL + path, data=data, headers=h, method=method)
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers), time.monotonic() - t0, None, len(data or b"")
    first, chunks = None, []
    while True:
        c = resp.read1(65536) if stream_first else resp.read()
        if not c:
            break
        chunks.append(c)
        if first is None and (not stream_first or sum(map(len, chunks)) > 8192):  # past the WAV header: real audio
            first = time.monotonic() - t0
        if not stream_first:
            break
    return resp.status, b"".join(chunks), dict(resp.headers), time.monotonic() - t0, first, len(data or b"")


def multipart(fields, fname, fbytes):
    b = uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
              f"Content-Type: audio/wav\r\n\r\n".encode() + fbytes + f"\r\n--{b}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={b}"


def wav_slice(path, start, dur):
    w = wave.open(path)
    sr = w.getframerate()
    w.setpos(int(start * sr))
    frames = w.readframes(int(dur * sr))
    out = io.BytesIO()
    o = wave.open(out, "wb")
    o.setnchannels(w.getnchannels()); o.setsampwidth(w.getsampwidth()); o.setframerate(sr)
    o.writeframes(frames); o.close()
    return out.getvalue(), len(frames) / w.getsampwidth() / w.getnchannels() / sr


# ---------------------------------------------------------------- sampling
class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.samples, self.phase, self.stop_ = [], "start", False
        self.gpu = subprocess.run(["sh", "-c", "command -v nvidia-smi"], capture_output=True).returncode == 0

    @staticmethod
    def _cpu():
        v = list(map(int, open("/proc/stat").readline().split()[1:]))
        return sum(v), v[3] + v[4]

    def run(self):
        last = self._cpu()
        while not self.stop_:
            t = time.time()
            s = {"t": t, "phase": self.phase}
            try:
                ds = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
                                    capture_output=True, text=True, timeout=20).stdout
                s["containers"] = {}
                for line in ds.splitlines():
                    n, c, m = line.split("\t")
                    if "voice-core" not in n:
                        continue
                    mu = m.split("/")[0].strip()
                    num = float(re.match(r"[\d.]+", mu).group())
                    mult = {"KiB": 1 / 1024 ** 2, "MiB": 1 / 1024, "GiB": 1, "B": 1 / 1024 ** 3}[re.sub(r"[\d.]+", "", mu)]
                    s["containers"][n.replace("voice-core-", "").rsplit("-", 1)[0]] = {
                        "cpu": float(c.rstrip("%")), "mem_gb": round(num * mult, 3)}
            except Exception as e:  # noqa: BLE001
                s["docker_error"] = str(e)
            cur = self._cpu()
            dt, di = cur[0] - last[0], cur[1] - last[1]
            last = cur
            s["host_cpu"] = round(100 * (1 - di / dt), 1) if dt else 0
            mi = dict(l.split(":") for l in open("/proc/meminfo"))
            kb = lambda k: int(mi[k].split()[0])  # noqa: E731
            s["host_mem_used_gb"] = round((kb("MemTotal") - kb("MemAvailable")) / 1024 ** 2, 2)
            if self.gpu:
                g = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw",
                                    "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.split(",")
                try:
                    s["gpu"] = {"util": float(g[0]), "vram_gb": round(float(g[1]) / 1024, 2), "temp": float(g[2]),
                                "power_w": float(g[3])}
                except (ValueError, IndexError):
                    pass
            self.samples.append(s)
            time.sleep(max(0, 1 - (time.time() - t)))


class Probe(threading.Thread):
    """What a user's program feels: one fixed single-thread task (pure Python loop) every 3 s, its time in ms.
    Compared with the idle phase it shows how much slower other programs on the machine get. A 0.5 s untimed spin
    before each measurement takes the core out of its idle clock, otherwise power saving makes idle look slower."""
    def __init__(self):
        super().__init__(daemon=True)
        self.samples, self.stop_ = [], False

    def run(self):
        while not self.stop_:
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 0.5:  # untimed spin: let the CPU leave its idle clock first (laptops)
                pass
            t0 = time.perf_counter()
            x = 0
            for i in range(1_000_000):
                x += i * i
            self.samples.append((smp.phase, (time.perf_counter() - t0) * 1000))
            time.sleep(3)


def summary(samples, phase):
    ss = [s for s in samples if s["phase"] == phase]
    if not ss:
        return {}
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    out = {"seconds": len(ss), "host_cpu_avg": avg([s["host_cpu"] for s in ss]),
           "host_cpu_max": max(s["host_cpu"] for s in ss),
           "host_mem_max_gb": max(s["host_mem_used_gb"] for s in ss)}
    cont = {}
    for s in ss:
        for n, c in s.get("containers", {}).items():
            d = cont.setdefault(n, {"cpu": [], "mem": []})
            d["cpu"].append(c["cpu"]); d["mem"].append(c["mem_gb"])
    out["containers"] = {n: {"cpu_avg": avg(d["cpu"]), "cpu_max": max(d["cpu"]), "mem_max_gb": max(d["mem"])}
                         for n, d in sorted(cont.items())}
    node = [sum(c["cpu"] for c in s.get("containers", {}).values()) / (os.cpu_count() or 1) for s in ss]
    out["node_cpu_avg"], out["node_cpu_max"] = avg(node), round(max(node), 1)
    out["node_mem_max_gb"] = round(max(sum(c["mem_gb"] for c in s.get("containers", {}).values()) for s in ss), 2)
    g = [s["gpu"] for s in ss if "gpu" in s]
    if g:
        out["gpu"] = {"util_avg": avg([x["util"] for x in g]), "util_max": max(x["util"] for x in g),
                      "vram_max_gb": max(x["vram_gb"] for x in g), "temp_max": max(x["temp"] for x in g),
                      "power_avg_w": avg([x["power_w"] for x in g])}
    return out


# ---------------------------------------------------------------- work
caps = json.loads(req("GET", "/capabilities")[1])
SVC = caps.get("services", {})
STT_MODEL = SVC.get("stt", {}).get("model") or "deepdml/faster-whisper-large-v3-turbo-ct2"
res = {"product": caps.get("product"), "mode": (caps.get("node") or {}).get("mode", "normal"), "version": caps.get("product_version") or caps.get("version"),
       "date": time.strftime("%Y-%m-%d %H:%M %Z"), "host": {"cpus": os.cpu_count(),
       "cpu": next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), "?"),
       "mem_gb": round(int(open("/proc/meminfo").readline().split()[1]) / 1024 ** 2, 1)},
       "devices": {k: v.get("device") for k, v in SVC.items()}, "results": {}, "phases": {}}
if Sampler().gpu:
    res["host"]["gpu"] = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                                        capture_output=True, text=True).stdout.strip()
R = res["results"]
clip, clip_s = wav_slice(a.audio, 0, a.clip)
head, head_s = wav_slice(a.audio, 0, 30)
smp = Sampler(); smp.start()
probe = Probe(); probe.start()
time.sleep(1.5)


def phase(name):
    smp.phase = name
    print(f"== {name}", flush=True)


def loaded():
    out = {}
    for s in ("tts", "translate", "diarize"):
        try:
            out[s] = json.loads(req("GET", f"/{s}/health", timeout=30)[1]).get("loaded")
        except Exception:  # noqa: BLE001
            out[s] = None
    return out


def stt(audio, name="clip.wav"):
    body, ct = multipart({"model": STT_MODEL, "response_format": "verbose_json"}, name, audio)
    st, b, h, sec, _, sent = req("POST", "/stt/v1/audio/transcriptions", body, ct)
    j = json.loads(b) if st == 200 else {"error": b[:300].decode(errors="replace")}
    return j, sec, sent, len(b)


def sentences(j):
    segs = [s["text"].strip() for s in j.get("segments", []) if s.get("text", "").strip()]
    return segs or re.split(r"(?<=[.!?])\s+", j.get("text", ""))


def translate(segs):
    st, b, h, sec, _, sent = req("POST", "/translate/v1/translate", {"segments": segs, "target": "uk"})
    return (json.loads(b) if st == 200 else {"error": b[:300].decode(errors="replace")}), sec, sent, len(b)


def diarize(audio):
    body, ct = multipart({}, "clip.wav", audio)
    st, b, h, sec, _, sent = req("POST", "/diarize/v1/diarize", body, ct)
    return (json.loads(b) if st == 200 else {"error": b[:300].decode(errors="replace")}), sec, sent, len(b)


def tts_stream(text):
    st, b, h, sec, first, sent = req("POST", "/tts/v1/audio/speech/stream",
                                     {"text": text, "voice": a.voice, "response_format": "wav"}, stream_first=True)
    return {"status": st, "first_audio_s": round(first or 0, 2), "total_s": round(sec, 2), "bytes": len(b)}


def tts_file(text):
    st, b, h, sec, _, sent = req("POST", "/tts/v1/audio/speech", {"text": text, "voice": a.voice, "response_format": "wav"})
    dur = float(h.get("X-Audio-Duration") or h.get("x-audio-duration") or 0)
    return {"status": st, "seconds": round(sec, 2), "audio_s": round(dur, 1), "rtf": round(sec / dur, 3) if dur else None,
            "bytes": len(b)}


def _cpu_worker(sec, q):
    n, t0 = 0, time.perf_counter()
    while time.perf_counter() - t0 < sec:
        x = 0
        for i in range(20000):
            x += i * i
        n += 1
    q.put(n)


def cpu_probe(sec=10):
    """all cores busy with a fixed pure-Python loop: total loops per second (a stand-in for the owner's programs)"""
    import multiprocessing
    mp = multiprocessing.get_context("fork")  # the script body must not run again in the workers
    q = mp.Queue()
    ps = [mp.Process(target=_cpu_worker, args=(sec, q)) for _ in range(os.cpu_count() or 1)]
    for x in ps:
        x.start()
    total = sum(q.get() for _ in ps)
    for x in ps:
        x.join()
    return total / sec


GPU_PROBE = ("import torch,time\n"
             "a=torch.randn(4096,4096,device='cuda',dtype=torch.float16)\n"
             "for _ in range(10): a@a\n"
             "torch.cuda.synchronize(); n=0; t=time.time()\n"
             "while time.time()-t<10:\n"
             "    for _ in range(10): a@a\n"
             "    torch.cuda.synchronize(); n+=10\n"
             "print(n*2*4096**3/(time.time()-t)/1e12)")


def gpu_probe():
    """CUDA fp16 matmul TFLOPS for 10 s, in the node's tts image (torch present), None on a CPU node"""
    img = SVC.get("tts", {}).get("image")
    if not res["host"].get("gpu") or not img:
        return None
    r = subprocess.run(["docker", "run", "--rm", "--gpus", "all", "--entrypoint", "python", img, "-c", GPU_PROBE],
                       capture_output=True, text=True, timeout=300)
    try:
        return round(float(r.stdout.strip().splitlines()[-1]), 1)
    except (ValueError, IndexError):
        print("gpu probe failed:", r.stderr[-300:], flush=True)
        return None


phases = a.phases.split(",")
uk_text = "Це тестовий текст для перевірки швидкості синтезу. Вузол читає його звичайним голосом, без поспіху."
transcript = None
if "idle" in phases:
    for _ in range(240):  # let earlier work (another client, a previous run) finish first
        h = json.loads(req("GET", "/tts/health", timeout=30)[1])
        if not h.get("busy") and not sum((h.get("jobs") or {}).values()):
            break
        time.sleep(1)
    time.sleep(5)
    R["idle_loaded"] = loaded()
    phase("idle"); time.sleep(a.idle)
if "stt" in phases or "translate" in phases or "together" in phases:
    phase("stt")
    j, sec, sent, got = stt(clip)
    R["stt_cold"] = {"seconds": round(sec, 1), "audio_s": round(clip_s), "rtf": round(sec / clip_s, 3), "upload_mb": round(sent / 1e6, 1)}
    j, sec, sent, got = stt(clip)
    R["stt_warm"] = {"seconds": round(sec, 1), "rtf": round(sec / clip_s, 3), "language": j.get("language"),
                     "chars": len(j.get("text", "")), "error": j.get("error")}
    transcript = sentences(j)
if "translate" in phases and transcript:
    phase("translate")
    t, sec, sent, got = translate(transcript)
    R["translate_cold"] = {"seconds": round(sec, 2), "sentences": len(transcript), "error": t.get("error")}
    t, sec, sent, got = translate(transcript)
    R["translate_warm"] = {"seconds": round(sec, 2), "sentences_per_s": round(len(transcript) / sec, 1)}
    uk = [s for s in t.get("segments", []) if s]
    if uk:
        uk_text = " ".join(uk)
if "diarize" in phases:
    phase("diarize")
    d, sec, sent, got = diarize(clip)
    R["diarize"] = {"seconds": round(sec, 1), "rtf": round(sec / clip_s, 3),
                    "speakers": len({t["speaker"] for t in d.get("turns", [])}) if "turns" in d else None,
                    "engine": SVC.get("diarize", {}).get("engine"), "error": d.get("error")}
text = uk_text[:700].rsplit(" ", 1)[0]
if "tts" in phases:
    phase("tts")
    was = loaded().get("tts")
    t0 = time.monotonic()
    req("POST", "/tts/v1/warmup?engine=holos", b"")
    while not loaded().get("tts") and time.monotonic() - t0 < 300:
        time.sleep(0.5)
    R["tts_load"] = {"was_loaded": was, "seconds": round(time.monotonic() - t0, 1)}
    R["tts_stream"] = tts_stream(text)
    R["tts_file"] = {**tts_file(text), "chars": len(text)}
if "first" in phases:
    phase("first")
    t0 = time.monotonic()
    j, s1, _, _ = stt(head)
    tr, s2, _, _ = translate(sentences(j)[:3])
    first_uk = next((s for s in tr.get("segments", []) if s), "Привіт.")
    ts = tts_stream(first_uk)
    R["first_phrase"] = {"stt_30s": round(s1, 2), "translate": round(s2, 2), "tts_first_audio": ts["first_audio_s"],
                         "total_s": round(time.monotonic() - t0 - (ts["total_s"] - ts["first_audio_s"]), 2),
                         "note": "models warm; first 30 s of audio, 3 sentences"}
if "together" in phases:
    phase("together")
    segs = transcript or [uk_text]
    # gentle node: the connector takes one task at a time, so the coordinator's tasks arrive one after another
    serial = (caps.get("node") or {}).get("mode") == "gentle"
    t_together = time.monotonic()
    with ThreadPoolExecutor(1 if serial else 4) as ex:
        f_stt = ex.submit(stt, clip)
        f_dia = ex.submit(diarize, clip)
        f_tr = ex.submit(lambda: [translate(segs)[1] for _ in range(3)])
        f_tts = ex.submit(lambda: (tts_stream(text), tts_file(text)))
        _, s_stt, _, _ = f_stt.result()
        _, s_dia, _, _ = f_dia.result()
        trs = f_tr.result()
        tst, tfi = f_tts.result()
    R["together"] = {"stt_rtf": round(s_stt / clip_s, 3), "diarize_rtf": round(s_dia / clip_s, 3),
                     "translate_s": round(sum(trs) / len(trs), 2), "tts_first_audio": tst["first_audio_s"],
                     "tts_rtf": tfi["rtf"], "wall_s": round(time.monotonic() - t_together, 1),
                     "mode": "serial (gentle)" if serial else "parallel"}
if "impact" in phases:
    phase("impact_idle")
    for _ in range(240):  # the node must be quiet for the baseline
        h = json.loads(req("GET", "/tts/health", timeout=30)[1])
        if not h.get("busy") and not sum((h.get("jobs") or {}).values()):
            break
        time.sleep(1)
    base_cpu, base_gpu = cpu_probe(), gpu_probe()
    phase("impact_load")
    serial = (caps.get("node") or {}).get("mode") == "gentle"
    segs = transcript or [uk_text]
    stop_load = threading.Event()

    def load():  # the node keeps working for the whole probe: stt + diarize + translate + tts, again and again
        with ThreadPoolExecutor(1 if serial else 4) as ex:
            while not stop_load.is_set():
                fs = [ex.submit(stt, clip), ex.submit(diarize, clip), ex.submit(translate, segs),
                      ex.submit(tts_file, text)]
                for f in fs:
                    f.result()
    th = threading.Thread(target=load, daemon=True)
    th.start()
    time.sleep(20)  # models loaded, all services busy
    load_cpu, load_gpu = cpu_probe(), gpu_probe()
    stop_load.set()
    R["impact"] = {"cpu_keep_pct": round(100 * load_cpu / base_cpu), "cpu_idle_loops_s": round(base_cpu),
                   "cpu_load_loops_s": round(load_cpu)}
    if base_gpu and load_gpu:
        R["impact"].update(gpu_keep_pct=round(100 * load_gpu / base_gpu), gpu_idle_tflops=base_gpu,
                           gpu_load_tflops=load_gpu)
    th.join()
phase("end"); time.sleep(1.5)
smp.stop_ = True; smp.join(); probe.stop_ = True; probe.join()
for p in dict.fromkeys(s["phase"] for s in smp.samples):
    if p not in ("start", "end"):
        res["phases"][p] = summary(smp.samples, p)
        ms = sorted(v for ph, v in probe.samples if ph == p)
        if ms:
            res["phases"][p]["probe_ms_median"] = round(ms[len(ms) // 2], 1)
base = res["phases"].get("idle", {}).get("probe_ms_median")
res["samples"] = smp.samples
json.dump(res, open(a.out, "w"), indent=1, ensure_ascii=False)

# ---------------------------------------------------------------- table
print(f"\n### {res['product']} {res['version']} · {res['date']} · {res['host']['cpu']} ({res['host']['cpus']} CPU), "
      f"{res['host']['mem_gb']} GB RAM{' · ' + res['host'].get('gpu', '') if res['host'].get('gpu') else ''}")
print("| phase | result | node CPU avg/max % | host CPU avg/max % | node RAM max GB | GPU util avg/max % | VRAM max GB | GPU °C max |")
print("|---|---|---|---|---|---|---|---|")
show = {"idle": lambda: f"loaded {R.get('idle_loaded')}",
        "stt": lambda: f"cold RTF {R['stt_cold']['rtf']}, warm RTF {R['stt_warm']['rtf']}",
        "translate": lambda: f"{R['translate_warm']['sentences_per_s']} sent/s (cold {R['translate_cold']['seconds']} s)",
        "diarize": lambda: f"RTF {R['diarize']['rtf']}, {R['diarize']['speakers']} speakers",
        "tts": lambda: f"load {R['tts_load']['seconds']} s, first audio {R['tts_stream']['first_audio_s']} s, RTF {R['tts_file']['rtf']}",
        "first": lambda: f"first phrase {R['first_phrase']['total_s']} s",
        "together": lambda: ", ".join(f"{k} {v}" for k, v in R["together"].items()),
        "impact_idle": lambda: "baseline for impact_load",
        "impact_load": lambda: (f"owner's programs keep {R['impact']['cpu_keep_pct']} % CPU speed"
                                + (f", {R['impact']['gpu_keep_pct']} % GPU speed" if "gpu_keep_pct" in R["impact"] else ""))}
for p, s in res["phases"].items():
    g = s.get("gpu", {})
    try:
        r = show[p]()
    except Exception:  # noqa: BLE001
        r = "?"
    print(f"| {p} | {r} | {s['node_cpu_avg']}/{s['node_cpu_max']} | {s['host_cpu_avg']}/{s['host_cpu_max']} | {s['node_mem_max_gb']} | "
          f"{g.get('util_avg', '–')}/{g.get('util_max', '–')} | {g.get('vram_max_gb', '–')} | {g.get('temp_max', '–')} |")
print(f"\nJSON: {a.out}")
