"""Source-bound answer keys, rendered through the existing checked PDF engine."""
from __future__ import annotations

import difflib
import re
import unicodedata
from typing import NamedTuple

from .worksheet_pdf import (MAX_PAGES, DesignError, _empty, _integer, _key_fields, _normalize,
                            _question_number, _text, normalize_spec, source_texts, validate_spec)


def _plain(text: str) -> str:
    """Comparable text: case, spacing, punctuation and quote styles are ignored."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def _same(a: str, b: str) -> bool:
    pa, pb = _plain(a), _plain(b)
    return pa == pb if pa or pb else _normalize(a) == _normalize(b)  # emoji-only options


def _answer_text(value, where: str) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(int(value)) if float(value).is_integer() else str(value)
    return _text(value, where, 2400)


_LETTER = re.compile(r"\s*\(?([A-H])\)?(?:[.):]?\s*|(?:[.):]|\s+[-–—])\s*(.+))", re.S)


def resolve_choice(where: str, options: list[str], choice, answer: str) -> tuple[str, str] | None:
    """The (letter, option text) a key selects, or None when it gives neither.
    `answer` may be a letter, a letter with its exact option text, or the exact text."""
    letters = ", ".join(chr(65 + i) for i in range(len(options)))
    picked = None
    if choice not in (None, ""):
        m = re.fullmatch(r"\s*\(?([A-Ha-h])\)?[.):]?\s*", choice) if isinstance(choice, str) else None
        picked = ord(m[1].upper()) - 65 if m else None
        if picked is None or picked >= len(options):
            raise DesignError(f"{where}: choice must be one of its actual option letters "
                              f"({letters}), not {choice!r}.")
    if answer.strip():
        meant = {i for i, option in enumerate(options) if _same(option, answer)}
        m = _LETTER.fullmatch(answer)
        if m and ord(m[1]) - 65 < len(options) and (
                not m[2] or _same(m[2], options[ord(m[1]) - 65])):
            meant.add(ord(m[1]) - 65)
        if picked is not None and picked not in meant:
            if len(meant) == 1:
                other = meant.pop()
                raise DesignError(f"{where}: choice {chr(65 + picked)} is {options[picked]!r}, but "
                                  f"answer {answer!r} is option {chr(65 + other)}. Solve the question "
                                  "again, then make choice and answer name the same, correct option.")
            raise DesignError(f"{where}: answer {answer!r} is not option {chr(65 + picked)} "
                              f"({options[picked]!r}). Solve the question again, then set choice "
                              "to the correct letter and copy that option's exact text into "
                              "answer; do not invent or reword options.")
        if picked is None:
            if len(meant) != 1:
                named = (f" Option {m[1]} is {options[ord(m[1]) - 65]!r}."
                         if m and m[2] and ord(m[1]) - 65 < len(options) else "")
                raise DesignError(f"{where}: answer {answer!r} does not name exactly one option.{named} "
                                  f"Add choice with the correct letter ({letters}).")
            picked = meant.pop()
    return None if picked is None else (chr(65 + picked), options[picked])


class _NotQuoted(DesignError):
    """Evidence that is not a quotation of the source. closeness (0-1) is how near the
    nearest source sentence is: low means it is a reason, not a misquote."""

    def __init__(self, message: str, closeness: float):
        super().__init__(message)
        self.closeness = closeness


# Quoted words inside evidence such as: The story says "Maya smiled." An apostrophe inside
# a word (didn't) does not end a single-quoted segment.
_QUOTED = re.compile(r"“([^”]+)”|\"([^\"]+)\"|‘([^’]+)’|(?<!\w)'((?:[^']|(?<=\w)'(?=\w))+?)'(?!\w)")
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+")
_PIECES = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+|\s*(?:\.{3}|…)\s*")
NEAR_QUOTE = 0.9    # a quote this close to one source sentence is a misquote of it
REASON_BELOW = 0.5  # evidence this far from every sentence is a reason, not a quote
GAP_WORDS = 3       # most words a shortened quote may leave out between two of its words
# Words a shortened quote may not leave out: without them a sentence means the opposite.
_NEGATIONS = frozenset({"not", "no", "never", "nobody", "nothing", "none", "nor", "neither",
                        "cannot", "t", "without", "nowhere"})


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())


def _negations(text: str) -> int:
    """How many negations ("not", "never", the t of "didn't", ...) text has."""
    return sum(w in _NEGATIONS for w in _words(text))


def _shortened(words: list[str], tokens: list[str]) -> bool:
    """Whether a quote's words occur in a sentence's words in order, leaving out at most
    GAP_WORDS between two of them, no more than the quote's own length in all, and never a
    negation. Nothing may be added."""
    states = {p: 0 for p, t in enumerate(tokens) if t == words[0]}
    for word in words[1:]:
        reached: dict[int, int] = {}
        for p, skipped in states.items():
            for q in range(p + 1, min(p + 2 + GAP_WORDS, len(tokens))):
                left_out = skipped + q - p - 1
                if (tokens[q] == word and left_out <= len(words)
                        and left_out < reached.get(q, len(words) + 1)
                        and not _NEGATIONS.intersection(tokens[p + 1:q])):
                    reached[q] = left_out
        states = reached
        if not states:
            return False
    return bool(states)


def _cite(where: str, quote, sources: list[str], hint: str) -> tuple[list[int], str, list[str]]:
    """(1-based source paragraphs, quote to show, notes) for a quotation of the sources.
    Case, spacing, punctuation and quote styles are ignored. Also accepted, and shown in the
    source's own words: the quoted part of 'The story says "..."', exact sentences from
    different places (joined with " … "), a short phrase found in exactly one sentence
    (shown as that sentence), a nearly exact sentence, and a sentence with a few words left
    out ("Benny collected..." for "Benny the beaver collected shiny leaves."), shown whole.
    Raises _NotQuoted otherwise."""
    quote = _text(quote, f"{where} evidence", 1000, required=True).strip()
    plain = _plain(quote)
    plains = [_plain(text) for text in sources]
    sentences = [(i, s.strip()) for i, text in enumerate(sources, 1)
                 for s in _SENTENCE_END.split(text) if _plain(s)]
    tokens = [_words(s) for _i, s in sentences]

    def shortened(text: str) -> tuple[int, str] | None:
        """(paragraph, whole sentence) for a quote that leaves a few words out of exactly one
        source sentence."""
        words = _words(text)
        if len(words) < 2:
            return None
        hits = [(i, s) for (i, s), t in zip(sentences, tokens)
                if len(_plain(s)) <= 400 and _shortened(words, t)]
        return hits[0] if hits and len({_plain(s) for _i, s in hits}) == 1 else None

    def nearest(text: str) -> tuple[float, int, str]:
        best, p = (0.0, 0, ""), _plain(text)
        for i, sentence in sentences:
            match = difflib.SequenceMatcher(None, p, _plain(sentence), autojunk=False)
            if match.real_quick_ratio() > best[0] and match.quick_ratio() > best[0] \
                    and (ratio := match.ratio()) > best[0]:
                best = (ratio, i, sentence)
        return best

    def find(text: str, near: bool) -> tuple[int, str] | None:
        """(paragraph, text as shown) for one exact or, with near, nearly exact piece."""
        p = _plain(text)
        if len(p) < 12:  # a short piece must be a whole sentence, not part of a word
            whole = next(((i, text) for i, s in sentences if _plain(s) == p), None) if p else None
            return whole or (shortened(text) if near and p else None)
        found = next((i for i, s in enumerate(plains, 1) if p in s), None)
        if found:
            return found, text
        if near and len(p) >= 20:
            ratio, i, sentence = nearest(text)
            if ratio >= NEAR_QUOTE and _negations(text) == _negations(sentence):
                return i, sentence
        return shortened(text) if near else None

    if plain and plain in plains:
        return [plains.index(plain) + 1], quote, []
    if len(plain) < 12:
        words = re.findall(r"\w+", quote.casefold())
        pattern = re.compile(r"(?<!\w)" + r"\W+".join(map(re.escape, words)) + r"(?!\w)") if words else None
        hits = [(i, s) for i, s in sentences if pattern and pattern.search(s.casefold())]
        if len(hits) == 1 and len(_plain(hits[0][1])) <= 400:
            return ([hits[0][0]], hits[0][1],
                    [f"{where}: the short quote {quote!r} is shown as its whole source sentence"])
        if found := shortened(quote):
            return ([found[0]], found[1],
                    [f"{where}: the short quote {quote!r} is shown as its whole source sentence"])
        raise _NotQuoted(f"{where}: evidence {quote!r} is too short to check"
                         + (" (it is in several sentences)" if hits else "")
                         + f". Quote the whole source sentence that supports the answer. {hint}", 0.0)
    segments = [next(g for g in m.groups() if g is not None).strip() for m in _QUOTED.finditer(quote)]
    segments = [s for s in segments if len(_plain(s)) >= 12]
    pieces = [p.strip(" \"'“”‘’") for p in _PIECES.split(quote)]
    pieces = [p for p in pieces if _plain(p)]
    for near in (False, True):
        whole = find(quote, near)
        if whole:
            return [whole[0]], whole[1], ([] if whole[1] == quote else [
                f"{where}: evidence {quote!r} was not word for word, so the source's own words "
                "are shown"])
        for parts, note in ((segments, "only the quoted source words were kept as evidence"),
                            (pieces if len(pieces) > 1 else [], None)):
            found = [find(part, near) for part in parts]
            if not parts or not all(found):
                continue
            runs = [found[0]]
            for i, shown in found[1:]:
                if i == runs[-1][0] and find(runs[-1][1] + " " + shown, False):
                    runs[-1] = (i, runs[-1][1] + " " + shown)  # next to each other in the source
                else:
                    runs.append((i, shown))
            notes = [f"{where}: {note}"] if note else []
            if len(runs) > 1 and not note:
                notes.append(f"{where}: quoted sentences from different places are joined with “…”")
            if near and [s for _i, s in found] != parts:
                notes.append(f"{where}: a quote that was not word for word is shown in the "
                             "source's own words")
            return sorted({i for i, _s in runs}), " … ".join(s for _i, s in runs), notes
    closeness, _i, sentence = nearest(quote)
    missing = [part for part in pieces if not find(part, True)] if len(pieces) > 1 else []
    if missing and len(missing) < len(pieces):
        # Most of it is quoted: name the parts to fix rather than one unrelated sentence.
        parts = []
        for part in missing[:3]:
            close, _i, nearby = nearest(part)
            parts.append(repr(part) + (f" (closest source sentence: {nearby!r})"
                                       if close >= REASON_BELOW else ""))
        raise _NotQuoted(f"{where}: part of the evidence is not in the source: "
                         f"{'; '.join(parts)}. Copy each sentence exactly, or list the sentences "
                         f"as separate quotes. {hint}", closeness)
    closest = (f" The closest source sentence is {sentence!r}: if it supports the answer, copy it "
               "exactly; otherwise move this to explanation." if closeness >= REASON_BELOW else "")
    raise _NotQuoted(f"{where}: evidence is not in the source: {quote!r}.{closest} {hint}", closeness)


def _cited(numbers: list[int], label: str) -> str:
    """'Paragraph [3]' or 'Paragraphs [3], [5]' (label "Paragraph"); 'Source excerpt 2' or
    'Source excerpts 2, 4' (label "Source excerpt")."""
    if label == "Paragraph":
        marks = ", ".join(f"[{n}]" for n in numbers)
    else:
        marks = ", ".join(map(str, numbers))
    return f"{label}{'s' if len(numbers) > 1 else ''} {marks}"


_UNITS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
          "fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_OPS = {"−": "-", "–": "-", "×": "*", "x": "*", "X": "*", "÷": "/"}
_MATH_TOKEN = re.compile(
    r"(?P<n>(?<![\w.,:\u2044])(?:\d{1,3}(?:,\d{3})+|\d{1,6})(?!\w|[.,:]\d|\u2044))"
    r"|(?P<b>_{2,}|[□▢◻⬜]|\[\s*\]|\(\s*\))|(?P<q>\?)|(?P<o>[-+−–×÷*/]|(?<=\s)[xX](?=\s))"
    r"|(?P<e>=)|(?P<f>[\u00bc-\u00be\u2150-\u215e\u2189])|(?P<w>[^\W\d_]+(?:'[^\W\d_]+)?)"
    r"|(?P<p>\S)")
# One character per token: n number (1,920 too), b blank, q "?", o operator, e "=",
# f a fraction character such as ½ (a number, never a unit word), W/I/S question words.
_MATH_SHAPE = re.compile(
    r"(?<![noqbe])(?P<a>n(?:on)+)e(?:[bq](?![noe])|$)"           # 3 + 4 = __
    r"|(?<![noqbe])[bq]e(?P<r>n(?:on)+)(?![noeq])"               # __ = 3 + 4
    r"|(?<![noqbe])(?P<m>[bq]on|no[bq])e(?P<t>n)(?![noeq])"      # 4 + __ = 9
    r"|(?:WI|S)p?(?P<c>n(?:on)+)(?=[qp]|$)")                     # What is 3 + 4?


def _number(text: str) -> int:
    return int(text.replace(",", ""))


def _evaluate(values: list[int], ops: list[str]) -> int | None:
    """Whole-number result with normal precedence; None for fractions or negatives."""
    terms, signs = [values[0]], []
    for op, value in zip(ops, values[1:]):
        if op == "*":
            terms[-1] *= value
        elif op == "/":
            if not value or terms[-1] % value:
                return None
            terms[-1] //= value
        else:
            signs.append(op)
            terms.append(value)
    total = terms[0]
    for op, value in zip(signs, terms[1:]):
        total = total + value if op == "+" else total - value
    return total if total >= 0 else None


def _missing_operand(left: int | None, op: str, right: int | None, total: int) -> int | None:
    if left is None:                    # __ op right = total
        value = (total - right if op == "+" else total + right if op == "-" else
                 total * right if op == "/" and right else
                 total // right if op == "*" and right and not total % right else None)
    else:                               # left op __ = total
        value = (total - left if op == "+" else left - total if op == "-" else
                 total // left if op == "*" and left and not total % left else
                 left // total if op == "/" and total and not left % total else None)
    return value if value is not None and value >= 0 else None


def arithmetic_target(prompt: str) -> tuple[str, int] | None:
    """(equation, whole-number result) when a prompt asks exactly one simple sum;
    None for anything ambiguous (several sums, decimals, times, dates, ranges)."""
    shape, values, texts = [], [], []
    for m in _MATH_TOKEN.finditer(prompt.replace("\u2019", "'")):
        kind, text = m.lastgroup, m[0]
        if kind == "w":
            word = text.casefold()
            kind = ("W" if word in ("what", "much") else "I" if word == "is" else
                    "S" if word in ("what's", "solve", "find", "calculate", "compute") else "w")
        shape.append(kind)
        texts.append(text)
        values.append(_number(text) if kind == "n" else _OPS.get(text, text))
    shape = "".join(shape)
    found = []
    for m in _MATH_SHAPE.finditer(shape):
        if m["m"]:
            start = m.start("m")
            left, op, right = values[start:start + 3]
            value = _missing_operand(None if shape[start] in "bq" else left, op,
                                     None if shape[start + 2] in "bq" else right,
                                     values[m.start("t")])
        else:
            group = next(g for g in ("a", "r", "c") if m[g])
            part = values[m.start(group):m.end(group)]
            value = _evaluate(part[0::2], part[1::2])
        found.append((" ".join(texts[m.start():m.end()]), value))
    return found[0] if len(found) == 1 and found[0][1] is not None else None


def _numbers(text: str) -> set[int]:
    text = text.casefold()
    found = {int(n) for n in re.findall(r"\d+", text)}
    found |= {_number(n) for n in re.findall(r"(?<![\d,])\d{1,3}(?:,\d{3})+(?!\d)", text)}
    words = re.findall(r"[^\W\d_]+", text)
    for i, word in enumerate(words):
        if word in _UNITS:
            found.add(_UNITS.index(word))
        elif word in _TENS:
            tens = 20 + 10 * _TENS.index(word)
            found.add(tens)
            if i + 1 < len(words) and words[i + 1] in _UNITS[1:10]:
                found.add(tens + _UNITS.index(words[i + 1]))
        elif word == "hundred":
            found.add(100 * (_UNITS.index(words[i - 1]) if i and words[i - 1] in _UNITS[1:10] else 1))
    return found


def check_math(where: str, prompt: str, answer: str) -> bool:
    """True when the prompt's simple arithmetic was checked; raises when the answer disagrees."""
    target = arithmetic_target(prompt)
    if target is None:
        return False
    equation, value = target
    if value not in _numbers(answer):
        raise DesignError(f"{where}: {equation} gives {value}, but its answer is {answer!r}. "
                          "Correct the answer (or the question) so the math is right.")
    return True


