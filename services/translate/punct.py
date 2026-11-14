"""Punctuation, true-casing and sentence boundaries for text without punctuation (YouTube auto-captions, raw STT).

Model: 1-800-BAD-CODE/xlm-roberta_punctuation_fullstop_truecase (Apache-2.0, 47 languages incl. uk/ru/en/pl/de),
ONNX on onnxruntime. Inference follows the `punctuators` package (which pulls in torch) without torch: lower-cased
sentencepiece ids in windows of 254 tokens with a 16-token overlap, per-token pre/post punctuation labels,
per-character capitalisation and a sentence-boundary flag. Runs in the translate worker process (lazy, unloaded
with the other models)."""

from __future__ import annotations

import os
import re

REPO = "1-800-BAD-CODE/xlm-roberta_punctuation_fullstop_truecase"
REVISION = "d1769a597ce8dfaa070d436bc67d4ee761f58884"
OVERLAP = 16
BATCH_TOKENS = 4096
ACRONYM = "<ACRONYM>"
_EXISTING = re.compile(r"[.,!?;:…«»\"“”„()\[\]¿¡]|(?<!\w)[-–—](?!\w)")  # punctuation the model will redo
_model = None


def _load():
    global _model
    if _model is None:
        import numpy as np  # noqa: F401
        import onnxruntime as ort
        import sentencepiece as spm
        import yaml
        from huggingface_hub import snapshot_download

        kw = dict(revision=REVISION, allow_patterns=["model.onnx", "sp.model", "config.yaml"])
        try:  # local cache first: online loads re-check the HF CDN, which can hang here
            path = snapshot_download(REPO, local_files_only=True, **kw)
        except Exception:
            path = snapshot_download(REPO, **kw)
        cfg = yaml.safe_load(open(os.path.join(path, "config.yaml"), encoding="utf-8"))
        so = ort.SessionOptions()
        so.intra_op_num_threads = int(os.environ.get("TRANSLATE_THREADS", "2"))
        so.inter_op_num_threads = 1
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        sess = ort.InferenceSession(os.path.join(path, "model.onnx"), so, providers=["CPUExecutionProvider"])
        _model = (sess, spm.SentencePieceProcessor(os.path.join(path, "sp.model")), cfg)
    return _model


def _predict(ids: list[int]):
    """Token ids of one text -> per-token (pre, post, cap, sbd) predictions, windows stitched like punctuators."""
    import numpy as np

    sess, sp, cfg = _load()
    max_len = int(cfg["max_length"]) - 2
    windows, start, n = [], 0, 0
    while start < len(ids):
        a = start - (0 if n == 0 else OVERLAP)
        windows.append(ids[a:a + max_len])
        start, n = a + max_len, n + 1
    results = [None] * len(windows)
    order = sorted(range(len(windows)), key=lambda i: -len(windows[i]))
    batch: list[int] = []

    def run(batch_idx: list[int]) -> None:
        rows = [[sp.bos_id()] + windows[i] + [sp.eos_id()] for i in batch_idx]
        width = max(len(r) for r in rows)
        arr = np.full((len(rows), width), sp.pad_id(), dtype=np.int64)
        for j, r in enumerate(rows):
            arr[j, :len(r)] = r
        pre, post, cap, sbd = sess.run(None, {"input_ids": arr})
        for j, i in enumerate(batch_idx):
            L = len(rows[j])
            results[i] = (pre[j, 1:L - 1], post[j, 1:L - 1], cap[j, 1:L - 1], sbd[j, 1:L - 1])

    for i in order:
        if batch and (len(batch) + 1) * (max(len(windows[k]) for k in batch + [i]) + 2) > BATCH_TOKENS:
            run(batch)
            batch = []
        batch.append(i)
    if batch:
        run(batch)
    out = {"ids": [], "pre": [], "post": [], "cap": [], "sbd": []}
    for i, (pre, post, cap, sbd) in enumerate(results):
        a = OVERLAP // 2 if i > 0 else 0
        b = len(pre) - (OVERLAP // 2 if i < len(results) - 1 else 0)
        w = windows[i]
        out["ids"] += w[a:b]
        out["pre"] += [cfg["pre_labels"][int(x)] for x in pre[a:b]]
        out["post"] += [cfg["post_labels"][int(x)] for x in post[a:b]]
        out["cap"] += [list(c) for c in cap[a:b]]
        out["sbd"] += [int(x) for x in sbd[a:b]]
    return out


def punctuate(text: str) -> list[str]:
    """Text without (or with sloppy) punctuation -> sentences with punctuation and capitals."""
    _, sp, cfg = _load()
    null = cfg.get("null_token", "<NULL>")
    clean = " ".join(_EXISTING.sub(" ", text).lower().split())
    if not clean:
        return []
    p = _predict(sp.EncodeAsIds(clean))
    sentences, cur = [], []
    for k, tok in enumerate(sp.IdToPiece(i) for i in p["ids"]):
        if tok.startswith("▁") and cur:
            cur.append(" ")
        first = 1 if tok.startswith("▁") else 0
        for ci, ch in enumerate(tok[first:], start=first):
            if ci == first and p["pre"][k] != null:
                cur.append(p["pre"][k])
            cur.append(ch.upper() if ci < len(p["cap"][k]) and p["cap"][k][ci] else ch)
            if p["post"][k] == ACRONYM:
                cur.append(".")
            elif ci == len(tok) - 1 and p["post"][k] != null:
                cur.append(p["post"][k])
            if ci == len(tok) - 1 and p["sbd"][k]:
                sentences.append("".join(cur).strip())
                cur = []
    if cur:
        sentences.append("".join(cur).strip())
    return [s for s in sentences if s]


def punctuate_segments(segments: list[str]) -> list[str]:
    """Caption lines -> the same lines punctuated with the context of their neighbours (word counts are kept, so
    timings stay aligned). Empty lines stay empty."""
    counts = [len(_EXISTING.sub(" ", s).split()) for s in segments]
    words = " ".join(punctuate(" ".join(segments))).split()
    out, pos = [], 0
    for n in counts:
        out.append(" ".join(words[pos:pos + n]))
        pos += n
    if pos < len(words) and out:  # should not happen; keep any tail on the last non-empty line
        last = max(i for i, n in enumerate(counts) if n) if any(counts) else len(out) - 1
        out[last] = (out[last] + " " + " ".join(words[pos:])).strip()
    return out
