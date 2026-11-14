"""Runs in a child process: the models live only here, so stopping the process frees their memory.

Routing: to Ukrainian from a language with an Opus-MT tc-big model (CTranslate2 int8, converted once by
convert_opus.sh into /cache/opus-mt) -> that model (~5x faster than NLLB on this CPU, its 57k vocabulary is cheap to
decode); everything else -> NLLB-200 distilled (CTranslate2 int8, 256k vocabulary, ~40 ms per output token here).
"""

import os
import re
from collections import OrderedDict

NLLB = {  # name -> (HF repo, pinned revision)
    "nllb-600m": ("JustFrederik/nllb-200-distilled-600M-ct2-int8", "302d78f00e6fdb50a1064059df7c392b735e9d05"),
}
OPUS_DIR = "/cache/opus-mt"
OPUS_TO_UK = {  # source ISO -> Opus-MT tc-big pair (target token >>ukr<<)
    "en": "en-zle", "de": "de-zle", "ru": "zle-zle", "be": "zle-zle", "pl": "zlw-zle", "cs": "zlw-zle",
    "sk": "zlw-zle", "fr": "fr-zle", "es": "es-zle", "it": "it-zle", "pt": "pt-zle",
}
# ISO 639-1 -> NLLB (FLORES-200) codes; NLLB codes themselves are accepted too.
LANGS = {
    "uk": "ukr_Cyrl", "en": "eng_Latn", "ru": "rus_Cyrl", "pl": "pol_Latn", "de": "deu_Latn", "fr": "fra_Latn",
    "es": "spa_Latn", "it": "ita_Latn", "pt": "por_Latn", "nl": "nld_Latn", "cs": "ces_Latn", "sk": "slk_Latn",
    "be": "bel_Cyrl", "bg": "bul_Cyrl", "sr": "srp_Cyrl", "hr": "hrv_Latn", "sl": "slv_Latn", "ro": "ron_Latn",
    "hu": "hun_Latn", "lt": "lit_Latn", "lv": "lvs_Latn", "et": "est_Latn", "fi": "fin_Latn", "sv": "swe_Latn",
    "no": "nob_Latn", "nb": "nob_Latn", "da": "dan_Latn", "el": "ell_Grek", "tr": "tur_Latn", "ka": "kat_Geor",
    "hy": "hye_Armn", "kk": "kaz_Cyrl", "az": "azj_Latn", "he": "heb_Hebr", "ar": "arb_Arab", "fa": "pes_Arab",
    "hi": "hin_Deva", "zh": "zho_Hans", "ja": "jpn_Jpan", "ko": "kor_Hang", "vi": "vie_Latn", "id": "ind_Latn",
    "th": "tha_Thai",
}
ISO = {v: k for k, v in LANGS.items()}
MAX_TOKENS = 400
MAX_LOADED = int(os.environ.get("TRANSLATE_MAX_MODELS", "3"))
THREADS = int(os.environ.get("TRANSLATE_THREADS", "4"))
_models: "OrderedDict[str, tuple]" = OrderedDict()  # name -> (translator, encode, decode), least recently used first


def init() -> None:
    import ctranslate2  # noqa: F401  (imported here so the idle API process stays light)


def _get(name: str):
    if name in _models:
        _models.move_to_end(name)
        return _models[name]
    import ctranslate2

    if name.startswith("opus:"):
        import sentencepiece as spm

        path = f"{OPUS_DIR}/{name[5:]}"
        src, tgt = spm.SentencePieceProcessor(f"{path}/source.spm"), spm.SentencePieceProcessor(f"{path}/target.spm")
        encode = lambda text: [">>ukr<<"] + src.encode(text, out_type=str)[:MAX_TOKENS] + ["</s>"]
        decode = lambda tokens: tgt.decode(tokens)
    else:
        from huggingface_hub import snapshot_download
        from tokenizers import Tokenizer

        repo, rev = NLLB[name]
        kw = dict(revision=rev, allow_patterns=["*.bin", "*.json", "*.txt"])
        try:  # cached copy without network (online loads re-check the HF CDN, which can hang)
            path = snapshot_download(repo, local_files_only=True, **kw)
        except Exception:
            path = snapshot_download(repo, **kw)
        tok = Tokenizer.from_file(os.path.join(path, "tokenizer.json"))
        encode = lambda text: tok.encode(text, add_special_tokens=False).tokens[:MAX_TOKENS] + ["</s>"]
        decode = lambda tokens: tok.decode([tok.token_to_id(t) for t in tokens], skip_special_tokens=True)
    _models[name] = (ctranslate2.Translator(path, device="cpu", compute_type="int8", intra_threads=THREADS), encode, decode)
    while len(_models) > MAX_LOADED:
        _models.popitem(last=False)
    return _models[name]


_lid = None
# Prior for the usual video sources: short Russian lines otherwise come out as Bulgarian/Serbian.
COMMON = {"en", "ru", "uk", "pl", "de", "fr", "es", "it", "pt"}


def detect(text: str) -> str:
    """ISO code of the most likely supported language (langid probabilities x prior)."""
    global _lid
    if _lid is None:
        import py3langid.langid as langid

        _lid = langid._load_identifier(norm_probs=True)
    scores = {lang: float(p) * (3.0 if lang in COMMON else 1.0) for lang, p in _lid.rank(text) if lang in LANGS}
    return max(scores, key=scores.get) if scores else "en"


def nllb_code(lang: str) -> str:
    code = LANGS.get(lang.lower(), lang)
    if not re.fullmatch(r"[a-z]{3}_[A-Z][a-z]{3}", code):
        raise ValueError(f"unsupported language: {lang}")
    return code


def route(source: str, target: str) -> str:
    """Model name for a pair of NLLB codes."""
    pair = OPUS_TO_UK.get(ISO.get(source, "")) if target == "ukr_Cyrl" else None
    if pair and os.path.isfile(f"{OPUS_DIR}/{pair}/model.bin") and os.environ.get("TRANSLATE_OPUS", "1") == "1":
        return "opus:" + pair
    if not nllb_enabled():
        raise ValueError(f"{source} -> {target} needs NLLB-200, which is off (CC-BY-NC license: non-commercial "
                         f"use only); set TRANSLATE_NLLB=1 to enable it, or translate from a language with an "
                         f"Opus-MT model to Ukrainian")
    return os.environ.get("TRANSLATE_MODEL", "nllb-600m")


def nllb_enabled() -> bool:
    """NLLB-200 is CC-BY-NC (non-commercial), so it is opt-in."""
    return os.environ.get("TRANSLATE_NLLB", "0") == "1"


def punctuate(text: str | None, segments: list[str] | None) -> list[str]:
    """Punctuation + capitals + sentence boundaries (punct.py): text -> sentences; segments -> same lines."""
    import punct

    return punct.punctuate(text) if text is not None else punct.punctuate_segments(segments)


def translate(texts: list[str], source: str, target: str, beam_size: int) -> tuple[list[str], str]:
    """texts: non-empty sentences/segments, all in `source` (NLLB code). Returns (translations, model name)."""
    name = route(source, target)
    tr, encode, decode = _get(name)
    opus = name.startswith("opus:")
    batch = [encode(t) if opus else [source] + encode(t) for t in texts]
    results = tr.translate_batch(batch, target_prefix=None if opus else [[target]] * len(batch), beam_size=beam_size,
                                 max_decoding_length=MAX_TOKENS, max_batch_size=16, repetition_penalty=1.1)
    out = [decode([t for t in r.hypotheses[0] if t != target]).strip() for r in results]
    return out, name
