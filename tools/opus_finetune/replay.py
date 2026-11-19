"""Replay pairs that keep general language (README.md): long English sentences from WikiText-103 (CC-BY-SA 3.0)
translated by the CURRENT node model itself, so training on our domain does not drift on formal text.
  python replay.py <old ct2 dir> <out tsv> <n>     (env MIN_WORDS=15, MAX_WORDS=60, DEVICE=cuda|cpu)"""
import os, random, re, sys

import ctranslate2, sentencepiece as spm
from datasets import load_dataset

OLD, OUT, N = sys.argv[1], sys.argv[2], int(sys.argv[3])
LO, HI = int(os.environ.get("MIN_WORDS", "15")), int(os.environ.get("MAX_WORDS", "60"))
SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
seen, pool = set(), []
for line in load_dataset("Salesforce/wikitext", "wikitext-103-raw-v1", split="train")["text"]:
    line = line.strip()
    if not line or line.startswith("="):
        continue
    for s in SENT.split(line.replace(" @-@ ", "-").replace(" @,@ ", ",").replace(" @.@ ", ".")):
        s = re.sub(r"\s+([,.;:!?)'])", r"\1", s).replace("( ", "(").strip()
        if LO <= len(s.split()) <= HI and s.endswith((".", "!", "?")) and s not in seen:
            seen.add(s)
            pool.append(s)
random.Random(7).shuffle(pool)
pool = pool[:N]
dev = os.environ.get("DEVICE") or ("cuda" if ctranslate2.get_cuda_device_count() else "cpu")
tr = ctranslate2.Translator(OLD, device=dev, compute_type="int8_float16" if dev == "cuda" else "int8")
try:
    tr.translate_batch([[">>ukr<<", "▁Hi", "</s>"]], max_decoding_length=4)
except RuntimeError as exc:  # e.g. no libcublas: still works, only slower
    print("replay on cpu:", exc)
    dev, tr = "cpu", ctranslate2.Translator(OLD, device="cpu", compute_type="int8")
src, tgt = spm.SentencePieceProcessor(f"{OLD}/source.spm"), spm.SentencePieceProcessor(f"{OLD}/target.spm")
with open(OUT, "w", encoding="utf-8") as f:
    for i in range(0, len(pool), 256):
        part = pool[i:i + 256]
        r = tr.translate_batch([[">>ukr<<"] + src.encode(t, out_type=str)[:400] + ["</s>"] for t in part],
                               beam_size=2, max_decoding_length=400, max_batch_size=64, repetition_penalty=1.1)
        for en, x in zip(part, r):
            uk = tgt.decode(x.hypotheses[0]).strip()
            if uk and "\t" not in en + uk:
                f.write(f"{en}\t{uk}\n")
print(f"replay: {len(pool)} WikiText sentences of {LO}-{HI} words translated by {OLD} on {dev} -> {OUT}")