_MONEY = {"$", "¢", "€", "£"}
_QUANTITY = re.compile(r"\bhow (?:many|much|old|far|long|tall)\b|\btotal\b|\bin all\b"
                       r"|\baltogether\b|\bleft\b", re.I)


class _Step(NamedTuple):
    """One "=" of worked math. "12 × 8 × 5 = 96 × 5" has expression "12 × 8 × 5" (value
    480), stated 96 (the first number after "=") and result "96 × 5" (value 480)."""
    expression: str
    value: int | None           # None when it can't be judged (fractions, negatives)
    operands: tuple[int, ...]
    stated: int
    result: str                 # "96 × 5", or just "96"
    result_value: int | None    # None when the right side can't be judged

    @property
    def wrong(self) -> bool:
        # Both ways of writing several steps are right: a chain whose parts are all equal
        # ("12 × 8 × 5 = 96 × 5 = 480") and a running total ("12 × 8 = 96 × 5 = 480").
        return (self.value is not None and self.value != self.stated
                and self.result_value is not None and self.result_value != self.value)


def _steps(text: str) -> list[_Step]:
    """Worked steps such as "3 apples × $2 = $6": each "=" with a whole-number expression
    before it and a number after it. Words and money signs right after a number are its
    unit and are skipped; punctuation ends a step, so decimals and times are left alone.
    A step whose leftmost operand is unknown ("½ × 8 × 6", "base × 8", "(5 + 3) × 2",
    "−3 + 5") is left out rather than judged on the numbers that remain."""
    tokens = [(m.lastgroup, m[0]) for m in _MATH_TOKEN.finditer(text.replace("\u2019", "'"))
              if m[0] not in _MONEY]
    steps = []
    for i, (kind, _) in enumerate(tokens):
        if kind != "e" or i + 1 == len(tokens) or tokens[i + 1][0] != "n":
            continue
        parts, want, edge = [], "n", None
        for token, raw in reversed(tokens[:i]):
            if token == "w" and want == "n":  # "3 apples × 2", "2 bags = 6"
                continue
            if token != want:
                edge = token
                break
            parts.append(raw)
            want = "o" if want == "n" else "n"
        # An operator with no number before it, or a number glued to another ("2 1/2"),
        # means part of the expression is unknown.
        if want == "n" or edge in ("n", "f") or len(parts) < 3:
            continue
        parts.reverse()
        result, want, edge, j = [], "n", None, i + 1
        while j < len(tokens):
            token, raw = tokens[j]
            if token == "w" and want == "o":  # a unit before the next operator: "96 cm × 5"
                k = j
                while k < len(tokens) and tokens[k][0] == "w":
                    k += 1
                if k < len(tokens) and tokens[k][0] == "o":
                    j = k
                    continue
            if token != want:
                edge = token
                break
            result.append(raw)
            want = "o" if want == "n" else "n"
            j += 1
        stated = _number(result[0])
        if want == "n":
            result.pop()
            result_value = None             # "= 2 × (3 + 4)"
        elif edge in ("n", "f"):
            result_value = None             # "= 3 1/2"
        elif len(result) == 1:
            result_value = stated
        else:
            result_value = _evaluate([_number(n) for n in result[0::2]],
                                     [_OPS.get(o, o) for o in result[1::2]])
        operands = tuple(_number(n) for n in parts[0::2])
        value = _evaluate(list(operands), [_OPS.get(o, o) for o in parts[1::2]])
        steps.append(_Step(" ".join(parts), value, operands, stated, " ".join(result),
                           result_value))
    return steps


