"""Runs in a child process: sherpa-onnx speaker diarization (pyannote segmentation-3.0 + a speaker-embedding model,
both ONNX, CPU) and per-speaker pitch for gender. Stopping the process frees the models."""

from __future__ import annotations

import os

import numpy as np

SR = 16000
SEG_MODEL = os.environ.get("DIARIZE_SEG_MODEL", "/models/segmentation/model.onnx")
EMB_MODEL = os.environ.get("DIARIZE_EMB_MODEL", "/models/embedding.onnx")
THREADS = (lambda n: max(1, n // 2) if os.environ.get("NODE_MODE") == "gentle" else n)(int(os.environ.get("DIARIZE_THREADS", "4")))  # NODE_MODE=gentle: half
THRESHOLD = float(os.environ.get("DIARIZE_THRESHOLD", "0.5"))
FEMALE_HZ, MALE_HZ = 165.0, 150.0  # pitch fallback: median F0 above / below -> female / male
MERGE_SIM = float(os.environ.get("DIARIZE_MERGE_SIM", "0.88"))  # same-gender clusters this similar = one person
# Gender from the speaker embedding (timbre), not pitch: logistic regression on WeSpeaker embeddings of real voices
# (tools/gender_train_real.py, since 0.12.1: Common Voice 19 uk/en/ru/pl + LibriSpeech, 1850 speakers, Platt-
# calibrated: held-out speakers 94.7 %, ECE 0.022; our videos 7/7 speakers incl. men at f0 ~210-230 Hz). Pitch alone
# fails on energetic male let's-players.
_GENDER = np.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gender_lr.npz"))
GENDER_MODEL = "wespeaker-lr-real-1"  # gender_lr.npz: real voices (CV19 uk/en/ru/pl + LibriSpeech), Platt-calibrated
_diar: dict = {}
_emb = None
# Engine: sherpa-onnx (CPU, fast) or pyannote community-1 (better speaker counting, exclusive turns; meant for a GPU,
# image node-diarize-cuda). Speaker embeddings for gender and cross-window linking stay WeSpeaker either way,
# so the response and sessions do not change.
ENGINE = os.environ.get("DIARIZE_ENGINE", "sherpa-onnx")
PYANNOTE_DIR = os.environ.get("DIARIZE_PYANNOTE_DIR", "/models/community-1")
_pyannote = None


def device() -> str:
    if ENGINE != "pyannote":
        return "cpu"
    import torch

    return "cuda" if os.environ.get("DIARIZE_DEVICE", "cpu") == "cuda" and torch.cuda.is_available() else "cpu"


def _pyannote_pipeline():
    global _pyannote
    if _pyannote is None:
        import torch
        from pyannote.audio import Pipeline

        torch.set_num_threads(THREADS)
        _pyannote = Pipeline.from_pretrained(PYANNOTE_DIR)  # local weights: no token, no network
        _pyannote.to(torch.device(device()))
    return _pyannote


def _pyannote_turns(audio, num_speakers: int, min_speakers: int, max_speakers: int) -> list:
    import torch

    kw = {}
    if num_speakers:
        kw["num_speakers"] = num_speakers
    else:
        if min_speakers:
            kw["min_speakers"] = min_speakers
        if max_speakers:
            kw["max_speakers"] = max_speakers
    try:
        out = _pyannote_pipeline()({"waveform": torch.from_numpy(audio)[None], "sample_rate": SR}, **kw)
    finally:
        if device() == "cuda":  # give the cached activations back: torch 2.11 kept ~4.1 GB until IDLE_TTL (2.8: ~2.6)
            torch.cuda.empty_cache()
    ids: dict[str, int] = {}
    return [(s.start, s.end, ids.setdefault(lab, len(ids)))
            for s, _, lab in out.exclusive_speaker_diarization.itertracks(yield_label=True) if s.end - s.start > 0.05]


def init() -> None:
    import sherpa_onnx  # noqa: F401  (loaded in the worker only)

    try:
        with open("/tmp/diarize-device", "w") as f:  # read by /health (the API process stays torch-free)
            f.write(device())
    except OSError:
        pass


def _diarizer(num_clusters: int, threshold: float):
    import sherpa_onnx

    key = (num_clusters, threshold)
    if key not in _diar:
        cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=SEG_MODEL),
                num_threads=THREADS),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMB_MODEL, num_threads=THREADS),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_clusters, threshold=threshold),
            min_duration_on=0.3, min_duration_off=0.5)
        if not cfg.validate():
            raise RuntimeError("bad diarization config")
        if len(_diar) > 4:
            _diar.clear()
        _diar[key] = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    return _diar[key]


def _embedding(audio: np.ndarray) -> list[float]:
    """Speaker embedding (L2-normalised) of a speaker's audio, for linking speakers across windows."""
    global _emb
    import sherpa_onnx

    if _emb is None:
        _emb = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMB_MODEL, num_threads=THREADS))
    st = _emb.create_stream()
    st.accept_waveform(SR, audio)
    st.input_finished()
    v = np.array(_emb.compute(st), dtype=np.float32)
    return (v / (np.linalg.norm(v) + 1e-9)).tolist()


