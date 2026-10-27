"""Source-bound answer keys, rendered through the existing checked PDF engine."""
from __future__ import annotations

import re
import unicodedata

from .worksheet_pdf import (DesignError, _integer, _normalize, _text, normalize_spec, source_texts,
                            validate_spec)


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


def _cite(where: str, quote, sources: list[str], hint: str) -> tuple[int, str]:
    """(1-based source paragraph, quote) for an exact, punctuation-insensitive quotation."""
    quote = _text(quote, f"{where} evidence", 1000, required=True).strip()
    plain = _plain(quote)
    if len(plain) < 12 and not any(plain == _plain(text) for text in sources):
        raise DesignError(f"{where}: quote a meaningful source detail, not a single word.")
    found = next((i for i, text in enumerate(sources, 1) if plain in _plain(text)), None)
    if found is None:
        raise DesignError(f"{where}: evidence is not in the source: {quote!r}. {hint}")
    return found, quote


_UNITS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
          "fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_OPS = {"−": "-", "–": "-", "×": "*", "x": "*", "X": "*", "÷": "/"}
_MATH_TOKEN = re.compile(
    r"(?P<n>(?<![\w.,:])\d{1,6}(?!\w|[.,:]\d))|(?P<b>_{2,}|[□▢◻⬜]|\[\s*\]|\(\s*\))|(?P<q>\?)"
    r"|(?P<o>[-+−–×÷*/]|(?<=\s)[xX](?=\s))|(?P<e>=)|(?P<w>[^\W\d_]+(?:'[^\W\d_]+)?)|(?P<p>\S)")
# One character per token: n number, b blank, q "?", o operator, e "=", W/I/S question words.
_MATH_SHAPE = re.compile(
    r"(?<![noqbe])(?P<a>n(?:on)+)e(?:[bq](?![noe])|$)"           # 3 + 4 = __
    r"|(?<![noqbe])[bq]e(?P<r>n(?:on)+)(?![noeq])"               # __ = 3 + 4
    r"|(?<![noqbe])(?P<m>[bq]on|no[bq])e(?P<t>n)(?![noeq])"      # 4 + __ = 9
    r"|(?:WI|S)p?(?P<c>n(?:on)+)(?=[qp]|$)")                     # What is 3 + 4?


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
        values.append(int(text) if kind == "n" else _OPS.get(text, text))
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


def _steps(text: str) -> list[tuple[str, int | None, int]]:
    """Worked steps such as "3 apples × $2 = $6" as (expression, its whole-number value or
    None when it can't be judged, the stated result). Words and money signs between the
    numbers are skipped; punctuation ends a step, so decimals and times are left alone."""
    tokens = [(m.lastgroup, m[0]) for m in _MATH_TOKEN.finditer(text.replace("\u2019", "'"))
              if m[0] not in _MONEY]
    steps = []
    for i, (kind, _) in enumerate(tokens):
        if kind != "e" or i + 1 == len(tokens) or tokens[i + 1][0] != "n":
            continue
        parts, want = [], "n"
        for token, raw in reversed(tokens[:i]):
            if token == "w" and want == "n":  # "3 apples × 2", "2 bags = 6"
                continue
            if token != want:
                break
            parts.append(raw)
            want = "o" if want == "n" else "n"
        if want == "n" and parts:  # an operator with no number before it
            parts.pop()
        if len(parts) < 3:
            continue
        parts.reverse()
        value = _evaluate([int(n) for n in parts[0::2]], [_OPS.get(o, o) for o in parts[1::2]])
        steps.append((" ".join(parts), value, int(tokens[i + 1][1])))
    return steps


def check_working(where: str, prompt: str, working: str, answer: str) -> bool:
    """Recomputes every worked step in the explanation or answer, and for a "how many" or
    "how much" question checks that the steps reach the answer. True when that held using
    only the question's numbers; raises when a step is wrong or the steps end elsewhere."""
    steps = _steps(working) + _steps(answer)
    for expression, value, stated in steps:
        if value is not None and value != stated:
            raise DesignError(f"{where}: the working says {expression} = {stated}, but "
                              f"{expression} = {value}. Fix that step and anything that depends on it.")
    judged = [(expression, value) for expression, value, _ in steps if value is not None]
    if not judged or not _QUANTITY.search(prompt):
        return False
    given, known = _numbers(answer), _numbers(prompt)
    operands = {int(n) for expression, _ in judged for n in re.findall(r"\d+", expression)}
    # A number the steps found that isn't in the question (7 + 3 = 10 finds 3) counts too.
    reached = {value for _, value in judged} | (operands - known)
    if given and given.isdisjoint(reached):
        raise DesignError(f"{where}: the working ends at {judged[-1][1]}, but the answer is "
                          f"{answer!r}. Solve it again from the question's numbers, writing each "
                          "step down to the answer, then make the answer match.")
    if not given & {value for _, value in judged}:
        return False
    for expression, value in judged:
        if not {int(n) for n in re.findall(r"\d+", expression)} <= known:
            return False
        known.add(value)
    return True


def _fields(value, allowed: set[str], where: str) -> dict:
    if not isinstance(value, dict):
        raise DesignError(f"{where} must be an object.")
    unknown = value.keys() - allowed
    if unknown:
        raise DesignError(f"Unknown {where} field(s): {', '.join(sorted(unknown))}. "
                          "Load answer-key for the supported format; do not rewrite the source quiz.")
    return value