def check_working(where: str, prompt: str, working: str, answer: str) -> bool:
    """Recomputes every worked step in the explanation or answer, and for a "how many" or
    "how much" question checks that the steps reach the answer. True when that held using
    only the question's numbers; raises when a step is wrong or the steps end elsewhere."""
    steps = _steps(working) + _steps(answer)
    for step in steps:
        if step.wrong:
            comma = "," in step.expression + step.result  # 1,920 not 1920, like the working
            raise DesignError(
                f"{where}: the working says {step.expression} = {step.result}, but "
                f"{step.expression} = {step.value:{',' if comma else ''}}"
                + (f" and {step.result} = {step.result_value:{',' if comma else ''}}"
                   if " " in step.result else "")
                + ". Fix that step and anything that depends on it.")
    judged = [step for step in steps if step.value is not None]
    if not judged or not _QUANTITY.search(prompt):
        return False
    given, known = _numbers(answer), _numbers(prompt)
    operands = {n for step in judged for n in step.operands}
    # A number the steps found that isn't in the question (7 + 3 = 10 finds 3) counts too.
    reached = {step.value for step in judged} | (operands - known)
    if given and given.isdisjoint(reached):
        raise DesignError(f"{where}: the working ends at {judged[-1].value}, but the answer is "
                          f"{answer!r}. Solve it again from the question's numbers, writing each "
                          "step down to the answer, then make the answer match.")
    if not given & {step.value for step in judged}:
        return False
    for step in judged:
        if not set(step.operands) <= known:
            return False
        known.add(step.value)
    return True


