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
lexicon.write_text("GitHub => гі+тхаб\nVisual Studio Code => ві+жуал сту+діо ко+уд\nнаприкінці => наприкі+нці\n")
out, names = speech.english("У Visual Studio Code відкрий GitHub.")
assert "ві+жуал сту+діо ко+уд" in out and "гі+тхаб" in out, out
assert speech.say_name("github") == "гі+тхаб"
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

# English names: Ukrainian conventions on top of espeak's American IPA (2026-11-14, picked by ear)
import phonemize
for w, want in {"Minecraft": "ма+йнкрафт", "Telegram": "те+леграм", "Instagram": "і+нстаграм", "Bank": "ба+нк",
                "Google": "гу+гл", "Python": "па+йтон", "Microsoft": "ма+йкрософт", "Docker": "до+кер",
                "Link": "лі+нк", "Batman": "бе+тмен", "MC": "ем-сі"}.items():
    assert phonemize.name(w) == want, (w, phonemize.name(w))
print("english names ok")

# Explicit pauses "{пауза N}" (0.10.1): private characters inside, cut into segments, never in captions or spoken
t = speech.normalize("Раз {пауза 1,5} два {pause 0.3с} три {пауза 9} чотири {пауза 0.01}.")
assert speech.show_pauses(t).split() == "Раз {пауза 1.5} два {пауза 0.3} три {пауза 5} чотири {пауза 0.1} .".split(), speech.show_pauses(t)
assert "{" not in speech.strip_pauses(t) and speech.strip_pauses(t).startswith("Раз два"), speech.strip_pauses(t)
segs, _ = speech.plan("Перше речення. {пауза 2} Друге речення.\n\nНовий абзац {пауза 0,5} кінець.")
assert [(s["text"], s["pause_after_ms"]) for s in segs] == [("Перше речення.", 2000), ("Друге речення.", 550),
                                                            ("Новий абзац", 500), ("кінець.", 0)], segs
assert segs[2]["spoken"] == "Новий абзац,", segs[2]  # cut mid-sentence: read as unfinished
assert not any(ch in s["text"] + s["spoken"] for s in segs for ch in "{}"), segs
assert speech.plan("Без пауз. Зовсім.")[0] == [{"text": "Без пауз. Зовсім.", "pause_after_ms": 0, "spoken": "Без пауз. Зовсім."}]
text, _ = speech.prepare_text("Зачекайте {пауза 1} і слухайте.")
assert speech.show_pauses(text).split() == "Зачекайте {пауза 1} і слухайте.".split(), text
print("pauses ok")

# A word already marked with "+"/"++" keeps exactly that mark, also next to the same word unmarked or marked
# differently (the coordinator applies lexicon rules itself before synthesis)
import worker
A, L = worker.ACUTE, worker.LONG
src = f"Старий за{A}мок і новий за{A}{L}мок, во{A}да і вода."
auto = f"Стари{A}й за{A}мок і{A} нови{A}й за{A}мок, во{A}да і{A} вода{A}."
out = worker._keep_user_stress(src, auto)
assert f"за{A}мок і" in out and f"за{A}{L}мок," in out and f"во{A}да" in out and f"вода{A}" in out, out
kind, pat, right = lexicon.parse_rule("замок => за++мок")  # a lexicon word rule skips a word already marked
assert pat.sub(lambda m: lexicon._apply_marks(m.group(0), right), "за+мок і замок") == "за+мок і за++мок"
print("user stress kept ok")

# lexicon=false (0.10.6): no lexicon rule at all, so a word can be heard as the automatic stress says it
lexicon.write_text("ЗСУ => Збро+йні си+ли\nGitHub => гі+тхаб\n")
on, _ = speech.prepare_text("ЗСУ і GitHub.")
off, _ = speech.prepare_text("ЗСУ і GitHub.", use_lexicon=False)
assert "Збро+йні си+ли" in on and "гі+тхаб" in on, on
assert "Збро+йні" not in off and "гі+тхаб" not in off, off
segs, _ = speech.plan("ЗСУ і GitHub.", use_lexicon=False)
assert "Збро+йні" not in segs[0]["spoken"], segs
print("lexicon off ok")

# 0.16.2 audit fixes (model-free part; tests/test_verbalize.py checks the verbalizer)
for w in ("World", "Earth", "Girl", "Person"):  # a lone ɜ (British-style) is a vowel too, not dropped
    assert any(c in phonemize.VOWELS for c in phonemize.name(w)), (w, phonemize.name(w))
assert phonemize.name("R1") == "ар 1" and phonemize.name("P2P") == "пі 2 пі", (phonemize.name("R1"), phonemize.name("P2P"))
out, _ = speech.english("Кубик 3x3, екран 1920×1080, музика 20s і 90's, модель R1.")
assert "3 на 3" in out and "1920 на 1080" in out and "20-ті" in out and "90-ті" in out and "ар 1" in out, out
out, names = speech.english("Мої IV у покемона, на базу I, Rocky IV, Dungeons II, Петро I, iPhone X, у X столітті, I think.")
assert "Rocky" in names and "ро+кі IV" in out and "II" in out and "Петро I" in out and "у X столітті" in out, out
assert "Мої IV" in out  # capitalised word before it: a name, the numeral stays (rule from the audit)
assert "базу ай" in out and "IV у" not in out.replace("Мої IV у", ""), out
out, _ = speech.english("у покемона IV та EV.")
assert out == "у покемона ай-ві та і-ві.", out
print("audit fixes ok:", out)

# stress never sits on й or another non-vowel (accentuk put "й́ти")
real = worker._accent_uk
worker._accent_uk = lambda t: f"Тре{A}ба й{A}ти додо{A}му."
try:
    assert worker.stress("Треба йти додому.") == f"Тре{A}ба йти додо{A}му.", worker.stress("Треба йти додому.")
finally:
    worker._accent_uk = real
print("stress on vowels only ok")
