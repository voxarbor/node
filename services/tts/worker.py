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
HOLOS_DIR = os.environ.get("HOLOS_DIR", "/opt/holos")  # holos.pt, voices.pt, voices.json, voice_encoder.pt
# Engines on offer: HolosTTS by default; StyleTTS2 is opt-in (its preset voices are named after real narrators:
# check the rights before using them publicly). TTS_ENGINES="holos,styletts2" enables both.
# OmniVoice (GPU nodes, compose profile omnivoice): synthesis by the tts-omnivoice service (omnivoice.cpp tts-server),
# enabled with TTS_ENGINES=holos,omnivoice and OMNIVOICE_URL. Weights CC-BY-NC-4.0 (NOTICE).
OMNIVOICE_URL = os.environ.get("OMNIVOICE_URL", "").rstrip("/")
OMNIVOICE_LANG = os.environ.get("OMNIVOICE_LANG", "Ukrainian")
_KNOWN = ("holos", "styletts2") + (("omnivoice",) if OMNIVOICE_URL else ())
ENGINES = tuple(e for e in os.environ.get("TTS_ENGINES", "holos").replace(" ", "").split(",") if e in _KNOWN) or ("holos",)
DEFAULT_ENGINE = os.environ.get("TTS_DEFAULT_ENGINE", "holos")
# Favorite voices per engine, in order (the first is the default): "alias=Voice;alias=Voice;...".
# voice="male"/"male2"/"female"/... picks them; omitted = "male".
def _favorites(env: str, default: str) -> dict[str, str]:
    pairs = (item.split("=", 1) for item in os.environ.get(env, default).split(";") if "=" in item)
    return {k.strip().lower(): v.strip() for k, v in pairs}


ALIASES = {
    "styletts2": _favorites("TTS_VOICES_STYLETTS2", "male=Денис Денисенко;male2=Матвій Ніколаєв;female=Марина Панас"),
    "holos": _favorites("TTS_VOICES_HOLOS", "male=Speaker_1;male2=Speaker_36;male3=Speaker_56;female=Speaker_12;female2=Speaker_30"),
    "omnivoice": {"male": "male", "female": "female"},
}
# OmniVoice voices without a recording: "voice design" by attributes (its README: gender, age, pitch, ...); "auto"
# lets the model pick. Custom voices (a 3-10 s recording + its text) are "custom:<id>" with model omnivoice.
OMNI_DESIGN = {"male": "male, middle-aged, moderate pitch", "female": "female, young adult, moderate pitch", "auto": ""}
# What /v1/audio/speech "settings" accepts for model omnivoice (JSON Schema, published in /capabilities models)
OMNI_SETTINGS = {"type": "object", "additionalProperties": False, "properties": {
    "num_step": {"type": "integer", "minimum": 4, "maximum": 64, "default": 32,
                 "description": "MaskGIT steps: fewer is faster, more is cleaner"},
    "guidance_scale": {"type": "number", "minimum": 0, "maximum": 5, "default": 2.0},
    "instructions": {"type": "string", "maxLength": 300,
                     "description": "voice design in English: gender, age, pitch, accent, whisper (comma-separated)"},
    "seed": {"type": "integer", "minimum": -1, "maximum": 2147483647, "default": -1,
             "description": "-1: random; the same seed gives the same audio"},
    "speed": {"type": "number", "minimum": 0.5, "maximum": 1.5, "default": 1.0,
              "description": "tempo (time-stretch after synthesis); overrides the request's speed"}}}