def quiz_from_spec(value, pdf_text: str | None = None) -> dict:
    spec = validate_spec(normalize_spec(value)[0])
    if not spec["questions"] and not spec["challenge"]:
        raise DesignError("The source has no questions to answer.")
    if pdf_text is not None:
        actual = _normalize(pdf_text)
        if any(_normalize(text) not in actual for text in source_texts(spec)):
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
            {"number": i, "label": q["label"], "prompt": q["prompt"], "options": q["options"]}
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
                          "Use read_file (or OCR for a scan) and provide a matching worksheet "
                          "JSON source; do not guess missing questions.")
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


def key_document(value, quiz: dict, source_path: str, digest: str) -> tuple[dict, dict]:
    value = _fields(value, {"source_path", "source_sha256", "answers", "max_pages"}, "answer key")
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
    paragraphs = []
    issues = []
    evidence_count = arithmetic = worked = 0
    for answer in sorted(answers, key=lambda a: a["question"]):
        answer = _fields(answer, {"question", "choice", "answer", "explanation", "evidence", "issue"},
                         "answer")
        number = answer["question"]
        where = f"Question {number}"
        question = quiz["questions"][number - 1]
        issue = _text(answer.get("issue", ""), f"Question {number} issue", 1200)
        choice = answer.get("choice", "")
        response = _answer_text(answer.get("answer", ""), f"Question {number} answer")
        explanation = _text(answer.get("explanation", ""), f"Question {number} explanation", 1600)
        checked = response
        if question["options"]:
            picked = (None if issue.strip() and choice in (None, "")
                      else resolve_choice(where, question["options"], choice, response))
            if picked:
                response, checked = f"{picked[0]}. {picked[1]}", picked[1]
            elif not issue.strip():
                raise DesignError(f"Question {number}: choose an actual option or explain why "
                                  "no option is defensible in issue. Never force a wrong choice.")
            else:
                response = response or "No single defensible option."
            if not explanation.strip() and not issue.strip():
                raise DesignError(f"Question {number} needs an explanation grounded in the passage.")
        elif choice not in (None, ""):
            raise DesignError(f"Question {number} is not multiple choice; provide a written answer.")
        elif not response.strip():
            raise DesignError(f"Question {number} needs a written answer, not a blank answer area.")
        if not issue.strip():
            arithmetic += check_math(where, question["prompt"], checked)
            worked += check_working(where, question["prompt"], explanation, response)
        quotes = answer.get("evidence")
        if not isinstance(quotes, list) or not 1 <= len(quotes) <= 6:
            raise DesignError(f"Question {number} needs 1-6 exact evidence quotes from the source.")
        citations = []
        for quote in quotes:
            found, quote = _cite(where, quote, quiz["passages"],
                                 "Reread with read_quiz; never invent quotations.")
            citations.append(f'Source excerpt {found}: "{quote}"')
            evidence_count += 1
        body = f"{question['prompt']}\n\nAnswer: {response}"
        if explanation:
            body += "\nWhy: " + explanation
        if issue:
            body += "\nSource issue: " + issue
            issues.append(number)
        body += "\n\n" + "\n".join(citations)
        paragraphs.append({"heading": question["label"], "text": body})
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
        document["max_pages"] = _integer(value["max_pages"], "max_pages", 1, 10)
    validate_spec(document)
    return document, {
        "source_path": source_path, "source_sha256": digest,
        "questions_answered": len(answers), "evidence_quotes_checked": evidence_count,
        "arithmetic_checked": arithmetic, "worked_math_checked": worked, "source_issues": issues,
        "semantic_accuracy_verified": False,
    }


def worksheet_key(spec: dict) -> tuple[dict, dict]:
    """A parent/teacher key from answers stored with a validated worksheet's own questions.
    Reports every problem at once, so one edit can fix them all."""
    questions = [*spec["questions"], *([spec["challenge"]] if spec["challenge"] else [])]
    if not any(q["key"].get(k) not in (None, "") for q in questions for k in ("choice", "answer")):
        raise DesignError("No answers found. For answer_key_path, add choice (the correct option "
                          "letter) or answer (the expected written answer) to every question.")
    passages = [p["text"] for p in spec["reading"]]
    sources = passages or [q["prompt"] for q in questions]
    paragraphs, errors, unverified, reasons = [], [], [], []
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
            citations, reasoning = [], []
            for quote in quotes:
                try:
                    citations.append(_cite(where, quote, sources, hint))
                except DesignError:
                    # Without a reading there is nothing to quote: a worked reason (common for
                    # math) is an explanation, which the key shows as "Why:".
                    if passages or not isinstance(quote, str) or not quote.strip():
                        raise
                    reasoning.append(quote.strip())
            if reasoning:
                pieces = [explanation.strip()] if explanation.strip() else []
                for reason in reasoning:  # skip a reason the explanation already gives
                    if not _plain(reason) or _plain(reason) not in _plain(" ".join(pieces)):
                        pieces.append(reason)
                explanation = _text(" ".join(p if i == len(pieces) - 1 or p[-1] in ".!?" else p + "."
                                             for i, p in enumerate(pieces)),
                                    f"{where} explanation", 1600)
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
                (f"Paragraph [{found}]: " if passages else "Evidence: ") + f'"{quote}"'
                for found, quote in citations)
        paragraphs.append({"heading": q["label"], "text": body})
    if errors:
        raise DesignError(f"{len(errors)} answer problem(s); fix them all in one edit, then render "
                          "again. Nothing was rendered.\n- " + "\n- ".join(errors[:12]))
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
    return document, {**audit, "semantic_accuracy_verified": False}
