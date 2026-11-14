"""Self-check of the TTS text pipeline (no models): Markdown cleanup, English names, lexicon name rules, segments.
docker run --rm -e API_KEY=x -e TTS_LEXICON=/tmp/lex.txt -v $PWD/services/tts:/app:ro \
  -v $PWD/tests/test_speech.py:/t.py:ro ghcr.io/voxarbor/node-tts:<tag> python /t.py"""
import os
import sys

sys.path.insert(0, "/app")
import lexicon
import speech

# Markdown / URLs
md = """# Вступ

Це **важливо**: див. [документацію](https://example.com/docs) або https://www.github.com/x/y.

- перший пункт
- другий пункт

```python
print("код не читаємо")
```

| A | B |
|---|---|
| один | два |
"""
c = speech.clean(md)
assert c.startswith("Вступ.\n"), c
assert "**" not in c and "](" not in c and "print" not in c and "|" not in c, c
assert "документацію" in c and "github.com" in c and "https" not in c, c
assert "перший пункт" in c and "один, два" in c, c
print("clean ok")

# snake_case and math stay intact
assert speech.clean("змінна my_var_name і 2*3*4") == "змінна my_var_name і 2*3*4", speech.clean("змінна my_var_name і 2*3*4")

# English names through espeak-ng
out, names = speech.english("Відкрий GitHub і CleanYourTV, потім API.")
assert "GitHub" in names and "+" in names["GitHub"], names
assert not any("a" <= ch.lower() <= "z" for ch in out), out
print("english ok:", out)

# lexicon name rules win over espeak
lexicon.write_text("GitHub => ґі+тхаб\nVisual Studio Code => ві+жуал сту+діо ко+уд\nнаприкінці => наприкі+нці\n")
out, names = speech.english("У Visual Studio Code відкрий GitHub.")
assert "ві+жуал сту+діо ко+уд" in out and "ґі+тхаб" in out, out
assert speech.say_name("github") == "ґі+тхаб"
try:
    lexicon.parse_rule("GitHub => GitHu+b")
    raise AssertionError("latin right side accepted")
except lexicon.RuleError:
    pass
assert lexicon.apply("наприкінці GitHub") == "наприкі+нці GitHub"  # name rules are not applied to Cyrillic text
print("lexicon names ok")

# segments: first short, paragraphs get longer pauses, hard-wrapped lines are joined
text = ("Перше речення тут. Друге речення трохи довше\n"
        "за перше, і ще одне. " + "Ще речення для довжини абзацу. " * 12 +
        "\n\nДругий абзац починається тут.")
segs, _ = speech.plan(text, pause_ms=150)
assert len(segs[0]["text"]) <= speech.FIRST_CHARS, segs[0]
assert all(len(s["text"]) <= speech.SEG_CHARS for s in segs), [len(s["text"]) for s in segs]
assert any("довше за перше" in s["text"] for s in segs), [s["text"] for s in segs]  # hard wrap joined
para_end = [s for s in segs if s["pause_after_ms"] == 550]
assert len(para_end) == 1 and segs[-1]["pause_after_ms"] == 0, [s["pause_after_ms"] for s in segs]
assert segs[-1]["text"] == "Другий абзац починається тут."
long = "Слово, " * 120 + "кінець."
assert all(len(s) <= speech.LONG_SENTENCE for s in speech._sentences(long))
print(f"segments ok: {len(segs)} segments, first {len(segs[0]['text'])} chars")


# Roman numerals are numbers, not English words
for w in ("XVI", "XVII", "XIX", "IV", "X", "I", "XXI", "XLIV"):
    assert speech.is_roman(w), w
for w in ("CD", "DC", "MIX", "CV", "C", "D", "GitHub", "XP", "IVX"):
    assert not speech.is_roman(w), w
out, names = speech.english("Початок XVI століття, Петро I, iPhone X, CD-диск.")
assert "XVI" in out and "Петро I" in out and "XVI" not in names and "I" not in names, (out, names)
print("roman ok:", out)

# text pasted from a PDF: hard-wrapped lines, hyphenation, decomposed й
pdf = ("Людина, як ми бачили, характеризується невичерпним колом\n"
       "визначень, у тому числі здатністю проектування свого майбутнього\n"
       "і, відповідно, отриманням від нього інформації. Наука виникає в Європі, що зна-\n"
       "менувалось коперніканським переворотом і геометрію\n"
       "Евкліда та сприйняттям\n"
       "світу і̆ знанням.\n\nРозділ 2\nНовий абзац тут.")