OMNI_SERVER_KEYS = ("num_step", "guidance_scale", "instructions", "seed")  # what tts-server itself takes
STRESS = os.environ.get("TTS_STRESS", "accentuk")  # accentuk | word-stress
ACCENTUK_MODE = os.environ.get("ACCENTUK_MODE", "light")  # light (CT2 INT8) | full (FP32 + spaCy TRF)
VERBALIZER = os.environ.get("TTS_VERBALIZER", "accentuk")  # accentuk (span model) | m2m100 (seq2seq, slower)
ACUTE = "\u0301"
PAUSE_CHARS = "[\ue00a-\ue1f4]"  # explicit pauses, see speech.PAUSE_RE (10 ms steps from U+E000)
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
    if engine == "omnivoice":
        return list(OMNI_DESIGN)
    if engine == "holos":
        return list(_holos_voice_names())
    return _uk_sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(VOICES_DIR, "*.pt")))


# Custom Holos voices (zero-shot, from a reference recording): one JSON file per voice {id, name, gender, vector[256],
# sha256, consent_ref, created}; installed by the coordinator with PUT /v1/voices/custom/{id}. Requests use "custom:<id>".
CUSTOM_DIR = os.environ.get("TTS_CUSTOM_VOICES", "/cache/custom-voices")
VECTOR_DIM = 256


def custom_voices() -> dict[str, dict]:
    """{id: voice record}, read from files only (no ML imports: the API process calls this)."""
    out = {}
    for p in sorted(glob.glob(os.path.join(CUSTOM_DIR, "*.json"))):
        try:
            v = json.load(open(p, encoding="utf-8"))
            out[v["id"]] = v
        except (OSError, ValueError, KeyError):
            continue
    return out


def custom_vector(voice_id: str) -> list[float]:
    try:
        return json.load(open(os.path.join(CUSTOM_DIR, f"{voice_id}.json"), encoding="utf-8"))["vector"]
    except (OSError, KeyError):
        raise ValueError(f"unknown custom voice {voice_id!r}; see GET /v1/voices") from None


def custom_model(voice_id: str) -> str | None:
    """the model a custom voice belongs to: "holos" (a vector) or "omnivoice" (a recording); None if there is none"""
    try:
        return json.load(open(os.path.join(CUSTOM_DIR, f"{voice_id}.json"), encoding="utf-8")).get("model", "holos")
    except (OSError, ValueError):
        return None


def resolve_voice(engine: str, voice: str | None) -> str:
    """None/"male"/"male2"/"female"/... -> the engine's favorites; "custom:<id>" (Holos); else a preset name."""
    voice = (voice or "male").strip()
    if voice.startswith("custom:"):
        owner = custom_model(voice[7:])
        if owner is None:
            raise ValueError(f"unknown custom voice {voice[7:]!r}; see GET /v1/voices")
        if owner != engine:
            raise ValueError(f"custom voice {voice[7:]!r} belongs to model {owner}: use model {owner}")
        return voice
    names = voice_names(engine)
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


CANCEL_DIR = "/tmp/tts-cancel"  # the API process drops a file named after a call's token when its client is gone
_call_token: str | None = None


class Cancelled(Exception):
    """The client of this call went away: stop instead of finishing work nobody will receive."""


def call(token: str, fn, *args):
    """Run fn(*args) in the worker as the call `token`; synthesize() checks cancelled() between sentence parts."""
    global _call_token
    _call_token = token
    try:
        if cancelled():
            raise Cancelled(token)
        return fn(*args)
    finally:
        _call_token = None
        try:
            os.remove(os.path.join(CANCEL_DIR, token))
        except OSError:
            pass


def cancelled() -> bool:
    return bool(_call_token) and os.path.exists(os.path.join(CANCEL_DIR, _call_token))


DEVICE_FILE = "/tmp/tts-device"  # the device the worker really uses, read by /health (the API process stays torch-free)


def device() -> str:
    """TTS_DEVICE=cuda (GPU image + compose.gpu.yaml) when torch sees a GPU, else cpu."""
    import torch

    return "cuda" if os.environ.get("TTS_DEVICE", "cpu") == "cuda" and torch.cuda.is_available() else "cpu"