def _fields(value, allowed: set[str], where: str) -> dict:
    if not isinstance(value, dict):
        raise DesignError(f"{where} must be an object.")
    unknown = value.keys() - allowed
    if unknown:
        raise DesignError(f"Unknown {where} field(s): {', '.join(sorted(unknown))}. Allowed: "
                          f"{', '.join(sorted(allowed))}. Load answer-key for the supported "
                          "format; do not rewrite the source quiz.")
    return value


def quiz_from_spec(value, pdf_text: str | None = None, name: str = "") -> dict:
    spec = validate_spec(normalize_spec(value, name)[0])
    if not spec["questions"] and not spec["challenge"]:
        raise DesignError("The source has no questions to answer.")
    if pdf_text is not None:
        actual = _normalize(pdf_text)
        # A PDF from this renderer prints tidy math (10 − 4 for 10 - 4); older ones, the text as written.
        if not any(all(_normalize(text) in actual for text in source_texts(spec, printed))
                   for printed in (True, False)):
            raise DesignError("The companion JSON does not match the actual PDF. "
                              "Read the PDF and resolve that mismatch before generating a key.")
        numbers = re.findall(r"(?m)^Question\s+(\d+):\s*$", pdf_text)
        if numbers and list(map(int, numbers)) != list(range(1, len(spec["questions"]) + 1)):
            raise DesignError("The PDF's question numbers do not match its companion JSON.")
    questions = [*spec["questions"], *([spec["challenge"]] if spec["challenge"] else [])]
    passages = [p["text"] for p in spec["reading"]]
    return {
        "title": spec["title"],
        "theme": spec["theme"],
        "passages": passages or [q["prompt"] for q in questions],
        "questions": [
            {"number": i, "label": q["label"], "prompt": q["prompt"], "options": q["options"],
             **({"figure": q["figure"]} if q["figure"] else {})}
            for i, q in enumerate(questions, 1)
        ],
    }


