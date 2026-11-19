"""Old vs new Opus-MT (CTranslate2 int8, exactly as the node's translate service runs it: >>ukr<< prefix, beam 2,
repetition_penalty 1.1) on our test sets + general sets to catch forgetting
(Tatoeba eng-ukr test, NTREX-128, WMT22 en-uk).
  python eval.py <prep dir> <old ct2 dir> <new ct2 dir> <out dir>
Writes BLEU/chrF per set, placeholder keep rate (must be 100 %), and blind judge files (judge/*.md + key.json)."""
import json, os, random, re, subprocess, sys, urllib.request

import ctranslate2, sacrebleu, sentencepiece as spm

P, OLD, NEW, OUT = sys.argv[1:5]
os.makedirs(os.path.join(OUT, "judge"), exist_ok=True)
PH = re.compile(r"\bN\d+\b")
GH = "https://raw.githubusercontent.com"


def tsv(name):
    p = os.path.join(P, name)
    return [l.rstrip("\n").split("\t")[:2] for l in open(p, encoding="utf-8")] if os.path.exists(p) else []


def fetch(url, path):
    if not os.path.exists(path):
        urllib.request.urlretrieve(url, path + ".part")
        os.replace(path + ".part", path)
    return [l.rstrip("\n") for l in open(path, encoding="utf-8")]


def general():
    """general (non-game) sets to catch forgetting; each is optional (no network -> skipped, reported)"""
    G = os.environ.get("GENERAL_DIR", os.path.join(OUT, "general"))
    os.makedirs(G, exist_ok=True)
    out = {}
    try:  # Tatoeba-Challenge test eng-ukr (CC-BY 2.0), from GitHub: the HF copy is gated
        rows = [l.split("\t")[2:4] for l in fetch(f"{GH}/Helsinki-NLP/Tatoeba-Challenge/master/data/test/eng-ukr/test.txt",
                                                    os.path.join(G, "tatoeba.txt"))]
        rows = [r for r in rows if len(r) == 2 and r[0] and r[1]]
        random.Random(1).shuffle(rows)
        out["general_tatoeba"] = rows[:1000]
    except Exception as exc:  # noqa: BLE001
        print("general_tatoeba unavailable:", exc)
    try:  # NTREX-128 (CC-BY-SA 4.0): 1997 news sentences, long formal text
        b = f"{GH}/MicrosoftTranslator/NTREX/main/NTREX-128/newstest2019"
        out["general_ntrex"] = list(zip(fetch(f"{b}-src.eng.txt", os.path.join(G, "ntrex.en")),
                                        fetch(f"{b}-ref.ukr.txt", os.path.join(G, "ntrex.uk"))))
    except Exception as exc:  # noqa: BLE001
        print("general_ntrex unavailable:", exc)
    try:  # WMT22 general en-uk through sacrebleu's own download
        echo = lambda side: subprocess.run(["sacrebleu", "-t", "wmt22", "-l", "en-uk", "--echo", side], check=True,
                                           capture_output=True, text=True, env={**os.environ, "SACREBLEU": G}).stdout.splitlines()
        out["general_wmt22"] = list(zip(echo("src"), echo("ref")))
    except Exception as exc:  # noqa: BLE001
        print("general_wmt22 unavailable:", exc)
    return {k: v for k, v in out.items() if v}


def translator(path):
    # CPU int8, as on every node (the CUDA libraries of the pip wheel are not needed then)
    tr = ctranslate2.Translator(path, device="cpu", compute_type="int8", intra_threads=int(os.environ.get("THREADS", "8")))
    src, tgt = spm.SentencePieceProcessor(f"{path}/source.spm"), spm.SentencePieceProcessor(f"{path}/target.spm")

    def run(texts):
        b = [[">>ukr<<"] + src.encode(t, out_type=str)[:400] + ["</s>"] for t in texts]
        r = tr.translate_batch(b, beam_size=2, max_decoding_length=400, max_batch_size=32, repetition_penalty=1.1)
        return [tgt.decode(x.hypotheses[0]).strip() for x in r]
    return run


sets = {n: tsv(f"{n}.tsv") for n in ("test", "test_random250", "test_auto", "test_ph", "dev_ph", "gold")}
sets.update(general())
sets ={k: v for k, v in sets.items() if v}
old, new = translator(OLD), translator(NEW)
report, judge = {}, []
for name, rows in sets.items():
    src, ref = [r[0] for r in rows], [r[1] for r in rows]
    o, n = old(src), new(src)
    rep = {"n": len(rows)}
    for tag, hyp in (("old", o), ("new", n)):
        rep[tag] = {"bleu": round(sacrebleu.corpus_bleu(hyp, [ref]).score, 2),
                    "chrf": round(sacrebleu.corpus_chrf(hyp, [ref]).score, 2)}
        if name.endswith("_ph"):
            ok = sum(sorted(PH.findall(h)) == sorted(PH.findall(s)) for h, s in zip(hyp, src))
            rep[tag]["placeholders_kept"] = f"{ok}/{len(rows)}"
    report[name] = rep
    if name in ("test", "test_random250", "test_ph"):
        judge += [(name, s, a, b) for s, a, b in zip(src, o, n)]
    with open(os.path.join(OUT, f"{name}.jsonl"), "w", encoding="utf-8") as f:
        for s, r_, a, b in zip(src, ref, o, n):
            f.write(json.dumps({"en": s, "ref": r_, "old": a, "new": b}, ensure_ascii=False) + "\n")
json.dump(report, open(os.path.join(OUT, "report.json"), "w"), indent=1)
print(json.dumps(report, indent=1))
# blind A/B for the judge: random side per sentence, 2 parts
random.seed(5)
key, parts = {}, [[], []]
for i, (name, s, a, b) in enumerate(judge):
    flip = random.random() < 0.5
    key[f"j{i}"] = {"A": "new" if flip else "old", "B": "old" if flip else "new", "set": name}
    A, B = (b, a) if flip else (a, b)
    parts[i % 2].append(f"### j{i}\nEN: {s}\nA: {A}\nB: {B}\n")
json.dump(key, open(os.path.join(OUT, "judge", "key.json"), "w"))
for k, p in enumerate(parts):
    open(os.path.join(OUT, "judge", f"part{k}.md"), "w", encoding="utf-8").write("\n".join(p))