def init() -> None:
    import torch

    torch.set_num_threads((lambda n: max(1, n // 2) if os.environ.get("NODE_MODE") == "gentle" else n)(int(os.environ.get("TORCH_THREADS", "4"))))  # NODE_MODE=gentle: half
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
    words = _WORD.findall(src)
    if not any(ACUTE in w for w in words):
        return out
    if len(_WORD.findall(out)) == len(words):  # by position: one word marked twice differently keeps both marks
        it = iter(words)

        def pos(m):
            w = next(it)
            return w if ACUTE in w and bare(w) == bare(m.group(0)) else m.group(0)
        return _WORD.sub(pos, out)
    marked = {bare(w): w for w in words if ACUTE in w}  # the stressifier changed the words: match by spelling
    return _WORD.sub(lambda m: marked.get(bare(m.group(0)), m.group(0)), out)


_NOT_ON_VOWEL = re.compile(f"(?<![аеєиіїоуюяАЕЄИІЇОУЮЯ]){ACUTE}")  # accentuk sometimes stresses й ("й́ти")


def stress(t: str) -> str:
    """Put U+0301 after the stressed vowel of every word; user marks ("+" already turned into U+0301) win."""
    if STRESS == "accentuk":
        out = _NOT_ON_VOWEL.sub("", _keep_user_stress(t, _accent_uk(t.replace(ACUTE, "").replace(LONG, ""))))
        # words the lexicon/models left unmarked: the word-stress fork's neural accentor guesses them
        def guess(m):
            w = m.group(0)
            if ACUTE in w or sum(c in _VOWELS for c in w) < 2:
                return w
            return _stress()(w)
        return _WORD.sub(guess, out)
    return _NOT_ON_VOWEL.sub("", _stress()(t))


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


_PLUS = re.compile(r"(?<=\d) ?\+ ?(?=\d)|(?<!\S)\+(?!\S)")  # "13 + 14", "13+14": a sum, not a stress mark
_NUMBER = r"\d+(?:[  ]\d{3})*"
_SPOKEN = re.compile(_NUMBER + r"(?:[.,:]\d+)*(?:-[\w'ʼ’]+)?|[\w'ʼ’]+(?:-[\w'ʼ’]+)*")  # "1 000", "20-х": one each


def _plus(t: str) -> str:
    return _PLUS.sub(" плюс ", t).replace("  ", " ")


def verbalize(text: str) -> str:
    verb = _verbalize_m2m100 if VERBALIZER == "m2m100" else _verbalize_accentuk
    out = []
    for part in split_to_parts(_plus(text), group=False):
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
        # "90-ті" (speech.english: 90s): the verbalizer only knows "90-х", read "дев'яностих" -> "дев'яності"
        bare = re.sub(r"(?<!\w)(\d+)-ті(?!\w)", lambda m: re.sub(r"их$", "і", verb(f"{m.group(1)}-х").strip(" .")), bare)
        done = verb(bare)
        if len(_SPOKEN.findall(done)) < len(_SPOKEN.findall(bare)):
            # a number swallowed or glued to the next word ("ар 1" -> "ар", "11 ер" -> "одинадцятогоер"):
            # read every number as a plain cardinal instead
            print(f"verbalizer lost a word, numbers as cardinals: {done[:80]!r}", flush=True)
            bare = re.sub(_NUMBER, lambda m: verb(m.group(0)).strip(" ."), bare)
            done = verb(bare)
        if re.search(r"\d", done) and VERBALIZER != "m2m100":
            # digits left over are not spoken at all (IPA drops them): a missing/failed span model must not
            # silence numbers, so fall back to the seq2seq verbalizer for this sentence
            print(f"verbalizer left digits, using m2m100: {done[:80]!r}", flush=True)
            done = _verbalize_m2m100(bare)
        for plain, marked in stressed.items():
            done = re.sub(rf"(?<![\w'ʼ’]){re.escape(plain)}(?![\w'ʼ’])", marked, done)
        out.append(done)
    return " ".join(out)


def _prepare(t: str, use_lexicon: bool = True) -> str:
    from ukrainian_word_stress import StressSymbol

    t = t.strip().replace('"', "")
    t = _plus(t)  # without the verbalizer too: "+" between numbers is not a stress mark
    t = lexicon.apply(t) if use_lexicon else t  # user stress lexicon (whole words, phrases, regex contexts) before automatic stress
    # "++" = stress plus the IPA length mark, which ipa_uk passes through and both engines' vocabularies know:
    # HolosTTS barely lengthens some stressed vowels in running speech (за́мок sounded like замок).
    t = t.replace("++", StressSymbol.CombiningAcuteAccent + LONG).replace("+", StressSymbol.CombiningAcuteAccent)
    t = normalize("NFKC", t)
    # The stress dictionary only knows the ASCII apostrophe: "пʼятсот"/"п’ятсот" were missed and guessed wrong.
    t = re.sub(r"[ʼ’‘`]", "'", t)
    t = re.sub(r"[᠆‐‑‒–—―⁻₋−⸺⸻]", "-", t)
    if t[-1] not in ".?!:-,;…":  # a piece cut by a pause ends with "," (continuation), see synthesize
        t += "."
    t = re.sub(r" - ", ": ", t)
    return stress(t)


def _holos():
    """Returns generate(phonemes, voice, speed) -> float32 numpy audio at 24 kHz."""
    global _holos_model
    if _holos_model is None:
        import numpy as np

        import torch
        from holos.models import HolosTTS

        # PyTorch FP32 only (owner: quality first; the ONNX/INT8 builds were removed from the image in 0.10.2)
        m = HolosTTS(os.path.join(HOLOS_DIR, "holos.pt"), device=device(),
                     voices_path=os.path.join(HOLOS_DIR, "voices.pt"))

        def gen(ps, voice, speed):
            if voice.startswith("custom:"):
                voice = torch.tensor(custom_vector(voice[7:]), dtype=torch.float32)
            with torch.inference_mode():
                return m.generate(ps, voice, speed=speed).squeeze().cpu().numpy().astype(np.float32)
        _holos_model = gen
    return _holos_model


def _omni_call(path: str, body: dict | None = None, method: str = "POST", timeout: float = 600) -> bytes:
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(OMNIVOICE_URL + path, data, {"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def omni_register(voice_id: str) -> None:
    """(Re)registers a custom OmniVoice voice on tts-omnivoice: its registry lives only in RAM (lost on a restart)."""
    import base64

    rec = json.load(open(os.path.join(CUSTOM_DIR, f"{voice_id}.json"), encoding="utf-8"))
    wav = open(os.path.join(CUSTOM_DIR, f"{voice_id}.wav"), "rb").read()
    _omni_call("/v1/audio/voices", {"name": voice_id, "ref_text": rec["ref_text"],
                                    "wav_b64": base64.b64encode(wav).decode()}, timeout=120)


def omni_unregister(voice_id: str) -> None:
    import urllib.parse

    try:
        _omni_call(f"/v1/audio/voices/{urllib.parse.quote(voice_id)}", method="DELETE", timeout=10)
    except Exception:
        pass  # not registered (or the service is down): nothing to drop


def _omni_speak(text: str, voice: str, settings: dict | None):
    """One text piece through tts-omnivoice -> float32 mono PCM at 24 kHz."""
    import io
    import urllib.error

    import numpy as np
    import soundfile as sf

    body = {"input": text, "language": OMNIVOICE_LANG, "response_format": "wav"}
    name = voice[7:] if voice.startswith("custom:") else None
    if name:
        body["voice"] = name
    elif OMNI_DESIGN.get(voice):
        body["instructions"] = OMNI_DESIGN[voice]
    for k, v in (settings or {}).items():
        if k in OMNI_SERVER_KEYS and v not in (None, ""):
            body[k] = v
    for attempt in (1, 2):
        try:
            data = _omni_call("/v1/audio/speech", body)
            break
        except urllib.error.HTTPError as exc:
            msg = exc.read().decode(errors="replace")
            if name and attempt == 1 and exc.code == 400 and "unknown voice" in msg:
                omni_register(name)  # tts-omnivoice restarted since: register again, then retry once
                continue
            raise RuntimeError(f"omnivoice: HTTP {exc.code}: {msg[:200]}") from None
    audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE:
        import librosa

        audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
    return np.asarray(audio, dtype=np.float32)


def synthesize(text: str, voice: str | None, speed: float, model: str, pause_ms: int,
               do_verbalize: bool, engine: str = "styletts2", use_lexicon: bool = True,
               settings: dict | None = None) -> tuple[bytes, dict]:
    """Returns (float32 mono PCM at 24 kHz as bytes, info)."""
    import numpy as np
    from ipa_uk import ipa

    started = time.monotonic()
    omni = engine == "omnivoice"
    if omni:  # no stress marks or IPA: OmniVoice reads plain text (numbers spelled out by the verbalizer)
        voice = resolve_voice("omnivoice", voice)
        speed = float((settings or {}).get("speed", speed))

        def generate(ps):
            wav = _omni_speak(ps, voice, settings)
            if speed != 1.0 and len(wav):
                import librosa

                wav = librosa.effects.time_stretch(wav, rate=speed).astype(np.float32)
            return wav
        group = 10 ** 6  # one request per piece: the server splits long text itself
    elif engine == "holos":
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
    silence = lambda ms: np.zeros(int(SAMPLE_RATE * ms / 1000), dtype=np.float32)
    chunks, parts, said, explicit = [], [], [], 0
    # explicit pause markup (private characters from speech.normalize) is cut out before the verbalizer sees it
    tokens = re.split(f"({PAUSE_CHARS})", text)
    for n, piece in enumerate(tokens):
        if re.fullmatch(PAUSE_CHARS, piece):
            explicit += (ord(piece) - 0xE000) * 10
            continue
        if not piece.strip(" ,.;:—–-\n"):
            continue
        piece = _before_pause(piece, tokens, n)
        if do_verbalize:
            piece = verbalize(piece)
        said.append(piece)
        for part in split_to_parts(piece, group_chars=group):
            if cancelled():
                raise Cancelled("client gone")
            if not part.strip():
                continue
            stressed = re.sub(r"\++", "", part) if omni else _prepare(part, use_lexicon)
            ps = stressed.strip() if omni else ipa(stressed)
            if not ps:
                continue
            wav = generate(ps)
            if explicit:
                chunks.append(silence(explicit))
                explicit = 0
            elif chunks and len(pause):
                chunks.append(pause)
            chunks.append(wav)
            parts.append(stressed)
    if not parts:
        raise ValueError("nothing to synthesize")
    if explicit:
        chunks.append(silence(explicit))
    text = " ".join(said)
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


def _before_pause(piece: str, tokens: list[str], n: int) -> str:
    """Text right before an explicit pause without its own punctuation is read as unfinished (comma), not as the
    end of a sentence."""
    piece = piece.rstrip()
    if n + 1 < len(tokens) and re.fullmatch(PAUSE_CHARS, tokens[n + 1]) and piece and piece[-1] not in ".?!:;,…-—":
        piece += ","
    return piece


def prepare(text: str, do_verbalize: bool, use_lexicon: bool = True) -> dict:
    """Show what the model will read: verbalized text and the stressed sentence parts; explicit pauses are shown as
    "{пауза 0.8}" both in the text and as items of their own in "stressed"."""
    shown, stressed = [], []
    tokens = re.split(f"({PAUSE_CHARS})", text)
    for n, piece in enumerate(tokens):
        if re.fullmatch(PAUSE_CHARS, piece):
            mark = "{пауза %s}" % f"{(ord(piece) - 0xE000) / 100:g}"
            shown.append(mark)
            stressed.append(mark)
            continue
        if not piece.strip(" ,.;:—–-\n"):
            continue
        piece = _before_pause(piece, tokens, n)
        if do_verbalize:
            piece = verbalize(piece)
        shown.append(piece.strip())
        stressed += [_prepare(p, use_lexicon) for p in split_to_parts(piece) if p.strip()]
    return {"text": " ".join(shown), "stressed": stressed}


_encoder = None


def encode_voice(wav_path: str) -> dict:
    """Reference recording (24 kHz mono WAV) -> Holos voice vector + quality warnings. Runs in the worker process."""
    global _encoder
    import numpy as np
    import soundfile as sf
    import torch
    from holos.models import VoiceEncoder

    if _encoder is None:
        _encoder = VoiceEncoder(os.path.join(HOLOS_DIR, "voice_encoder.pt"), device=device())
    x, sr = sf.read(wav_path, dtype="float32")
    if x.ndim > 1:
        x = x.mean(1)
    dur = len(x) / sr
    warnings = []
    if dur < 10:
        warnings.append({"code": "short", "detail": f"{dur:.0f} s: 15-30 s of continuous speech gives a closer voice"})
    if dur > 90:
        warnings.append({"code": "long", "detail": "only the first 90 s are used"})
        x = x[: int(90 * sr)]
    peak = float(np.abs(x).max()) if len(x) else 0.0
    clip = float((np.abs(x) > 0.985).mean()) if len(x) else 0.0
    if clip > 0.001:
        warnings.append({"code": "clipping", "detail": f"{clip:.1%} of samples are clipped: record quieter"})
    frame = int(0.03 * sr)
    n = len(x) // frame
    rms_db = 20 * np.log10(np.sqrt((x[: n * frame].reshape(n, frame) ** 2).mean(1)) + 1e-9) if n else np.array([-120.0])
    loud = float(np.percentile(rms_db, 90))
    if loud < -35:
        warnings.append({"code": "quiet", "detail": f"speech level {loud:.0f} dBFS: record louder or closer"})
    snr = float(np.percentile(rms_db, 90) - np.percentile(rms_db, 10))
    if snr < 20:
        warnings.append({"code": "noisy", "detail": f"speech/background ~{snr:.0f} dB: background noise or music"})
    voiced = rms_db > (np.percentile(rms_db, 90) - 25)
    if n and voiced.mean() < 0.4:
        warnings.append({"code": "little_speech", "detail": f"speech in {voiced.mean():.0%} of the recording"})

    def enc(seg):  # the encoder reads files (it resamples and builds the mel itself)
        tmp = wav_path + ".part.wav"
        sf.write(tmp, seg, sr)
        try:
            return _encoder(tmp)
        finally:
            os.remove(tmp)

    with torch.inference_mode():
        vec = enc(x).squeeze().float().cpu()
        speakers = None
        if dur >= 15:  # several speakers: the parts of one recording should give close vectors
            k = 3
            parts = [enc(x[i * len(x) // k:(i + 1) * len(x) // k]).squeeze().float().cpu()[:128] for i in range(k)]
            sims = [float(torch.nn.functional.cosine_similarity(a, b, dim=0)) for i, a in enumerate(parts) for b in parts[i + 1:]]
            speakers = round(min(sims), 3)
            if speakers < SPEAKER_SIM_MIN:
                warnings.append({"code": "several_speakers",
                                 "detail": f"parts of the recording sound like different voices (similarity {speakers})"})
    return {"vector": [round(float(v), 6) for v in vec.tolist()], "duration_s": round(dur, 2),
            "peak": round(peak, 3), "speech_level_db": round(loud, 1), "snr_db": round(snr, 1),
            "part_similarity": speakers, "warnings": warnings}


SPEAKER_SIM_MIN = float(os.environ.get("TTS_SPEAKER_SIM_MIN", "0.80"))  # one voice ~0.98, two voices ~0.40 (2026-11-14)