def quiz_from_pdf(text: str, title: str) -> dict:
    """Read numbered questions when a PDF has no matching worksheet JSON."""
    pages = text.split("\f")
    repeated = set()
    if len(pages) > 1:
        headers = [set(p.strip().splitlines()[:2]) for p in pages if p.strip()]
        if len(headers) > 1:
            repeated = set.intersection(*headers)
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if (not line or line in repeated or re.search(r"\bPage \d+ of \d+$", line)
                or re.fullmatch(r"\d{2}", line)
                or not any(unicodedata.category(c)[0] in "LN" for c in line)):
            continue
        lines.append(line)
    clean = "\n".join(lines)
    worksheet_layout = (re.search(r"(?m)^\d{2}\s*\nQuestion \d+:", text)
                        and re.search(r"\bPage \d+ of \d+", text))
    headings = list(re.finditer(r"(?m)^(?:Question\s+(\d+):\s*|(\d+)[.)]\s+)", clean))
    numbers = [int(m[1] or m[2]) for m in headings]
    if not numbers or numbers != list(range(1, len(numbers) + 1)) or len(numbers) > 30:
        raise DesignError("Cannot reliably identify every numbered question in this PDF. "
                          "Use read_file on it (read_file also reads words in pictures with "
                          "OCR), then copy its questions exactly into worksheet JSON, with each "
                          "answer stored in its question, and make the key with "
                          "render_answer_key on that JSON (or render_worksheet with "
                          "answer_key_path for a new quiz plus its key). Do not guess missing "
                          "questions.")
    reading = clean[:headings[0].start()].strip()
    questions = []
    for i, match in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(clean)
        block = clean[match.end():end].strip()
        if worksheet_layout and i == len(headings) - 1:
            # This renderer puts its closing callout after the last question, led by an emoji.
            body = block.splitlines()
            for j, line in enumerate(body[1:], 1):
                if line and unicodedata.category(line[0]) == "So":
                    block = "\n".join(body[:j]).strip()
                    break
        options = list(re.finditer(r"(?m)^([A-H])[.)]\s*", block))
        prompt = block[:options[0].start()].strip() if options else block
        choices = []
        for j, option in enumerate(options):
            if option[1] != chr(65 + j):
                raise DesignError(f"Question {i + 1} has unclear option labels. Read the source.")
            stop = options[j + 1].start() if j + 1 < len(options) else len(block)
            choices.append(" ".join(block[option.end():stop].split()))
        if not prompt or any(not option for option in choices):
            raise DesignError(f"Question {i + 1} was not completely extracted. Read the source.")
        questions.append({"number": i + 1, "label": match[0].strip(),
                          "prompt": " ".join(prompt.split()), "options": choices})
    paragraph_starts = list(re.finditer(r"(?m)^\[(\d+)\]\s*", reading))
    if paragraph_starts and [int(m[1]) for m in paragraph_starts] == list(range(1, len(paragraph_starts) + 1)):
        passages = [
            reading[m.end():paragraph_starts[i + 1].start()
                    if i + 1 < len(paragraph_starts) else len(reading)].strip()
            for i, m in enumerate(paragraph_starts)
        ]
    else:
        passages = [reading] if reading else [q["prompt"] for q in questions]
    return {"title": title, "passages": passages, "questions": questions}


_KEY_TOP = frozenset({"source_path", "source_sha256", "answers", "max_pages"})
_ANSWER_FIELDS = frozenset({"question", "choice", "answer", "explanation", "evidence", "issue"})
# Question numbers under other names.
_REF_FIELDS = ("number", "question_number", "num", "qnum", "no", "q", "id", "question_id",
               "index")
_TYPE_FIELDS = ("type", "kind", "question_type", "format")


def _decorative(value, count: int, depth: int = 0) -> bool:
    """Details a key draft may carry that hold no answers, such as a title, date or a "meta"
    object of them: short text, numbers, flags, and objects made only of those (lists of
    short text, like icons, inside them)."""
    if _empty(value) or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= 400
    if depth and isinstance(value, list):
        return all(isinstance(v, str) and len(v) <= 80 for v in value)
    if isinstance(value, dict) and depth < 2:
        return (not value.keys() & (_ANSWER_FIELDS | {"answers"})
                and not any(_question_number(k, count) for k in value)
                and all(_decorative(v, count, depth + 1) for v in value.values()))
    return False


def _entry_ref(value, quiz: dict) -> int | None:
    """The source question an answer names: 2, "2", "Q2", "Question 2" or its exact prompt."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    number = _question_number(value, len(quiz["questions"]))
    if number or not isinstance(value, str) or not _plain(value):
        return number
    matches = [q["number"] for q in quiz["questions"]
               if _plain(value) in (_plain(q["prompt"]), _plain(q["label"]))]
    return matches[0] if len(matches) == 1 else None


def _copied(given, actual: str) -> bool:
    """A field that repeats the source question's own text (exactly or nearly)."""
    if not isinstance(given, str) or not _plain(given):
        return False
    return _plain(given) == _plain(actual) or difflib.SequenceMatcher(
        None, _plain(given), _plain(actual), autojunk=False).ratio() >= NEAR_QUOTE


