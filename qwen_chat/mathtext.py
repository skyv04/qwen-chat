"""Neat math for worksheet text: stacked fractions and tidy operator symbols.

A fraction such as ``3/4`` is drawn as a small inline SVG whose digits are the
glyph outlines of the page font, so it looks like type without entering the PDF
text layer. A nearly invisible ``3/4`` span sits on top of the drawing, which
keeps copy/paste and ``pdftotext -raw`` reading the fraction in order.

Only presentation changes here. Specs keep the text exactly as written, and
:func:`printed` returns the text that ends up in the PDF so checks can compare.

Stdlib only: the renderer runs this module as part of a standalone script.
"""
from __future__ import annotations

import bisect
import functools
import html
import re
import struct
from typing import NamedTuple
from pathlib import Path

REGULAR_FONTS = (Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
                 Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
BOLD_FONTS = (Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
              Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))


class Font:
    """The small part of a TrueType font needed to draw glyph outlines."""

    def __init__(self, path: Path):
        data = path.read_bytes()
        self.path = path
        self.data = data
        tables = {}
        for i in range(struct.unpack_from(">H", data, 4)[0]):
            tag, _check, offset, length = struct.unpack_from(">4sIII", data, 12 + 16 * i)
            tables[tag.decode("latin-1")] = (offset, length)
        head = tables["head"][0]
        self.upem = struct.unpack_from(">H", data, head + 18)[0]
        self.long_loca = struct.unpack_from(">h", data, head + 50)[0] == 1
        self.glyphs = struct.unpack_from(">H", data, tables["maxp"][0] + 4)[0]
        self.metrics = struct.unpack_from(">H", data, tables["hhea"][0] + 34)[0]
        self.hmtx = tables["hmtx"][0]
        self.loca = tables["loca"][0]
        self.glyf = tables["glyf"][0]
        self._read_cmap(tables["cmap"][0])

    def _read_cmap(self, base: int) -> None:
        data = self.data
        self._ends = []
        table = None
        for i in range(struct.unpack_from(">H", data, base + 2)[0]):
            platform, encoding, offset = struct.unpack_from(">HHI", data, base + 4 + 8 * i)
            if (platform, encoding) in ((3, 1), (0, 3)) and \
                    struct.unpack_from(">H", data, base + offset)[0] == 4:
                table = base + offset
                break
        if table is None:
            return
        count = struct.unpack_from(">H", data, table + 6)[0] // 2
        self._ends = list(struct.unpack_from(f">{count}H", data, table + 14))
        self._starts = struct.unpack_from(f">{count}H", data, table + 16 + 2 * count)
        self._deltas = struct.unpack_from(f">{count}h", data, table + 16 + 4 * count)
        self._range_at = table + 16 + 6 * count
        self._ranges = struct.unpack_from(f">{count}H", data, self._range_at)

    def glyph(self, char: str) -> int:
        code = ord(char)
        i = bisect.bisect_left(self._ends, code)
        if code > 0xFFFF or i >= len(self._ends) or self._starts[i] > code:
            return 0
        if not self._ranges[i]:
            return (code + self._deltas[i]) & 0xFFFF
        at = self._range_at + 2 * i + self._ranges[i] + 2 * (code - self._starts[i])
        found = struct.unpack_from(">H", self.data, at)[0]
        return (found + self._deltas[i]) & 0xFFFF if found else 0

    def advance(self, glyph: int) -> int:
        return struct.unpack_from(">H", self.data, self.hmtx + 4 * min(glyph, self.metrics - 1))[0]

    def _span(self, glyph: int) -> tuple[int, int]:
        if glyph >= self.glyphs:
            return 0, 0
        if self.long_loca:
            start, end = struct.unpack_from(">II", self.data, self.loca + 4 * glyph)
        else:
            start, end = (2 * v for v in struct.unpack_from(">HH", self.data, self.loca + 2 * glyph))
        return self.glyf + start, end - start

    def bounds(self, glyph: int) -> tuple[int, int, int, int] | None:
        start, length = self._span(glyph)
        if length < 10:
            return None
        return struct.unpack_from(">hhhh", self.data, start + 2)

    @functools.lru_cache(maxsize=512)
    def contours(self, glyph: int, depth: int = 0) -> tuple:
        """Glyph outline as contours of ``(x, y, on_curve)`` points, y pointing up."""
        start, length = self._span(glyph)
        if length < 10 or depth > 6:
            return ()
        data = self.data
        count = struct.unpack_from(">h", data, start)[0]
        at = start + 10
        if count >= 0:
            ends = struct.unpack_from(f">{count}H", data, at)
            at += 2 * count
            at += 2 + struct.unpack_from(">H", data, at)[0]
            total = ends[-1] + 1 if count else 0
            flags = []
            while len(flags) < total:
                flag = data[at]
                at += 1
                flags.append(flag)
                if flag & 8:
                    flags.extend([flag] * data[at])
                    at += 1
            flags = flags[:total]
            coords = []
            for short, same in ((2, 16), (4, 32)):
                value, values = 0, []
                for flag in flags:
                    if flag & short:
                        value += data[at] if flag & same else -data[at]
                        at += 1
                    elif not flag & same:
                        value += struct.unpack_from(">h", data, at)[0]
                        at += 2
                    values.append(value)
                coords.append(values)
            xs, ys = coords
            result, first = [], 0
            for end in ends:
                result.append(tuple((xs[k], ys[k], bool(flags[k] & 1)) for k in range(first, end + 1)))
                first = end + 1
            return tuple(result)
        result = []
        while True:
            flags, part = struct.unpack_from(">HH", data, at)
            at += 4
            if flags & 1:
                dx, dy = struct.unpack_from(">hh", data, at)
                at += 4
            else:
                dx, dy = struct.unpack_from(">bb", data, at)
                at += 2
            if not flags & 2:
                dx = dy = 0
            a, b, c, d = 1.0, 0.0, 0.0, 1.0
            if flags & 8:
                a = d = struct.unpack_from(">h", data, at)[0] / 16384
                at += 2
            elif flags & 0x40:
                a, d = (v / 16384 for v in struct.unpack_from(">hh", data, at))
                at += 4
            elif flags & 0x80:
                a, b, c, d = (v / 16384 for v in struct.unpack_from(">hhhh", data, at))
                at += 8
            for contour in self.contours(part, depth + 1):
                result.append(tuple((a * x + c * y + dx, b * x + d * y + dy, on)
                                    for x, y, on in contour))
            if not flags & 0x20:
                return tuple(result)


