"""Old vs new gender classifier on speakers of our own videos (labels given by the owner). Speaker audio comes from
the community-1 partition (c1_<id>.json); p_female from the speaker's first 60 s and, for stability, per 10 s window.
  python gender_eval_videos.py <dir with <id>.wav + c1_<id>.json> <old gender_lr.npz> <new gender_lr.npz>"""
import json, sys

import numpy as np
import soundfile as sf

sys.path.insert(0, "/app")
import worker  # noqa: E402

SR = 16000
D, OLD, NEW = sys.argv[1:4]
LABELS = {  # video: {speaker label in c1_<id>.json: "m" | "f"}
    "YKK1BDo8j80": {"SPEAKER_00": "m", "SPEAKER_01": "m", "SPEAKER_02": "m"},   # v32: all men (owner)
    "0of0Jte_92M": {"SPEAKER_01": "m", "SPEAKER_00": "f"},                        # Will & Anna
    "9njefMDxzqw": {"SPEAKER_04": "f", "SPEAKER_01": "m"},                        # v42: Agnes (f), the f0-115 man
}
models = {k: np.load(p) for k, p in (("old", OLD), ("new", NEW))}


def pf(m, emb):
    z = float(np.asarray(emb, np.float32) @ m["w"] + float(m["b"]))
    return 1 / (1 + np.exp(-z))


tot = {"old": [0, 0], "new": [0, 0]}
win = {"old": [0, 0], "new": [0, 0]}
for vid, lab in LABELS.items():
    x, _ = sf.read(f"{D}/{vid}.wav", dtype="float32")
    turns = json.load(open(f"{D}/c1_{vid}.json"))["turns"]
    for spk, g in lab.items():
        a = np.concatenate([x[int(t["start"] * SR):int(t["end"] * SR)] for t in turns
                            if t["speaker"] == spk and t["end"] - t["start"] > 0.8] or [np.zeros(1, np.float32)])
        if len(a) < 3 * SR:
            continue
        emb = worker._embedding(a[: 60 * SR])
        f0 = worker.f0_frames(a[: 60 * SR])
        line = [f"{vid} {spk} truth={g} f0={np.median(f0):.0f}" if f0 else f"{vid} {spk} truth={g}"]
        for k, m in models.items():
            p = pf(m, emb)
            ok = (p >= 0.5) == (g == "f")
            tot[k][0] += ok; tot[k][1] += 1
            ws = [pf(m, worker._embedding(a[i:i + 10 * SR])) for i in range(0, min(len(a), 120 * SR) - 5 * SR, 10 * SR)]
            wok = sum((p_ >= 0.5) == (g == "f") for p_ in ws)
            win[k][0] += wok; win[k][1] += len(ws)
            line.append(f"{k}: p_female={p:.2f} {'OK' if ok else 'WRONG'} (10 s windows {wok}/{len(ws)})")
        print(" | ".join(line), flush=True)
for k in ("old", "new"):
    print(f"{k}: speakers {tot[k][0]}/{tot[k][1]}, 10 s windows {win[k][0]}/{win[k][1]}")
