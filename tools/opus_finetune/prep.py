"""Training data for the Opus-MT EN->UK fine-tune (README.md): adds placeholder pairs so the model keeps N0/N1.

  python3 prep.py <data dir with train.tsv dev.tsv test.tsv test_random250.tsv glossary.tsv> <out dir>

Our /translate gets glossary terms replaced by N0, N1 ... and the coordinator puts the Ukrainian term back, so the
model must copy those tokens exactly once and build grammatical Ukrainian around them. From each pair where a
glossary term is in the English and its Ukrainian form (any ending) in the Ukrainian, a copy with both replaced by
N0 (N1 for a second term) is added; the same for Latin names the reference keeps as they are ("Crainer", "SMP").
dev and test get such placeholder variants as separate files (dev_ph.tsv, test_ph.tsv) for the 100 % check.
An optional replay.tsv (general sentences + the current model's own translations) is mixed in unchanged.
"""
import csv, os, random, re, sys

random.seed(13)
D, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)


def read(name):
    p = os.path.join(D, name)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [tuple(r[:2]) for r in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE) if len(r) >= 2 and r[0] and r[1]]


gl = []
with open(os.path.join(D, "glossary.tsv"), encoding="utf-8") as f:
    for r in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(r) >= 2:
            gl.append((r[0], r[1]))
gl.sort(key=lambda x: -len(x[0]))


def uk_pattern(uk):
    """the Ukrainian term with any inflection: each word's stem (all but the last 2 letters) + any letters"""
    words = uk.split()
    parts = [re.escape(w[:max(3, len(w) - 2)]) + r"[\w'’-]*" for w in words]
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)", re.I)


GL = [(re.compile(r"(?<!\w)" + re.escape(en) + r"(?:s|es)?(?!\w)", re.I), uk_pattern(uk)) for en, uk in gl]
LATIN = re.compile(r"(?<![\w>])([A-Z][A-Za-z0-9]{2,}(?:\s[A-Z][A-Za-z0-9]+)?)(?!\w)")


def placeholder(en, uk):
    """(en, uk) with up to 2 terms replaced by N0/N1, or None"""
    k = 0
    for ep, up in GL:
        if k >= 2:
            break
        me, mu = ep.search(en), up.search(uk)
        if me and mu:
            en, uk = en[:me.start()] + f"N{k}" + en[me.end():], uk[:mu.start()] + f"N{k}" + uk[mu.end():]
            k += 1
    if k < 2:
        for m in LATIN.finditer(en):
            name = m.group(1)
            if m.start() > 0 and name in uk and not name.startswith("N"):  # not sentence-initial, kept verbatim
                en, uk = en.replace(name, f"N{k}", 1), uk.replace(name, f"N{k}", 1)
                k += 1
                if k >= 2:
                    break
    return (en, uk) if k else None


def write(name, rows):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        for en, uk in rows:
            f.write(f"{en}\t{uk}\n")


train = read("train.tsv")
ph = [p for p in (placeholder(en, uk) for en, uk in train) if p]
aug = train + ph * 2  # placeholder pairs twice: a small, important behaviour
# general-language replay (optional replay.tsv: general English sentences translated by the CURRENT model itself):
# keeps the old behaviour on long formal text while the model learns our domain (v1 without it lost 2-3 chrF there)
replay = read("replay.tsv")
aug += replay
random.shuffle(aug)
write("train_aug.tsv", aug)
for name in ("dev", "test", "test_random250", "gold"):
    rows = read(f"{name}.tsv")
    write(f"{name}.tsv", rows)
    if name in ("dev", "test"):
        write(f"{name}_ph.tsv", [p for p in (placeholder(en, uk) for en, uk in rows) if p])
print(f"train {len(train)} + {len(ph)} placeholder pairs (x2) + {len(replay)} replay = {len(aug)}; "
      + ", ".join(f"{n} {sum(1 for _ in open(os.path.join(OUT, n)))}" for n in sorted(os.listdir(OUT)) if n.endswith('.tsv')))
