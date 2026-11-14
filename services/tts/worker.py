"""Runs in a child process: torch and the models live only here, so stopping the process returns all memory.
Text pipeline follows the HF Spaces patriotyk/styletts2-ukrainian and patriotyk/HolosTTS (app.py): sentence split,
stress marks, IPA (ipa_uk), then the acoustic model. Engines: "styletts2" (multi/single) and "holos" (ONNX)."""

import glob
import json
import os
import re
import time
import functools
from unicodedata import normalize

import lexicon

SAMPLE_RATE = 24000
VOICES_DIR = os.environ.get("VOICES_DIR", "/opt/space/voices")
SINGLE_STYLE = os.environ.get("SINGLE_STYLE", "/opt/space/filatov.pt")
MODEL_REPOS = {
    "multi": "patriotyk/styletts2_ukrainian_multispeaker",
    "single": "patriotyk/styletts2_ukrainian_single",
}
HOLOS_DIR = os.environ.get("HOLOS_DIR", "/opt/holos")  # holos.pt, voices.pt, voices.json, onnx-int8/
HOLOS_BACKEND = os.environ.get("HOLOS_BACKEND", "pytorch")  # pytorch (FP32 reference) | onnx-int8 (faster)
# Engines on offer: HolosTTS by default; StyleTTS2 is opt-in (its preset voices are named after real narrators:
# check the rights before using them publicly). TTS_ENGINES="holos,styletts2" enables both.
ENGINES = tuple(e for e in os.environ.get("TTS_ENGINES", "holos").replace(" ", "").split(",") if e in ("holos", "styletts2")) or ("holos",)
DEFAULT_ENGINE = os.environ.get("TTS_DEFAULT_ENGINE", "holos")
# Favorite voices per engine, in order (the first is the default): "alias=Voice;alias=Voice;...".
# voice="male"/"male2"/"female"/... picks them; omitted = "male".
def _favorites(env: str, default: str) -> dict[str, str]:
    pairs = (item.split("=", 1) for item in os.environ.get(env, default).split(";") if "=" in item)
    return {k.strip().lower(): v.strip() for k, v in pairs}


ALIASES = {
    "styletts2": _favorites("TTS_VOICES_STYLETTS2", "male=Денис Денисенко;male2=Матвій Ніколаєв;female=Марина Панас"),
    "holos": _favorites("TTS_VOICES_HOLOS", "male=Speaker_1;male2=Speaker_36;male3=Speaker_56;female=Speaker_12;female2=Speaker_30"),
}
STRESS = os.environ.get("TTS_STRESS", "accentuk")  # accentuk | word-stress
ACCENTUK_MODE = os.environ.get("ACCENTUK_MODE", "light")  # light (CT2 INT8) | full (FP32 + spaCy TRF)
VERBALIZER = os.environ.get("TTS_VERBALIZER", "accentuk")  # accentuk (span model) | m2m100 (seq2seq, slower)
ACUTE = "\u0301"
LONG = "\u02d0"  # IPA length mark: "++" in text = stressed and lengthened vowel (за++мок -> ˈzaːmɔk)
_holos_model = None
_accentuk = None
_frontend = None
_models: dict = {}
_styles: dict = {}
_stressify = None
_verbalizer = None
NEEDS_VERBALIZING = re.compile(r"\d|\b[A-ZА-ЯІЇЄҐ]{2,}\b|\b[IVX]\b|[№%$€₴]")  # single Latin I/V/X: Roman numeral


_ABC = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"


def _uk_sorted(names) -> list[str]:
    return sorted(names, key=lambda n: [_ABC.find(c) if c in _ABC else 100 + ord(c) for c in n.lower()])


@functools.cache
def _holos_voice_names() -> tuple[str, ...]:
    names = json.load(open(os.path.join(HOLOS_DIR, "voices.json")))  # written at image build from voices.pt
    return tuple(sorted(names, key=lambda n: (not n.startswith("Speaker_"), int(n[8:]) if n[8:].isdigit() else 0, n)))