def _same_options(given, actual: list[str]) -> bool:
    if isinstance(given, dict):
        given = list(given.values())
    if not isinstance(given, list) or len(given) != len(actual) or not actual:
        return False
    return all(isinstance(g, str) and _same(re.sub(r"^\s*\(?[A-Ha-h][.):]\s+", "", g), a)
               for g, a in zip(given, actual))


def _key_draft(value, quiz: dict, digest: str) -> tuple[object, list[str]]:
    """A key draft in the documented shape where its meaning is unambiguous, with notes on
    what was read differently. Anything else is left for key_document to report."""
    if not isinstance(value, dict):
        return value, []
    seen: dict[str, list[str]] = {}

    def note(message: str, where: str = "") -> None:
        places = seen.setdefault(message, [])
        if where and where not in places:
            places.append(where)

    value = dict(value)
    count = len(quiz["questions"])
    if "answers" not in value:
        named = [k for k, v in value.items() if k not in _KEY_TOP and v and (
            isinstance(v, list) and all(isinstance(a, dict) for a in v)
            or isinstance(v, dict) and all(_question_number(n, count) for n in v))]
        if len(named) == 1:
            value["answers"] = value.pop(named[0])
            note(f"read field '{named[0]}' as 'answers'")
    for field in [k for k in value if k not in _KEY_TOP]:
        extra = value[field]
        if (_empty(extra) or field == "theme" and isinstance(extra, dict)
                or not isinstance(extra, list) and _decorative(extra, count)):
            del value[field]
            note(f"ignored field '{field}' (an answer key takes its title and look from its source)")
    sha = value.get("source_sha256")
    if not (isinstance(sha, str) and re.fullmatch(r"\s*[0-9a-fA-F]{64}\s*", sha)):
        value["source_sha256"] = digest
        note(f"source_sha256 was {'missing' if _empty(sha) else 'a placeholder'}, so the "
             "current source's fingerprint was used")
    else:
        value["source_sha256"] = sha.strip().lower()
    pages = value.get("max_pages")
    if isinstance(pages, str) and pages.strip().isdigit() or isinstance(pages, float) and pages.is_integer():
        value["max_pages"] = pages = int(pages)
    if "max_pages" in value and not (type(pages) is int and 1 <= pages <= MAX_PAGES):
        del value["max_pages"]
        note(f"ignored max_pages {pages!r} (not a page count from 1 to {MAX_PAGES})")
    answers = value.get("answers")
    if isinstance(answers, dict) and answers and all(_question_number(k, count) for k in answers):
        listed = []
        for key, item in answers.items():
            number = _question_number(key, count)
            entry = dict(item) if isinstance(item, dict) else {"answer": item}
            if type(entry.get("question")) is not int and _entry_ref(entry.get("question"), quiz) in (None, number):
                if isinstance(entry.get("question"), str) and not _entry_ref(entry["question"], quiz):
                    entry.setdefault("prompt", entry["question"])
                entry["question"] = number
            listed.append(entry)
        value["answers"] = answers = listed
        note("answers was an object keyed by question number, so it was read as a list")
    if not isinstance(answers, list):
        return value, [f"{m} ({', '.join(w)})" if w else m for m, w in seen.items()]
    entries = []
    for i, entry in enumerate(answers, 1):
        if not isinstance(entry, dict):
            entries.append(entry)
            continue
        entry, where = dict(entry), f"answer {i}"
        asked = entry.get("question")
        if "question" in entry and type(asked) is not int and not isinstance(asked, bool):
            number = _entry_ref(asked, quiz)
            if number:
                entry["question"] = number
                note("read the question reference as its number", where)
            elif isinstance(asked, str) and any(_entry_ref(entry.get(f), quiz) for f in _REF_FIELDS):
                # A reworded prompt next to a number: checked below like a copied prompt.
                entry["prompt" if "prompt" not in entry else "question_text"] = entry.pop("question")
        for field in _REF_FIELDS:
            if field not in entry:
                continue
            number = _entry_ref(entry[field], quiz)
            if number and "question" not in entry:
                del entry[field]
                entry["question"] = number
                note(f"read field '{field}' as 'question'", where)
            elif number and entry.get("question") == number:
                del entry[field]
                note(f"ignored field '{field}' (same as 'question')", where)
        numbered = [f for f in entry if f not in _ANSWER_FIELDS and _question_number(f, count)]
        if len(numbered) == 1 and "question" not in entry:
            field = numbered[0]
            inner = entry.pop(field)
            entry["question"] = _question_number(field, count)
            if isinstance(inner, dict):
                for k, v in inner.items():
                    entry.setdefault(k, v)
            elif _empty(entry.get("answer")) and _empty(entry.get("choice")):
                entry["answer"] = inner
            else:
                entry[field] = inner
            note(f"read field '{field}' as the question number and its answer", where)
        entries.append(entry)
    dicts = [e for e in entries if isinstance(e, dict)]
    if len(dicts) == len(entries) == count and not any("question" in e for e in dicts):
        for number, entry in enumerate(dicts, 1):
            entry["question"] = number
        note("the answers had no question numbers, so they were read in source order")
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        number, where = entry.get("question"), f"Question {entry.get('question')}"
        question = quiz["questions"][number - 1] if type(number) is int and 1 <= number <= count else None
        if question is None:
            continue
        entry = _key_fields(entry, note, where, options=bool(question["options"]))
        for field in ("prompt", "question_text", "text"):
            if field in entry and _copied(entry[field], question["prompt"]):
                del entry[field]
                note(f"ignored field '{field}' (a copy of the question)", where)
        for field in ("options", "choices"):
            if field in entry and _same_options(entry[field], question["options"]):
                del entry[field]
                note(f"ignored field '{field}' (a copy of the options)", where)
        if "label" in entry and _copied(entry["label"], question["label"]):
            del entry["label"]
            note("ignored field 'label' (a copy of the question label)", where)
        for field in _TYPE_FIELDS:
            if isinstance(entry.get(field), str) and len(entry[field]) <= 40:
                del entry[field]
                note(f"ignored field '{field}' (the source sets the question type)", where)
        if isinstance(entry.get("evidence"), str) and entry["evidence"].strip():
            entry["evidence"] = [entry["evidence"]]
        entries[i - 1] = entry
    value["answers"] = entries
    return value, [f"{m} ({', '.join(w)})" if w else m for m, w in seen.items()]


