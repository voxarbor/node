"""Text -> spoken segments, in the API process (no ML): Markdown/URL cleanup, English names -> Cyrillic phonetics
(lexicon name rules first, then espeak-ng), sentence grouping for streaming and jobs; plus the streaming encoder.

A segment is a few sentences of one paragraph: the first one is short so audio starts quickly; the rest are up to
SEG_CHARS. `text` is what people see (cleaned original), `spoken` what the voice gets (names respelled)."""

from __future__ import annotations

import asyncio
import re
import struct
import unicodedata

import lexicon
import phonemize

SAMPLE_RATE = 24000
FIRST_CHARS = 90    # first segment: usually just the first sentence -> first audio after ~2-4 s warm
SEG_CHARS = 300     # later segments
LONG_SENTENCE = 400  # longer sentences are cut at commas/semicolons/dashes
PARA_PAUSE_MS = 400  # extra pause after a paragraph or heading

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"\b(?:https?://|www\.)(?:www\.)?([^/\s)\]>]+)[^\s)\]>]*")
_EMPH = re.compile(r"(\*\*|__|~~|`|(?<!\w)\*|(?<!\w)_)(\S(?:.*?\S)?)\1(?!\w)")
_LIST = re.compile(r"^(?:[-*+•▪◦]|\d{1,3}[.)])\s+")
_RULE = re.compile(r"^(?:[-*_=]\s*){3,}$|^\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-*:?\s*$")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9'’/_.\-]*[A-Za-z0-9]|[A-Za-z]")
_ROMAN = re.compile(r"M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})")
_SENT = re.compile(r"(?<=[.!?…])(?<!\b[A-ZА-ЯІЇЄҐ]\.)[\"'»”)\]]*\s+")  # "Т. Г. Шевченко" is not a sentence end
_UP, _LOW = "А-ЯІЇЄҐ", "а-яіїєґ'’ʼ"
LETTER_NAMES = {"А": "а", "Б": "бе", "В": "ве", "Г": "ге", "Ґ": "ґе", "Д": "де", "Е": "е", "Є": "є", "Ж": "же",
                "З": "зе", "И": "и", "І": "і", "Ї": "ї", "Й": "йот", "К": "ка", "Л": "ел", "М": "ем", "Н": "ен",
                "О": "о", "П": "пе", "Р": "ер", "С": "ес", "Т": "те", "У": "у", "Ф": "еф", "Х": "ха", "Ц": "це",
                "Ч": "че", "Ш": "ша", "Щ": "ща", "Ю": "ю", "Я": "я"}
INITIALS_MODES = ("letters", "skip")
# 1-3 initials before a surname ("Т. Г. Шевченко", "Т.Г. Шевченка", "А. Прізвище") or after it ("Шевченко Т. Г.")
_INIT_BEFORE = re.compile(rf"(?<![\w'’])((?:[{_UP}]\.\s*){{1,3}})(?=[{_UP}][{_LOW}])")
_INIT_AFTER = re.compile(rf"(?<![\w'’])([{_UP}][{_LOW}]{{2,}}\s+)((?:[{_UP}]\.\s?){{0,2}}[{_UP}]\.)(?![\w'’])")
_END = ".!?…:;"


