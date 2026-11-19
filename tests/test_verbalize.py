"""Verbalizer + stress on real models (CPU, accentuk light from the TTS image): every digit is spoken, "+" between
numbers is "плюс". Slow (~1 min on first load).
docker run --rm -e API_KEY=x -e TTS_DEVICE=cpu -v voxarbor-node_tts-cache:/cache:ro -v $PWD/services/tts:/app:ro \
  -v $PWD/tests/test_verbalize.py:/t.py:ro ghcr.io/voxarbor/node-tts:<tag> python /t.py"""
import sys

sys.path.insert(0, "/app")
import speech
import worker


def say(text: str) -> str:
    return worker.verbalize(speech.english(text)[0])


for src, want in [("Модель R1 вийшла.", "один"), ("Кубик 3x3 і 6x6.", "три на три"), ("Кубик 3x3 і 6x6.", "шість на шість"),
                  ("Музика 20s та 90s.", "двадцяті"), ("Музика 20s та 90s.", "дев'яності"),
                  ("Купив 11 Air вчора.", "одинадцять"), ("Мережа P2P і ще P2P.", "ту"),
                  ("Маємо 13 + 14 разом.", "тринадцять плюс чотирнадцять"), ("Маємо 13+14 разом.", "тринадцять плюс чотирнадцять")]:
    got = say(src)
    assert want in got and not any(c.isdigit() for c in got), (src, got)
    print("ok", repr(src), "->", got)
got = say("Мережа P2P і ще P2P.")
assert got.count("пі") == 4 and "пі пі" not in got, got
got = say("Купив 11 Air вчора.")
assert "ер" in got.split() or "е+р" in got.split(), got  # the name stays a word of its own
stressed = worker.prepare("Маємо 13 + 14 разом. Треба йти.", True)["stressed"]
assert "плюс" in " ".join(stressed) and "й" + worker.ACUTE not in " ".join(stressed), stressed
print("VERBALIZE OK")