t, _ = speech.prepare_text(pdf, use_english=False)
assert "колом визначень" in t and "майбутнього і," in t and "знаменувалось" in t, t
assert "геометрію Евкліда" in t and "сприйняттям світу й знанням" in t, t
assert "Розділ 2. Новий абзац" in t and "\n\n" in t, t
print("pdf text ok")

# brackets are read as an aside with comma pauses; footnotes dropped
for src, want in [("вивести (типу: пряма лінія) значну", "вивести, типу: пряма лінія, значну"),
                  ("між точками (див. рис. 2).", "між точками, див. рис. 2."),
                  ("(Примітка: важливо) Далі текст.", "Примітка: важливо. Далі текст."),
                  ("Київ[1] — столиця[2, 3].", "Київ — столиця."),
                  ("слово [уточнення] і далі", "слово, уточнення, і далі")]:
    got = speech.speakable(src)
    assert got == want, (src, got)
segs, _ = speech.plan("Можна (типу: пряма лінія) вивести.", use_english=False)
assert segs[0]["text"] == "Можна (типу: пряма лінія) вивести." and segs[0]["spoken"] == "Можна, типу: пряма лінія, вивести."
t = "вивести (типу: пряма лінія) значну"
assert speech.speakable(t, "skip") == "вивести значну", speech.speakable(t, "skip")
assert speech.speakable(t, "announce") == "вивести, у дужках: типу: пряма лінія, значну", speech.speakable(t, "announce")
v = speech.speakable(t, "voice")
assert speech.aside_runs(v) == [("вивести,", False), ("типу: пряма лінія", True), ("значну", False)], speech.aside_runs(v)
assert speech.speakable("Текст (а (б) в) кінець.", "skip") == "Текст кінець."
assert speech.speakable("Кінець (примітка).", "skip") == "Кінець."
assert speech.speakable(t, "aside", True) == speech.speakable(t, "announce")
va = speech.speakable(t, "voice", True)
assert speech.aside_runs(va) == [("вивести, у дужках:", False), ("типу: пряма лінія", True), ("значну", False)], speech.aside_runs(va)
assert speech.speakable(t, "skip", True) == "вивести значну"
print("brackets ok")

# initials: letter names next to a surname, or skipped; not a sentence end; lexicon replace rules first
for src, want in [("Поет Т. Г. Шевченко народився.", "Поет те ге Шевченко народився."),
                  ("Твори Т.Г. Шевченка відомі.", "Твори те ге Шевченка відомі."),
                  ("А. Прізвище писав про це.", "а Прізвище писав про це."),
                  ("Учений В. І. Вернадський заснував.", "Учений ве і Вернадський заснував."),
                  ("Автор: Шевченко Т. Г., видання друге.", "Автор: Шевченко те ге, видання друге."),
                  ("Писав М. Грушевський у праці.", "Писав ем Грушевський у праці."),
                  ("Приїхав у м. Київ.", "Приїхав у м. Київ.")]:
    got = speech.initials(src)
    assert got == want, (src, got)
assert speech.initials("Поет Т. Г. Шевченко народився.", "skip") == "Поет Шевченко народився."
assert speech.initials("Автор: Шевченко Т. Г., видання.", "skip") == "Автор: Шевченко, видання."
assert speech._sentences("Поет Т. Г. Шевченко народився. Інше речення.") == ["Поет Т. Г. Шевченко народився.", "Інше речення."]
lexicon.write_text("Т. Г. Шевченко => Тара+с Григо+рович Шевче+нко\nт. зв. => так зва+ний\n")
segs, _ = speech.plan("Поет Т.Г. Шевченко і т. зв. класика. Писав А. Прізвище.", use_english=False)
sp = " ".join(s["spoken"] for s in segs)
assert "Тара+с Григо+рович Шевче+нко" in sp and "так зва+ний" in sp and "а Прізвище" in sp, sp
assert segs[0]["text"].startswith("Поет Т.Г. Шевченко"), segs[0]  # captions keep the initials
print("initials ok:", sp)

print("ALL OK")