def _with_reasons(explanation: str, reasons: list[str], where: str) -> str:
    """The explanation followed by each reason it does not already give, as sentences."""
    pieces = [explanation.strip()] if explanation.strip() else []
    for reason in reasons:
        if not _plain(reason) or _plain(reason) not in _plain(" ".join(pieces)):
            pieces.append(reason)
    return _text(" ".join(p if i == len(pieces) - 1 or p[-1] in ".!?" else p + "."
                          for i, p in enumerate(pieces)), f"{where} explanation", 1600)


def _problems(errors: list[str]) -> DesignError:
    return DesignError(f"{len(errors)} answer problem(s); fix them all in one edit, then render "
                       "again. Nothing was rendered.\n- " + "\n- ".join(errors[:12])
                       + (f"\n- …and {len(errors) - 12} more" if len(errors) > 12 else ""))


def key_document(value, quiz: dict, source_path: str, digest: str) -> tuple[dict, dict]:
    """The answer-key document for a source quiz, and its checks. A draft's harmless shape
    differences are fixed (listed in the checks' "adjusted"); every remaining problem is
    reported at once."""
    value, adjusted = _key_draft(value, quiz, digest)
    value = _fields(value, set(_KEY_TOP), "answer key")
    if value.get("source_path") != source_path or value.get("source_sha256") != digest:
        raise DesignError("The key is not bound to the current source. Call read_quiz and use "
                          "its exact source_path and source_sha256; do not reuse an older key.")
    answers = value.get("answers")
    if not isinstance(answers, list):
        raise DesignError("answers must be a list with one entry for every source question.")
    expected = list(range(1, len(quiz["questions"]) + 1))
    numbers = [a.get("question") if isinstance(a, dict) else None for a in answers]
    if (any(type(n) is not int for n in numbers) or sorted(numbers) != expected):
        raise DesignError(f"The key must answer source questions {expected} exactly once, "
                          f"without omissions or invented questions; received {numbers}.")
    # Without a reading, the source excerpts are the questions themselves: there is nothing
    # else to quote, so a reason given as evidence is an explanation.
    prompts_only = quiz["passages"] == [q["prompt"] for q in quiz["questions"]]
    hint = "Reread with read_quiz; never invent quotations."
    paragraphs, issues, errors, reasons = [], [], [], []
    evidence_count = arithmetic = worked = 0
    for answer in sorted(answers, key=lambda a: a["question"]):
        number = answer["question"]
        where = f"Question {number}"
        question = quiz["questions"][number - 1]
        problems = []
        try:
            answer = _fields(answer, set(_ANSWER_FIELDS), "answer")
            issue = _text(answer.get("issue", ""), f"{where} issue", 1200)
            choice = answer.get("choice", "")
            response = _answer_text(answer.get("answer", ""), f"{where} answer")
            explanation = _text(answer.get("explanation", ""), f"{where} explanation", 1600)
        except DesignError as exc:
            errors.append(str(exc) if str(exc).startswith(where) else f"{where}: {exc}")
            continue
        quotes = answer.get("evidence")
        if prompts_only and _empty(quotes):
            quotes = [question["prompt"]]
            adjusted.append(f"{where}: the question itself is quoted as evidence (the source "
                            "has no reading)")
        citations, reasoning = [], []
        if not isinstance(quotes, list) or not 1 <= len(quotes) <= 6:
            problems.append(f"{where} needs 1-6 exact evidence quotes from the source.")
            quotes = []
        for quote in quotes:
            try:
                found, shown, notes = _cite(where, quote, quiz["passages"], hint)
            except DesignError as exc:
                if prompts_only and isinstance(quote, str) and quote.strip():
                    reasoning.append(quote.strip())
                else:
                    problems.append(str(exc))
                continue
            citations.append(f'{_cited(found, "Source excerpt")}: "{shown}"')
            adjusted.extend(notes)
        if reasoning:
            reasons.append(number)
            explanation = _with_reasons(explanation, reasoning, where)
            if not citations:
                citations.append(f'Source excerpt {number}: "{question["prompt"]}"')
        checked = response
        try:
            if question["options"]:
                picked = (None if issue.strip() and choice in (None, "")
                          else resolve_choice(where, question["options"], choice, response))
                if picked:
                    response, checked = f"{picked[0]}. {picked[1]}", picked[1]
                elif not issue.strip():
                    raise DesignError(f"{where}: choose an actual option or explain why "
                                      "no option is defensible in issue. Never force a wrong choice.")
                else:
                    response = response or "No single defensible option."
                if not explanation.strip() and not issue.strip():
                    raise DesignError(f"{where} needs an explanation grounded in the passage.")
            elif choice not in (None, ""):
                raise DesignError(f"{where} is not multiple choice; provide a written answer.")
            elif not response.strip():
                raise DesignError(f"{where} needs a written answer, not a blank answer area.")
            if not issue.strip() and not problems:
                arithmetic += check_math(where, question["prompt"], checked)
                worked += check_working(where, question["prompt"], explanation, response)
        except DesignError as exc:
            problems.append(str(exc))
        if problems:
            errors.extend(problems)
            continue
        evidence_count += len(citations)
        body = f"{question['prompt']}\n\nAnswer: {response}"
        if explanation:
            body += "\nWhy: " + explanation
        if issue:
            body += "\nSource issue: " + issue
            issues.append(number)
        body += "\n\n" + "\n".join(citations)
        paragraphs.append({"heading": question["label"], "text": body,
                           **({"figure": question["figure"]} if question.get("figure") else {})})
    if errors:
        raise _problems(errors)
    document = {
        "title": "Answer Key",
        "subtitle": quiz["title"],
        "series": "Parent / Teacher Reference",
        "student_line": "",
        "reading_title": "Answers and evidence",
        "reading": paragraphs,
        "closing_note": "Accept equivalent supported answers. Source issues are flagged, not "
                        "silently corrected. Evidence matching is not an independent fact check.",
    }
    if "theme" in quiz:
        document["theme"] = quiz["theme"]
    if "max_pages" in value:
        document["max_pages"] = _integer(value["max_pages"], "max_pages", 1, MAX_PAGES)
    validate_spec(document)
    audit = {
        "source_path": source_path, "source_sha256": digest,
        "questions_answered": len(answers), "evidence_quotes_checked": evidence_count,
        "arithmetic_checked": arithmetic, "worked_math_checked": worked, "source_issues": issues,
    }
    if reasons:
        audit["evidence_used_as_explanation"] = reasons
    if adjusted:
        audit["adjusted"] = list(dict.fromkeys(adjusted))
    return document, {**audit, "semantic_accuracy_verified": False}