@functools.lru_cache(maxsize=4)
def _chain(bold: bool) -> tuple[Font, ...]:
    return tuple(Font(path) for path in (BOLD_FONTS if bold else REGULAR_FONTS) if path.exists())


def _find(char: str, bold: bool) -> tuple[Font, int] | None:
    for font in _chain(bold):
        glyph = font.glyph(char)
        if glyph:
            return font, glyph
    return None


def _num(value: float, places: int) -> str:
    text = f"{value:.{places}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _outline(contours, scale: float, left: float, base: float, places: int) -> str:
    """SVG path data for contours placed with the baseline at ``base`` (y down)."""
    out = []
    for contour in contours:
        points = [(left + x * scale, base - y * scale, on) for x, y, on in contour]
        if not points:
            continue
        first = next((i for i, point in enumerate(points) if point[2]), None)
        if first is None:
            (x0, y0, _), (x1, y1, _) = points[-1], points[0]
            points.insert(0, ((x0 + x1) / 2, (y0 + y1) / 2, True))
            first = 0
        points = points[first:] + points[:first]
        f = lambda v: _num(v, places)  # noqa: E731
        out.append(f"M{f(points[0][0])} {f(points[0][1])}")
        control = None
        for x, y, on in points[1:] + points[:1]:
            if on:
                if control:
                    out.append(f"Q{f(control[0])} {f(control[1])} {f(x)} {f(y)}")
                else:
                    out.append(f"L{f(x)} {f(y)}")
                control = None
            else:
                if control:
                    mx, my = (control[0] + x) / 2, (control[1] + y) / 2
                    out.append(f"Q{f(control[0])} {f(control[1])} {f(mx)} {f(my)}")
                control = (x, y)
        out.append("Z")
    return "".join(out)


