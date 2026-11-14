"""User stress lexicon: an editable text file, hot-reloaded, applied before automatic stress.

One rule per line, "#" starts a comment:
    наприкінці => наприкі+нці              word: every occurrence of the whole word (any case)
    ваш замок => ваш за+мок                 phrase: whole-word phrase, any case/whitespace; "+" go where marked
    ~ \\bзамок(?=\\s+на\\s+двер) => замо+к    regex (Python, case-insensitive): the match is replaced by the
                                            right side; keep the match to the word itself with lookarounds
    GitHub => гі+тхаб                       name: a Latin word/phrase -> how to say it (used by the English-names
                                            step before automatic phonetics; whole words, any case)
    Т. Г. Шевченко => Тарас Григорович Шевченко   replace: any other text -> what to say (full names for
                                            initials, abbreviations: т. зв. => так зва+ний); "+" optional
"++" instead of "+" also lengthens the stressed vowel (за++мок -> ˈzaːmɔk): use it when the model's
stress sounds too weak in running speech.
Words the user already marked with "+" are left alone (a marked word no longer matches a rule).
"""

import os
import threading

import regex as re  # variable-width lookbehind, e.g. (?<=(?:ваш|свій)\s)замок

PATH = os.environ.get("TTS_LEXICON", "/config/tts-lexicon.txt")
SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lexicon.seed.txt")
_W = r"[\w'ʼ’]"
_lock = threading.Lock()
_cache: tuple[float, list] = (-1.0, [])


class RuleError(ValueError):
    pass


def parse_rule(line: str):
    """-> (kind, compiled pattern, replacement) or None for blank/comment lines."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "=>" not in line:
        raise RuleError(f"missing '=>': {line}")
    left, right = (x.strip() for x in line.split("=>", 1))
    if not left or not right:
        raise RuleError(f"empty side: {line}")
    # words joined by spaces; after an abbreviation dot the space is optional ("Т.Г. Шевченко" = "Т. Г. Шевченко")
    words = left.split()
    pattern = "".join(re.escape(w) + ((r"\s*" if w.endswith(".") else r"\s+") if i < len(words) - 1 else "")
                      for i, w in enumerate(words))
    if re.search(r"[A-Za-z]", left) and not left.startswith("~"):
        if re.search(r"[A-Za-z]", right):
            raise RuleError(f"name rule: write the right side in Cyrillic: {line}")
        return "name", re.compile(rf"(?<![\w'’]){pattern}(?![\w'’])", re.IGNORECASE), right
    if not left.startswith("~") and right.replace("+", "").lower() != left.lower():
        # any other text -> what to say: initials -> full names, abbreviations; applied before stress
        return "replace", re.compile(rf"(?<![\w'’]){pattern}(?![\w'’])", re.IGNORECASE), right
    if "+" not in right:
        raise RuleError(f"right side has no '+' stress mark: {line}")
    if left.startswith("~"):
        try:
            return "regex", re.compile(left[1:].strip(), re.IGNORECASE), right
        except re.error as exc:
            raise RuleError(f"bad regex ({exc}): {line}")
    pattern = r"\s+".join(re.escape(w) for w in words)
    return ("phrase" if len(words) > 1 else "word"), \
        re.compile(rf"(?<!{_W}){pattern}(?![\w'ʼ’+])", re.IGNORECASE), right


def validate(text: str) -> list[str]:
    errors = []
    for n, line in enumerate(text.splitlines(), 1):
        try:
            parse_rule(line)
        except RuleError as exc:
            errors.append(f"line {n}: {exc}")
    return errors


def read_text() -> str:
    if not os.path.exists(PATH) and os.path.exists(SEED):
        try:
            write_text(open(SEED, encoding="utf-8").read())
        except OSError:
            return open(SEED, encoding="utf-8").read()
    try:
        return open(PATH, encoding="utf-8").read()
    except FileNotFoundError:
        return ""


def write_text(text: str) -> None:
    errors = validate(text)
    if errors:
        raise RuleError("; ".join(errors))
    if not text.endswith("\n"):
        text += "\n"
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, PATH)


def rules() -> list:
    """Parsed rules, re-read when the file changes (mtime)."""
    global _cache
    try:
        mtime = os.stat(PATH).st_mtime
    except FileNotFoundError:
        mtime = 0.0
    with _lock:
        if mtime != _cache[0]:
            parsed = []
            for line in read_text().splitlines():
                try:
                    r = parse_rule(line)
                except RuleError:
                    continue  # a broken hand edit must not stop synthesis; PUT/POST validate
                if r:
                    parsed.append(r)
            # longest phrases first, then single words, regexes last
            order = {"phrase": 0, "word": 1, "regex": 2, "name": 3, "replace": 4}
            parsed.sort(key=lambda r: (order[r[0]], -len(r[2])))
            _cache = (mtime, parsed)
        return _cache[1]


def _apply_marks(original: str, marked: str) -> str:
    """Copy the '+' positions of `marked` onto `original` (same letters), keeping the original's case/spacing."""
    out, letters = [], [c for c in marked if c != "+" and not c.isspace()]
    plus_after: dict[int, int] = {}
    k = -1
    for c in marked:
        if c == "+":
            plus_after[k] = plus_after.get(k, 0) + 1
        elif not c.isspace():
            k += 1
    k = -1
    for c in original:
        out.append(c)
        if not c.isspace():
            k += 1
            if k in plus_after:
                out.append("+" * plus_after[k])
    return "".join(out)


def apply(text: str) -> str:
    for kind, pattern, right in rules():
        if kind in ("name", "replace"):  # applied earlier, in the API process (speech.py)
            continue
        if kind == "regex":
            text = pattern.sub(lambda m: _case_like(m.group(0), right), text)
        else:
            text = pattern.sub(lambda m: _apply_marks(m.group(0), right), text)
    return text


def _case_like(src: str, right: str) -> str:
    return right[:1].upper() + right[1:] if src[:1].isupper() else right