def f0_frames(x: np.ndarray) -> list[float]:
    """F0 of voiced 40 ms frames (normalised autocorrelation, 70-400 Hz)."""
    fl, hop, lo, hi = 640, 320, SR // 400, SR // 70
    out = []
    for s in range(0, len(x) - fl, hop):
        fr = x[s:s + fl] - x[s:s + fl].mean()
        if np.sqrt((fr ** 2).mean()) < 0.01:
            continue
        ac = np.correlate(fr, fr, "full")[fl - 1:]
        ac /= ac[0] + 1e-9
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] > 0.5:
            out.append(SR / lag)
    return out


def p_female(embedding) -> float:
    z = float(np.asarray(embedding, dtype=np.float32) @ _GENDER["w"] + float(_GENDER["b"]))
    return 1.0 / (1.0 + np.exp(-z))


def gender(pf: float | None, f0s: list[float]) -> tuple[str, float]:
    """(gender, confidence 0..1): the embedding classifier decides; when it is unsure (0.4..0.6) the median pitch
    may, with low confidence; under ~1.3 s of voiced speech and no embedding: unknown."""
    if pf is not None and (pf >= 0.6 or pf <= 0.4):
        return ("female" if pf >= 0.6 else "male"), round(abs(pf - 0.5) * 2, 2)
    if len(f0s) >= 40:
        m = float(np.median(f0s))
        if m >= FEMALE_HZ or m <= MALE_HZ:
            return ("female" if m >= FEMALE_HZ else "male"), 0.2
    return "unknown", 0.0


def _merge(speakers: dict, turns: list) -> tuple[dict, list]:
    """Clustering sometimes splits one voice (music, emotion changes): merge same-gender speakers whose embeddings
    are at least MERGE_SIM similar (different voices measured: p99 0.85), most similar pair first."""
    while True:
        best = None
        ids = sorted(speakers)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                da, db = speakers[a], speakers[b]
                if (da["p_female"] >= 0.5) != (db["p_female"] >= 0.5):
                    continue
                sim = float(np.dot(da["embedding"], db["embedding"]))
                if sim >= MERGE_SIM and (best is None or sim > best[0]):
                    best = (sim, a, b)
        if best is None:
            return speakers, turns
        _, a, b = best
        da, db = speakers[a], speakers.pop(b)
        wa, wb = min(da["total_s"], 30.0), min(db["total_s"], 30.0)
        e = (wa * np.array(da["embedding"]) + wb * np.array(db["embedding"])) / (wa + wb)
        da["embedding"] = (e / (np.linalg.norm(e) + 1e-9)).tolist()
        da["p_female"] = p_female(da["embedding"])
        da["f0"] = (da["f0"] + db["f0"])[:6000]
        da["total_s"] = round(da["total_s"] + db["total_s"], 2)
        turns = [(s, e_, a if sp == b else sp) for s, e_, sp in turns]


def diarize(path: str, offset: float, duration: float | None, num_speakers: int, min_speakers: int,
            max_speakers: int, threshold: float | None) -> dict:
    """16 kHz mono float32 file -> {turns [{start, end, speaker}] (absolute s), speakers {local id: {total_s,
    f0 [..voiced frames, at most ~60 s], embedding}}, audio_s}."""
    import soundfile as sf

    info = sf.info(path)
    start = int(offset * SR)
    frames = -1 if duration is None else int(duration * SR)
    audio, _ = sf.read(path, start=start, frames=frames, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(1)
    if len(audio) < SR // 2:
        return {"turns": [], "speakers": {}, "audio_s": info.frames / SR}
    if ENGINE == "pyannote":
        turns = _pyannote_turns(audio, num_speakers, min_speakers, max_speakers)
    else:
        thr = THRESHOLD if threshold is None else threshold
        res = _diarizer(num_speakers or -1, thr).process(audio).sort_by_start_time()
        turns = [(r.start, r.end, r.speaker) for r in res]
        n = len({t[2] for t in turns})
        if not num_speakers and ((max_speakers and n > max_speakers) or (min_speakers and 0 < n < min_speakers)):
            k = max_speakers if max_speakers and n > max_speakers else min_speakers
            res = _diarizer(k, thr).process(audio).sort_by_start_time()
            turns = [(r.start, r.end, r.speaker) for r in res]
    speakers: dict[int, dict] = {}
    for s, e, sp in turns:
        d = speakers.setdefault(sp, {"total_s": 0.0, "chunks": []})
        d["total_s"] += e - s
        d["chunks"].append(audio[int(s * SR):int(e * SR)])
    for sp, d in speakers.items():
        voice = np.concatenate(d.pop("chunks"))[: SR * 60]  # up to a minute per speaker is plenty
        d["f0"] = f0_frames(voice)
        d["embedding"] = _embedding(voice[: SR * 30])
        d["p_female"] = p_female(d["embedding"])
        d["total_s"] = round(d["total_s"], 2)
    if ENGINE != "pyannote":  # community-1 clusters well itself; merging could glue different hosts again
        speakers, turns = _merge(speakers, turns)
    return {"turns": [(round(s + offset, 2), round(e + offset, 2), sp) for s, e, sp in turns],
            "speakers": speakers, "audio_s": info.frames / SR}
