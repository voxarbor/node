"""Gender classifier for diarize, trained on REAL voices (replaces the TTS-only gender_lr.npz).

Data (CC0 / CC-BY-4.0, fine for a public image): Common Voice 19 (client_id + gender in the TSVs; uk, en, ru, pl)
and LibriSpeech dev/test-clean (SPEAKERS.TXT). Unit = speaker windows: 15-30 s of one speaker glued from several
clips, embedded with the node's own WeSpeaker model (worker._embedding), like diarize does per speaker. Logistic
regression (L2) on the 256-d embedding, split by speaker (train / calibration / test), Platt scaling on the
calibration part, so p_female is a calibrated probability. Writes gender_lr.npz (w, b) + a JSON report.

Runs inside the node-diarize image (sherpa-onnx, numpy, soundfile, ffmpeg):
  docker run --rm -v <data>:/d -v $PWD/services/diarize:/app:ro -v $PWD/tools:/tools:ro --entrypoint python \
    <node-diarize image> /tools/gender_train_real.py /d /d/out
"""
import csv, glob, io, json, os, random, subprocess, sys, tarfile

import numpy as np
import soundfile as sf

sys.path.insert(0, "/app")
import worker  # noqa: E402

SR = 16000
random.seed(7)
D, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)
MAX_SPK = int(os.environ.get("MAX_SPK_PER_SET", "400"))   # speakers per (lang, gender, split file)
WIN_S, MAX_WIN = 20.0, 2                                    # seconds per window, windows per speaker


def decode(data: bytes) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         input=data, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def windows(clips):
    """clips: list of float32 arrays of one speaker -> up to MAX_WIN windows of ~WIN_S s"""
    out, cur = [], []
    for c in clips:
        cur.append(c)
        if sum(len(x) for x in cur) >= WIN_S * SR:
            out.append(np.concatenate(cur)[: int(30 * SR)])
            cur = []
            if len(out) >= MAX_WIN:
                break
    if not out and cur and sum(len(x) for x in cur) >= 8 * SR:
        out.append(np.concatenate(cur))
    return out


rows = []  # (source, speaker, gender 1=female, window embedding)


def add(source, spk, g, clips):
    for w in windows(clips):
        rows.append((source, spk, g, worker._embedding(w)))


# ---- Common Voice
for tsv in sorted(glob.glob(f"{D}/cv/*/*.tsv")):
    lang, split = tsv.split("/")[-2], os.path.basename(tsv)[:-4]
    tarp = tsv[:-4] + ".tar"
    if not os.path.exists(tarp):
        continue
    by = {}
    with open(tsv, encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            g = (r.get("gender") or "").lower()
            if g.startswith("female"):
                gi = 1
            elif g.startswith("male"):
                gi = 0
            else:
                continue
            by.setdefault((r["client_id"], gi), []).append(r["path"])
    pick = {}
    for gi in (0, 1):
        spk = [k for k in by if k[1] == gi]
        random.shuffle(spk)
        for k in spk[:MAX_SPK]:
            pick[k] = set(by[k][:8])
    want = {p: k for k, ps in pick.items() for p in ps}
    clips = {}
    with tarfile.open(tarp) as t:
        for m in t:
            name = os.path.basename(m.name)
            if name in want:
                try:
                    clips.setdefault(want[name], []).append(decode(t.extractfile(m).read()))
                except subprocess.CalledProcessError:
                    pass
    for (spk, gi), cs in clips.items():
        add(f"cv-{lang}", f"cv-{lang}-{spk[:16]}", gi, cs)
    print(f"cv {lang}/{split}: {len(clips)} speakers, rows {len(rows)}", flush=True)

# ---- LibriSpeech
spk_g = {}
for line in open(f"{D}/LibriSpeech/SPEAKERS.TXT", encoding="utf-8"):
    if line.startswith(";"):
        continue
    p = [x.strip() for x in line.split("|")]
    if len(p) >= 3 and p[1] in ("M", "F"):
        spk_g[p[0]] = 1 if p[1] == "F" else 0
for sd in sorted(glob.glob(f"{D}/LibriSpeech/*/*")):
    spk = os.path.basename(sd)
    if spk not in spk_g:
        continue
    fl = sorted(glob.glob(f"{sd}/*/*.flac"))[:12]
    add("librispeech", f"ls-{spk}", spk_g[spk], [sf.read(f, dtype="float32")[0] for f in fl])
print(f"librispeech: rows {len(rows)}", flush=True)

# ---- split by speaker: 80 % train, 10 % calibration, 10 % test
spks = sorted({r[1] for r in rows})
random.shuffle(spks)
n = len(spks)
part = {s: ("train" if i < 0.8 * n else "cal" if i < 0.9 * n else "test") for i, s in enumerate(spks)}
X = {k: np.array([r[3] for r in rows if part[r[1]] == k], dtype=np.float32) for k in ("train", "cal", "test")}
Y = {k: np.array([r[2] for r in rows if part[r[1]] == k], dtype=np.float32) for k in ("train", "cal", "test")}
SRC = {k: [r[0] for r in rows if part[r[1]] == k] for k in ("train", "cal", "test")}


def fit_lr(x, y, l2=1e-2, it=3000, lr=0.5):
    w, b = np.zeros(x.shape[1], np.float32), 0.0
    pos = y.mean()
    sw = np.where(y == 1, 0.5 / pos, 0.5 / (1 - pos))  # balanced classes
    for _ in range(it):
        z = x @ w + b
        p = 1 / (1 + np.exp(-z))
        g = (p - y) * sw
        w -= lr * (x.T @ g / len(y) + l2 * w)
        b -= lr * g.mean()
    return w, b


w, b = fit_lr(X["train"], Y["train"])
# Platt scaling on the calibration speakers: p = sigmoid(a*z + c)
zc = X["cal"] @ w + b
a, c = 1.0, 0.0
for _ in range(4000):
    p = 1 / (1 + np.exp(-(a * zc + c)))
    g = p - Y["cal"]
    a -= 0.05 * (g * zc).mean()
    c -= 0.05 * g.mean()
W, B = (a * w).astype(np.float32), float(a * b + c)


def report(k):
    z = X[k] @ W + B
    p = 1 / (1 + np.exp(-z))
    acc = float(((p >= 0.5) == (Y[k] == 1)).mean())
    bins = np.clip((p * 10).astype(int), 0, 9)
    ece = float(sum(abs(p[bins == i].mean() - Y[k][bins == i].mean()) * (bins == i).mean() for i in range(10) if (bins == i).any()))
    per = {}
    for s in sorted(set(SRC[k])):
        m = np.array([x == s for x in SRC[k]])
        per[s] = round(float(((p[m] >= 0.5) == (Y[k][m] == 1)).mean()), 3)
    return {"n": int(len(p)), "acc": round(acc, 4), "ece": round(ece, 4), "by_source": per}


rep = {"speakers": n, "windows": len(rows), "female_share": round(float(np.mean([r[2] for r in rows])), 3),
       "train": report("train"), "test": report("test")}
np.savez(f"{OUT}/gender_lr.npz", w=W, b=np.float32(B))
json.dump(rep, open(f"{OUT}/report.json", "w"), indent=1)
print(json.dumps(rep, indent=1))