def voice_names(engine: str = "styletts2") -> list[str]:
    """Preset voices, read from files only (no ML imports: the API process calls this)."""
    if engine == "holos":
        return list(_holos_voice_names())
    return _uk_sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(VOICES_DIR, "*.pt")))


def resolve_voice(engine: str, voice: str | None) -> str:
    """None/"male"/"male2"/"female"/... -> the engine's favorites; otherwise a preset name of that engine."""
    names = voice_names(engine)
    voice = (voice or "male").strip()
    voice = ALIASES.get(engine, {}).get(voice.lower()) or voice
    if voice not in names:
        raise ValueError(f"unknown {engine} voice {voice!r}; see GET /v1/voices")
    return voice


def _cached_first(load):
    """Load from the local HF cache without network calls; go online only if something is missing.
    (Online loads re-validate every file and can hang on the HF CDN even when everything is cached.)"""
    from huggingface_hub import constants

    constants.HF_HUB_OFFLINE = True
    try:
        return load()
    except Exception:
        constants.HF_HUB_OFFLINE = False
        return load()
    finally:
        constants.HF_HUB_OFFLINE = False


DEVICE_FILE = "/tmp/tts-device"  # the device the worker really uses, read by /health (the API process stays torch-free)


def device() -> str:
    """TTS_DEVICE=cuda (GPU image + compose.gpu.yaml) when torch sees a GPU, else cpu."""
    import torch

    return "cuda" if os.environ.get("TTS_DEVICE", "cpu") == "cuda" and torch.cuda.is_available() else "cpu"


def init() -> None:
    import torch

    torch.set_num_threads(int(os.environ.get("TORCH_THREADS", "4")))
    try:
        with open(DEVICE_FILE, "w") as f:
            f.write(device())
    except OSError:
        pass


def _stress():
    global _stressify
    if _stressify is None:
        import stanza
        from ukrainian_word_stress import Stressifier

        if not os.path.isdir(os.path.join(os.environ.get("STANZA_RESOURCES_DIR", ""), "uk", "pos")):
            stanza.download("uk", processors="tokenize,pos,mwt", logging_level="WARN")
        _stressify = Stressifier()
    return _stressify


def _accent_uk(text: str) -> str:
    global _accentuk
    if _accentuk is None:
        from accentuk import Accentor

        _accentuk = Accentor(mode=ACCENTUK_MODE)
    return _accentuk.stress(text).text


_WORD = re.compile(r"[\w'\u0301]+")
_VOWELS = set("аеєиіїоуюяАЕЄИІЇОУЮЯ")


def _keep_user_stress(src: str, out: str) -> str:
    """Words the user marked with "+" keep the user's stress, whatever the stressifier chose."""
    bare = lambda w: w.replace(ACUTE, "").replace(LONG, "").lower()
    marked = {bare(w): w for w in _WORD.findall(src) if ACUTE in w}
    if not marked:
        return out
    return _WORD.sub(lambda m: marked.get(bare(m.group(0)), m.group(0)), out)


def stress(t: str) -> str:
    """Put U+0301 after the stressed vowel of every word; user marks ("+" already turned into U+0301) win."""
    if STRESS == "accentuk":
        out = _keep_user_stress(t, _accent_uk(t.replace(ACUTE, "").replace(LONG, "")))
        # words the lexicon/models left unmarked: the word-stress fork's neural accentor guesses them
        def guess(m):
            w = m.group(0)
            if ACUTE in w or sum(c in _VOWELS for c in w) < 2:
                return w
            return _stress()(w)
        return _WORD.sub(guess, out)
    return _stress()(t)


def _model(name: str):
    if name not in _models:
        import torch
        from styletts2_inference.models import StyleTTS2

        _models[name] = _cached_first(lambda: StyleTTS2(hf_path=MODEL_REPOS[name], device="cpu"))
        if name == "single":
            _styles["single"] = torch.load(SINGLE_STYLE)
    return _models[name]


def _style(model: str, voice: str | None):
    import torch

    if model == "single":
        return _styles["single"]
    if voice not in _styles:
        path = os.path.join(VOICES_DIR, f"{voice}.pt")
        if not os.path.isfile(path):
            raise ValueError(f"unknown voice: {voice}")
        _styles[voice] = torch.load(path)
    return _styles[voice]


