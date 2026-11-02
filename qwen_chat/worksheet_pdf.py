"""Local, checked HTML-to-PDF rendering for the worksheet-pdf skill.

Uses the command environment's Chromium and Poppler, not the GTK interpreter's
Python packages. Document input is plain data, never executable HTML or CSS.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata

DATA = Path(__file__).with_name("data")
MAX_SPEC_BYTES = 160_000
# A sanity bound, not a design limit: smaller limits come only from the user, and a layout
# that needs more pages than they set asks them before using more.
MAX_PAGES = 50
DEFAULT_ICONS = ["\U0001f4d6", "\U0001f50d", "\U0001f4a1", "\u2b50"]


class DesignError(ValueError):
    pass


class PageLimitError(DesignError):
    def __init__(self, pages: int, max_pages: int):
        self.pages = pages
        self.max_pages = max_pages
        super().__init__(f"The readable layout needs {pages} pages; max_pages is {max_pages}. "
                         "Keep the content and writing space. Ask before increasing the page limit.")


def _object(value, allowed: set[str], where: str) -> dict:
    if not isinstance(value, dict):
        raise DesignError(f"{where} must be an object.")
    unknown = value.keys() - allowed
    if unknown:
        raise DesignError(f"Unknown {where} field(s): {', '.join(sorted(unknown))}. "
                          f"Allowed: {', '.join(sorted(allowed))}.")
    return value


def _text(value, where: str, limit: int = 6000, required: bool = False) -> str:
    if not isinstance(value, str):
        raise DesignError(f"{where} must be plain text, not HTML or an object.")
    if len(value) > limit or any(ord(c) < 32 and c not in "\n\t\r" for c in value):
        raise DesignError(f"{where} is too long or contains control characters.")
    if required and not value.strip():
        raise DesignError(f"{where} cannot be empty.")
    return value


def _integer(value, where: str, lo: int, hi: int) -> int:
    if type(value) is not int or not lo <= value <= hi:
        raise DesignError(f"{where} must be an integer from {lo} to {hi}.")
    return value


def _emoji_clusters(text: str) -> list[str] | None:
    """Split text into visible emoji (joined, skin-tone, keycap and flag sequences
    stay whole), or return None when it contains ordinary text."""
    clusters: list[str] = []
    joined = flag_half = False
    for i, c in enumerate(text):
        if c == "\u200d" or c in "\ufe0e\ufe0f\u20e3" or "\U0001f3fb" <= c <= "\U0001f3ff" \
                or "\U000e0020" <= c <= "\U000e007f":
            if not clusters:
                return None
            clusters[-1] += c
            joined = joined or c == "\u200d"
            continue
        flag = "\U0001f1e6" <= c <= "\U0001f1ff"
        keycap = c in "#*0123456789" and "\u20e3" in text[i + 1:i + 3]
        if not (flag or keycap or unicodedata.category(c) == "So"):
            return None
        if joined or (flag and flag_half):
            clusters[-1] += c
            flag_half = False
        else:
            clusters.append(c)
            flag_half = flag
        joined = False
    return clusters


def _icon(value, where: str = "icon") -> str:
    if isinstance(value, str):
        icon = value.strip()
        clusters = _emoji_clusters(icon) if 0 < len(icon) <= 16 else None
        if clusters and len(clusters) == 1:
            return icon
        if clusters:
            raise DesignError(f"{where} has {len(clusters)} separate emoji ({icon}). Give each "
                              f"icon one emoji, for example {clusters[0]}.")
    raise DesignError(f"{where} must be one emoji such as \U0001f680, not {value!r}. "
                      "Put words in titles or prompts.")


def _icon_fallbacks(icon: str, theme_icons: list[str]) -> list[str]:
    """Simpler stand-ins, used only if the emoji font cannot draw `icon` as one picture."""
    parts = [c for c in icon if unicodedata.category(c) == "So" and not "\U0001f1e6" <= c <= "\U0001f1ff"]
    return [c for c in dict.fromkeys([*reversed(parts), *theme_icons, *DEFAULT_ICONS]) if c != icon]


KEY_FIELDS = ("choice", "answer", "explanation", "evidence")


def without_answers(data: dict) -> dict:
    """The student copy of a worksheet: answer-key fields never reach its renderer."""
    clean = {k: v for k, v in data.items() if k != "answer_key_max_pages"}
    if isinstance(clean.get("questions"), list):
        clean["questions"] = [{k: v for k, v in q.items() if k not in KEY_FIELDS}
                              if isinstance(q, dict) else q for q in clean["questions"]]
    if isinstance(clean.get("challenge"), dict):
        clean["challenge"] = {k: v for k, v in clean["challenge"].items() if k not in KEY_FIELDS}
    return clean


def _contrast(hex_color: str) -> float:
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
    return 1.05 / (sum(v * w for v, w in zip(linear, (.2126, .7152, .0722))) + .05)


def _darken(hex_color: str) -> str:
    """The same hue, just dark enough for headings on white."""
    rgb = [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]
    for step in range(1, 21):
        color = "#" + "".join(f"{round(v * (1 - step / 20)):02X}" for v in rgb)
        if _contrast(color) >= 4.5:
            return color
    return "#000000"


MOTIFS = ("leaves", "waves", "stars", "dots")
BACKGROUNDS = ("plain", "rainbow")
THEME_FIELDS = frozenset({"name", "accent", "icons", "motif", "background", "mascot"})
THEME_FORMAT = ("theme is an object: name, accent, icons, motif, background, mascot")
# Words that suggest a motif when a draft asks for one that doesn't exist.
_MOTIF_WORDS = {
    "waves": {"ocean", "sea", "water", "wave", "mermaid", "mermaids", "fish", "beach", "bubble",
              "bubbles", "underwater", "river", "lake", "shell", "shells", "coral", "pirate"},
    "leaves": {"forest", "woods", "wood", "leaf", "tree", "trees", "garden", "nature", "autumn",
               "jungle", "plant", "plants", "flower", "flowers", "farm"},
    "stars": {"space", "star", "night", "galaxy", "sky", "moon", "planet", "planets", "rocket",
              "astronaut", "sparkle", "sparkles", "magic"}}
# Theme fields a draft may put at the top level or under another name.
_THEME_AT_TOP = {"motif": "motif", "mascot": "mascot", "background": "background",
                 "accent": "accent", "accent_color": "accent"}
_THEME_ALIASES = {"accent_color": "accent", "color": "accent", "colour": "accent",
                  "emoji": "icons", "emojis": "icons", "hero": "mascot", "hero_icon": "mascot",
                  "character": "mascot"}
MASCOT_NAMES = {
    "mermaid": "\U0001f9dc\u200d\u2640\ufe0f", "merman": "\U0001f9dc\u200d\u2642\ufe0f",
    "fox": "\U0001f98a", "unicorn": "\U0001f984", "dragon": "\U0001f409",
    "dinosaur": "\U0001f995", "dino": "\U0001f996", "cat": "\U0001f431", "kitten": "\U0001f431",
    "dog": "\U0001f436", "puppy": "\U0001f436", "owl": "\U0001f989", "robot": "\U0001f916",
    "astronaut": "\U0001f9d1\u200d\U0001f680", "rocket": "\U0001f680", "alien": "\U0001f47d",
    "octopus": "\U0001f419", "dolphin": "\U0001f42c", "whale": "\U0001f433",
    "shark": "\U0001f988", "fish": "\U0001f420", "turtle": "\U0001f422", "bee": "\U0001f41d",
    "butterfly": "\U0001f98b", "lion": "\U0001f981", "tiger": "\U0001f42f",
    "panda": "\U0001f43c", "bear": "\U0001f43b", "penguin": "\U0001f427", "frog": "\U0001f438",
    "monkey": "\U0001f435", "rabbit": "\U0001f430", "bunny": "\U0001f430",
    "horse": "\U0001f434", "parrot": "\U0001f99c", "bird": "\U0001f426", "fairy": "\U0001f9da",
    "wizard": "\U0001f9d9", "princess": "\U0001f478", "ghost": "\U0001f47b",
    "rainbow": "\U0001f308", "star": "\u2b50", "sun": "\u2600\ufe0f", "moon": "\U0001f319"}
DOCUMENT_FIELDS = frozenset({
    "title", "subtitle", "series", "student_line", "meta", "instructions", "reading_title",
    "reading", "reading_note", "question_title", "questions", "challenge", "closing_note",
    "theme", "max_pages", "answer_key_max_pages"})
QUESTION_FIELDS = frozenset({"prompt", "label", "lines", "options", "icon", *KEY_FIELDS})
_READING_ALIASES = ("passage", "story")
_ICON_ALIASES = ("icons", "emoji", "emojis", "color_emoji")
_QUESTION_ALIASES = {"question": "prompt", "text": "prompt", "choices": "options"}
# Answer-key fields under other names. An option letter given as the answer is resolved
# against the options when the key is checked.
_KEY_ALIASES = {
    "correct_answer": "answer", "correct": "answer", "correct_option": "answer",
    "correct_choice": "answer", "correct_letter": "answer", "answer_letter": "answer",
    "answer_choice": "answer", "solution": "answer", "expected_answer": "answer",
    "sample_answer": "answer", "model_answer": "answer", "example_answer": "answer",
    "rationale": "explanation", "reason": "explanation", "reasoning": "explanation",
    "why": "explanation", "explain": "explanation",
    "quote": "evidence", "quotes": "evidence", "text_evidence": "evidence",
    "evidence_quote": "evidence", "evidence_quotes": "evidence",
    "supporting_evidence": "evidence", "citation": "evidence", "citations": "evidence",
    "source_quote": "evidence"}
# A separate answer summary next to the questions, which belongs inside each question.
_SUMMARY_FIELDS = ("answer_key", "answers", "answer_sheet", "answerkey", "answer_keys", "key",
                   "solutions", "correct_answers")
_QUESTION_REF = re.compile(r"\s*(?:q(?:uestion)?|#|no\.?|number)?\s*[#_.-]?\s*(\d{1,2})\s*[.):]?\s*",
                           re.I)
_IGNORED_QUESTION_FIELDS = ("type", "kind", "id", "number", "points", "difficulty")
_IGNORED_DOCUMENT_FIELDS = ("version", "schema_version", "id")
# Text fields of the document that a draft sometimes puts inside theme.
_TEXT_FIELDS = ("title", "subtitle", "series", "student_line", "meta", "instructions",
                "reading_title", "reading_note", "question_title", "closing_note")
_COLOR_NAMES = {
    "red": "#C62828", "crimson": "#B71C1C", "maroon": "#800000", "scarlet": "#C62828",
    "orange": "#E65100", "coral": "#D84315", "peach": "#E65100", "yellow": "#F9A825",
    "gold": "#B8860B", "golden": "#B8860B", "amber": "#FF8F00", "green": "#2E7D32",
    "emerald": "#047857", "mint": "#2E8B57", "lime": "#558B2F", "olive": "#6B6B00",
    "forest": "#1B5E20", "teal": "#00796B", "turquoise": "#00838F", "aqua": "#00838F",
    "cyan": "#00838F", "blue": "#1565C0", "sky": "#0277BD", "navy": "#1A237E",
    "indigo": "#3949AB", "purple": "#6A1B9A", "violet": "#7B1FA2", "lavender": "#7E57C2",
    "lilac": "#8E5BA8", "plum": "#6A1B9A", "magenta": "#AD1457", "fuchsia": "#AD1457",
    "pink": "#C2185B", "rose": "#C2185B", "brown": "#6D4C41", "chocolate": "#6D4C41",
    "gray": "#546E7A", "grey": "#546E7A", "silver": "#607D8B", "black": "#212121"}
_PAGE_WORDS = {"page", "pages", "budget", "max", "maximum", "limit", "count", "total", "num",
               "number", "of", "student", "worksheet", "quiz", "answer", "key", "parent", "teacher"}
# Field-name words that only describe looks. The theme draws the page, so an extra field
# such as "margin_art" or "font_size" can be dropped without losing any content.
_LOOK_WORDS = {"art", "decor", "decoration", "decorations", "border", "borders", "frame",
               "clipart", "sticker", "stickers", "doodle", "doodles", "color", "colors",
               "colour", "colours", "palette", "font", "fonts", "style", "styles", "layout",
               "design", "background", "backgrounds", "pattern", "patterns", "gradient",
               "fading", "fade", "effect", "effects", "spacing", "placement", "position",
               "header", "footer"}
_OFF = ("", "none", "no", "off", "false", "plain")
_LETTERED = re.compile(r"\s*\(?([A-Ha-h])[.):]\s+(\S.*)", re.S)
_LETTER_ONLY = re.compile(r"\s*\(?[A-Ha-h]\)?[.):]?\s*")
_NUMBERED = re.compile(r"\s*(\d{1,2})[.):]\s+(\S.*)", re.S)


def _empty(value) -> bool:
    return value is None or (isinstance(value, (str, list, dict)) and not
                             (value.strip() if isinstance(value, str) else value))


def _extra_field(field: str, value) -> str | None:
    """Why an unknown field can be dropped safely (it is empty or only about looks), or
    None when it may hold content and validation should report it."""
    if _empty(value):
        return "empty"
    words = re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", field).casefold())
    return "the theme sets the look" if _LOOK_WORDS.intersection(words) else None


def _top_extra(field: str, value) -> str | None:
    """_extra_field for document and theme fields, where true/false can't be content."""
    return _extra_field(field, value) or (
        "an on/off switch; the renderer decides this" if isinstance(value, bool) else None)


