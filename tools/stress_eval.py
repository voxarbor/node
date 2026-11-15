"""Stress A/B on real phrases: A = current pipeline (accentuk + neural guess for words it leaves unmarked),
B = ByT5 G2P stressifier on the whole sentence, C = accentuk + ByT5 (in context) for the words accentuk leaves
unmarked. Writes per-word results (JSON) for building a gold set; multi-syllable words only.

docker run --rm --user 0 --cpus 2 -e API_KEY=x -e ACCENTUK_MODE=full -e ACCENTUK_ASSETS=/opt/accentuk-full \
  -v tts-accentuk-full:/opt/accentuk-full:ro -v local-ai_tts-cache:/cache -v $PWD/services/tts:/app:ro \
  -v $PWD/tools:/tools:ro -v /path/out:/out --entrypoint sh <tts image> -c \
  'pip install -q --no-deps git+https://github.com/patriotyk/stressifier-byt5-ctranslate2@<sha> && \
   python /tools/stress_eval.py /out/phrases.txt /out/eval.json'"""

import json
import re
import sys
import time

sys.path.insert(0, "/app")
import worker  # noqa: E402

A, VOW = worker.ACUTE, set("аеєиіїоуюяАЕЄИІЇОУЮЯ")
WORD = re.compile(r"[А-Яа-яІіЇїЄєҐґ'ʼ’" + A + r"]+")


def stress_pos(w: str) -> int | None:
    """Index of the stressed vowel among the word's vowels (None = unmarked)."""
    n = -1
    for i, c in enumerate(w):
        if c in VOW:
            n += 1
        if c == A and n >= 0:
            return n
    return None


def words(text: str) -> list[tuple[str, int | None]]:
    out = []
    for w in WORD.findall(text):
        bare = w.replace(A, "")
        if sum(c in VOW for c in bare) >= 2:
            out.append((bare.lower(), stress_pos(w)))
    return out


def main(src: str, dst: str) -> None:
    from ukrainian_stressifier_byt5_ct2 import UkrainianStressifier

    phrases = [ln.strip() for ln in open(src, encoding="utf-8") if ln.strip()]
    byt5 = UkrainianStressifier(device="cpu", stress_symbol=A)
    t_a = t_b = 0.0
    rows = []
    for ph in phrases:
        t = ph.replace("'", "'")
        s = time.monotonic()
        raw = worker._accent_uk(t)                   # accentuk alone (dictionary + its models)
        a = worker.stress(t)                          # current pipeline: + neural guess for unmarked words
        t_a += time.monotonic() - s
        s = time.monotonic()
        b = byt5.apply_stress_marks(t)
        t_b += time.monotonic() - s
        wr, wa, wb = words(raw), words(a), words(b)
        if not (len(wr) == len(wa) == len(wb)):
            rows.append({"phrase": ph, "skip": "token mismatch"})
            continue
        ws = []
        for (w, pr), (_, pa), (_, pb) in zip(wr, wa, wb):
            pc = pr if pr is not None else pb        # hybrid: accentuk where it knows, ByT5 elsewhere
            ws.append({"w": w, "acc": pr, "a": pa, "b": pb, "c": pc})
        rows.append({"phrase": ph, "a": a, "b": b, "words": ws})
    json.dump({"rows": rows, "time_a_s": round(t_a, 1), "time_b_s": round(t_b, 1)},
              open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n = sum(len(r.get("words", [])) for r in rows)
    print(f"{len(rows)} phrases, {n} words; A {t_a:.1f} s, ByT5 {t_b:.1f} s")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
