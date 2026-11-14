"""English names in Ukrainian text -> how the Ukrainian voices should say them, from espeak-ng phonetics (IPA with
stress, offline, no ML: runs in the API process).

`ukr("Sure")` -> "шу+р": the IPA is mapped sound by sound to Cyrillic and the stressed vowel gets "+" (this service's
stress markup). CamelCase names are said word by word ("CleanYourTV" -> "клі+н-ю+р-ті-ві"); short all-caps words are
letter names. None when the word has no Latin letters."""

from __future__ import annotations

import functools
import re
import shutil
import subprocess

# longest first; IPA as espeak-ng prints it for en-us (length marks and secondary stress dropped beforehand)
SOUNDS = [
    ("tʃ", "ч"), ("dʒ", "дж"), ("aɪɚ", "аєр"), ("aɪ", "ай"), ("aʊ", "ау"), ("ɔɪ", "ой"), ("eɪ", "ей"), ("oʊ", "о"),
    ("ɪɹ", "ір"), ("ɛɹ", "ер"), ("ʊɹ", "ур"), ("ɑɹ", "ар"), ("ɔɹ", "ор"), ("ɜɹ", "ер"), ("ɚ", "ер"), ("ɝ", "ер"),
    ("iəɹ", "ір"), ("iə", "іа"), ("i", "і"), ("ɪ", "і"), ("ᵻ", "і"), ("e", "е"), ("ɛ", "е"), ("æ", "е"), ("ɑ", "а"), ("ɒ", "о"),
    ("ɔ", "о"), ("o", "о"), ("ʊ", "у"), ("u", "у"), ("ʌ", "а"), ("ə", "е"), ("ɐ", "а"), ("a", "а"),
    ("p", "п"), ("b", "б"), ("t", "т"), ("ɾ", "т"), ("d", "д"), ("k", "к"), ("ɡ", "ґ"), ("g", "ґ"), ("f", "ф"),
    ("v", "в"), ("θ", "т"), ("ð", "д"), ("s", "с"), ("z", "з"), ("ʃ", "ш"), ("ʒ", "ж"), ("h", "х"), ("m", "м"),
    ("n", "н"), ("ŋ", "нг"), ("l", "л"), ("ɫ", "л"), ("ɹ", "р"), ("r", "р"), ("w", "в"), ("ʔ", ""),
]
IOTA = {"у": "ю", "ʊ": "ю", "е": "є", "а": "я", "і": "ї"}  # j + vowel -> one iotated letter
VOWELS = set("аеєиіїоуюя")


@functools.lru_cache(maxsize=4096)
def ipa(word: str, lang: str = "en-us") -> str | None:
    exe = shutil.which("espeak-ng")
    if not exe or not re.search(r"[A-Za-z]", word):
        return None
    try:
        out = subprocess.run([exe, "-v", lang, "-q", "--ipa", word], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.strip().split("\n")[0].strip() or None


def to_ukr(phon: str) -> str:
    phon = phon.replace("ː", "").replace("ˌ", "").replace("‍", "").replace(" ", "")
    phon = re.sub(r"jˈ", "ˈj", phon)  # the stress mark may sit between j and its vowel
    phon = re.sub(r"ə$", "ɐ", phon)  # a final schwa sounds like а (Prisma, Grafana)
    phon = re.sub(r"([bdfgklmnprstvzɹ])\1", r"\1", phon)  # doubled consonants are one sound
    out, i, stress, mark_next = "", 0, False, False
    while i < len(phon):
        if phon[i] == "ˈ":
            stress, i = True, i + 1
            continue
        if phon[i] == "j":
            nxt = next(((a, b) for a, b in SOUNDS if phon.startswith(a, i + 1)), None)
            if nxt and nxt[1][:1] in IOTA:
                piece, i = IOTA[nxt[1][0]] + nxt[1][1:], i + 1 + len(nxt[0])
            else:
                piece, i = "й", i + 1
        else:
            hit = next(((a, b) for a, b in SOUNDS if phon.startswith(a, i)), None)
            piece, i = (hit[1], i + len(hit[0])) if hit else ("", i + 1)
        if stress and any(c in VOWELS for c in piece):
            k = next(n for n, c in enumerate(piece) if c in VOWELS)
            piece, stress = piece[:k + 1] + "+" + piece[k + 1:], False
        out += piece
    return out


def ukr(word: str, lang: str = "en-us") -> str | None:
    phon = ipa(word, lang)
    return to_ukr(phon) if phon else None


LETTERS = {"a": "ей", "b": "бі", "c": "сі", "d": "ді", "e": "і", "f": "еф", "g": "джі", "h": "ейч", "i": "ай", "j": "джей",
           "k": "кей", "l": "ел", "m": "ем", "n": "ен", "o": "оу", "p": "пі", "q": "кʼю", "r": "ар", "s": "ес", "t": "ті",
           "u": "ю", "v": "ві", "w": "дабл-ю", "x": "екс", "y": "вай", "z": "зед"}
CAMEL = re.compile(r"[A-Z]{2,}(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|[A-Z]+|\d+")


def name(word: str, lang: str = "en-us") -> str | None:
    """One Latin token (a name, CamelCase, acronym, owner/repo-name) -> its Ukrainian spelling with stress."""
    if not re.search(r"[A-Za-z]", word):
        return None
    parts = [p for p in re.split(r"[/_.\-]+", word) if p]
    out = []
    for p in parts:
        bits = CAMEL.findall(p) if len(CAMEL.findall(p)) > 1 and "".join(CAMEL.findall(p)) == p else [p]
        for b in bits:
            if b.isdigit():
                out.append(b)
            elif (b.isupper() or not re.search(r"[aeiouy]", b, re.I)) and len(b) <= 5:  # MCP, npm, ssh: letters
                out.append("-".join(LETTERS.get(c, c) for c in b.lower()))
            else:
                out.append(ukr(b, lang) or b)
    return "-".join(out)


def latin_words(text: str) -> list[str]:
    """Distinct Latin tokens of a text, in order (punctuation around them dropped)."""
    seen: list[str] = []
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9/_.\-]*[A-Za-z0-9]|[A-Za-z]", text):
        if tok not in seen:
            seen.append(tok)
    return seen