def split_to_parts(text: str, group: bool = True, group_chars: int = 20) -> list[str]:
    """Sentence split from the Spaces: break on .?!: followed by a space; parts up to group_chars are merged."""
    text = re.sub(r"(\w+[^.,!:?\-])\n", r"\1. ", text)
    text = text.replace("\n", " ")
    parts, last = [""], len(text) - 1
    for i, s in enumerate(text):
        parts[-1] += s
        if s in ".?!:" and i < last and text[i + 1] == " ":
            if group and len(parts[-1]) <= group_chars:
                continue
            parts.append("")
    return parts


def _verbalize_m2m100(part: str) -> str:
    global _verbalizer
    if _verbalizer is None:
        import ctranslate2
        from huggingface_hub import snapshot_download
        from transformers import M2M100Tokenizer

        path = _cached_first(lambda: snapshot_download("skypro1111/m2m100-ukr-verbalization-ct2",
                                                       allow_patterns=["*.bin", "*.json"]))
        tok = _cached_first(lambda: M2M100Tokenizer.from_pretrained("skypro1111/m2m100-ukr-verbalization"))
        tok.src_lang = "uk"
        _verbalizer = (ctranslate2.Translator(path, device="cpu", compute_type="int8"), tok)
    translator, tok = _verbalizer
    source = tok.convert_ids_to_tokens(tok.encode(part))
    res = translator.translate_batch([source], target_prefix=[[tok.lang_code_to_token["uk"]]],
                                     beam_size=1, num_hypotheses=1)
    return tok.decode(tok.convert_tokens_to_ids(res[0].hypotheses[0][1:]))


def _verbalize_accentuk(part: str) -> str:
    global _frontend
    if _frontend is None:
        from accentuk import Frontend

        _frontend = Frontend(mode=ACCENTUK_MODE)
    return _frontend.prepare(part).text.replace(ACUTE, "")  # stress is placed later, with user marks


def verbalize(text: str) -> str:
    out = []
    for part in split_to_parts(text, group=False):
        part = part.strip()
        if not part:
            continue
        # Verbalizers also paraphrase words and mangle "+" stress marks, so only sentences with digits or
        # abbreviations go through one, with "+" removed and restored on the same words afterwards.
        if not NEEDS_VERBALIZING.search(part):
            out.append(part)
            continue
        stressed = {w.replace("+", ""): w for w in re.findall(r"[\w'ʼ’+]*\+[\w'ʼ’+]*", part)}
        bare = part.replace("+", "")
        done = _verbalize_m2m100(bare) if VERBALIZER == "m2m100" else _verbalize_accentuk(bare)
        if re.search(r"\d", done) and VERBALIZER != "m2m100":
            # digits left over are not spoken at all (IPA drops them): a missing/failed span model must not
            # silence numbers, so fall back to the seq2seq verbalizer for this sentence
            print(f"verbalizer left digits, using m2m100: {done[:80]!r}", flush=True)
            done = _verbalize_m2m100(bare)
        for plain, marked in stressed.items():
            done = re.sub(rf"(?<![\w'ʼ’]){re.escape(plain)}(?![\w'ʼ’])", marked, done)
        out.append(done)
    return " ".join(out)


def _prepare(t: str) -> str:
    from ukrainian_word_stress import StressSymbol

    t = t.strip().replace('"', "")
    t = lexicon.apply(t)  # user stress lexicon (whole words, phrases, regex contexts) before automatic stress
    # "++" = stress plus the IPA length mark, which ipa_uk passes through and both engines' vocabularies know:
    # HolosTTS barely lengthens some stressed vowels in running speech (за́мок sounded like замок).
    t = t.replace("++", StressSymbol.CombiningAcuteAccent + LONG).replace("+", StressSymbol.CombiningAcuteAccent)
    t = normalize("NFKC", t)
    # The stress dictionary only knows the ASCII apostrophe: "пʼятсот"/"п’ятсот" were missed and guessed wrong.
    t = re.sub(r"[ʼ’‘`]", "'", t)
    t = re.sub(r"[᠆‐‑‒–—―⁻₋−⸺⸻]", "-", t)
    if t[-1] not in ".?!:-":
        t += "."
    t = re.sub(r" - ", ": ", t)
    return stress(t)