def _asks_rainbow(field: str, value) -> bool:
    """A drafted field like rainbow_fading: true or page_background: "rainbow fade"."""
    if "rainbow" in field.casefold():
        if isinstance(value, bool):
            return value
        return (isinstance(value, (str, int, float)) and len(str(value)) <= 60
                and str(value).strip().casefold() not in _OFF)
    return (_extra_field(field, value) is not None and isinstance(value, str)
            and "rainbow" in value.casefold())


def _page_limit_field(field: str, value) -> str | None:
    """max_pages or answer_key_max_pages for a renamed page limit such as student_budget_pages."""
    words = set(re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", field).casefold()))
    if type(value) is not int or not words & {"page", "pages"} or not words <= _PAGE_WORDS:
        return None
    return "answer_key_max_pages" if words & {"answer", "key", "parent", "teacher"} else "max_pages"


def _is_character(icon: str) -> bool:
    """Animals, people and creatures: the icons a new mascot replaces."""
    c = ord(icon[0]) if icon else 0
    return c not in (0x1F41A, 0x1F43D, 0x1F43E) and (
        0x1F400 <= c <= 0x1F43F or 0x1F980 <= c <= 0x1F9AE or 0x1F9D1 <= c <= 0x1F9DF
        or 0x1F466 <= c <= 0x1F478 or c in (0x1F47B, 0x1F47D, 0x1F47E, 0x1F916))


def _mascot(value: str) -> str | None:
    """One emoji for a mascot given as an emoji, as text with one emoji, or as a name."""
    for token in [value.strip(), *value.split()]:
        clusters = _emoji_clusters(token) if token else None
        if clusters and len(clusters) == 1:
            return token
    for word in re.findall(r"[a-z]+", value.casefold()):
        if word in MASCOT_NAMES or word.removesuffix("s") in MASCOT_NAMES:
            return MASCOT_NAMES.get(word) or MASCOT_NAMES[word.removesuffix("s")]
    return None


def _hex_color(value) -> str | None:
    """#RRGGBB for an accent given as #abc, #RRGGBBAA, rgb(...), bare hex digits or a common
    color name; None when it is not a color."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?", text)
    if m and (text.startswith("#") or re.search(r"\d", text)):
        return "#" + m[1].upper()
    m = re.fullmatch(r"#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])", text)
    if m:
        return "#" + "".join(c * 2 for c in m.groups()).upper()
    m = re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*[\d.]+%?\s*)?\)",
                     text, re.I)
    if m and all(int(v) <= 255 for v in m.groups()):
        return "#" + "".join(f"{int(v):02X}" for v in m.groups())
    words = re.findall(r"[a-z]+", text.casefold())
    return next((_COLOR_NAMES[w] for w in reversed(words) if w in _COLOR_NAMES), None)


def _title_from_name(name: str) -> str:
    """A title from a file name: unicorn-math_adventure_v2 -> Unicorn Math Adventure."""
    words = [w for w in re.split(r"[\W_]+", name or "") if w]
    while words and re.fullmatch(r"v?\d+|draft|final|spec|copy|tmp|temp|json|data", words[-1], re.I):
        words.pop()
    return " ".join(w[:1].upper() + w[1:] for w in words)[:160].strip()


def _question_number(value, count: int) -> int | None:
    """1-based question number from 3, "3", "q3" or "Question 3"; None otherwise."""
    if type(value) is int:
        number = value
    elif isinstance(value, str) and (m := _QUESTION_REF.fullmatch(value)):
        number = int(m[1])
    else:
        return None
    return number if 1 <= number <= count else None


def _key_fields(item: dict, note, where: str = "", options: bool | None = None) -> dict:
    """Moves answer-key fields given under other names (correct, rationale, quotes, ...) to
    choice, answer, explanation and evidence. A value that contradicts the field it would
    fill is left in place, so validation reports it."""
    item = dict(item)
    if options is None:
        options = bool(item.get("options"))
    for alias, field in _KEY_ALIASES.items():
        if alias not in item:
            continue
        value, current = item[alias], item.get(field)
        if field == "answer" and (isinstance(value, bool) or not isinstance(value, (str, int, float))
                                  or (options and not isinstance(value, str))):
            continue  # an option number or a flag: ambiguous, so validation reports it
        if _empty(current):
            item[field] = item.pop(alias)
            note(f"read question field '{alias}' as '{field}'", where)
        elif _empty(value) or str(value).strip().casefold() == str(current).strip().casefold():
            del item[alias]
            note(f"ignored question field '{alias}' (same as '{field}')", where)
        elif (field == "answer" and options and isinstance(value, str)
              and _LETTER_ONLY.fullmatch(value) and _empty(item.get("choice"))):
            item["choice"] = item.pop(alias)
            note(f"read question field '{alias}' as 'choice'", where)
        elif field == "evidence":
            quotes = [q for v in (current, item.pop(alias)) for q in (v if isinstance(v, list) else [v])]
            item["evidence"] = list(dict.fromkeys(q for q in quotes if not _empty(q)))
            note(f"added question field '{alias}' to 'evidence'", where)
        elif field == "explanation" and isinstance(value, str) and isinstance(current, str):
            item["explanation"] = current.rstrip() + (" " if current.rstrip()[-1:] in ".!?" else ". ") \
                + item.pop(alias).strip()
            note(f"added question field '{alias}' to 'explanation'", where)
    return item


def _summary_answers(summary, questions: list[dict]) -> dict[int, object] | None:
    """{0-based question index: answer or key fields} from a separate answer summary, or None
    when it can't be matched to the questions reliably."""
    count = len(questions)
    found: dict[int, object] = {}
    if isinstance(summary, dict):
        mc = [i for i, q in enumerate(questions) if q.get("options")]
        written = [i for i, q in enumerate(questions) if not q.get("options")]
        for field, value in summary.items():
            number = _question_number(field, count)
            words = set(re.findall(r"[a-z]+", str(field).casefold()))
            if number:
                found[number - 1] = value
            elif field in ("title", "name", "subtitle", "note", "notes", "instructions") \
                    and isinstance(value, str):
                continue
            elif isinstance(value, dict) and value and all(_question_number(k, count) for k in value):
                found.update({_question_number(k, count) - 1: v for k, v in value.items()})
            elif isinstance(value, list) and words & {"choice", "multiple", "mc", "mcq"} \
                    and len(value) == len(mc):
                found.update(zip(mc, value))
            elif isinstance(value, list) and words & {"written", "short", "open", "response",
                                                     "responses", "text"} \
                    and len(value) == len(written):
                found.update(zip(written, value))
            elif not _empty(value):
                return None
        return found or None
    if not isinstance(summary, list) or not summary:
        return None
    refs = [next((_question_number(v.get(k), count) for k in ("question", "number", "q", "id",
                                                              "question_number", "num")
                  if k in v), None) if isinstance(v, dict) else None for v in summary]
    if all(refs) and len(set(refs)) == len(refs):
        for ref, value in zip(refs, summary):
            found[ref - 1] = {k: v for k, v in value.items()
                              if k not in ("question", "number", "q", "id", "question_number", "num")}
        return found
    if not any(refs) and len(summary) == count:
        return dict(enumerate(summary))
    return None


def _merge_summary(data: dict, note) -> None:
    """A top-level answer summary (answer_key, answers, ...) next to the questions: its answers
    fill questions that lack one; the questions' own answers are kept."""
    questions = data.get("questions")
    fields = [k for k in _SUMMARY_FIELDS if k in data]
    if (not fields or not isinstance(questions, list) or not questions
            or not all(isinstance(q, dict) for q in questions)
            or data.keys() & {"source_path", "source_sha256"}):
        return
    for field in fields:
        summary = data.pop(field)
        missing = [i for i, q in enumerate(questions)
                   if _empty(q.get("choice")) and _empty(q.get("answer"))]
        if _empty(summary):
            note(f"ignored an empty '{field}'")
            continue
        if not missing:
            note(f"ignored the separate '{field}' summary (every question already has its "
                 "answer; answers belong inside each question)")
            continue
        found = _summary_answers(summary, questions)
        if found is None:
            note(f"ignored the separate '{field}' summary: it could not be matched to the "
                 "questions (put each answer inside its question as choice or answer)")
            continue
        filled = []
        for i in missing:
            value = found.get(i)
            if _empty(value):
                continue
            q = questions[i]
            if isinstance(value, dict):
                value = _key_fields(value, lambda *_: None, options=bool(q.get("options")))
                extra = {k: v for k, v in value.items() if k in KEY_FIELDS and _empty(q.get(k))}
                if not extra:
                    continue
                q.update(extra)
            elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
                q["answer"] = value
            else:
                continue
            filled.append(f"Question {i + 1}")
        if filled:
            note(f"read the separate '{field}' summary into {', '.join(filled[:6])}"
                 f"{', …' if len(filled) > 6 else ''} (answers belong inside each question)")
        else:
            note(f"ignored the separate '{field}' summary (it had no answers for the questions "
                 "that lack one)")


def _normalize_theme(theme: dict, note) -> dict:
    theme = dict(theme)
    for alias, field in _THEME_ALIASES.items():
        if alias in theme and field not in theme:
            theme[field] = theme.pop(alias)
            note(f"read theme field '{alias}' as theme.{field}")
    for field in [k for k in theme if k not in THEME_FIELDS]:
        value = theme[field]
        if _asks_rainbow(field, value):
            del theme[field]
            theme.setdefault("background", "rainbow")
            note(f"read theme field '{field}' as theme.background 'rainbow'")
        elif _top_extra(field, value):
            del theme[field]
            note(f"ignored theme field '{field}' ({_top_extra(field, value)})")
        else:
            del theme[field]
            note(f"ignored theme field '{field}' (the theme only sets the look; put words in "
                 "the title, subtitle or instructions)")
    motif = theme.get("motif")
    if isinstance(motif, str) and motif not in MOTIFS:
        words = re.findall(r"[a-z]+", motif.casefold())
        chosen = next((m for m in MOTIFS if m in words or m[:-1] in words), None) or next(
            (m for m, related in _MOTIF_WORDS.items() if related.intersection(words)), "dots")
        theme["motif"] = chosen
        also = []
        if "rainbow" in words and "background" not in theme:
            theme["background"] = "rainbow"
            also.append("theme.background 'rainbow'")
        character = _mascot(motif)
        if character and _is_character(character) and "mascot" not in theme:
            theme["mascot"] = character
            also.append(f"theme.mascot {character}")
        if motif.strip().casefold() != chosen:
            note(f"theme.motif '{motif}' is not available, so '{chosen}' was used"
                 + (f" with {' and '.join(also)}" if also else "")
                 + f" (choices: {', '.join(MOTIFS)})")
    icons = theme.get("icons")
    if isinstance(icons, str):
        icons = theme["icons"] = _emoji_clusters("".join(icons.split())) or icons
    if isinstance(icons, list) and len(icons) > 6:
        theme["icons"] = icons[:6]
        note(f"theme.icons had {len(icons)} emoji, so the first 6 were used")
    background = theme.get("background")
    if isinstance(background, str) and background not in BACKGROUNDS:
        theme["background"] = "rainbow" if "rainbow" in background.casefold() else "plain"
        note(f"theme.background '{background}' is not available, so '{theme['background']}' "
             f"was used (choices: {', '.join(BACKGROUNDS)})")
    mascot = theme.get("mascot")
    if isinstance(mascot, str) and mascot.strip():
        emoji = _mascot(mascot)
        if emoji and emoji != mascot:
            theme["mascot"] = emoji
            note(f"theme.mascot '{mascot}' was drawn as {emoji}")
    accent = theme.get("accent")
    if "accent" in theme and not (isinstance(accent, str)
                                  and re.fullmatch(r"#[0-9a-fA-F]{6}", accent)):
        color = _hex_color(accent)
        emoji = (_emoji_clusters("".join(accent.split()))
                 if isinstance(accent, str) and accent.strip() else None)
        if color:
            theme["accent"] = color
            note(f"theme.accent {accent!r} was read as {color}")
        else:
            del theme["accent"]
            if emoji and _empty(theme.get("icons")):
                theme["icons"] = emoji[:6]
                note("theme.accent held emoji, so they were used as theme.icons and the "
                     "default accent color was used (accent is a #RRGGBB color)")
            else:
                note(f"theme.accent {accent!r} is not a color, so the default was used "
                     "(accent is a #RRGGBB color)")
    accent = theme.get("accent")
    if isinstance(accent, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", accent) \
            and _contrast(accent) < 4.5:
        theme["accent"] = _darken(accent)
        note(f"theme.accent {accent} was too pale for headings, so {theme['accent']} was used")
    return theme


def restyle_theme(old, change: dict) -> tuple[dict, list[str]]:
    """The theme for a look-only change: `change` over the worksheet's theme. A new mascot
    replaces the old animal and character icons unless `change` lists icons too."""
    notes: list[str] = []
    change = _normalize_theme(change, lambda text, where="": notes.append(text))
    theme = {**(old if isinstance(old, dict) else {}), **change}
    if change.get("mascot") and "icons" not in change and isinstance(theme.get("icons"), list):
        kept = [i for i in theme["icons"] if not (isinstance(i, str) and _is_character(i))]
        if len(kept) < len(theme["icons"]):
            notes.append("the new mascot replaced " + "".join(
                i for i in theme["icons"] if i not in kept) + " in theme.icons")
            theme["icons"] = kept
    return theme, notes


def normalize_spec(value, name: str = "") -> tuple[dict, list[str]]:
    """Fixes common near-misses in a drafted worksheet (renamed fields, a theme given as text,
    top-level icons, empty or look-only extra fields, an unavailable motif, a pale accent,
    hand-typed numbering, a separate answer summary, a missing title) and says what changed,
    so they don't each cost a rewrite and another render. `name` is the JSON file's name,
    used for a missing title. Anything else is left for validate_spec to report."""
    if not isinstance(value, dict):
        return value, []
    found: dict[str, list[str]] = {}

    def note(text: str, where: str = "") -> None:
        found.setdefault(text, [])
        if where:
            found[text].append(where)

    data = dict(value)
    for alias in _READING_ALIASES:
        if alias in data and "reading" not in data:
            data["reading"] = data.pop(alias)
            note(f"read '{alias}' as 'reading'")
    if isinstance(data.get("reading"), str):
        data["reading"] = [p.strip() for p in re.split(r"\n\s*\n", data["reading"]) if p.strip()]
    reading = data.get("reading")
    if isinstance(reading, list):
        kept, heading, dropped = [], None, 0
        for p in reading:
            if _empty(p) or (isinstance(p, dict) and set(p) <= {"text", "heading"}
                             and _empty(p.get("text"))):
                dropped += 1
                if isinstance(p, dict) and isinstance(p.get("heading"), str) and p["heading"].strip():
                    heading = p["heading"]  # a heading on its own leads the next paragraph
                continue
            if heading is not None:
                if isinstance(p, str):
                    p = {"text": p, "heading": heading}
                elif isinstance(p, dict) and _empty(p.get("heading")):
                    p = {**p, "heading": heading}
                heading = None
            kept.append(p)
        if dropped:
            data["reading"] = kept
            note(f"removed {dropped} empty reading paragraph{'s' if dropped > 1 else ''}")
    theme = data.get("theme")
    if isinstance(theme, str):
        label = " ".join(theme.split())
        if label.casefold() in MOTIFS:
            data["theme"] = {"motif": label.casefold()}
            note(f"theme was text, so it was used as theme.motif ({THEME_FORMAT})")
        elif label:
            label = label[:1].upper() + label[1:]
            if len(label) > 36:
                label = label[:36].rsplit(" ", 1)[0].strip() or label[:36]
            data["theme"] = {"name": label}
            note(f"theme was text, so it was used as theme.name ({THEME_FORMAT})")
        else:
            del data["theme"]
            note("ignored an empty theme")
    if isinstance(data.get("theme", {}), dict):
        for field in _ICON_ALIASES:
            if field not in data:
                continue
            icons = data.pop(field)
            if isinstance(icons, str):
                icons = _emoji_clusters("".join(icons.split())) or icons
            theme = data["theme"] = dict(data.get("theme", {}))
            if "icons" in theme:
                note(f"ignored '{field}' (theme.icons is already set)")
            else:
                theme["icons"] = icons
                note(f"moved '{field}' into theme.icons")
        for field, target in _THEME_AT_TOP.items():
            if field not in data:
                continue
            value = data.pop(field)
            theme = data["theme"] = dict(data.get("theme", {}))
            if target in theme:
                note(f"ignored '{field}' (theme.{target} is already set)")
            else:
                theme[target] = value
                note(f"moved '{field}' into theme.{target}")
    for field in [k for k in data if k not in DOCUMENT_FIELDS]:
        value, target = data[field], _page_limit_field(field, data[field])
        if field in _IGNORED_DOCUMENT_FIELDS:
            del data[field]
            note(f"ignored field '{field}'")
        elif target:
            del data[field]
            if target in data:
                note(f"ignored '{field}' ({target} is already set)")
            else:
                data[target] = value
                note(f"read '{field}' as '{target}'")
        elif _asks_rainbow(field, value) and isinstance(data.get("theme", {}), dict):
            del data[field]
            data["theme"] = {"background": "rainbow", **data.get("theme", {})}
            note(f"read field '{field}' as theme.background 'rainbow'")
        elif _top_extra(field, value):
            del data[field]
            note(f"ignored field '{field}' ({_top_extra(field, value)})")
    for field in ("max_pages", "answer_key_max_pages"):
        if field not in data:
            continue
        value = data[field]
        number = (value if type(value) is int else
                  int(value.strip()) if isinstance(value, str) and value.strip().isdigit() else
                  int(value) if isinstance(value, float) and value.is_integer() else None)
        if number is not None and 1 <= number <= MAX_PAGES:
            if type(value) is not int:
                data[field] = number
                note(f"read {field} {value!r} as {number}")
        else:
            del data[field]
            note(f"ignored {field} {value!r} (a page limit is a whole number from 1 to {MAX_PAGES})")
    theme = data.get("theme")
    if isinstance(theme, dict) and theme.keys() & set(_TEXT_FIELDS):
        theme = data["theme"] = dict(theme)
        for field in _TEXT_FIELDS:
            if field not in theme:
                continue
            text = theme.pop(field)
            if not _empty(data.get(field)):
                note(f"ignored theme.{field} ({field} is already set)")
            elif not _empty(text):
                data[field] = text
                note(f"moved theme.{field} to the document's {field}")
    if isinstance(data.get("theme"), dict):
        data["theme"] = _normalize_theme(data["theme"], note)
    title = data.get("title")
    if title is None or (isinstance(title, str) and not title.strip()):
        theme_name = (data["theme"].get("name") if isinstance(data.get("theme"), dict) else None)
        if _title_from_name(name):
            data["title"] = _title_from_name(name)
            how = "from the file name"
        elif isinstance(theme_name, str) and theme_name.strip():
            data["title"] = theme_name.strip()[:160]
            how = "the theme name"
        else:
            data["title"], how = "Worksheet", "a default"
        note(f"the title was missing, so {data['title']!r} ({how}) was used; set title to change it")

    def question(item, where: str):
        if not isinstance(item, dict):
            return item
        item = dict(item)
        for alias, field in _QUESTION_ALIASES.items():
            if alias in item and field not in item:
                item[field] = item.pop(alias)
                note(f"read question field '{alias}' as '{field}'", where)
        item = _key_fields(item, note, where)
        for field in _IGNORED_QUESTION_FIELDS:
            if field in item:
                del item[field]
                why = (" (options make a question multiple choice)" if field in ("type", "kind")
                       else " (the PDF numbers questions)" if field in ("id", "number") else "")
                note(f"ignored question field '{field}'{why}", where)
        if "answer_text" in item:
            extra, answer = item.pop("answer_text"), item.get("answer")
            bare = [isinstance(v, str) and bool(_LETTER_ONLY.fullmatch(v)) for v in (answer, extra)]
            if item.get("options") and _empty(item.get("choice")) and bare.count(True) == 1:
                letter, text = (answer, extra) if bare[0] else (extra, answer)
                item["choice"] = letter
                if _empty(text):
                    item.pop("answer", None)
                else:
                    item["answer"] = text
                note("read the option letter as 'choice' and the option text as 'answer'", where)
            elif _empty(answer):
                item["answer"] = extra
                note("read question field 'answer_text' as 'answer'", where)
            elif str(extra).strip().casefold() == str(answer).strip().casefold():
                note("ignored question field 'answer_text' (same as 'answer')", where)
            elif not item.get("options") and _empty(item.get("explanation")):
                item["explanation"] = extra
                note("read question field 'answer_text' as 'explanation'", where)
            else:
                item["answer_text"] = extra
        for field in [k for k in item if k not in QUESTION_FIELDS]:
            why = _extra_field(field, item[field])
            if why:
                del item[field]
                note(f"ignored question field '{field}' ({why})", where)
        options = item.get("options")
        if isinstance(options, list) and options and all(isinstance(o, str) for o in options):
            marks = [_LETTERED.fullmatch(o) for o in options]
            if all(m and m[1].upper() == chr(65 + i) for i, m in enumerate(marks)):
                item["options"] = [m[2].strip() for m in marks]
                note("removed typed A./B. letters from options (the PDF adds them)", where)
        lines = item.get("lines")
        low = 0 if item.get("options") else 1
        if type(lines) is int and not low <= lines <= 12:
            item["lines"] = min(max(lines, low), 12)
            note(f"lines must be {low}-12 here, so {item['lines']} was used", where)
        return item

    questions = data.get("questions")
    if isinstance(questions, list):
        questions = [question(q, f"Question {i}") for i, q in enumerate(questions, 1)]
        numbered = [_NUMBERED.fullmatch(q["prompt"]) if isinstance(q, dict)
                    and isinstance(q.get("prompt"), str) and "label" not in q else None
                    for q in questions]
        if numbered and all(m and int(m[1]) == i for i, m in enumerate(numbered, 1)):
            for q, m in zip(questions, numbered):
                q["prompt"] = m[2].strip()
            note("removed typed numbers from prompts (the PDF numbers questions)")
        data["questions"] = questions
    if "challenge" in data:
        data["challenge"] = question(data["challenge"], "challenge")
    _merge_summary(data, note)
    notes = []
    for text, places in found.items():
        places = list(dict.fromkeys(places))
        notes.append(text + (f" ({', '.join(places[:4])}{', …' if len(places) > 4 else ''})"
                             if places else ""))
    return data, notes


def validate_spec(value) -> dict:
    value = _object(value, DOCUMENT_FIELDS, "document")
    spec = {}
    defaults = {
        "title": "", "subtitle": "", "series": "Adventure Readers Club",
        "student_line": "Name: ____________________    Date: ____________________",
        "meta": "", "instructions": "", "reading_title": "Read & explore",
        "reading_note": "", "question_title": "Your challenge", "closing_note": "",
    }
    for key, default in defaults.items():
        limit = 160 if key in ("title", "subtitle", "series", "reading_title", "question_title") else 6000
        spec[key] = _text(value.get(key, default), key, limit, required=key == "title")
    spec["max_pages"] = _integer(value.get("max_pages", MAX_PAGES), "max_pages", 1, MAX_PAGES)
    spec["answer_key_max_pages"] = (
        _integer(value["answer_key_max_pages"], "answer_key_max_pages", 1, MAX_PAGES)
        if "answer_key_max_pages" in value else None)
    theme = _object(value.get("theme", {}), THEME_FIELDS, "theme")
    accent = theme.get("accent", "#176B56")
    if not isinstance(accent, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        raise DesignError("theme.accent must be a six-digit hex color such as #176B56.")
    if _contrast(accent) < 4.5:
        raise DesignError("theme.accent is too pale for readable headings. Use a darker color; "
                          "the renderer makes its own pale backgrounds.")
    motif = theme.get("motif", "dots")
    if motif not in MOTIFS:
        raise DesignError("theme.motif must be leaves, waves, stars or dots.")
    background = theme.get("background", "plain")
    if background not in BACKGROUNDS:
        raise DesignError("theme.background must be plain or rainbow.")
    mascot = _icon(theme["mascot"], "theme.mascot") if "mascot" in theme else None
    icons = theme.get("icons", [] if mascot else DEFAULT_ICONS)
    if not isinstance(icons, list) or not (0 if mascot else 1) <= len(icons) <= 6:
        raise DesignError("theme.icons must contain 1-6 related emoji.")
    icons = [_icon(icon, f"theme.icons[{i}]") for i, icon in enumerate(icons)]
    spec["theme"] = {
        "name": _text(theme.get("name", "Discovery"), "theme.name", 36, True),
        "accent": accent, "motif": motif, "background": background,
        # The mascot is the main picture: first in icons, so it leads every page.
        "icons": list(dict.fromkeys([mascot, *icons] if mascot else icons))[:6],
    }
    if mascot:
        spec["theme"]["mascot"] = mascot
    icons = spec["theme"]["icons"]
    reading = value.get("reading", [])
    if not isinstance(reading, list) or len(reading) > 60:
        raise DesignError("reading must be a list of at most 60 paragraphs.")
    spec["reading"] = []
    for i, item in enumerate(reading, 1):
        if isinstance(item, str):
            item = {"text": item}
        item = _object(item, {"text", "heading"}, f"reading paragraph {i}")
        spec["reading"].append({
            "text": _text(item.get("text", ""), f"reading paragraph {i}", required=True),
            "heading": _text(item.get("heading", ""), f"reading heading {i}", 160),
        })
    questions = value.get("questions", [])
    if not isinstance(questions, list) or len(questions) > 30:
        raise DesignError("questions must be a list of at most 30 questions.")

    def question(item, number: int, bonus: bool = False) -> dict:
        where = "challenge" if bonus else f"Question {number}"
        item = _object(item, QUESTION_FIELDS, where)
        options = item.get("options", [])
        if not isinstance(options, list) or len(options) > 8:
            raise DesignError(f"{where} options must be a list of at most eight strings.")
        return {
            "prompt": _text(item.get("prompt", ""), f"{where} prompt", 3000, True),
            "label": _text(item.get("label", "Extra Challenge:" if bonus else f"Question {number}:"),
                           f"{where} label", 100, True),
            "lines": _integer(item.get("lines", 0 if options else 5), f"{where} lines",
                              0 if options else 1, 12),
            "options": [_text(o, f"{where} option", 1000, True) for o in options],
            "icon": _icon(item.get("icon", icons[(number - 1) % len(icons)]),
                          f"{where} icon"),
            # Parent-key data: checked by answer_key.worksheet_key, never rendered here.
            "key": {k: item[k] for k in KEY_FIELDS if k in item},
        }

    spec["questions"] = [question(q, i) for i, q in enumerate(questions, 1)]
    spec["challenge"] = question(value["challenge"], len(questions) + 1, True) if "challenge" in value else None
    if not reading and not questions and not spec["challenge"]:
        raise DesignError("Provide a reading passage or questions; a title page alone is not a worksheet.")
    if sum(len(t) for t in source_texts(spec)) > 60_000:
        raise DesignError("Too much text for one worksheet. Split it into separate documents.")
    return spec


def source_texts(spec: dict) -> list[str]:
    texts = [spec[k] for k in ("title", "subtitle", "series", "student_line", "meta",
                               "instructions", "reading_note", "closing_note")]
    if spec["reading"]:
        texts.append(spec["reading_title"])
    if spec["questions"] or spec["challenge"]:
        texts.append(spec["question_title"])
    for paragraph in spec["reading"]:
        texts.extend((paragraph["heading"], paragraph["text"]))
    for question in [*spec["questions"], *([spec["challenge"]] if spec["challenge"] else [])]:
        texts.extend((question["label"], question["prompt"], *question["options"]))
    return [t for t in texts if t.strip()]


def _font_file(paths: list[Path], description: str) -> Path:
    for path in paths:
        if path.is_file():
            return path
    raise DesignError(f"Missing {description}. Use install_packages with manager apt and "
                      "packages [fonts-liberation2, fonts-noto-color-emoji], then retry.")


def _fonts() -> str:
    sans = _font_file([Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
                       Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")], "text font")
    bold = _font_file([Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
                       Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")], "bold text font")
    emoji = _font_file([Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
                        Path("/system/fonts/NotoColorEmoji.ttf")], "color emoji font")
    rules = []
    for family, weight, path in (("QwenSans", 400, sans), ("QwenSans", 700, bold),
                                  ("QwenEmoji", 400, emoji)):
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        rules.append(f"@font-face{{font-family:{family};font-weight:{weight};"
                     f"src:url(data:font/ttf;base64,{encoded}) format('truetype')}}")
    return "\n".join(rules)


def _e(text: str) -> str:
    return html.escape(text, quote=True).replace("\n", "<br>")


def _emoji(icon: str, extra: str = "") -> str:
    return f'<span class="emoji {extra}" aria-hidden="true">{_e(icon)}</span>'


def _decor(motif: str) -> str:
    shapes = []
    for x in (32, 784):
        for i, y in enumerate(range(175, 1000, 118)):
            if motif == "leaves":
                shapes.append(f'<path d="M{x},{y + 36}q-12,-20 0,-35q12,12 0,35m0,-12l-7,-13" />')
            elif motif == "waves":
                shapes.append(f'<path d="M{x},{y}q12,12 0,25t0,25" fill="none"/>'
                              f'<circle cx="{x + 4}" cy="{y + 64}" r="3"/>')
            elif motif == "stars":
                shapes.append(f'<path d="M{x},{y}l2,6h5l-4,3l2,6l-5,-3l-5,3l2,-6l-4,-3h5z"/>')
            else:
                shapes.append(f'<circle cx="{x}" cy="{y}" r="{4 + i % 3}"/>'
                              f'<circle cx="{x - 5}" cy="{y + 24}" r="2"/>')
    return '<svg class="decor" viewBox="0 0 816 1056" aria-hidden="true">' + "".join(shapes) + "</svg>"


def make_html(spec: dict) -> str:
    icons = spec["theme"]["icons"]
    blocks: list[str] = []

    def chunk(body: str) -> None:
        blocks.append(f'<section class="chunk" data-block="{len(blocks) + 1}">{body}</section>')

    def band(text: str, icon: str) -> str:
        return f'<h2 class="band">{_emoji(icon)}<span>{_e(text)}</span></h2>'

    def note(text: str, icon: str) -> str:
        return f'<aside class="callout">{_emoji(icon)}<p>{_e(text)}</p></aside>'

    intro = ""
    if spec["student_line"]:
        intro += f'<p class="student-line">{_e(spec["student_line"])}</p>'
    if spec["instructions"]:
        intro += note(spec["instructions"], "\U0001f50d")
    if spec["meta"]:
        intro += f'<p class="meta">{_e(spec["meta"])}</p>'
    if intro:
        chunk(intro)
    for i, p in enumerate(spec["reading"], 1):
        heading = band(spec["reading_title"], icons[0]) if i == 1 else ""
        if p["heading"]:
            heading += f'<h3 class="reading-heading">{_e(p["heading"])}</h3>'
        body = (heading + f'<p class="reading-text"><span class="paragraph-number">[{i}]</span> '
                + _e(p["text"]) + "</p>")
        if i == len(spec["reading"]) and spec["reading_note"]:
            body += note(spec["reading_note"], icons[-1])
        chunk(body)
    if spec["reading_note"] and not spec["reading"]:
        chunk(note(spec["reading_note"], icons[-1]))
    questions = [*spec["questions"], *([spec["challenge"]] if spec["challenge"] else [])]
    for i, q in enumerate(questions, 1):
        bonus = i > len(spec["questions"])
        badge = "+" if bonus else f"{i:02}"
        options = "".join(f'<li><b>{chr(65 + j)}.</b> {_e(o)}</li>' for j, o in enumerate(q["options"]))
        lines = "".join('<div class="answer-line"></div>' for _ in range(q["lines"]))
        question = (
            f'<article class="question" data-question="{i}" data-answer-lines="{q["lines"]}">'
            f'<div class="question-head"><span class="number-badge">{badge}</span>'
            f'<div class="question-copy"><p class="question-label">{_e(q["label"])}</p>'
            f'<p class="q-prompt">{_e(q["prompt"])}</p></div>{_emoji(q["icon"])}</div>'
            + (f'<ol class="options">{options}</ol>' if options else "")
            + (f'<div class="answer-space" aria-label="Answer space">{lines}</div>' if lines else "")
            + "</article>")
        if i == 1:
            question = band(spec["question_title"], "\U0001f50d") + question
        if i == len(questions) and spec["closing_note"]:
            question += note(spec["closing_note"], icons[-1])
        chunk(question)
    if spec["closing_note"] and not questions:
        chunk(note(spec["closing_note"], icons[-1]))
    used_icons = list(dict.fromkeys([*icons, "\U0001f50d", *(q["icon"] for q in questions)]))
    config = json.dumps({"max_pages": spec["max_pages"], "blocks": len(blocks), "icons": used_icons,
                         "fallbacks": {i: _icon_fallbacks(i, icons) for i in used_icons}},
                        ensure_ascii=True).replace("<", "\\u003c")
    css = (DATA / "worksheet.css").read_text()
    js = (DATA / "worksheet.js").read_text()
    accent = spec["theme"]["accent"]
    body = ' class="bg-rainbow"' if spec["theme"].get("background") == "rainbow" else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        'style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; font-src data:; '
        'img-src data:; connect-src \'none\'; frame-src \'none\'; object-src \'none\'">'
        f"<title>{_e(spec['title'])}</title><style>{_fonts()}\n{css}\n"
        f":root{{--accent:{accent}}}</style></head><body{body}>"
        '<div id="document"></div><template id="page-template"><article class="page">'
        + _decor(spec["theme"]["motif"])
        + f'<header class="page-header"><div class="series">{_e(spec["series"])}</div>'
        f'<div class="hero"><div class="hero-copy"><h1>{_e(spec["title"])}</h1>'
        f'<p class="subtitle">{_e(spec["subtitle"])}</p></div>'
        f'<div class="hero-art">{_emoji(icons[0], "hero-icon")}'
        f'{_emoji(icons[1 % len(icons)], "companion-icon")}</div></div></header>'
        '<main class="content"></main>'
        f'<footer><span class="footer-theme">{_e(spec["theme"]["name"])}</span>'
        '<span class="footer-icons">' + "".join(_emoji(i) for i in icons[:4])
        + '</span><span class="page-number"></span></footer></article></template>'
        '<div id="blocks" hidden>' + "".join(blocks) + "</div>"
        f'<script id="worksheet-config" type="application/json">{config}</script>'
        f'<pre id="layout-audit" hidden>pending</pre><script>{js}</script></body></html>')


def browser_environment() -> dict:
    env = dict(os.environ)
    env.pop("LD_PRELOAD", None)
    # Even headless Chromium probes PCI in PRoot. Reuse only the existing PCI shim,
    # not the camera/process-interposing shims that normal command execution removes.
    client = env.get("BRIDGE_CLIENT")
    if client:
        shim = Path(client).parent / "native" / "libpcibridge.so"
        if shim.is_file():
            env["LD_PRELOAD"] = str(shim)
    return env


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def render(spec: dict, destination: Path) -> dict:
    browser = next((p for n in ("chromium", "chromium-browser", "google-chrome")
                    if (p := shutil.which(n))), None)
    if not browser or not shutil.which("pdfinfo") or not shutil.which("pdftotext"):
        raise DesignError("Missing PDF tools. Use check_programs for chromium, pdfinfo and "
                          "pdftotext; install missing chromium/poppler-utils with install_packages.")
    destination = Path(destination).resolve()
    if destination.suffix.lower() != ".pdf":
        raise DesignError("The output file needs a .pdf extension.")
    page = make_html(spec)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".worksheet-", dir=destination.parent) as scratch:
        root = Path(scratch)
        document, pdf = root / "document.html", root / "document.pdf"
        document.write_text(page, encoding="utf-8")
        argv = [browser, "--headless", "--disable-gpu", "--disable-dev-shm-usage",
                "--disable-background-networking", "--disable-component-update",
                "--disable-extensions", "--disable-sync", "--disable-breakpad",
                "--no-first-run", "--no-default-browser-check",
                "--host-resolver-rules=MAP * ~NOTFOUND", "--no-pdf-header-footer",
                f"--user-data-dir={root / 'profile'}", "--virtual-time-budget=5000",
                f"--print-to-pdf={pdf}", "--dump-dom", document.as_uri()]
        if os.geteuid() == 0 or Path("/data/data/com.termux/files/usr/bin/proot").exists():
            argv.insert(1, "--no-sandbox")
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=browser_environment())
        if result.returncode:
            raise DesignError("The PDF browser failed: " + result.stderr.decode(errors="replace")[-1800:])
        match = re.search(r'<pre id="layout-audit"[^>]*>(.*?)</pre>',
                          result.stdout.decode(errors="replace"), re.S)
        try:
            audit = json.loads(html.unescape(match[1])) if match else None
        except json.JSONDecodeError:
            audit = None
        if not isinstance(audit, dict) or not isinstance(audit.get("ok"), bool):
            raise DesignError("The browser did not finish the layout checks. No PDF was saved.")
        if not audit["ok"]:
            if (audit.get("code") == "page_limit" and type(audit.get("pages")) is int
                    and audit["pages"] > spec["max_pages"]
                    and audit.get("max_pages") == spec["max_pages"]):
                raise PageLimitError(audit["pages"], spec["max_pages"])
            raise DesignError(audit.get("error", "The worksheet failed its layout checks."))
        if not pdf.is_file():
            raise DesignError("The browser did not create the PDF.")
        info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=20)
        count = re.search(r"^Pages:\s+(\d+)$", info.stdout, re.M)
        size = re.search(r"^Page size:\s+612 x 792 pts", info.stdout, re.M)
        if info.returncode or not count or int(count[1]) != audit["pages"] or not size:
            raise DesignError("The printed PDF does not match the checked portrait page layout.")
        # Physical-column extraction inserts margin emoji halfway through wrapped
        # questions. Content-stream order keeps each DOM text block together.
        text = subprocess.run(["pdftotext", "-raw", str(pdf), "-"],
                              capture_output=True, text=True, timeout=20)
        if text.returncode:
            raise DesignError("Could not check the PDF's text: " + text.stderr[-600:])
        actual = _normalize(text.stdout)
        for i, source in enumerate(source_texts(spec), 1):
            if _normalize(source) not in actual:
                raise DesignError(f"Text block {i} did not survive PDF export intact. "
                                  "No PDF was saved; do not delete or paraphrase it to hide the problem.")
        audit["text_preserved"] = True
        audit["questions"] = len(spec["questions"])
        audit["multiple_choice"] = sum(bool(q["options"]) for q in spec["questions"])
        audit["challenge"] = bool(spec["challenge"])
        os.replace(pdf, destination)
        return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec")
    parser.add_argument("output")
    args = parser.parse_args()
    try:
        with Path(args.spec).open("rb") as fh:
            raw = fh.read(MAX_SPEC_BYTES + 1)
        if len(raw) > MAX_SPEC_BYTES:
            raise DesignError("The worksheet JSON is too large.")
        spec = validate_spec(json.loads(raw))
        print(json.dumps(render(spec, Path(args.output)), ensure_ascii=False))
        return 0
    except PageLimitError as exc:
        print(json.dumps({"ok": False, "code": "page_limit", "pages": exc.pages,
                          "max_pages": exc.max_pages, "error": str(exc)}))
        return 2
    except (DesignError, OSError, UnicodeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"Worksheet PDF: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