def worksheet_key(spec: dict) -> tuple[dict, dict]:
    """A parent/teacher key from answers stored with a validated worksheet's own questions.
    Reports every problem at once, so one edit can fix them all."""
    questions = [*spec["questions"], *([spec["challenge"]] if spec["challenge"] else [])]
    if not any(q["key"].get(k) not in (None, "") for q in questions for k in ("choice", "answer")):
        raise DesignError("No answers found. For an answer key, add choice (the correct option "
                          "letter) or answer (the expected written answer) to every question.")
    passages = [p["text"] for p in spec["reading"]]
    sources = passages or [q["prompt"] for q in questions]
    paragraphs, errors, unverified, reasons, notes = [], [], [], [], []
    counts = {"multiple_choice_checked": 0, "evidence_quotes_checked": 0, "arithmetic_checked": 0,
              "worked_math_checked": 0}
    hint = ("Copy exact words from the reading, or move this sentence to explanation. Do not "
            "add sentences to the reading to match evidence." if passages else
            "Copy exact words from the question.")
    for number, q in enumerate(questions, 1):
        where = "challenge" if number > len(spec["questions"]) else f"Question {number}"
        key = q["key"]
        try:
            answer = _answer_text(key.get("answer", ""), f"{where} answer")
            explanation = _text(key.get("explanation", ""), f"{where} explanation", 1600)
            if q["options"]:
                picked = resolve_choice(where, q["options"], key.get("choice"), answer)
                if picked is None:
                    raise DesignError(f"{where} needs choice: the letter of its correct option.")
                response, value = f"{picked[0]}. {picked[1]}", picked[1]
            elif key.get("choice") not in (None, ""):
                raise DesignError(f"{where} has no options; put its expected answer in answer, not choice.")
            elif not answer.strip():
                raise DesignError(f"{where} needs answer: the expected answer, or for open "
                                  "questions 'Answers will vary' with an example.")
            else:
                response = value = answer
            quotes = key.get("evidence", [])
            quotes = [quotes] if isinstance(quotes, str) else quotes
            if not isinstance(quotes, list) or len(quotes) > 6:
                raise DesignError(f"{where} evidence must be one quote or a list of up to six quotes.")
            citations, reasoning, bad = [], [], []
            for quote in quotes:
                try:
                    found, shown, cite_notes = _cite(where, quote, sources, hint)
                except DesignError as exc:
                    # Without a reading there is nothing to quote: a worked reason (common for
                    # math) is an explanation, which the key shows as "Why:".
                    if passages or not isinstance(quote, str) or not quote.strip():
                        bad.append(str(exc))
                    else:
                        reasoning.append(quote.strip())
                    continue
                citations.append((found, shown))
                notes.extend(cite_notes)
            if bad:
                raise DesignError(" ".join(bad))
            if reasoning:  # a reason the explanation already gives is not repeated
                explanation = _with_reasons(explanation, reasoning, where)
            math = check_math(where, q["prompt"], value)
            worked = check_working(where, q["prompt"], explanation, response)
        except DesignError as exc:
            errors.append(str(exc))
            continue
        if reasoning:
            reasons.append(number)
        counts["multiple_choice_checked"] += bool(q["options"])
        counts["evidence_quotes_checked"] += len(citations)
        counts["arithmetic_checked"] += math
        counts["worked_math_checked"] += worked
        if not citations and not math and not worked:
            unverified.append(number)
        body = f"{q['prompt']}\n\nAnswer: {response}"
        if explanation.strip():
            body += "\nWhy: " + explanation
        if citations:
            body += "\n\n" + "\n".join(
                (f"{_cited(found, 'Paragraph')}: " if passages else "Evidence: ") + f'"{shown}"'
                for found, shown in citations)
        paragraphs.append({"heading": q["label"], "text": body,
                           **({"figure": q["figure"]} if q["figure"] else {})})
    if errors:
        raise _problems(errors)
    document = {
        "title": "Answer Key",
        "subtitle": spec["title"],
        "series": "Parent / Teacher Answer Key",
        "student_line": "",
        "reading_title": "Answers",
        "reading": paragraphs,
        "closing_note": "Accept equivalent correct answers, including answers in the child's own words.",
        "theme": spec["theme"],
    }
    if spec["answer_key_max_pages"]:
        document["max_pages"] = spec["answer_key_max_pages"]
    validate_spec(document)
    audit = {"answers_checked": len(questions), **counts, "unverified_questions": unverified}
    if reasons:
        audit["evidence_used_as_explanation"] = reasons
    if notes:
        audit["adjusted"] = list(dict.fromkeys(notes))
    return document, {**audit, "semantic_accuracy_verified": False}