class Frac(NamedTuple):
    whole: str
    num: str
    den: str

    @property
    def text(self) -> str:
        """The fraction as plain text, which is what the PDF text layer carries."""
        fraction = f"{_wrapped(self.num)}/{_wrapped(self.den)}"
        return f"{self.whole} {fraction}" if self.whole else fraction


_HOLD = 0xE001
_PRIVATE = re.compile("[\ue000-\uf8ff]")
_HELD = re.compile("([\ue001-\uf8ff])")
_BLANK = re.compile(r"\?|_{1,8}|[□☐]")
_TOKEN = r"\d{1,4}|\?|_{1,8}|[□☐]"
_PLAIN = re.compile(r"(?<![\w/.,:\u2044^])(?:(\d{1,3}) )?(" + _TOKEN + r")/(" + _TOKEN + r")"
                    r"(?![\w/]|[.,]\d)")
_TEX_FRAC = re.compile(r"(?<![\w.,])(?:(\d{1,3})\s*)?\\[dt]?frac\s*\{([^{}]{1,24})\}\s*\{([^{}]{1,24})\}")
_TEX_SHORT = re.compile(r"(?<![\w.,])(?:(\d{1,3})\s*)?\\[dt]?frac\s*(\d)\s*(\d)(?!\d)")
_DATED = re.compile(r"\b(?:due|dated?|deadline|until|born|week\s+of)[\s:]*(?:on\s+)?$", re.I)
_RANGED = re.compile(r"\b(?:questions?|problems?|pages?|items?|steps?|chapters?|paragraphs?|"
                     r"grades?|ages?|numbers?|exercises?|lines?|sections?|parts?|days?|weeks?|"
                     r"years?|levels?|units?|lessons?|from|between)\s+\d+\s*$", re.I)
