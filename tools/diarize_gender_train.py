"""Trains services/diarize/gender_lr.npz: logistic regression on WeSpeaker embeddings of our 57 TTS voices with known
gender (2-3 chunks each; samples from the voice-selection run, map.tsv + genders.json from /v1/voices meta).
Leave-one-voice-out accuracy 57/57 (2026-11-13); on a real energetic male let's-player whose pitch median is
170-210 Hz it gives P(female) 0.16-0.29, where the pitch rule said female. Also prints the cosine distribution
between different voices used for DIARIZE_MERGE_SIM. Run inside the diarize container (paths /tmp/vsel, /app)."""
import sys, glob, json, csv, subprocess, numpy as np, soundfile as sf
sys.path.insert(0, "/app")
import worker; worker.THREADS = 1
SR = 16000
G = json.load(open("/tmp/vsel/genders.json"))
X, y, grp = [], [], []
for f, eng, v in csv.reader(open("/tmp/vsel/map.tsv"), delimiter="\t"):
    g = G.get(f"{eng}|{v}")
    if g not in ("male", "female"): continue
    a = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", f"/tmp/vsel/{f}", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True).stdout, np.float32)
    n = len(a) // (SR * 2)
    for k in range(max(1, min(3, n))):
        X.append(worker._embedding(a[k * SR * 2:(k + 1) * SR * 2 + SR]))
        y.append(1 if g == "female" else 0); grp.append(f"{eng}|{v}")
X, y = np.array(X), np.array(y); print("train", X.shape, "female share", y.mean().round(2))
def fit(X, y, l2=1.0, it=3000, lr=0.5):
    w = np.zeros(X.shape[1]); b = 0.0
    for _ in range(it):
        p = 1 / (1 + np.exp(-(X @ w + b))); g = p - y
        w -= lr * (X.T @ g / len(y) + l2 * w / len(y)); b -= lr * g.mean()
    return w, b
# leave-one-voice-out
voices = sorted(set(grp)); ok = 0
for v in voices:
    m = np.array([g != v for g in grp]); w, b = fit(X[m], y[m])
    p = 1 / (1 + np.exp(-(X[~m] @ w + b))); ok += int(round(p.mean()) == y[~m][0])
print(f"leave-one-voice-out accuracy: {ok}/{len(voices)}")
w, b = fit(X, y)
# different-speaker cosine distribution (voice centroids)
C = {}
for v in voices: c = X[[g == v for g in grp]].mean(0); C[v] = c / np.linalg.norm(c)
cs = [float(C[a] @ C[b]) for i, a in enumerate(voices) for b in voices[i + 1:]]
print(f"different voices cosine: max {max(cs):.3f}, p99 {np.percentile(cs, 99):.3f}, p95 {np.percentile(cs, 95):.3f}")
# the real narrator (two clusters of one man)
path = glob.glob("/tmp/diarize/*/audio.wav")[0]
x, _ = sf.read(path, start=0, frames=300 * SR, dtype="float32")
for name, a0, a1 in [("cluster A 7-60s", 7, 60), ("cluster A 60-140s", 60, 140), ("cluster B 145-207s", 145, 207), ("cluster B 257-280s", 257, 280)]:
    e = np.array(worker._embedding(x[a0 * SR:min(a1, a0 + 30) * SR]))
    p = 1 / (1 + np.exp(-(e @ w + b)))
    print(f"narrator {name}: P(female) = {p:.3f}")
np.savez("/tmp/gender_lr.npz", w=w, b=b)
print("saved", w.shape)
