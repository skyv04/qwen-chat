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
MAX_PAGES = 10
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
                          "Load the worksheet-pdf skill for the supported format.")
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


def _icon(value) -> str:
    value = _text(value, "icon", 8, True)
    chars = value.replace("\ufe0f", "")
    if len(chars) != 1 or unicodedata.category(chars) != "So":
        raise DesignError("Use one emoji per icon, not a word, flag or joined emoji sequence.")
    return value


def _contrast(hex_color: str) -> float:
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
    return 1.05 / (sum(v * w for v, w in zip(linear, (.2126, .7152, .0722))) + .05)


def validate_spec(value) -> dict:
    fields = {"title", "subtitle", "series", "student_line", "meta", "instructions",
              "reading_title", "reading", "reading_note", "question_title", "questions",
              "challenge", "closing_note", "theme", "max_pages"}
    value = _object(value, fields, "document")
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
    theme = _object(value.get("theme", {}), {"name", "accent", "icons", "motif"}, "theme")
    accent = theme.get("accent", "#176B56")
    if not isinstance(accent, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        raise DesignError("theme.accent must be a six-digit hex color such as #176B56.")
    if _contrast(accent) < 4.5:
        raise DesignError("theme.accent is too pale for readable headings. Use a darker color; "
                          "the renderer makes its own pale backgrounds.")
    motif = theme.get("motif", "dots")
    if motif not in ("leaves", "waves", "stars", "dots"):
        raise DesignError("theme.motif must be leaves, waves, stars or dots.")
    icons = theme.get("icons", DEFAULT_ICONS)
    if not isinstance(icons, list) or not 1 <= len(icons) <= 6:
        raise DesignError("theme.icons must contain 1-6 related emoji.")
    spec["theme"] = {
        "name": _text(theme.get("name", "Discovery"), "theme.name", 36, True),
        "accent": accent, "motif": motif, "icons": [_icon(i) for i in icons],
    }
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
        item = _object(item, {"prompt", "label", "lines", "options", "icon"}, "question")
        options = item.get("options", [])
        if not isinstance(options, list) or len(options) > 8:
            raise DesignError("Question options must be a list of at most eight strings.")
        return {
            "prompt": _text(item.get("prompt", ""), "question prompt", 3000, True),
            "label": _text(item.get("label", "Extra Challenge:" if bonus else f"Question {number}:"),
                           "question label", 100, True),
            "lines": _integer(item.get("lines", 0 if options else 5), "answer lines",
                              0 if options else 1, 12),
            "options": [_text(o, "answer option", 1000, True) for o in options],
            "icon": _icon(item.get("icon", spec["theme"]["icons"][(number - 1) % len(icons)])),
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
    config = json.dumps({"max_pages": spec["max_pages"], "blocks": len(blocks),
                         "icons": list(dict.fromkeys([*icons, "\U0001f50d",
                                                     *(q["icon"] for q in questions)]))},
                        ensure_ascii=True).replace("<", "\\u003c")
    css = (DATA / "worksheet.css").read_text()
    js = (DATA / "worksheet.js").read_text()
    accent = spec["theme"]["accent"]
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        'style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; font-src data:; '
        'img-src data:; connect-src \'none\'; frame-src \'none\'; object-src \'none\'">'
        f"<title>{_e(spec['title'])}</title><style>{_fonts()}\n{css}\n"
        f":root{{--accent:{accent}}}</style></head><body>"
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