_TEX_HINT = re.compile(r"\\(?:[A-Za-z]{2,}|[()\[\]])")
_WORD = re.compile(r"[A-Za-z]+")
_SUP = str.maketrans("0123456789+-−=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿⁱ")
_SUB = str.maketrans("0123456789+-−=()", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎")
_SUP_OK = set("0123456789+-−=()ni")
_SUB_OK = set("0123456789+-−=()")
_SIMPLE = {
    "times": "×", "div": "÷", "cdot": "·", "cdotp": "·", "pm": "±", "mp": "∓", "pi": "π",
    "le": "≤", "leq": "≤", "leqslant": "≤", "ge": "≥", "geq": "≥", "geqslant": "≥",
    "ne": "≠", "neq": "≠", "approx": "≈", "sim": "~", "angle": "∠", "measuredangle": "∠",
    "triangle": "△", "perp": "⊥", "parallel": "∥", "infty": "∞", "to": "→",
    "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "dots": "…", "ldots": "…",
    "cdots": "⋯", "degree": "°", "circ": "∘", "quad": " ", "qquad": " ", "lt": "<",
    "gt": ">", "colon": ":", "square": "□", "Box": "□", "checkmark": "✓",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "Delta": "Δ", "theta": "θ",
    "lambda": "λ", "mu": "μ", "sigma": "σ", "Sigma": "Σ", "phi": "φ", "omega": "ω",
    "Omega": "Ω", "cent": "¢", "percent": "%",
}
_WRAPPERS = {"text", "textrm", "textbf", "textit", "textsf", "mathrm", "mathbf", "mathit",
             "mathsf", "operatorname", "mbox", "boxed", "emph", "overline", "underline",
             "bar", "vec", "hat", "overrightarrow", "overleftrightarrow", "mathbb"}
_DROPPED = {"left", "right", "displaystyle", "textstyle", "big", "Big", "bigl", "bigr",
            "Bigl", "Bigr", "limits", "nolimits"}
_ESCAPES = {"%": "%", "$": "$", "&": "&", "#": "#", "_": "_", "{": "\x01", "}": "\x02",
            ",": " ", ";": " ", ":": " ", "!": "", " ": " ", "\\": " ", "(": "", ")": "",
            "[": "", "]": ""}
_LEFT = r"[\d)\]\ue001-\uf8ff]"
_RIGHT = r"[\d(\[\ue001-\uf8ff]"
_NUMBERISH = r"[\d\ue001-\uf8ff]"
_COMPARE = r"[\s\d\ue001-\uf8ff]"


def _group(text: str, at: int) -> tuple[str, int] | None:
    """One LaTeX argument at ``text[at:]``: a ``{...}`` group, a command or one character."""
    while at < len(text) and text[at] == " ":
        at += 1
    if at >= len(text) or text[at] == "}":
        return None
    if text[at] == "{":
        depth, end = 0, at
        while end < len(text):
            char = text[end]
            if char == "\\":
                end += 2
                continue
            depth += char == "{"
            depth -= char == "}"
            if not depth:
                return text[at + 1:end], end + 1
            end += 1
        return None
    if text[at] == "\\":
        word = _WORD.match(text, at + 1)
        end = word.end() if word else at + 2
        return text[at:end], end
    return text[at], at + 1


def _wrapped(part: str) -> str:
    return part if re.fullmatch(r"[\w.,?□☐²³¹⁰-⁹°π]+", part) else f"({part})"


def _script(content: str, table: dict, allowed: set, mark: str) -> str:
    if content in ("∘", "°", "o") and mark == "^":
        return "°"
    if content and set(content) <= allowed:
        return content.translate(table)
    return mark + (content if len(content) == 1 else f"({content})")


def _latex(text: str) -> str:
    """Turn the LaTeX commands people type into plain Unicode text."""
    out, at = [], 0
    while at < len(text):
        char = text[at]
        if char == "\\" and at + 1 < len(text):
            word = _WORD.match(text, at + 1)
            if not word:
                follower = text[at + 1]
                out.append(_ESCAPES.get(follower, "\\" + follower))
                at += 2
                continue
            name, at = word.group(), word.end()
            if name in _WRAPPERS:
                arg = _group(text, at)
                if arg:
                    out.append(_latex(arg[0]))
                    at = arg[1]
            elif name == "sqrt":
                root = "√"
                index = re.match(r"\s*\[\s*(\d)\s*\]", text[at:])
                if index:
                    root = {"3": "∛", "4": "∜"}.get(index.group(1), "√")
                    at += index.end()
                arg = _group(text, at)
                if arg:
                    out.append(root + _wrapped(_latex(arg[0])))
                    at = arg[1]
                else:
                    out.append(root)
            elif name in ("frac", "dfrac", "tfrac", "cfrac"):
                top = _group(text, at)
                bottom = _group(text, top[1]) if top else None
                if bottom:
                    out.append(f"{_wrapped(_latex(top[0]))}/{_wrapped(_latex(bottom[0]))}")
                    at = bottom[1]
                else:
                    out.append("\\" + name)
            elif name in _DROPPED:
                if name in ("left", "right") and at < len(text) and text[at] == ".":
                    at += 1
            elif name in _SIMPLE:
                out.append(_SIMPLE[name])
            else:
                out.append("\\" + name)
        elif char in "^_":
            previous = out[-1][-1:] if out and out[-1] else ""
            arg = _group(text, at + 1)
            script_ok = bool(previous) and (previous.isalnum() or previous in ")}")
            if not arg or char == "_" and (not script_ok or arg[0] == "_"):
                out.append(char)
                at += 1
                continue
            content = _latex(arg[0])
            if char == "^":
                out.append(_script(content, _SUP, _SUP_OK, "^"))
            else:
                out.append(_script(content, _SUB, _SUB_OK, "_"))
            at = arg[1]
        else:
            out.append(char)
            at += 1
    return "".join(out)


def _unwrap(text: str) -> str:
    """Drop math delimiters: ``\\( \\)``, ``\\[ \\]`` and ``$...$`` around real math."""
    mathy = lambda inner: bool(re.search(r"\\[A-Za-z]|\^|\{", inner))  # noqa: E731
    text = re.sub(r"\\\(\s*(.+?)\s*\\\)", r"\1", text, flags=re.S)
    text = re.sub(r"\\\[\s*(.+?)\s*\\\]", r"\1", text, flags=re.S)
    text = re.sub(r"\$\$(.+?)\$\$", lambda m: m.group(1) if mathy(m.group(1)) else m.group(),
                  text, flags=re.S)
    return re.sub(r"(?<!\\)\$(?!\s)([^$\n]{1,300}?)(?<!\s)\$",
                  lambda m: m.group(1) if mathy(m.group(1)) else m.group(), text)


def _spaced_minus(match: re.Match) -> str:
    return match.group() if _RANGED.search(match.string, 0, match.start()) else " − "


_RULES = (
    (re.compile(rf"(?<={_LEFT})( ?)\*( ?)(?={_RIGHT})"), r"\1×\2"),
    (re.compile(rf"(?<={_NUMBERISH})( ?)[xX]( ?)(?={_NUMBERISH})"), r"\1×\2"),
    (re.compile(rf"(?<={_LEFT}) [-–] (?={_RIGHT})"), _spaced_minus),
    (re.compile(rf"(?<={_LEFT})-(?=[\d(\ue001-\uf8ff][^\s=]*\s*=)"), "−"),
    (re.compile(r"(?:^|(?<=[\s(=]))-(?=\d)"), "−"),
    (re.compile(rf"(?<={_COMPARE})<=(?={_COMPARE})"), "≤"),
    (re.compile(rf"(?<={_COMPARE})>=(?={_COMPARE})"), "≥"),
    (re.compile(rf"(?<={_COMPARE})!=(?={_COMPARE})"), "≠"),
    (re.compile(r"(?<=\d)\^o\b"), "°"),
    (re.compile(r"(?<=[A-Za-z\d)\]])\^(\d{1,3})(?![\d.])"), lambda m: m.group(1).translate(_SUP)),
)


def _symbols(text: str, latex: bool) -> str:
    if latex:
        text = _latex(text)
        text = text.replace("{", "").replace("}", "").replace("\x01", "{").replace("\x02", "}")
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def _digits(token: str) -> bool:
    return token.isdigit() and not (len(token) > 1 and token[0] == "0")


def _drawable(text: str, bold: bool = False) -> bool:
    return bool(text) and all(char == " " or _find(char, bold) for char in text)


def segments(text: str) -> list:
    """Split text into plain pieces and :class:`Frac` pieces with tidy symbols."""
    if not text:
        return []
    latex = bool(_TEX_HINT.search(text))
    if latex:
        text = _unwrap(text)
    if _PRIVATE.search(text):
        return [_symbols(text, latex)]
    held: list[Frac] = []

    def hold(whole: str | None, num: str, den: str, raw: str) -> str:
        if len(held) >= 0x17FF or not (_drawable(num) and _drawable(den)):
            return raw
        mixed = whole and _digits(whole) and _digits(num) and _digits(den) and int(num) < int(den)
        held.append(Frac(whole if mixed else "", num, den))
        mark = chr(_HOLD + len(held) - 1)
        return mark if mixed or not whole else f"{whole} {mark}"

    def tex(match: re.Match) -> str:
        whole, num, den = match.groups()
        num, den = _symbols(num, True).strip(), _symbols(den, True).strip()
        if not num or not den or len(num) > 12 or len(den) > 12:
            return match.group()
        return hold(whole, num, den, f"{whole + ' ' if whole else ''}{_wrapped(num)}/{_wrapped(den)}")

    def plain(match: re.Match) -> str:
        whole, num, den = match.groups()
        sides = (num, den)
        if not any(_digits(side) for side in sides) or den in ("0", "00", "000", "0000") or \
                any(not _digits(side) and not _BLANK.fullmatch(side) for side in sides) or \
                _DATED.search(match.string, 0, match.start()):
            return match.group()
        return hold(whole, num, den, match.group())

    if latex:
        text = _TEX_FRAC.sub(tex, text)
        text = _TEX_SHORT.sub(tex, text)
    text = _PLAIN.sub(plain, text)
    text = _symbols(text, latex)
    parts: list = []
    for piece in _HELD.split(text):
        if len(piece) == 1 and _HELD.fullmatch(piece):
            parts.append(held[ord(piece) - _HOLD])
        elif piece:
            parts.append(piece)
    return parts


def printed(text: str) -> str:
    """The text that a rendered worksheet carries in its PDF text layer."""
    return "".join(part.text if isinstance(part, Frac) else part for part in segments(text))


_SCALE = 0.9          # fraction digits relative to the surrounding text
_GAP = 100            # bar to digits, in thousandths of an em
_OVERHANG = 50        # bar beyond the widest row
_SIDE = 40            # clear space outside the bar
_MARGIN = 20
_BAR = {False: 70, True: 85}
_BOX = 0.58           # width of a blank box, in ems of the digits


def _glyphs(text: str, bold: bool) -> list | None:
    run = []
    for char in text:
        found = _find(char, bold)
        if not found:
            return None
        run.append(found)
    return run


def _advance(run: list, size: float) -> float:
    return sum(font.advance(glyph) * size / font.upem for font, glyph in run)


def _digit_extent(bold: bool) -> tuple[float, float]:
    font, glyph = _find("0", bold)
    _x0, bottom, _x1, top = font.bounds(glyph)
    return top / font.upem, bottom / font.upem


@functools.lru_cache(maxsize=4)
def axis(bold: bool = False) -> float:
    """Height of the minus sign's middle above the baseline, in ems."""
    found = _find("−", bold) or _find("+", bold)
    font, glyph = found
    _x0, bottom, _x1, top = font.bounds(glyph)
    return (top + bottom) / 2 / font.upem


def _row(part: str, size: float, bold: bool) -> dict | None:
    """Glyphs (or a blank box) for one side of a fraction, measured at ``size``."""
    top, bottom = (v * size for v in _digit_extent(bold))
    if re.fullmatch(r"_{1,8}|[□☐]", part):
        return {"run": None, "width": _BOX * size, "top": top, "bottom": min(bottom, 0)}
    run = _glyphs(part, bold)
    if run is None:
        return None
    for font, glyph in run:
        box = font.bounds(glyph)
        if box:
            top = max(top, box[3] * size / font.upem)
            bottom = min(bottom, box[1] * size / font.upem)
    return {"run": run, "width": _advance(run, size), "top": top, "bottom": bottom}


def _layout(frac: Frac, bold: bool, lead: bool = False) -> dict | None:
    """Stacked fraction geometry in thousandths of an em; the bar's middle is y=0."""
    size = 1000 * _SCALE
    num, den = _row(frac.num, size, bold), _row(frac.den, size, bold)
    if not num or not den:
        return None
    bar = _BAR[bold]
    inner = max(num["width"], den["width"])
    left = (0 if lead else _SIDE) + _OVERHANG
    width = left + inner + _OVERHANG + _SIDE
    num["base"] = -bar / 2 - _GAP + num["bottom"]
    den["base"] = bar / 2 + _GAP + den["top"]
    for row in (num, den):
        row["left"] = left + (inner - row["width"]) / 2
    return {"num": num, "den": den, "size": size, "width": width,
            "bar": (left - _OVERHANG, -bar / 2, inner + 2 * _OVERHANG, bar),
            "top": num["base"] - num["top"], "bottom": den["base"] - den["bottom"]}


def _rect(x: float, y: float, w: float, h: float, places: int) -> str:
    f = lambda v: _num(v, places)  # noqa: E731
    return f"M{f(x)} {f(y)}H{f(x + w)}V{f(y + h)}H{f(x)}Z"


def _ring(x: float, y: float, w: float, h: float, stroke: float, places: int) -> str:
    f = lambda v: _num(v, places)  # noqa: E731
    a, b, c, d = x + stroke, y + stroke, x + w - stroke, y + h - stroke
    return _rect(x, y, w, h, places) + f"M{f(a)} {f(b)}V{f(d)}H{f(c)}V{f(b)}Z"


def _row_path(row: dict, size: float, scale: float, dx: float, dy: float, places: int) -> str:
    left = dx + row["left"] * scale
    if row["run"] is None:
        top, height = row["base"] - row["top"], row["top"] - row["bottom"]
        return _ring(left, dy + top * scale, row["width"] * scale, height * scale,
                     0.07 * size * scale, places)
    out = []
    for font, glyph in row["run"]:
        k = size * scale / font.upem
        out.append(_outline(font.contours(glyph), k, left, dy + row["base"] * scale, places))
        left += font.advance(glyph) * k
    return "".join(out)


def _frac_paths(shape: dict, scale: float, dx: float, dy: float, places: int) -> str:
    x, y, w, h = shape["bar"]
    return (_row_path(shape["num"], shape["size"], scale, dx, dy, places)
            + _row_path(shape["den"], shape["size"], scale, dx, dy, places)
            + _rect(dx + x * scale, dy + y * scale, w * scale, h * scale, places))


def _escape(text: str) -> str:
    return html.escape(text, quote=True).replace("\n", "<br>")


def _em(units: float) -> str:
    return _num(units / 1000, 3) + "em"


def _frac_html(frac: Frac, bold: bool) -> str:
    shape = _layout(frac, bold, lead=bool(frac.whole))
    if shape is None:
        return _escape(frac.text)
    text = Frac("", frac.num, frac.den).text
    run = _glyphs(text, bold)
    advance = _advance(run, 1000) if run else 1000 * len(text) * 0.55
    top, bottom = shape["top"] - _MARGIN, shape["bottom"] + _MARGIN
    width, height = shape["width"], bottom - top
    lift = axis(bold) * 1000 - bottom
    whole = _escape(frac.whole) + " " if frac.whole else ""
    return (f'<span class="frac">{whole}<span class="frac-text" '
            f'style="font-size:{_em(970 * width / advance)}">{_escape(text)}</span>'
            f'<svg class="frac-art" viewBox="0 {_num(top, 0)} {_num(width, 0)} {_num(height, 0)}" '
            f'style="width:{_em(width)};height:{_em(height)};vertical-align:{_em(lift)}" '
            f'fill="currentColor" aria-hidden="true">'
            f'<path d="{_frac_paths(shape, 1, 0, 0, 0)}"/></svg></span>')


def to_html(text: str, bold: bool = False) -> str:
    """Escaped HTML for body text, with stacked fractions and tidy symbols."""
    return "".join(_frac_html(part, bold) if isinstance(part, Frac) else _escape(part)
                   for part in segments(text))


def label(text: str, x: float, y: float, size: float, anchor: str = "middle",
          bold: bool = False, cls: str = "ink") -> tuple[str, tuple[float, float, float, float]]:
    """Text drawn as outlines for SVG figures, centred on ``y``; fraction bars sit on ``y``.

    Returns the ``<path>`` markup and its ``(x0, y0, x1, y1)`` box.
    """
    pieces = []
    top_digit, bottom_digit = _digit_extent(bold)
    middle = (top_digit + bottom_digit) / 2 * size
    cursor, top, bottom = 0.0, y - top_digit * size + middle, y - bottom_digit * size + middle
    for part in segments(text) if text else []:
        if isinstance(part, Frac):
            if part.whole:
                run = [found for found in (_find(c, bold) for c in part.whole + " ") if found]
                pieces.append(("run", run, cursor))
                cursor += _advance(run, size)
            shape = _layout(part, bold, lead=bool(part.whole))
            if shape:
                pieces.append(("frac", shape, cursor))
                cursor += shape["width"] * size / 1000
                top = min(top, y + shape["top"] * size / 1000)
                bottom = max(bottom, y + shape["bottom"] * size / 1000)
                continue
            part = Frac("", part.num, part.den).text
        run = [found for found in (_find(c, bold) for c in part) if found]
        pieces.append(("run", run, cursor))
        cursor += _advance(run, size)
        for font, glyph in run:
            box = font.bounds(glyph)
            if box:
                top = min(top, y + middle - box[3] * size / font.upem)
                bottom = max(bottom, y + middle - box[1] * size / font.upem)
    start = x - {"middle": cursor / 2, "end": cursor}.get(anchor, 0)
    out = []
    for kind, item, offset in pieces:
        if kind == "frac":
            out.append(_frac_paths(item, size / 1000, start + offset, y, 1))
            continue
        left = start + offset
        for font, glyph in item:
            k = size / font.upem
            out.append(_outline(font.contours(glyph), k, left, y + middle, 1))
            left += font.advance(glyph) * k
    path = "".join(out)
    markup = f'<path class="{cls}" d="{path}"/>' if path else ""
    return markup, (start, top, start + cursor, bottom)


def width(text: str, size: float, bold: bool = False) -> float:
    box = label(text, 0, 0, size, "start", bold)[1]
    return box[2] - box[0]