def clean(text: str) -> str:
    """Markdown and links -> plain readable text. Headings become their own paragraph ending with a full stop;
    code blocks are dropped; bare URLs are shortened to their domain."""
    text = _FENCE.sub("\n\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(lambda m: m.group(1), text)
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if _RULE.match(s):
            continue
        heading = bool(re.match(r"^#{1,6}\s", s))
        s = re.sub(r"^#{1,6}\s+", "", s)
        s = re.sub(r"^>\s?", "", s)
        s = _LIST.sub("", s)
        if s.startswith("|") and s.endswith("|"):  # table row
            s = ", ".join(c.strip() for c in s.strip("|").split("|") if c.strip())
        for _ in range(2):  # nested emphasis
            s = _EMPH.sub(r"\2", s)
        s = re.sub(r"<[^>\n]{1,40}>", "", s)  # stray HTML tags
        if heading and s:
            out += ["", s if s[-1] in _END else s + ".", ""]
        else:
            out.append(s)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _name_rules() -> list[tuple[re.Pattern, str]]:
    return [(pattern, right) for kind, pattern, right in lexicon.rules() if kind == "name"]


def initials(text: str, mode: str = "letters") -> str:
    """Initials next to a surname: read as letter names ("Т. Г. Шевченко" -> "те ге Шевченко") or skipped, so the
    voice does not say a bare consonant with a sentence-end pause. Full names come from lexicon replace rules,
    which run first."""
    def say(chunk: str) -> str:
        return "" if mode == "skip" else " ".join(LETTER_NAMES[c] for c in re.findall(f"[{_UP}]", chunk))

    text = _INIT_BEFORE.sub(lambda m: (say(m.group(1)) + " ").lstrip(), text)
    text = _INIT_AFTER.sub(lambda m: (m.group(1) + say(m.group(2))).rstrip(), text)
    text = re.sub(r"[ \t]+([,.;:!?…])", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def replace_rules(text: str) -> str:
    """Lexicon replace rules ("Т. Г. Шевченко => Тарас Григорович Шевченко", "т. зв. => так званий")."""
    for kind, pattern, right in lexicon.rules():
        if kind == "replace":
            text = pattern.sub(lambda m: right, text)
    return text


def is_roman(word: str) -> bool:
    """XVI, XIX, IV, XLIV: Roman numerals are numbers (the verbalizer reads them in the right case), not English
    words. Only I/V/X, or >= 4 letters with L/C, so CD, DC, MIX, CV stay words."""
    if not word or not _ROMAN.fullmatch(word):
        return False
    return set(word) <= set("IVX") or (len(word) >= 4 and set(word) <= set("IVXLC"))


def say_name(word: str, lang: str = "en-us") -> str | None:
    """One Latin token -> Cyrillic with '+' stress: a lexicon name rule if one matches, else espeak-ng."""
    for pattern, right in _name_rules():
        if pattern.fullmatch(word):
            return right
    return phonemize.name(word, lang)


def english(text: str, lang: str = "en-us", cache: dict | None = None) -> tuple[str, dict[str, str]]:
    """Latin words -> how the Ukrainian voice should say them. Returns (text, {word: spelling})."""
    used: dict[str, str] = {}
    for pattern, right in _name_rules():  # multi-word names ("Visual Studio Code") before single tokens
        text = pattern.sub(lambda m: used.setdefault(m.group(0), right), text)
    cache = {} if cache is None else cache

    def sub(m: re.Match) -> str:
        w = m.group(0)
        if is_roman(w):
            return w
        if w not in cache:
            cache[w] = phonemize.name(w, lang) or w
        used[w] = cache[w]
        return cache[w]

    return _LATIN.sub(sub, text), used


def normalize(text: str) -> str:
    """Unicode fixes for text copied from PDFs: composed letters, й written as і/и + breve, soft hyphens."""
    text = unicodedata.normalize("NFC", text.replace("\u00ad", ""))
    return re.sub(r"[іи]\u0306", "й", re.sub(r"[ІИ]\u0306", "Й", text))


_CITE = re.compile(r"\s*\[\d+(?:\s*[,–-]\s*\d+)*\]")  # Wikipedia-style footnotes [1], [2, 3]


BRACKET_MODES = ("aside", "skip", "announce", "voice")
ASIDE_START, ASIDE_END = "\ue000", "\ue001"  # spoken-text markers: bracket text read by the second voice


def _bracket_spans(text: str) -> list[tuple[int, int]]:
    """Outermost matched () / [] pairs as (open index, close index); unmatched brackets are ignored."""
    spans, stack = [], []
    for i, ch in enumerate(text):
        if ch in "([":
            stack.append(i)
        elif ch in ")]" and stack:
            j = stack.pop()
            if not stack:
                spans.append((j, i))
    return spans


def speakable(text: str, mode: str = "aside", announce: bool = False) -> str:
    """What the voice gets for text in brackets (the models barely know "()" and drop "[]"):
    aside    -> read as an aside with comma pauses: "вивести, типу: пряма лінія, значну" (default)
    skip     -> not read at all
    announce -> "вивести, у дужках: типу: пряма лінія, значну"
    voice    -> wrapped in ASIDE_START/ASIDE_END for the second voice (app._synth splits the runs)
    announce=True adds "у дужках:" (said by the main voice) to aside or voice; "announce" mode = aside + announce.
    Footnote marks [1] are always removed. Only the spoken text changes, captions keep the brackets."""
    text = _CITE.sub("", text)
    if mode == "announce":
        mode, announce = "aside", True
    say = "у дужках: " if announce else ""
    out, pos = [], 0
    for a, b in _bracket_spans(text):
        before, inner, after = text[pos:a].rstrip(), text[a + 1:b].strip(), text[b + 1:].lstrip()
        inner = speakable(inner, "aside") if any(c in inner for c in "()[]") else inner
        out.append(before)
        lead = "" if not "".join(out).strip() or "".join(out).rstrip()[-1] in ".!?…:;,—–-" else ","
        end_soft = not after or after[0] in ".!?…:;,)]"
        tail = "" if end_soft else (". " if after[0].isupper() else ", ")
        if mode == "skip" or not inner:
            out.append("" if end_soft else (". " if after[0].isupper() and lead == "," else " "))
        elif mode == "voice":
            out.append(f"{lead} {say}{ASIDE_START}{inner}{ASIDE_END}{tail or ' '}")
        else:
            out.append(f"{lead} {say}{inner}{tail}")
        pos = b + 1
    out.append(text[pos:])
    text = "".join(out).replace("(", "").replace(")", "").replace("[", "").replace("]", "")
    text = re.sub(r"\s+([,.;:!?…])", r"\1", text)
    text = re.sub(r",(?:\s*,)+", ",", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def aside_runs(spoken: str) -> list[tuple[str, bool]]:
    """Spoken text with ASIDE markers -> [(text, is_aside)] in order, empty runs dropped."""
    runs = []
    for i, part in enumerate(re.split(f"[{ASIDE_START}{ASIDE_END}]", spoken)):
        if part.strip(" ,.;:—–-"):
            runs.append((part.strip().lstrip(",;:—– ").strip(), i % 2 == 1))
    return runs


def _paragraphs(text: str) -> list[str]:
    """Blank lines separate paragraphs. Inside one, hard-wrapped lines (PDFs, books) are joined back: a line break
    ends a sentence only after end punctuation, or after a clearly short line (heading, list item) followed by a
    capital or digit; "зна-\nменувалось" becomes one word."""
    paras = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        full = max(60, *(len(ln) for ln in lines))  # typical wrapped line length (60+ for short blocks)
        joined = lines[0]
        for prev, ln in zip(lines, lines[1:]):
            if re.search(r"\w-$", joined) and ln[0].islower():  # word hyphenated at the line end
                joined = joined[:-1] + ln
            elif joined[-1] in _END:
                joined += " " + ln
            elif (ln[0].isupper() or ln[0].isdigit()) and len(prev) < 0.5 * full:
                joined += ". " + ln
            else:
                joined += " " + ln
        paras.append(joined)
    return paras


def _sentences(para: str) -> list[str]:
    out = []
    for s in (x.strip() for x in _SENT.split(para)):
        while len(s) > LONG_SENTENCE:  # cut at the last , ; : — before ~250 chars, else at a space
            cut = max(s.rfind(c, 120, 260) for c in (", ", "; ", ": ", " — ", " – "))
            cut = cut + 1 if cut > 0 else (s.rfind(" ", 120, 260) if s.rfind(" ", 120, 260) > 0 else 260)
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            out.append(s)
    return out


def plan(text: str, *, clean_text: bool = True, use_english: bool = True, pause_ms: int = 150,
         lang: str = "en-us", brackets: str = "aside", announce: bool = False,
         initials_mode: str = "letters") -> tuple[list[dict], dict[str, str]]:
    """-> (segments [{text, spoken, pause_after_ms}], names {latin word: spelling})."""
    text = normalize(text)
    if clean_text:
        text = clean(text)
    segs: list[dict] = []
    names: dict[str, str] = {}
    cache: dict[str, str] = {}
    paras = _paragraphs(text)
    for p, para in enumerate(paras):
        group = ""
        for sent in _sentences(para):
            limit = FIRST_CHARS if not segs else SEG_CHARS
            if group and len(group) + 1 + len(sent) > limit:
                segs.append({"text": group, "pause_after_ms": pause_ms})
                group = sent
            else:
                group = f"{group} {sent}".strip()
        if group:
            segs.append({"text": group, "pause_after_ms": pause_ms})
        if segs and p < len(paras) - 1:
            segs[-1]["pause_after_ms"] = pause_ms + PARA_PAUSE_MS
    if segs:
        segs[-1]["pause_after_ms"] = 0
    for s in segs:
        spoken = initials(replace_rules(s["text"]), initials_mode)
        if use_english:
            s["spoken"], used = english(spoken, lang, cache)
            names.update(used)
        else:
            s["spoken"] = spoken
        s["spoken"] = speakable(s["spoken"], brackets, announce)
    return segs, names


def prepare_text(text: str, *, clean_text: bool = True, use_english: bool = True,
                 brackets: str = "aside", announce: bool = False,
                 initials_mode: str = "letters") -> tuple[str, dict[str, str]]:
    """Whole text through cleanup + English names, hard-wrapped lines joined, paragraphs kept (one-shot
    /v1/audio/speech and /v1/text/prepare; the worker would otherwise end a sentence at every line break)."""
    text = normalize(text)
    if clean_text:
        text = clean(text)
    text = "\n\n".join(speakable(initials(replace_rules(p), initials_mode), brackets, announce)
                         for p in _paragraphs(text))
    return english(text) if use_english else (text, {})


def time_stretch(pcm: bytes, factor: float) -> bytes:
    """float32 PCM 24 kHz played `factor` times faster (>1 shorter), pitch kept (ffmpeg atempo)."""
    import subprocess

    return subprocess.run(["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
                           "-af", f"atempo={factor:.5f}", "-f", "f32le", "pipe:1"],
                          input=pcm, capture_output=True, check=True).stdout


# ---- streaming encoder -------------------------------------------------------------------------------------------

STREAM_FORMATS = {"mp3": "audio/mpeg", "opus": "audio/ogg", "aac": "audio/aac", "wav": "audio/wav",
                  "pcm": "audio/L16;rate=24000;channels=1"}
_FFMPEG = {"mp3": ["-ar", "48000", "-c:a", "libmp3lame", "-b:a", "320k", "-f", "mp3"],
           "opus": ["-ar", "48000", "-c:a", "libopus", "-b:a", "160k", "-page_duration", "100000", "-f", "ogg"],
           "aac": ["-ar", "48000", "-c:a", "aac", "-b:a", "192k", "-f", "adts"]}


def _s16(pcm: bytes) -> bytes:
    import numpy as np

    return (np.clip(np.frombuffer(pcm, dtype=np.float32), -1, 1) * 32767).astype("<i2").tobytes()


def wav_stream_header() -> bytes:
    """WAV header for a stream of unknown length (sizes set to the maximum; players read until EOF)."""
    return b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16) + b"data" + struct.pack("<I", 0xFFFFFFFF)


async def encode_stream(pcm_chunks, fmt: str):
    """Async iterator of float32 PCM (24 kHz mono) -> async iterator of encoded bytes, one continuous stream."""
    if fmt in ("pcm", "wav"):
        if fmt == "wav":
            yield wav_stream_header()
        async for pcm in pcm_chunks:
            yield _s16(pcm)
        return
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-fflags", "nobuffer", "-probesize", "32", "-analyzeduration", "0", "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
        "-af", "aresample=resampler=soxr", *_FFMPEG[fmt], "-flush_packets", "1", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)

    async def feed():
        try:
            async for pcm in pcm_chunks:
                proc.stdin.write(pcm)
                await proc.stdin.drain()
        finally:
            proc.stdin.close()

    feeder = asyncio.create_task(feed())
    try:
        while chunk := await proc.stdout.read(16384):
            yield chunk
        await feeder  # re-raise a synthesis error after the encoder flushed what it had
    finally:
        feeder.cancel()
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