def _holos():
    """Returns generate(phonemes, voice, speed) -> float32 numpy audio at 24 kHz."""
    global _holos_model
    if _holos_model is None:
        import numpy as np

        if HOLOS_BACKEND == "pytorch":
            import torch
            from holos.models import HolosTTS

            m = HolosTTS(os.path.join(HOLOS_DIR, "holos.pt"), device=device(),
                         voices_path=os.path.join(HOLOS_DIR, "voices.pt"))

            def gen(ps, voice, speed):
                with torch.inference_mode():
                    return m.generate(ps, voice, speed=speed).squeeze().cpu().numpy().astype(np.float32)
        else:
            import onnxruntime as ort
            from holos_onnx import HolosTTS

            so = ort.SessionOptions()
            so.intra_op_num_threads = int(os.environ.get("TORCH_THREADS", "4"))
            so.inter_op_num_threads = 1
            so.add_session_config_entry("session.intra_op.allow_spinning", "0")
            m = HolosTTS(os.path.join(HOLOS_DIR, HOLOS_BACKEND), session_options=so)

            def gen(ps, voice, speed):
                return np.asarray(m.generate(ps, voice, speed=speed), dtype=np.float32).squeeze()
        _holos_model = gen
    return _holos_model


def synthesize(text: str, voice: str | None, speed: float, model: str, pause_ms: int,
               do_verbalize: bool, engine: str = "styletts2") -> tuple[bytes, dict]:
    """Returns (float32 mono PCM at 24 kHz as bytes, info)."""
    import numpy as np
    from ipa_uk import ipa

    started = time.monotonic()
    if do_verbalize:
        text = verbalize(text)
    if engine == "holos":
        holos, voice = _holos(), resolve_voice("holos", voice)
        generate = lambda ps: holos(ps, voice, speed)
        group = 100  # HolosTTS Space merges sentences up to 100 chars
    else:
        import torch

        tts = _model(model)
        voice = None if model == "single" else resolve_voice("styletts2", voice)
        style = _style(model, voice)

        def generate(ps):
            with torch.no_grad():
                return tts(tts.tokenizer.encode(ps), speed=speed, s_prev=style).cpu().numpy().astype(np.float32)
        group = 20
    pause = np.zeros(int(SAMPLE_RATE * pause_ms / 1000), dtype=np.float32)
    chunks, parts = [], []
    for part in split_to_parts(text, group_chars=group):
        if not part.strip():
            continue
        stressed = _prepare(part)
        ps = ipa(stressed)
        if not ps:
            continue
        wav = generate(ps)
        if chunks and len(pause):
            chunks.append(pause)
        chunks.append(wav)
        parts.append(stressed)
    if not chunks:
        raise ValueError("nothing to synthesize")
    audio = np.concatenate(chunks)
    peak = float(np.abs(audio).max())
    if peak > 0.99:
        audio = audio * (0.99 / peak)
    elapsed = time.monotonic() - started
    duration = len(audio) / SAMPLE_RATE
    return audio.tobytes(), {"text": text, "stressed": parts, "engine": engine, "voice": voice or "single",
                             "duration_s": round(duration, 2), "elapsed_s": round(elapsed, 2),
                             "rtf": round(elapsed / duration, 3)}


def warmup(engine: str = DEFAULT_ENGINE) -> bool:
    """Load the engine and the text models with a tiny phrase (the UI calls it when a page opens)."""
    synthesize("Привіт.", None, 1.0, "multi", 0, True, engine)
    return True


def prepare(text: str, do_verbalize: bool) -> dict:
    """Show what the model will read: verbalized text and the stressed sentence parts."""
    if do_verbalize:
        text = verbalize(text)
    return {"text": text, "stressed": [_prepare(p) for p in split_to_parts(text) if p.strip()]}
