"""Small math figures for worksheets: shapes, fraction models, number lines and more.

A question may carry ``"figure": {...}`` or a list of up to four figures. Input is
lenient and output is canonical: :func:`parse` turns what a model wrote into plain
dictionaries (parsing those again changes nothing), :func:`svg` draws one as inline
SVG, and :func:`infer` reads a figure out of a prompt such as "A rectangle has a
length of 6 units and a width of 4 units".

Labels are drawn as glyph outlines, so figures add nothing to the PDF text layer.
Stdlib only: the renderer runs this module as part of a standalone script.
"""
from __future__ import annotations

import math
import re

try:
    from . import mathtext
except ImportError:  # run as a script next to mathtext.py
    import mathtext

PER_QUESTION = 4
PER_DOCUMENT = 60
FIELD_ALIASES = ("figures", "diagram", "diagrams", "drawing", "drawings", "picture", "pictures",
                 "image", "images", "shape", "shapes", "visual", "visuals", "illustration",
                 "graphic", "graphics")
_OFF = {"", "none", "no", "false", "null", "nil", "n/a", "na", "nothing", "no figure",
        "no picture", "no diagram", "no image"}

_NAMES = {
    "rectangle": "rectangle", "rect": "rectangle", "oblong": "rectangle",
    "square": "square", "triangle": "triangle", "right triangle": "triangle",
    "isosceles triangle": "triangle", "equilateral triangle": "triangle",
    "scalene triangle": "triangle", "acute triangle": "triangle",
    "obtuse triangle": "triangle", "circle": "circle", "disk": "circle", "disc": "circle",
    "parallelogram": "parallelogram", "trapezoid": "trapezoid", "trapezium": "trapezoid",
    "right trapezoid": "trapezoid", "isosceles trapezoid": "trapezoid", "rhombus": "rhombus",
    "diamond": "rhombus", "pentagon": "pentagon", "hexagon": "hexagon", "heptagon": "heptagon",
    "octagon": "octagon", "nonagon": "nonagon", "decagon": "decagon", "polygon": "polygon",
    "regular polygon": "polygon", "regular pentagon": "pentagon", "regular hexagon": "hexagon",
    "regular octagon": "octagon", "angle": "angle", "right angle": "angle",
    "acute angle": "angle", "obtuse angle": "angle", "straight angle": "angle",
    "lines": "lines", "line": "lines", "line segment": "lines", "segment": "lines",
    "ray": "lines", "parallel lines": "lines", "perpendicular lines": "lines",
    "intersecting lines": "lines", "fraction": "fraction", "fraction model": "fraction",
    "fraction bar": "fraction", "fraction strip": "fraction", "fraction circle": "fraction",
    "fraction set": "fraction", "fraction tiles": "fraction", "pie": "fraction",
    "number line": "number_line", "numberline": "number_line", "array": "array",
    "dot array": "array", "area model": "area_model", "box model": "area_model",
    "partial products": "area_model", "prism": "prism", "rectangular prism": "prism",
    "box": "prism", "cuboid": "prism", "cube": "cube", "triangular prism": "triangular_prism",
    "triangle prism": "triangular_prism", "coordinate grid": "coordinate_grid",
    "coordinate plane": "coordinate_grid", "grid": "coordinate_grid", "clock": "clock",
    "analog clock": "clock", "base ten": "base_ten", "base ten blocks": "base_ten",
    "place value blocks": "base_ten", "bar graph": "bar_graph", "bar chart": "bar_graph",
}
_POLYGONS = {"pentagon": 5, "hexagon": 6, "heptagon": 7, "octagon": 8, "nonagon": 9, "decagon": 10}
_VERTEX_COUNT = {"rectangle": 4, "square": 4, "triangle": 3, "parallelogram": 4,
                 "trapezoid": 4, "rhombus": 4, **_POLYGONS}
_ALIASES = {
    "type": "shape", "name": "shape", "figure": "shape", "object": "shape",
    "shape_type": "shape", "l": "length", "len": "length", "long": "length", "w": "width",
    "breadth": "width", "wide": "width", "h": "height", "tall": "height", "high": "height",
    "altitude": "height", "b": "base", "r": "radius", "d": "diameter", "s": "side",
    "side_length": "side", "sidelength": "side", "edge": "side", "edge_length": "side",
    "length_of_side": "side", "units": "unit", "pieces": "parts", "slices": "parts",
    "sections": "parts", "equal_parts": "parts", "denominator": "parts", "filled": "shaded",
    "colored": "shaded", "coloured": "shaded", "shade": "shaded", "shaded_parts": "shaded",
    "numerator": "shaded", "deg": "degrees", "degree": "degrees", "measure": "degrees",
    "min": "start", "from": "start", "max": "end", "to": "end", "ticks": "divisions",
    "intervals": "divisions", "steps": "divisions", "dims": "dimensions",
    "num_sides": "sides", "number_of_sides": "sides", "n": "sides", "top_base": "top",
    "base1": "top", "base_1": "top", "short_base": "top", "bottom_base": "bottom",
    "base2": "bottom", "base_2": "bottom", "long_base": "bottom", "vertex_labels": "vertices",
    "vertex_names": "vertices", "corners": "vertices", "edge_labels": "edges",
    "side_labels": "edges", "cols": "columns", "column": "columns", "row": "rows",
    "hours": "hour", "minutes": "minute", "data": "bars", "values": "bars",
    "text": "description", "alt": "description", "show_grid": "grid", "unit_squares": "grid",
    "gridlines": "grid", "unit_cubes": "cubes", "connected": "connect", "join": "connect",
    "value": "fraction", "type_of_lines": "kind", "line_type": "kind", "max_value": "size",
    "triangle_base": "base", "triangle_height": "height", "prism_length": "length",
}
_UNITS = {
    "unit": "units", "units": "units", "u": "units", "inch": "in", "inches": "in", "in": "in",
    "foot": "ft", "feet": "ft", "ft": "ft", "yard": "yd", "yards": "yd", "yd": "yd",
    "mile": "mi", "miles": "mi", "mi": "mi", "millimeter": "mm", "millimeters": "mm",
    "millimetre": "mm", "millimetres": "mm", "mm": "mm", "centimeter": "cm",
    "centimeters": "cm", "centimetre": "cm", "centimetres": "cm", "cm": "cm",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m", "m": "m", "kilometer": "km",
    "kilometers": "km", "kilometre": "km", "kilometres": "km", "km": "km",
}
_NUMBER = r"\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+"
_MEASURE = re.compile(rf"\s*({_NUMBER}|\?|[a-z])\s*([a-z]{{1,12}}\.?)?\s*", re.I)
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


class FigureError(ValueError):
    pass


class Impossible(FigureError):
    """The figure's own numbers contradict each other: a mistake in the question itself."""


def shape_name(value) -> str | None:
    if not isinstance(value, str):
        return None
    name = re.sub(r"[\s_\-]+", " ", value.strip().lower())
    for candidate in (name, name[:-1] if name.endswith("s") else name, name[:-2]
                      if name.endswith("es") else name):
        if candidate in _NAMES:
            return _NAMES[candidate]
    return None


def unit_name(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().lower().rstrip(".")
    text = re.sub(r"^(?:square|sq\.?|cubic)\s+", "", text)
    if text in _UNITS:
        return _UNITS[text]
    if re.fullmatch(r"[a-z]{1,10}", text):
        return text
    raise FigureError(f"unit {value!r} is not a short unit name such as cm or units")


def number(value) -> float | None:
    """A drawable amount: 6, 2.5, "3 1/2" and "3/4" work; "?" and "x" give None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)?\s*(?:(\d+)/(\d+))?\s*", value)
    if not match or not any(match.groups()) or match.group(3) == "0":
        return None
    whole = float(match.group(1) or 0)
    if match.group(2):
        part = int(match.group(2)) / int(match.group(3))
        whole = whole - part if whole < 0 else whole + part
    return whole


def _plain(value: float):
    return int(value) if float(value).is_integer() else round(float(value), 4)


def measure(value) -> tuple[object, str | None]:
    """Canonical length plus any unit written with it: ``"6 units"`` -> ``(6, "units")``."""
    if isinstance(value, bool) or value is None:
        raise FigureError("a length must be a number such as 6, 2.5 or \"3 1/2\", or \"?\"")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or not 0 < value <= 100000:
            raise FigureError(f"length {value!r} must be above 0 and at most 100000")
        return _plain(value), None
    if not isinstance(value, str):
        raise FigureError("a length must be a number such as 6 or a short string such as \"6 cm\"")
    match = _MEASURE.fullmatch(value)
    if not match:
        raise FigureError(f"length {value!r} must look like 6, 2.5, \"3 1/2\", \"6 cm\" or \"?\"")
    amount, unit = match.group(1), match.group(2)
    if amount.isalpha() and len(amount) == 1 and unit:
        raise FigureError(f"length {value!r} must look like 6, 2.5, \"3 1/2\", \"6 cm\" or \"?\"")
    unit = unit_name(unit) if unit else None
    if re.fullmatch(r"\d+(?:\.\d+)?", amount):
        return measure(float(amount))[0], unit
    if amount not in ("?",) and not amount.isalpha() and not number(amount):
        raise FigureError(f"length {value!r} must be above 0")
    return amount, unit


def count(value, low: int, high: int, what: str) -> int:
    if isinstance(value, str) and value.strip().lower() in _WORDS:
        value = _WORDS[value.strip().lower()]
    elif isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
        value = int(value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise FigureError(f"{what} must be a whole number from {low} to {high}")
    return value


def flag(value, what: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("yes", "true", "on", "no", "false", "off"):
        return value.strip().lower() in ("yes", "true", "on")
    raise FigureError(f"{what} must be true or false")


def short_text(value, limit: int, what: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise FigureError(f"{what} must be short text")
    text = _plain(value) if isinstance(value, (int, float)) else value.strip()
    text = str(text)
    if len(text) > limit or "\n" in text:
        raise FigureError(f"{what} must be at most {limit} characters on one line")
    return text


def fmt(value) -> str:
    if isinstance(value, float):
        value = _plain(value)
    if isinstance(value, int):
        return f"{value:,}" if abs(value) >= 10000 else str(value)
    return str(value)


def dim_label(value, unit: str | None) -> str:
    if value is None:
        return ""
    if value == "?" or not unit:
        return fmt(value)
    if unit == "units" and value == 1:
        unit = "unit"
    return f"{fmt(value)} {unit}"


def _key(key) -> str:
    return re.sub(r"[\s\-]+", "_", str(key).strip().lower())


class _Reader:
    """One raw figure object, with aliases resolved and the used keys remembered."""

    def __init__(self, raw: dict):
        self.values: dict = {}
        for key, value in raw.items():
            name = _key(key)
            self.values.setdefault(_ALIASES.get(name, name), value)
        self.used: set = set()
        self.shape_key = ""
        self.unit: str | None = None
        self.notes: list[str] = []

    def get(self, key: str, default=None):
        if key == self.shape_key:
            return default
        self.used.add(key)
        value = self.values.get(key)
        return default if value is None or value == "" else value

    def length(self, *keys: str):
        for key in keys:
            value = self.get(key)
            if value is not None:
                amount, unit = measure(value)
                if unit and self.unit and unit != self.unit:
                    self.notes.append(f"{key} is in {unit} but the figure uses {self.unit}")
                self.unit = self.unit or unit
                return amount
        return None

    def lengths(self, key: str, size: int) -> list | None:
        value = self.get(key)
        if value is None:
            return None
        if isinstance(value, str):
            value = [part for part in re.split(r"\s*(?:,|\bby\b|×|\bx\b|\*)\s*", value.strip()) if part]
        if not isinstance(value, list) or len(value) != size:
            raise FigureError(f"{key} needs {size} lengths")
        out = []
        for item in value:
            amount, unit = measure(item)
            self.unit = self.unit or unit
            out.append(amount)
        return out

    def count(self, key: str, low: int, high: int, default=None):
        value = self.get(key)
        return default if value is None else count(value, low, high, key)

    def flag(self, key: str) -> bool:
        value = self.get(key)
        return False if value is None else flag(value, key)

    def names(self, key: str, size: int) -> list | None:
        value = self.get(key)
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            value = re.split(r"[\s,]+", value) if re.search(r"[\s,]", value) else list(value)
        if not isinstance(value, list) or len(value) != size:
            raise FigureError(f"{key} needs {size} short names, one per corner, such as "
                              f"{list('ABCDEFGHIJ'[:size])}")
        return [short_text(item, 3, f"{key} name") for item in value]

    def leftovers(self) -> list[str]:
        return sorted(key for key in self.values if key not in self.used)


def _angle_value(value):
    if value is None or value == "":
        return None
    if value == "?":
        return "?"
    if isinstance(value, str):
        value = value.strip().rstrip("°").strip()
        if value.lower().endswith("degrees"):
            value = value[:-7].strip()
        if value == "?":
            return "?"
        value = number(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 360:
        raise FigureError("angles must be degrees between 0 and 360, or \"?\"")
    return _plain(value)


def _split_place_value(value: int) -> list[int]:
    digits = str(value)
    parts = [int(d) * 10 ** (len(digits) - i - 1) for i, d in enumerate(digits) if d != "0"]
    return parts[:4] or [value]


def _factor_list(r: _Reader, key: str) -> list | None:
    value = r.get(key)
    if value is None:
        return None
    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
        amount = number(value) if isinstance(value, str) else value
        if amount is None or not float(amount).is_integer() or not 0 < amount < 10000:
            raise FigureError(f"{key} must be a whole number below 10000 or a list such as [30, 3]")
        return _split_place_value(int(amount))
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        raise FigureError(f"{key} must list 1 to 4 parts, such as [30, 3]")
    out = []
    for item in value:
        amount = number(item) if isinstance(item, str) else item
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 < amount <= 100000:
            raise FigureError(f"{key} parts must be numbers above 0")
        out.append(_plain(amount))
    return out


def _point(item, what: str) -> dict:
    if isinstance(item, str):
        match = re.fullmatch(r"\s*([A-Za-z]{0,2})\s*\(?\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)?\s*", item)
        if not match:
            raise FigureError(f"{what} {item!r} must look like [2, 3] or \"A(2, 3)\"")
        label, x, y = match.groups()
        item = {"x": float(x), "y": float(y), "label": label}
    if isinstance(item, (list, tuple)) and len(item) in (2, 3):
        item = {"x": item[0], "y": item[1], "label": item[2] if len(item) == 3 else ""}
    if not isinstance(item, dict):
        raise FigureError(f"{what} must be [x, y] or {{\"x\": 2, \"y\": 3, \"label\": \"A\"}}")
    x, y = (number(item.get(k)) if isinstance(item.get(k), str) else item.get(k) for k in ("x", "y"))
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (x, y)):
        raise FigureError(f"{what} needs numeric x and y")
    out = {"x": _plain(x), "y": _plain(y)}
    label = item.get("label") or item.get("name")
    if label:
        out["label"] = short_text(label, 6, f"{what} label")
    return out


def _number_point(item, what: str) -> dict:
    if not isinstance(item, dict):
        item = {"value": item}
    value = item.get("value", item.get("at", item.get("x")))
    if isinstance(value, str) and number(value) is not None:
        value = value.strip() if "/" in value else _plain(number(value))
    if isinstance(value, bool) or not isinstance(value, (int, float, str)) or number(value) is None:
        raise FigureError(f"{what} needs a value such as 3 or \"3/4\"")
    if isinstance(value, float):
        value = _plain(value)
    out = {"value": value}
    label = item.get("label") or item.get("name")
    if label:
        out["label"] = short_text(label, 8, f"{what} label")
    return out


def _parts(r: _Reader, out: dict, high: int = 24) -> None:
    parts = r.count("parts", 1, high)
    shaded = r.count("shaded", 0, 100)
    if shaded is not None and parts is None:
        raise FigureError("shaded needs parts, the number of equal parts")
    if parts is not None and (parts > 1 or shaded):
        out["parts"] = parts
    if shaded:
        if shaded > (parts or 1):
            raise FigureError(f"shaded ({shaded}) cannot be more than parts ({parts})")
        out["shaded"] = shaded


_TRIANGLE_KINDS = ("right", "equilateral", "isosceles", "scalene", "acute", "obtuse")
_ANGLE_KINDS = {"acute": 50, "right": 90, "obtuse": 125, "straight": 180, "reflex": 250}
_LINE_KINDS = ("parallel", "perpendicular", "intersecting", "segment", "ray", "line")


def _named_kind(name: str, choices) -> str | None:
    return next((choice for choice in choices if re.search(rf"\b{choice}\b", name)), None)


def _style(r: _Reader, name: str, choices, what: str) -> str | None:
    value = r.get("kind") or r.get("style") or r.get("variant")
    if value is None:
        return _named_kind(name, choices)
    text = str(value).strip().lower()
    found = _named_kind(text, choices)
    if not found:
        raise FigureError(f"{what} kind must be one of: {', '.join(choices)}")
    return found


def _build(kind: str, name: str, r: _Reader) -> dict:
    out: dict = {"shape": kind}
    unit = r.get("unit")
    if unit is not None:
        r.unit = unit_name(str(unit))
    if kind in ("rectangle", "square"):
        dims = r.lengths("dimensions", 2)
        if kind == "rectangle":
            out["length"] = r.length("length", "base") or (dims[0] if dims else None)
            out["width"] = r.length("width", "height") or (dims[1] if dims else None)
        else:
            out["side"] = r.length("side", "length", "width") or (dims[0] if dims else None)
        _parts(r, out)
        out["grid"] = r.flag("grid")
        out["vertices"] = r.names("vertices", 4)
    elif kind == "triangle":
        out["base"] = r.length("base")
        out["height"] = r.length("height")
        sides = r.lengths("sides", 3) if isinstance(r.values.get("sides"), (list, str)) else None
        side = r.length("side")
        style = _style(r, name, _TRIANGLE_KINDS, "triangle")
        if r.flag("right"):
            style = "right"
        if side is not None and not sides:
            sides = [side] * 3
            style = style or "equilateral"
        out["sides"] = sides
        angles = r.get("angles")
        if angles is not None:
            if not isinstance(angles, list) or len(angles) != 3:
                raise FigureError("angles needs 3 entries: [left, right, top], using null to hide one")
            angles = [_angle_value(a) for a in angles]
            if any(a is not None for a in angles):
                out["angles"] = angles
        out["kind"] = style
        out["vertices"] = r.names("vertices", 3)
    elif kind == "circle":
        out["radius"] = r.length("radius")
        out["diameter"] = r.length("diameter")
        _parts(r, out)
    elif kind == "parallelogram":
        out["base"] = r.length("base", "length")
        out["height"] = r.length("height")
        out["side"] = r.length("side", "width")
        out["vertices"] = r.names("vertices", 4)
    elif kind == "trapezoid":
        bases = r.lengths("bases", 2) if r.values.get("bases") is not None else None
        out["top"] = r.length("top") or (bases[0] if bases else None)
        out["bottom"] = r.length("bottom", "base") or (bases[1] if bases else None)
        top, bottom = number(out["top"]), number(out["bottom"])
        if top and bottom and top > bottom:
            out["top"], out["bottom"] = out["bottom"], out["top"]
        out["height"] = r.length("height")
        out["right"] = r.flag("right") or "right" in name
        out["vertices"] = r.names("vertices", 4)
    elif kind == "rhombus":
        out["side"] = r.length("side")
        diagonals = r.lengths("diagonals", 2) if r.values.get("diagonals") is not None else None
        out["diagonals"] = diagonals
        out["vertices"] = r.names("vertices", 4)
    elif kind in _POLYGONS or kind == "polygon":
        points = r.get("points")
        if kind == "polygon" and points is not None:
            if not isinstance(points, list) or not 3 <= len(points) <= 12:
                raise FigureError("points must list 3 to 12 corners such as [[0,0],[6,0],[6,4]]")
            corners = [_point(p, "point") for p in points]
            out["points"] = [[c["x"], c["y"]] for c in corners]
            edges = r.get("edges")
            if edges is not None:
                if not isinstance(edges, list) or len(edges) > len(points):
                    raise FigureError("edges must list one label per side, in point order")
                out["edges"] = [None if e in (None, "") else _edge(e, r) for e in edges]
            out["grid"] = r.flag("grid")
            out["vertices"] = r.names("vertices", len(points))
        else:
            if kind == "polygon":
                out["sides"] = r.count("sides", 3, 12, 6)
            out["side"] = r.length("side", "length")
            out["triangles"] = r.flag("triangles")
            out["vertices"] = r.names("vertices", _POLYGONS.get(kind) or out["sides"])
    elif kind == "angle":
        out["degrees"] = _angle_value(r.get("degrees"))
        style = _style(r, name, tuple(_ANGLE_KINDS), "angle")
        if out["degrees"] is None and style:
            out["kind"] = style
        label = r.get("label")
        out["label"] = None if label is None else short_text(label, 12, "label")
    elif kind == "lines":
        style = _style(r, name, _LINE_KINDS, "lines") or "line"
        out["kind"] = style
        points = r.get("points")
        if points is not None:
            if isinstance(points, str):
                points = list(points.replace(" ", "")) if len(points) <= 4 else None
            if not isinstance(points, list) or len(points) > 4:
                raise FigureError("points must list up to 4 short point names such as [\"A\", \"B\"]")
            out["points"] = [short_text(p, 3, "point name") for p in points]
    elif kind == "fraction":
        value = r.get("fraction")
        if value is not None:
            match = re.fullmatch(r"\s*(?:(\d+)\s+)?(\d+)\s*/\s*(\d+)\s*", str(value))
            if not match or int(match.group(3)) < 1:
                raise FigureError("fraction must look like \"3/8\" or \"1 1/4\"")
            whole, top, bottom = int(match.group(1) or 0), int(match.group(2)), int(match.group(3))
            r.values.setdefault("parts", bottom)
            r.values.setdefault("shaded", whole * bottom + top)
        style = r.get("model") or r.get("kind") or ("circle" if re.search(r"circle|pie", name)
                                                   else "set" if "set" in name else "bar")
        style = {"strip": "bar", "rectangle": "bar", "tape": "bar", "pie": "circle",
                 "group": "set", "dots": "set"}.get(str(style).lower(), str(style).lower())
        if style not in ("bar", "circle", "set"):
            raise FigureError("model must be bar, circle or set")
        out["model"] = style
        parts = r.count("parts", 1, 24)
        if parts is None:
            raise FigureError("a fraction model needs parts, such as {\"fraction\": \"3/8\"}")
        out["parts"] = parts
        out["shaded"] = r.count("shaded", 0, 4 * parts, 0)
    elif kind == "number_line":
        start, end = r.get("start", 0), r.get("end")
        start = number(start) if isinstance(start, str) else start
        end = number(end) if isinstance(end, str) else end
        if end is None:
            end = start + 10 if isinstance(start, (int, float)) else None
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (start, end)) \
                or not -10000 <= start < end <= 10000:
            raise FigureError("a number line needs numbers start < end, such as start 0 and end 1")
        out["start"], out["end"] = _plain(start), _plain(end)
        span = end - start
        default = int(span) if float(span).is_integer() and span <= 20 else 10
        out["divisions"] = r.count("divisions", 1, 24, default)
        labels = r.get("labels")
        if isinstance(labels, list):
            if len(labels) != out["divisions"] + 1:
                raise FigureError(f"labels needs {out['divisions'] + 1} entries, one per tick")
            labels = ["" if item is None else short_text(item, 8, "tick label") for item in labels]
        elif labels is not None:
            labels = str(labels).strip().lower()
            if labels not in ("all", "ends", "none"):
                raise FigureError("labels must be all, ends, none, or a list with one per tick")
        out["labels"] = labels
        points = r.get("points")
        if points is not None:
            points = points if isinstance(points, list) else [points]
            if len(points) > 6:
                raise FigureError("a number line takes at most 6 points")
            points = [_number_point(p, "point") for p in points]
            for p in points:
                if not start - 1e-9 <= number(p["value"]) <= end + 1e-9:
                    raise FigureError(f"point {p['value']} is off the line ({fmt(start)} to {fmt(end)})")
            out["points"] = points
    elif kind == "array":
        out["rows"] = r.count("rows", 1, 12)
        out["columns"] = r.count("columns", 1, 12)
        if out["rows"] is None or out["columns"] is None:
            raise FigureError("an array needs rows and columns")
    elif kind == "area_model":
        out["rows"] = _factor_list(r, "rows")
        out["columns"] = _factor_list(r, "columns")
        if not out["rows"] or not out["columns"]:
            raise FigureError("an area model needs rows and columns, such as rows [10, 7] and columns [30, 3]")
        cells = r.get("cells")
        if isinstance(cells, list):
            if len(cells) != len(out["rows"]) or any(not isinstance(row, list) or len(row) != len(out["columns"]) for row in cells):
                raise FigureError("cells must have one row per row part and one entry per column part")
            cells = [["" if c is None else short_text(c, 8, "cell") for c in row] for row in cells]
        elif cells is not None:
            cells = str(cells).strip().lower()
            if cells not in ("blank", "products"):
                raise FigureError("cells must be blank, products, or a list of rows")
            cells = None if cells == "blank" else cells
        out["cells"] = cells
    elif kind == "triangular_prism":
        out["base"] = r.length("base")
        out["height"] = r.length("height")
        out["length"] = r.length("length", "depth", "prism_height")
        style = r.get("kind")
        if r.flag("right") or isinstance(style, str) and "right" in style.lower():
            out["kind"] = "right"
    elif kind in ("prism", "cube"):
        if kind == "prism":
            dims = r.lengths("dimensions", 3)
            out["length"] = r.length("length") or (dims[0] if dims else None)
            out["width"] = r.length("width", "depth") or (dims[1] if dims else None)
            out["height"] = r.length("height") or (dims[2] if dims else None)
        else:
            out["side"] = r.length("side", "length")
        out["cubes"] = r.flag("cubes")
    elif kind == "coordinate_grid":
        out["size"] = r.count("size", 2, 20, 10)
        points = r.get("points")
        if points is not None:
            if not isinstance(points, list) or len(points) > 12:
                raise FigureError("points must list up to 12 points such as [[2, 3], [5, 1]]")
            points = [_point(p, "point") for p in points]
            for p in points:
                if not (0 <= p["x"] <= out["size"] and 0 <= p["y"] <= out["size"]):
                    raise FigureError(f"point ({fmt(p['x'])}, {fmt(p['y'])}) is off the grid (0 to {out['size']})")
            out["points"] = points
        out["connect"] = r.flag("connect")
    elif kind == "clock":
        time = r.get("time")
        if time is not None:
            match = re.fullmatch(r"\s*(\d{1,2})\s*:\s*(\d{2})\s*(?:[ap]\.?m\.?)?\s*", str(time), re.I)
            if not match:
                raise FigureError("time must look like \"3:45\"")
            r.values.setdefault("hour", int(match.group(1)))
            r.values.setdefault("minute", int(match.group(2)))
        hour = r.count("hour", 0, 23, 12)
        out["hour"] = hour % 12 or 12
        out["minute"] = r.count("minute", 0, 59, 0)
    elif kind == "base_ten":
        total = r.get("number")
        if total is not None:
            total = count(total, 0, 999, "number")
            r.values.setdefault("hundreds", total // 100)
            r.values.setdefault("tens", total // 10 % 10)
            r.values.setdefault("ones", total % 10)
        out["hundreds"] = r.count("hundreds", 0, 9, 0)
        out["tens"] = r.count("tens", 0, 19, 0)
        out["ones"] = r.count("ones", 0, 19, 0)
        if not out["hundreds"] + out["tens"] + out["ones"]:
            raise FigureError("base-ten blocks need hundreds, tens or ones")
    elif kind == "bar_graph":
        bars = r.get("bars")
        if isinstance(bars, dict):
            bars = [{"label": k, "value": v} for k, v in bars.items()]
        if not isinstance(bars, list) or not 1 <= len(bars) <= 8:
            raise FigureError("bars must list 1 to 8 bars such as {\"Mon\": 4, \"Tue\": 6}")
        clean = []
        for bar in bars:
            if isinstance(bar, (list, tuple)) and len(bar) == 2:
                bar = {"label": bar[0], "value": bar[1]}
            if not isinstance(bar, dict):
                raise FigureError("each bar needs a label and a value")
            label = bar.get("label", bar.get("name", bar.get("category")))
            value = bar.get("value", bar.get("count", bar.get("amount")))
            value = number(value) if isinstance(value, str) else value
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100000:
                raise FigureError("bar values must be numbers from 0 to 100000")
            clean.append({"label": short_text(label or "", 10, "bar label"), "value": _plain(value)})
        out["bars"] = clean
        title = r.get("title")
        out["title"] = None if title is None else short_text(title, 40, "title")
    if kind in _VERTEX_COUNT or kind in ("circle", "prism", "cube", "triangular_prism", "polygon"):
        out["unit"] = r.unit
    caption = r.get("caption")
    out["caption"] = None if caption is None else short_text(caption, 24, "caption")
    return {key: value for key, value in out.items() if value is not None and value is not False}


def _edge(value, r: _Reader):
    amount, unit = measure(value)
    r.unit = r.unit or unit
    return amount


def _figure(item: dict) -> tuple[list[dict], list[str]]:
    r = _Reader(item)
    for key in ("shape", "kind", "model"):
        kind = shape_name(r.values.get(key))
        if kind:
            name = re.sub(r"[\s_\-]+", " ", str(r.values[key]).strip().lower())
            r.used.add(key)
            r.shape_key = key
            break
    else:
        description = r.get("description")
        found = infer(description, explicit=True) if isinstance(description, str) else []
        if not found:
            raise FigureError("needs \"shape\", such as rectangle, square, triangle, circle, "
                              "number_line, fraction or area_model")
        return found, [f"read the figure from its description {description[:50]!r}"]
    extra = []
    if kind == "prism":
        for key in ("base_shape", "base_type", "cross_section", "face", "kind", "base"):
            value = r.values.get(key)
            if key != r.shape_key and isinstance(value, str) and re.match(r"\s*triang(?:le|ular)\b",
                                                                          value, re.I):
                kind = "triangular_prism"
                del r.values[key]
                extra.append(f"drew a triangular prism ({key} {value!r})")
                if "base" not in r.values and "width" in r.values:
                    r.values["base"] = r.values.pop("width")  # the box's width is the triangle's base
                break
    figure = _build(kind, name, r)
    if kind == "angle" and "right" in name and "degrees" not in figure:
        figure["degrees"] = 90
        figure.pop("kind", None)
    ignored = [key for key in r.leftovers() if key not in ("description", "label", "title")]
    notes = list(r.notes) + extra
    if ignored:
        notes.append(f"ignored {', '.join(ignored)} (not used by a {kind.replace('_', ' ')})")
    return [figure], notes


def parse(value, where: str = "figure") -> tuple[list[dict], list[str], list[str]]:
    """Canonical figures from a lenient ``figure`` value, plus notes and problems."""
    if value is None or value is False or isinstance(value, str) and value.strip().lower() in _OFF:
        return [], [], []
    items = value if isinstance(value, list) else [value]
    figures, notes, problems = [], [], []
    for index, item in enumerate(items):
        place = where if len(items) == 1 else f"{where} {index + 1}"
        if isinstance(item, str):
            if item.strip().lower() in _OFF:
                continue
            found = infer(item, explicit=True)
            if not found:
                problems.append(f"{place}: could not tell what to draw from {item[:60]!r}; use an "
                                "object such as {\"shape\": \"rectangle\", \"length\": 6, \"width\": 4}")
            figures.extend(found)
            continue
        if not isinstance(item, dict):
            problems.append(f"{place} must be an object such as {{\"shape\": \"circle\", \"radius\": 3}}")
            continue
        try:
            found, extra = _figure(item)
        except FigureError as error:
            problems.append(f"{place}: {error}")
            continue
        figures.extend(found)
        notes.extend(f"{place}: {note}" for note in extra)
    if len(figures) > PER_QUESTION:
        problems.append(f"{where} has {len(figures)} figures; use at most {PER_QUESTION}")
        figures = figures[:PER_QUESTION]
    return figures, notes, problems


_TRIANGULAR_PRISM = re.compile(r"\btriang(?:le|ular)\s+prisms?\b", re.I)
_BOX_PRISM = re.compile(r"\b(?:rectangular\s+prisms?|cuboids?)\b", re.I)


def follow_prompt(found: list[dict], prompt) -> tuple[list[dict], list[str]]:
    """Given figures, corrected where the prompt plainly describes another solid: a box
    drawn for "A triangular prism has..." would mislead. Returns the figures and notes."""
    if not isinstance(prompt, str) or not _TRIANGULAR_PRISM.search(prompt) or _BOX_PRISM.search(prompt):
        return found, []
    boxes = [i for i, figure in enumerate(found) if figure.get("shape") == "prism"]
    if len(boxes) != 1:
        return found, []
    old = found[boxes[0]]
    raw = {"shape": "triangular_prism"}
    for key, target in (("width", "base"), ("height", "height"), ("length", "length")):
        if key in old:
            raw[target] = old[key]
    told = next((f for f in infer(prompt, explicit=True) if f["shape"] == "triangular_prism"), {})
    raw.update({key: value for key, value in told.items() if key != "shape"})
    for key in ("unit", "caption"):
        if key in old and key not in raw:
            raw[key] = old[key]
    try:
        figure = _figure(raw)[0][0]
    except FigureError:
        return found, []
    fixed = found[:boxes[0]] + [figure] + found[boxes[0] + 1:]
    return fixed, ["drew a triangular prism, as the prompt describes, instead of a box"]


_COUNT = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_AMOUNT = r"(\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)"
_UNIT = (r"(?:\s*(units?|cm|centimet(?:er|re)s?|mm|millimet(?:er|re)s?|km|kilomet(?:er|re)s?|"
         r"met(?:er|re)s?|m|inch(?:es)?|ft|feet|foot|yd|yards?|miles?)\b)?")
_IS = r"\s*(?:of|is|are|=|:|measures?|measuring|that (?:is|are|measures?))?\s*(?:about\s+|each\s+)?"
_FOUND = re.compile(
    r"(?<!perfect\s)\b(?:(?P<style>right|isosceles|equilateral|scalene|obtuse|acute)\s+)?"
    r"(?P<name>triangular\s+prisms?|triangles?|triangular|rectangles?|rectangular(?!\s+prisms?)|squares?|circles?|"
    r"circular|parallelograms?|trapezoids?|trapeziums?|rhombus(?:es)?|"
    r"(?:pentagon|hexagon|heptagon|octagon|nonagon|decagon)s?|rectangular\s+prisms?|cubes?|"
    r"number\s+lines?)\b"
    r"(?!\s+(?:units?|feet|foot|inch(?:es)?|yards?|miles?|cm|mm|km|ft|yd|m|"
    r"centimet(?:er|re)s?|met(?:er|re)s?|roots?|numbers?|tiles?|prisms?|pyramids?|cylinders?|"
    r"cones?|graphs?|charts?|plots?)\b)", re.I)
_VERB = re.compile(r"\s+(?:the|your|each|all|every|one|which|whichever|yes|no|true|false|correct|"
                   r"a|an|any|both|two|three|four)\b")
_MADE_OF = re.compile(r"\b(?:made|composed|formed|built|divided)\s+(?:up\s+)?(?:of|from|into)\s+\w+\s+"
                      r"(?:(?:equilateral|equal|identical|congruent|same-size)\s+)?triangles\b")
_SHAPE_WORD = (r"(?:shapes?|figures?|triangles?|rectangles?|squares?|circles?|polygons?|angles?|"
               r"prisms?|cubes?|parallelograms?|trapezoids?|rhombus(?:es)?|"
               r"(?:pent|hex|hept|oct|non|dec)agons?|number\s+lines?|(?:bar\s+)?graphs?|charts?|"
               r"line\s+plots?|pictographs?|clocks?|arrays?|area\s+models?|grids?|diagrams?|"
               r"pictures?|images?|drawings?|models?)")
# A prompt that refers to a picture. Words alone ("an important figure", "the kindness shown")
# are not enough: reading questions use them too.
_PICTURE = re.compile(
    rf"\b{_SHAPE_WORD}\s+(?:(?:shown|pictured|drawn|illustrated)\b|below\b|above\b|"
    r"shows?\b|(?:on|at|to)\s+the\s+(?:right|left)\b)"
    r"|\b(?:shown|pictured|drawn|illustrated)\s+(?:below|above|here)\b|\bas\s+(?:shown|pictured)\b"
    rf"|\b(?:this|these|the\s+following)\s+{_SHAPE_WORD}"
    r"|\b(?:in|on|from|use|using|look\s+at|see|study)\s+(?:the|this)\s+(?:coordinate\s+)?"
    r"(?:diagram|picture|figure|image|drawing|(?:bar\s+)?graph|chart|line\s+plot|pictograph|model|"
    r"number\s+line|grid|plane|clock|array|area\s+model)\b"
    r"|\b(?:is|are)\s+(?:shaded|colou?red\s+in)\b|\bshaded\s+(?:parts?|regions?|areas?|portions?|"
    r"sections?|squares?|pieces?|boxes?)\b|\bthe\s+clock\s+(?:shows?|reads?|says?)\b", re.I)
_DRAWS = re.compile(r"\b(?:draw|sketch|make|create|construct|plot|design)\b", re.I)
_ON_GIVEN = re.compile(r"\b(?:on|in)\s+the\s+(?:coordinate\s+)?(?:grid|plane|number\s+line|dot\s+paper|"
                       r"clock|model|diagram|picture|figure)\b", re.I)
_AND = r"\s*(?:,\s*(?:and\s+)?|and\s+|&\s*)"
_SIDES = rf"\b(?:sides?|edges?)(?:\s+lengths?)?{_IS}"
_KIND_OF = {"triangular": "triangle", "rectangular": "rectangle", "circular": "circle",
            "rectangular prism": "prism", "triangular prism": "triangular_prism",
            "trapezium": "trapezoid", "rhombuse": "rhombus"}
_DIMS = {
    "length": (rf"\b(?:length|long side){_IS}{_AMOUNT}{_UNIT}", rf"{_AMOUNT}{_UNIT}\s+long\b"),
    "width": (rf"\b(?:width|short side){_IS}{_AMOUNT}{_UNIT}", rf"{_AMOUNT}{_UNIT}\s+wide\b"),
    "height": (rf"\bheight{_IS}{_AMOUNT}{_UNIT}", rf"{_AMOUNT}{_UNIT}\s+(?:tall|high)\b"),
    "base": (rf"\bbase{_IS}{_AMOUNT}{_UNIT}",),
    "side": (rf"\b(?:sides?|edges?)(?:\s+lengths?)?{_IS}{_AMOUNT}{_UNIT}",
             rf"{_AMOUNT}{_UNIT}\s+(?:on\s+)?(?:each|per|every)\s+side\b"),
    "radius": (rf"\bradius{_IS}{_AMOUNT}{_UNIT}",),
    "diameter": (rf"\bdiameter{_IS}{_AMOUNT}{_UNIT}",),
}
_USES = {
    "rectangle": ("length", "width"), "square": ("side",), "triangle": ("base", "height"),
    "circle": ("radius", "diameter"), "parallelogram": ("base", "height", "side"),
    "trapezoid": ("height",), "rhombus": ("side",), "prism": ("length", "width", "height"),
    "cube": ("side",), **{name: ("side",) for name in _POLYGONS},
}
# A triangular prism's sizes: the triangle's base and height, and the prism's length
# ("the prism is 10 cm long", or tall when it stands on its triangle).
_TRIANGLE_PART = r"(?:the|its|each)\s+(?:triangle|triangular\s+(?:base|face|end)s?)"
_PRISM_FACE = {
    "base": (rf"\bbase\s+of\s+{_TRIANGLE_PART}{_IS}{_AMOUNT}{_UNIT}", rf"\bbase{_IS}{_AMOUNT}{_UNIT}"),
    "height": (rf"\bheight\s+of\s+{_TRIANGLE_PART}{_IS}{_AMOUNT}{_UNIT}",
               rf"(?<!prism's )(?<!prism )\bheight{_IS}{_AMOUNT}{_UNIT}"),
}
_PRISM_LENGTH = (
    rf"\b(?:prism|it)\s+is\s+{_AMOUNT}{_UNIT}\s+(?:long|tall|high|deep)\b",
    rf"\b(?:length|height|depth)\s+of\s+(?:the|its|this)\s+prism{_IS}{_AMOUNT}{_UNIT}",
    rf"\bprism(?:'s)?\s+(?:length|height|depth){_IS}{_AMOUNT}{_UNIT}",
    rf"\b(?:length|depth){_IS}{_AMOUNT}{_UNIT}",
    rf"{_AMOUNT}{_UNIT}\s+(?:long|deep)\b",
)


def _triangular_prism_sizes(text: str, raw: dict) -> str | None:
    unit = None
    for dim, patterns in _PRISM_FACE.items():
        match = next(filter(None, (re.search(pattern, text) for pattern in patterns)), None)
        if match:
            raw[dim], unit = _amount(match.group(1)), unit or match.group(2)
    for pattern in _PRISM_LENGTH:
        for match in re.finditer(pattern, text):
            value = _amount(match.group(1))
            if pattern == _PRISM_LENGTH[-1] and value in (raw.get("base"), raw.get("height")):
                continue  # "a base 8 cm long" sizes the triangle
            raw["length"], unit = value, unit or match.group(2)
            return unit
    if re.search(r"\b(?:what is|what's|find|calculate|how (?:long|tall) is)\s+the\s+(?:length|height)\s+"
                 r"of\s+(?:the|this|that)\s+prism\b", text):
        raw["length"] = "?"
    return unit


def _count_word(text: str) -> int:
    return int(text) if text.isdigit() else _WORDS[text.lower()]


def _amount(text: str):
    text = re.sub(r"\s+", " ", text.strip())
    return _plain(float(text)) if re.fullmatch(r"\d+(?:\.\d+)?", text) else text


def _infer_area_model(text: str) -> list[dict]:
    match = re.search(r"\b(\d{1,4})\s*(?:×|x|\*|times)\s*(\d{1,4})\b", text)
    if not match or not re.search(r"\barea\s+models?\b|\bbox\s+method\b", text):
        return []
    top, side = int(match.group(1)), int(match.group(2))
    if top < 10 and side < 10:
        return []
    return [{"shape": "area_model", "rows": _split_place_value(side),
             "columns": _split_place_value(top)}]


def infer(text: str, explicit: bool = False) -> list[dict]:
    """Figures described by a prompt; [] unless it is clear what to draw.

    With ``explicit`` the text was written as a figure, so a bare "hexagon" is enough.
    Otherwise the prompt must name exactly one kind of shape and give a size or parts,
    and prompts that ask students to draw are left alone.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    lowered = re.sub(r"\s+", " ", text.lower())
    if not explicit and re.search(r"\b(?:draw|sketch|construct|trace)\b", lowered):
        return []
    found = _infer_area_model(lowered)
    if found:
        return found
    made = _MADE_OF.search(lowered)
    search = lowered[:made.start()] + lowered[made.end():] if made else lowered
    kinds, styles = [], []
    for match in _FOUND.finditer(search):
        word = re.sub(r"\s+", " ", match.group("name"))
        if word in ("circle", "square") and _VERB.match(search, match.end()):
            continue
        word = _KIND_OF.get(word) or _KIND_OF.get(word.rstrip("s")) or shape_name(word)
        if word == "number_line":
            continue
        if word and word not in kinds:
            kinds.append(word)
        if match.group("style"):
            styles.append(match.group("style"))
    numberline = re.search(rf"number line (?:from|that goes from|between) (-?\d+) (?:to|and) (-?\d+)", lowered)
    if numberline and not kinds:
        kinds = ["number_line"]
    if made and any(kind in _POLYGONS for kind in kinds):
        kinds = [kind for kind in kinds if kind != "triangle"]
    # A solid's faces ("a triangular base", "a rectangular base") are part of the solid.
    if "triangular_prism" in kinds:
        kinds = [kind for kind in kinds if kind not in ("triangle", "rectangle")]
    elif "prism" in kinds or "cube" in kinds:
        kinds = [kind for kind in kinds if kind not in ("rectangle", "square")]
    if not kinds or not explicit and len(kinds) != 1:
        return []
    kind = kinds[0]
    raw: dict = {"shape": kind}
    unit = None
    for dim in _USES.get(kind, ()):
        for pattern in _DIMS[dim]:
            match = re.search(pattern, lowered)
            if match:
                raw[dim] = _amount(match.group(1))
                unit = unit or match.group(2)
                break
    by = re.search(rf"{_AMOUNT}{_UNIT}\s*(?:by|x|×)\s*{_AMOUNT}{_UNIT}(?:\s*(?:by|x|×)\s*{_AMOUNT}{_UNIT})?",
                   lowered)
    if by and kind in ("rectangle", "prism") and "length" not in raw:
        raw["length"], raw["width"] = _amount(by.group(1)), _amount(by.group(3))
        if kind == "prism" and by.group(5):
            raw["height"] = _amount(by.group(5))
        unit = unit or by.group(2) or by.group(4)
    if kind == "rectangle" and "length" not in raw and "width" not in raw:
        for dim, target in (("base", "length"), ("height", "width")):
            match = re.search(_DIMS[dim][0], lowered)
            if match:
                raw[target] = _amount(match.group(1))
                unit = unit or match.group(2)
    if kind == "triangular_prism":
        unit = _triangular_prism_sizes(lowered, raw) or unit
        if not explicit and not (number(raw.get("base")) and number(raw.get("height")) and "length" in raw):
            return []
    if kind == "square" and "side" not in raw:
        match = re.search(_DIMS["length"][0], lowered)
        if match:
            raw["side"], unit = _amount(match.group(1)), unit or match.group(2)
    if kind == "trapezoid":
        match = re.search(rf"\b(?:bases|parallel sides){_IS}{_AMOUNT}{_UNIT}\s*(?:,|and|&)\s*(?:\w+\s+)?{_AMOUNT}{_UNIT}",
                          lowered)
        if match:
            raw["bases"] = [_amount(match.group(1)), _amount(match.group(3))]
            unit = unit or match.group(2) or match.group(4)
    if kind == "triangle" and "height" not in raw:
        three = re.search(rf"{_SIDES}{_AMOUNT}{_UNIT}{_AND}{_AMOUNT}{_UNIT}{_AND}{_AMOUNT}{_UNIT}", lowered)
        two = re.search(rf"\btwo\s+(?:of\s+(?:its|the)\s+)?sides{_IS}{_AMOUNT}{_UNIT}{_AND}{_AMOUNT}{_UNIT}",
                        lowered)
        pair = re.search(rf"\btwo\s+(?:equal|matching|congruent|same-length)\s+sides{_IS}{_AMOUNT}{_UNIT}",
                         lowered)
        if three and "base" not in raw:
            raw["sides"] = [_amount(three.group(n)) for n in (1, 3, 5)]
            unit = unit or three.group(2) or three.group(4) or three.group(6)
        elif two and "base" not in raw and re.search(
                r"\b(?:third|other|missing|last|remaining|unknown)\s+side\b", lowered):
            raw["sides"] = [_amount(two.group(1)), _amount(two.group(3)), "?"]
            unit = unit or two.group(2) or two.group(4)
        elif pair and "base" in raw:
            raw["sides"] = [raw.pop("base"), _amount(pair.group(1)), _amount(pair.group(1))]
            unit = unit or pair.group(2)
    if kind == "triangle" and styles:
        raw["kind"] = styles[0]
    if kind == "number_line":
        raw["start"], raw["end"] = int(numberline.group(1)), int(numberline.group(2))
    parts = re.search(rf"\b(?:divided|cut|split|separated|broken|partitioned)\s+(?:up\s+)?into\s+{_COUNT}\s+"
                      rf"(?:equal\s+|same-size\s+)?(?:parts|pieces|sections|slices|squares|rows|columns|"
                      rf"equal\s+parts)\b|\b{_COUNT}\s+equal\s+(?:parts|pieces|sections|slices)\b", lowered)
    if parts and kind in ("rectangle", "square", "circle"):
        raw["parts"] = _count_word(parts.group(1) or parts.group(2))
        shaded = re.search(rf"\b{_COUNT}\s+(?:of\s+(?:the|them|these|those)\s+)?(?:(?:equal\s+)?"
                           rf"(?:parts|pieces|sections|slices)\s+)?(?:are|is|were)\s+"
                           rf"(?:shaded|colored|coloured|filled|painted)\b", lowered)
        if shaded and _count_word(shaded.group(1)) <= raw["parts"]:
            raw["shaded"] = _count_word(shaded.group(1))
    elif parts and kind == "number_line":
        raw["divisions"] = _count_word(parts.group(1) or parts.group(2))
    shade = re.search(r"\b(?:shade|colou?r)\s+(?:in\s+)?\d{1,2}/(\d{1,2})\s+of\b", lowered)
    if shade and kind in ("rectangle", "square", "circle") and "parts" not in raw \
            and 2 <= int(shade.group(1)) <= 24:
        raw["parts"] = int(shade.group(1))
    if kind in _POLYGONS and made:
        raw["triangles"] = True
    if kind == "number_line" and "divisions" not in raw:
        span = raw["end"] - raw["start"]
        dens = {int(d) for d in re.findall(r"(?<![\d/])\d{1,2}/(\d{1,2})(?![\d/])", lowered) if int(d) > 1}
        step = math.lcm(*dens) if dens else 1
        if span <= 0 or not 1 <= span * step <= 24 or span < 2 and not dens:
            return []
        raw["divisions"] = span * step
    for match in re.finditer(r"\b(?:what is|what's|find|calculate|how (?:long|tall|wide) is)\s+"
                             r"(?:the|its|this|that)(?:\s+\w+'s)?\s+(length|width|height|base|side|radius|diameter)\b",
                             lowered):
        dim = match.group(1)
        if dim in _USES.get(kind, ()) and dim not in raw and not (kind == "circle" and len(raw) > 1):
            raw[dim] = "?"
    if unit:
        raw["unit"] = unit
    sized = [key for key in raw if key not in ("shape", "unit", "kind")]
    if not explicit and not sized:
        return []
    if kind == "cube" and "side" not in raw and not explicit:
        return []
    try:
        figures, _notes = _figure(raw)
    except FigureError:
        return []
    many = re.search(rf"\b{_COUNT}\s+(?:identical|equal|congruent|same-size|same size|matching)\s+(\w+)", lowered)
    copies = 1
    if many and shape_name(many.group(2)) == kind:
        copies = min(_count_word(many.group(1)), PER_QUESTION)
    return figures * copies


def needs_picture(prompt) -> bool:
    """True when a prompt talks about a picture that is not there and was not asked for."""
    if not isinstance(prompt, str):
        return False
    # "Draw a rectangle..." needs writing space, but "Plot (2, 3) on the grid" needs the grid.
    return bool(_PICTURE.search(prompt)) and not (_DRAWS.search(prompt) and not _ON_GIVEN.search(prompt))


# ---------------------------------------------------------------------------------------
# Drawing. One SVG unit is one CSS pixel; shapes fit in about 180 x 110 px.

def _n(value: float) -> str:
    text = f"{value:.1f}"
    text = text[:-2] if text.endswith(".0") else text
    return "0" if text == "-0" else text


def _unit(dx: float, dy: float) -> tuple[float, float]:
    length = math.hypot(dx, dy) or 1.0
    return dx / length, dy / length


def _mid(a, b) -> tuple[float, float]:
    return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2


class _Canvas:
    """SVG pieces plus their bounding box, so the figure is cropped to what was drawn."""

    def __init__(self):
        self.items: list[str] = []
        self.box = [math.inf, math.inf, -math.inf, -math.inf]

    def grow(self, x0: float, y0: float, x1: float | None = None, y1: float | None = None) -> None:
        box = self.box
        box[0], box[1] = min(box[0], x0), min(box[1], y0)
        box[2], box[3] = max(box[2], x0 if x1 is None else x1), max(box[3], y0 if y1 is None else y1)

    def poly(self, points, cls: str = "shape", closed: bool = True) -> None:
        for x, y in points:
            self.grow(x, y)
        path = "M" + "L".join(f"{_n(x)} {_n(y)}" for x, y in points) + ("Z" if closed else "")
        self.items.append(f'<path class="{cls}" d="{path}"/>')

    def line(self, a, b, cls: str = "line") -> None:
        self.poly([a, b], cls, closed=False)

    def circle(self, x: float, y: float, r: float, cls: str = "shape") -> None:
        self.grow(x - r, y - r, x + r, y + r)
        self.items.append(f'<circle class="{cls}" cx="{_n(x)}" cy="{_n(y)}" r="{_n(r)}"/>')

    def path(self, data: str, cls: str, box) -> None:
        self.grow(*box)
        self.items.append(f'<path class="{cls}" d="{data}"/>')

    def text(self, text, x: float, y: float, size: float = 13, anchor: str = "middle",
             bold: bool = False):
        if text in (None, ""):
            return None
        markup, box = mathtext.label(str(text), x, y, size, anchor, bold)
        if markup:
            self.items.append(markup)
            self.grow(*box)
        return box

    def tag(self, text, x: float, y: float, nx: float = 0.0, ny: float = 1.0, gap: float = 5,
            size: float = 13):
        """Text just outside the point (x, y), pushed along the direction (nx, ny)."""
        if text in (None, ""):
            return None
        box = mathtext.label(str(text), 0, 0, size)[1]
        nx, ny = _unit(nx, ny)
        reach = gap + (box[2] - box[0]) / 2 * abs(nx) + (box[3] - box[1]) / 2 * abs(ny)
        middle = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return self.text(text, x + nx * reach - middle[0], y + ny * reach - middle[1], size)

    def spot(self, text, x: float, y: float, nx: float = 0.0, ny: float = 1.0, gap: float = 5,
             size: float = 13) -> tuple[float, float, float, float]:
        """The box :meth:`tag` would fill, without drawing."""
        box = mathtext.label(str(text), 0, 0, size)[1]
        nx, ny = _unit(nx, ny)
        w, h = box[2] - box[0], box[3] - box[1]
        reach = gap + w / 2 * abs(nx) + h / 2 * abs(ny)
        cx, cy = x + nx * reach, y + ny * reach
        return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2

    def markup(self, pad: float = 5) -> tuple[str, int, int]:
        x0, y0, x1, y1 = self.box if math.isfinite(self.box[0]) else (0, 0, 0, 0)
        left, top = math.floor(x0 - pad), math.floor(y0 - pad)
        width, height = math.ceil(x1 + pad - left), math.ceil(y1 + pad - top)
        return (f'<svg class="figure" xmlns="http://www.w3.org/2000/svg" '
                f'viewBox="{left} {top} {width} {height}" width="{width}" height="{height}">'
                f'{"".join(self.items)}</svg>', width, height)


def _hits(box, a, b, pad: float = 3) -> bool:
    """True when the segment from a to b passes through the box (grown by pad)."""
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - box[0] + pad), (dx, box[2] + pad - a[0]),
                 (-dy, a[1] - box[1] + pad), (dy, box[3] + pad - a[1])):
        if p == 0:
            if q < 0:
                return False
        elif p < 0:
            t0 = max(t0, q / p)
        else:
            t1 = min(t1, q / p)
        if t0 > t1:
            return False
    return True


def _fit(points, width: float = 180, height: float = 110, ratio: float | None = 3.2):
    """Scale model points (y down) into a box. Unless ``ratio`` is None the aspect ratio is
    kept between 1/ratio and ratio, so a 20 by 1 rectangle is still a readable shape."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    w, h = max(max(xs) - min(xs), 1e-9), max(max(ys) - min(ys), 1e-9)
    sx = sy = min(width / w, height / h)
    if ratio and h * sy < w * sx / ratio:
        sy = w * sx / ratio / h
    elif ratio and w * sx < h * sy / ratio:
        sx = h * sy / ratio / w
    k = min(1.0, width / (w * sx), height / (h * sy))
    return [((x - min(xs)) * sx * k, (y - min(ys)) * sy * k) for x, y in points]


def _outward(points, index: int) -> tuple[float, float]:
    """Unit normal of edge ``index`` (point index to index + 1) pointing out of the polygon."""
    area = sum(points[k - 1][0] * points[k][1] - points[k][0] * points[k - 1][1]
               for k in range(len(points)))
    a, b = points[index], points[(index + 1) % len(points)]
    dx, dy = b[0] - a[0], b[1] - a[1]
    return _unit(dy, -dx) if area > 0 else _unit(-dy, dx)


def _edge_label(c: _Canvas, points, index: int, text, gap: float = 5, size: float = 13) -> None:
    if text in (None, ""):
        return
    a, b = points[index], points[(index + 1) % len(points)]
    c.tag(text, *_mid(a, b), *_outward(points, index), gap, size)


def _vertex_labels(c: _Canvas, points, names) -> None:
    for index, name in enumerate(names or []):
        n1, n2 = _outward(points, index - 1), _outward(points, index)
        c.tag(name, *points[index], n1[0] + n2[0], n1[1] + n2[1], 4)


def _ticks(c: _Canvas, a, b, count: int = 1, size: float = 4.5) -> None:
    mx, my = _mid(a, b)
    ux, uy = _unit(b[0] - a[0], b[1] - a[1])
    for k in range(count):
        x, y = mx + ux * (k - (count - 1) / 2) * 4, my + uy * (k - (count - 1) / 2) * 4
        c.line((x - uy * size, y + ux * size), (x + uy * size, y - ux * size), "thin")


def _corner(c: _Canvas, v, a, b, size: float = 8) -> None:
    """Right-angle mark at v between the directions toward a and b."""
    u1, u2 = _unit(a[0] - v[0], a[1] - v[1]), _unit(b[0] - v[0], b[1] - v[1])
    p1 = (v[0] + u1[0] * size, v[1] + u1[1] * size)
    p3 = (v[0] + u2[0] * size, v[1] + u2[1] * size)
    c.poly([p1, (p1[0] + u2[0] * size, p1[1] + u2[1] * size), p3], "thin", closed=False)


def _arc(c: _Canvas, v, a, b, radius: float) -> tuple[float, float]:
    """Small arc at v from the direction of a to the direction of b; returns the bisector."""
    u1, u2 = _unit(a[0] - v[0], a[1] - v[1]), _unit(b[0] - v[0], b[1] - v[1])
    start = v[0] + u1[0] * radius, v[1] + u1[1] * radius
    end = v[0] + u2[0] * radius, v[1] + u2[1] * radius
    sweep = 1 if u1[0] * u2[1] - u1[1] * u2[0] > 0 else 0
    bisector = _unit(u1[0] + u2[0], u1[1] + u2[1])
    tip = v[0] + bisector[0] * radius, v[1] + bisector[1] * radius
    xs, ys = (start[0], end[0], tip[0]), (start[1], end[1], tip[1])
    c.path(f"M{_n(start[0])} {_n(start[1])}A{_n(radius)} {_n(radius)} 0 0 {sweep} "
           f"{_n(end[0])} {_n(end[1])}", "thin", (min(xs), min(ys), max(xs), max(ys)))
    return bisector


def _arrow(c: _Canvas, tip, direction, size: float = 7) -> None:
    ux, uy = _unit(*direction)
    bx, by = tip[0] - ux * size, tip[1] - uy * size
    c.poly([tip, (bx - uy * size * .42, by + ux * size * .42),
            (bx + uy * size * .42, by - ux * size * .42)], "dot")


def _angle_at(points, index: int) -> float:
    v, a, b = points[index], points[index - 1], points[(index + 1) % len(points)]
    u1, u2 = _unit(a[0] - v[0], a[1] - v[1]), _unit(b[0] - v[0], b[1] - v[1])
    return math.degrees(math.acos(max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))))


def _cells(c: _Canvas, x: float, y: float, w: float, h: float, parts: int, shaded: int,
           strip: bool = False) -> None:
    """Equal parts of a rectangle (one strip up to 6 parts, else the squarest grid), with
    the first ``shaded`` parts shaded."""
    rows = 1
    if not strip and parts > 6:
        rows = max(d for d in range(1, int(parts ** .5) + 1) if parts % d == 0)
    cols = parts // rows
    if h > w:
        rows, cols = cols, rows
    cw, ch = w / cols, h / rows
    for k in range(min(shaded, parts)):
        row, col = divmod(k, cols)
        x0, y0 = x + col * cw, y + row * ch
        c.poly([(x0, y0), (x0 + cw, y0), (x0 + cw, y0 + ch), (x0, y0 + ch)], "shaded")
    for col in range(1, cols):
        c.line((x + col * cw, y), (x + col * cw, y + h), "thin")
    for row in range(1, rows):
        c.line((x, y + row * ch), (x + w, y + row * ch), "thin")


def _sectors(c: _Canvas, x: float, y: float, r: float, parts: int, shaded: int) -> None:
    def at(k):
        t = math.radians(-90 + 360 * k / parts)
        return x + r * math.cos(t), y + r * math.sin(t)
    if parts == 1 and shaded:
        c.circle(x, y, r, "shaded")
    for k in range(min(shaded, parts) if parts > 1 else 0):
        a, b = at(k), at(k + 1)
        c.path(f"M{_n(x)} {_n(y)}L{_n(a[0])} {_n(a[1])}A{_n(r)} {_n(r)} 0 0 1 {_n(b[0])} {_n(b[1])}Z",
               "shaded", (x - r, y - r, x + r, y + r))
    for k in range(parts if parts > 1 else 0):
        c.line((x, y), at(k), "thin")


def _label(f: dict, key: str) -> str:
    return dim_label(f.get(key), f.get("unit"))


def _rectangle(c: _Canvas, f: dict) -> None:
    square = f["shape"] == "square"
    across, down = (f.get("side"),) * 2 if square else (f.get("length"), f.get("width"))
    a, b = number(across), number(down)
    w, h = (100.0, 100.0) if square else _fit([(0, 0), (a or 1.6, b or 1)], 170, 100)[1]
    points = [(0, 0), (w, 0), (w, h), (0, h)]
    c.poly(points, "shape")
    parts, shaded = f.get("parts", 1), f.get("shaded", 0)
    if f.get("grid") and a and b and a.is_integer() and b.is_integer() and a * b <= 400:
        for i in range(1, int(a)):
            c.line((w * i / a, 0), (w * i / a, h), "grid")
        for j in range(1, int(b)):
            c.line((0, h * j / b), (w, h * j / b), "grid")
    elif parts > 1 or shaded:
        _cells(c, 0, 0, w, h, parts, shaded)
    c.poly(points, "edge")
    if square:
        for i in range(4 if not f.get("grid") else 0):
            _ticks(c, points[i], points[(i + 1) % 4])
        _edge_label(c, points, 2, _label(f, "side"), 5 if f.get("grid") else 8)
    else:
        _edge_label(c, points, 2, _label(f, "length"))
        _edge_label(c, points, 1, _label(f, "width"))
    _vertex_labels(c, points, f.get("vertices"))


def _valid(a: float, b: float, c: float) -> bool:
    return a + b > c * 1.0001 and a + c > b * 1.0001 and b + c > a * 1.0001


def _triangle(c: _Canvas, f: dict) -> None:
    kind, unit = f.get("kind"), f.get("unit")
    given = [number(s) for s in f.get("sides") or []]
    sides = list(given)
    if len(sides) == 3 and all(sides) and not _valid(*sides):
        low, mid, high = sorted(sides)
        raise Impossible(f"sides {fmt(low)}, {fmt(mid)} and {fmt(high)} cannot make a triangle: "
                          "the two shorter sides must add up to more than the longest")
    if len(sides) == 3 and not all(sides):
        # Unknown sides ("?", "x") get a stand-in length that only shapes the drawing.
        known = [s for s in sides if s]
        if len(known) == 2 and kind == "right":
            guess = math.hypot(*known)
        elif len(known) == 2:
            guess = abs(known[0] - known[1]) + 1.2 * min(known)
        else:
            guess = known[0] if known else 1.0
        sides = [s or guess for s in sides]
    exact = len(sides) == 3 and all(sides) and _valid(*sides)
    offset = None
    if exact:
        b, right, left = sides
        x = (left ** 2 - right ** 2 + b ** 2) / (2 * b)
        model = [(0, 0), (b, 0), (x, -math.sqrt(max(left ** 2 - x ** 2, 0)))]
    else:
        b = number(f.get("base")) or (sides[0] if sides and sides[0] else 1.0)
        h = number(f.get("height")) or b * (0.866 if kind == "equilateral" else 0.75)
        offset = {"right": 0.0, "equilateral": 0.5, "isosceles": 0.5, "obtuse": -0.35}.get(kind, 0.25)
        model = [(0, 0), (b, 0), (offset * b, -h)]
    points = _fit(model, 170, 110)
    a_, b_, c_ = points
    c.poly(points, "shape")
    if exact:
        # Tick marks claim equal sides, so only sides whose lengths are given get them.
        equal = [i for i in range(3) if given[i] and any(
            given[j] and abs(given[i] - given[j]) < 1e-9 for j in range(3) if j != i)]
    else:
        equal = {"equilateral": [0, 1, 2], "isosceles": [1, 2]}.get(kind, [])
    for i in equal:
        _ticks(c, points[i], points[(i + 1) % 3])
    gap = 8 if equal else 5
    if exact:
        for index, value in enumerate(f["sides"]):
            _edge_label(c, points, index, dim_label(value, unit), 8 if index in equal else 5)
    else:
        _edge_label(c, points, 0, _label(f, "base"), 8 if 0 in equal else 5)
    right_at = None  # only a triangle said to be right gets the mark; it can be the answer
    if exact and kind == "right":
        right_at = next((i for i in range(3) if abs(_angle_at(model, i) - 90) < 0.5), None)
    elif kind == "right":
        right_at = 0
    if not exact and "height" in f:
        if offset == 0:
            _edge_label(c, points, 2, _label(f, "height"), gap)
        else:
            foot = (c_[0], a_[1])
            c.line(c_, foot, "dash")
            if foot[0] < a_[0] - 1:
                c.line(foot, a_, "dash")
            toward = a_ if foot[0] - a_[0] >= 12 or foot[0] < a_[0] else b_
            _corner(c, foot, toward, c_, 7)
            c.tag(_label(f, "height"), foot[0], a_[1] + (c_[1] - a_[1]) * .42,
                  -1 if foot[0] < a_[0] else 1, 0, 5)
    angles = f.get("angles") or [None] * 3
    for i, value in enumerate(angles):
        if value is None and i != right_at:
            continue
        v, before, after = points[i], points[i - 1], points[(i + 1) % 3]
        if value == 90 or value is None:
            _corner(c, v, before, after, 9)
            bisector, radius = _unit(before[0] + after[0] - 2 * v[0], before[1] + after[1] - 2 * v[1]), 13
        else:
            drawn = _angle_at(points, i)
            radius = 16 if drawn >= 45 else min(34, 16 * 45 / max(drawn, 10))
            bisector = _arc(c, v, before, after, radius)
        if value is not None:
            c.tag("?" if value == "?" else f"{fmt(value)}°", v[0] + bisector[0] * radius,
                  v[1] + bisector[1] * radius, *bisector, 3, 12)
    _vertex_labels(c, points, f.get("vertices"))


def _circle(c: _Canvas, f: dict) -> None:
    r = 52.0
    c.circle(0, 0, r, "shape")
    parts, shaded = f.get("parts", 1), f.get("shaded", 0)
    if parts > 1 or shaded:
        _sectors(c, 0, 0, r, parts, shaded)
        c.circle(0, 0, r, "edge")
    radius, diameter = f.get("radius"), f.get("diameter")
    if diameter is not None:
        c.line((-r, 0), (r, 0))
        c.tag(_label(f, "diameter"), 0, 0, 0, -1 if radius is None else 1, 5)
    if radius is not None:
        t = math.radians(-35 if diameter is None else -62)
        end = r * math.cos(t), r * math.sin(t)
        c.line((0, 0), end)
        c.tag(_label(f, "radius"), end[0] / 2, end[1] / 2, math.sin(t), -math.cos(t), 4)
    if radius is not None or diameter is not None:
        c.circle(0, 0, 2.6, "dot")


def _height_line(c: _Canvas, f: dict, top, left, right) -> None:
    """Dashed height from ``top`` down to the base from ``left`` to ``right``, labelled."""
    foot = (top[0], left[1])
    c.line(top, foot, "dash")
    _corner(c, foot, left if foot[0] - left[0] >= 12 else right, top, 7)
    c.tag(_label(f, "height"), foot[0], left[1] + (top[1] - left[1]) * .45, 1, 0, 5)


def _parallelogram(c: _Canvas, f: dict) -> None:
    b, h, s = number(f.get("base")), number(f.get("height")), number(f.get("side"))
    b = b or 1.6
    h = h or (min(s, b) * .8 if s else b / 1.8)
    slant = min(math.sqrt(s * s - h * h) if s and s > h else b * .3, b * .6)
    points = _fit([(slant, 0), (slant + b, 0), (b, h), (0, h)], 180, 100)
    c.poly(points, "shape")
    _edge_label(c, points, 2, _label(f, "base"))
    _edge_label(c, points, 3, _label(f, "side"))
    if "height" in f:
        _height_line(c, f, points[0], points[3], points[2])
    _vertex_labels(c, points, f.get("vertices"))


def _trapezoid(c: _Canvas, f: dict) -> None:
    top, bottom, h = number(f.get("top")), number(f.get("bottom")), number(f.get("height"))
    bottom = bottom or (top / .6 if top else 1.6)
    top = top or bottom * .6
    h = h or bottom / 2.2
    off = 0 if f.get("right") else (bottom - top) / 2
    points = _fit([(off, 0), (off + top, 0), (bottom, h), (0, h)], 180, 100)
    c.poly(points, "shape")
    _edge_label(c, points, 0, _label(f, "top"))
    _edge_label(c, points, 2, _label(f, "bottom"))
    if f.get("right"):
        _corner(c, points[3], points[2], points[0], 8)
        _corner(c, points[0], points[3], points[1], 8)
        _edge_label(c, points, 3, _label(f, "height"))
    elif "height" in f:
        _height_line(c, f, points[0], points[3], points[2])
    _vertex_labels(c, points, f.get("vertices"))


def _rhombus(c: _Canvas, f: dict) -> None:
    diagonals = [number(v) for v in f.get("diagonals") or []]
    p, q = diagonals if len(diagonals) == 2 and all(diagonals) else (1.5, 1.0)
    points = _fit([(p / 2, 0), (p, q / 2), (p / 2, q), (0, q / 2)], 170, 110)
    top, right, bottom, left = points
    c.poly(points, "shape")
    if f.get("diagonals"):
        middle = _mid(left, right)
        c.line(left, right, "dash")
        c.line(top, bottom, "dash")
        _corner(c, middle, right, top, 7)
        c.tag(dim_label(f["diagonals"][0], f.get("unit")), (middle[0] + left[0]) / 2, middle[1], 0, 1, 4, 12)
        c.tag(dim_label(f["diagonals"][1], f.get("unit")), middle[0], (middle[1] + bottom[1]) / 2, 1, 0, 4, 12)
    for i in range(4):
        _ticks(c, points[i], points[(i + 1) % 4])
    _edge_label(c, points, 1, _label(f, "side"), 8)
    _vertex_labels(c, points, f.get("vertices"))


def _polygon(c: _Canvas, f: dict) -> None:
    if "points" in f:
        _custom_polygon(c, f)
        return
    n = _POLYGONS.get(f["shape"]) or f.get("sides", 6)
    angles = [math.pi / 2 + math.pi / n + 2 * math.pi * k / n for k in range(n)]
    points = [(56 * math.cos(t), 56 * math.sin(t)) for t in angles]
    c.poly(points, "shape")
    if f.get("triangles"):
        for point in points:
            c.line((0, 0), point, "thin")
    _edge_label(c, points, n - 1, _label(f, "side"))
    _vertex_labels(c, points, f.get("vertices"))


def _custom_polygon(c: _Canvas, f: dict) -> None:
    xs, ys = [p[0] for p in f["points"]], [p[1] for p in f["points"]]
    w, h = (max(xs) - min(xs)) or 1, (max(ys) - min(ys)) or 1
    whole = all(float(v).is_integer() for v in xs + ys) and w <= 20 and h <= 20
    grid = f.get("grid") and whole
    if grid:
        s = min(190 / w, 130 / h, 26)
        points = [((x - min(xs)) * s, (max(ys) - y) * s) for x, y in f["points"]]
    else:
        points = _fit([(x, -y) for x, y in f["points"]], 190, 130)
    c.poly(points, "shape")
    if grid:
        for i in range(int(w) + 1):
            c.line((i * s, 0), (i * s, h * s), "grid")
        for j in range(int(h) + 1):
            c.line((0, j * s), (w * s, j * s), "grid")
        c.poly(points, "edge")
    for index, value in enumerate(f.get("edges") or []):
        _edge_label(c, points, index, dim_label(value, f.get("unit")) if value is not None else "")
    _vertex_labels(c, points, f.get("vertices"))


def _angle(c: _Canvas, f: dict) -> None:
    degrees = f.get("degrees")
    value = degrees if isinstance(degrees, (int, float)) else _ANGLE_KINDS.get(f.get("kind"), 50)
    t = math.radians(value)
    a, b = (100.0, 0.0), (100 * math.cos(t), -100 * math.sin(t))
    for end in (a, b):
        c.line((0, 0), end)
        _arrow(c, end, end)
    c.circle(0, 0, 2.6, "dot")
    text = f.get("label") or ("?" if degrees == "?" else f"{fmt(value)}°" if degrees is not None else "")
    if abs(value - 90) < 1e-9:
        _corner(c, (0, 0), a, b, 13)
        radius, middle = 18.0, -math.pi / 4
    else:
        radius, middle = (24.0 if value >= 40 else 40.0), -t / 2
        start, end = (radius, 0.0), (radius * math.cos(t), -radius * math.sin(t))
        c.path(f"M{_n(start[0])} {_n(start[1])}A{_n(radius)} {_n(radius)} 0 {1 if value > 180 else 0} 0 "
               f"{_n(end[0])} {_n(end[1])}", "thin", (-radius, -radius, radius, radius))
    c.tag(text, radius * math.cos(middle), radius * math.sin(middle), math.cos(middle),
          math.sin(middle), 4)


def _lines(c: _Canvas, f: dict) -> None:
    kind, names = f.get("kind", "line"), list(f.get("points") or [])
    marks: list[tuple] = []
    if kind == "parallel":
        for row, y in enumerate((0.0, 46.0)):
            c.line((0, y), (160, y))
            _arrow(c, (0, y), (-1, 0))
            _arrow(c, (160, y), (1, 0))
            c.poly([(76, y - 5), (83, y), (76, y + 5)], "thin", closed=False)
            marks += [((40, y), (0, -1 if row == 0 else 1)), ((120, y), (0, -1 if row == 0 else 1))]
    elif kind in ("perpendicular", "intersecting"):
        turn = 90 if kind == "perpendicular" else 38
        for degrees in (0, turn):
            u = math.cos(math.radians(degrees)), -math.sin(math.radians(degrees))
            c.line((-75 * u[0], -75 * u[1]), (75 * u[0], 75 * u[1]))
            _arrow(c, (-75 * u[0], -75 * u[1]), (-u[0], -u[1]))
            _arrow(c, (75 * u[0], 75 * u[1]), u)
            marks += [((-52 * u[0], -52 * u[1]), (-u[1], u[0])), ((52 * u[0], 52 * u[1]), (-u[1], u[0]))]
        if kind == "perpendicular":
            _corner(c, (0, 0), (1, 0), (0, -1), 10)
        c.circle(0, 0, 2.6, "dot")
    else:
        c.line((0, 0), (160, 0))
        if kind in ("line", "ray"):
            _arrow(c, (160, 0), (1, 0))
        if kind == "line":
            _arrow(c, (0, 0), (-1, 0))
        ends = {"segment": (0, 160), "ray": (0, 110)}.get(kind, (40, 120))
        for x in ends:
            c.circle(x, 0, 3, "dot")
            marks.append(((x, 0), (0, -1)))
    for (point, direction), name in zip(marks, names):
        if kind in ("parallel", "perpendicular", "intersecting"):
            c.circle(*point, 3, "dot")
        c.tag(name, *point, *direction, 5)


def _fraction(c: _Canvas, f: dict) -> None:
    model, parts, shaded = f["model"], f["parts"], f.get("shaded", 0)
    wholes = max(1, -(-shaded // parts))
    for whole in range(wholes):
        fill = max(0, min(parts, shaded - whole * parts))
        if model == "bar":
            y = whole * 46.0
            c.poly([(0, y), (200, y), (200, y + 32), (0, y + 32)], "shape")
            _cells(c, 0, y, 200, 32, parts, fill, strip=True)
            c.poly([(0, y), (200, y), (200, y + 32), (0, y + 32)], "edge")
        elif model == "circle":
            x = whole * 100.0
            c.circle(x, 0, 42, "shape")
            _sectors(c, x, 0, 42, parts, fill)
            c.circle(x, 0, 42, "edge")
        else:
            per_row = parts if parts <= 6 else -(-parts // -(-parts // 6))
            top = whole * (24.0 * -(-parts // per_row) + 14)
            for k in range(parts):
                row, col = divmod(k, per_row)
                c.circle(col * 24.0, top + row * 24.0, 9, "token" if k < fill else "plain")


def _tick_text(value: float, f: dict, index: int) -> str:
    labels = f.get("labels")
    if isinstance(labels, list):
        return labels[index]
    span, divisions = f["end"] - f["start"], f["divisions"]
    if float(value).is_integer():
        return fmt(int(value))
    per_unit = divisions / span
    if float(per_unit).is_integer() and abs(value * per_unit - round(value * per_unit)) < 1e-6:
        top = round(value * per_unit)
        return f"{'-' if top < 0 else ''}{abs(top)}/{int(per_unit)}"
    return fmt(_plain(value))


def _number_line(c: _Canvas, f: dict) -> None:
    start, end, divisions = f["start"], f["end"], f["divisions"]
    length = 240.0 if divisions > 4 else 200.0
    spacing = length / divisions
    c.line((-14, 0), (length + 14, 0))
    _arrow(c, (-16, 0), (-1, 0))
    _arrow(c, (length + 16, 0), (1, 0))
    labels = f.get("labels")
    whole_every = 1 if spacing >= 20 else 2 if spacing >= 10 else 5
    for i in range(divisions + 1):
        value = start + (end - start) * i / divisions
        major = float(round(value, 9)).is_integer()
        size = 8 if major else 6
        c.line((i * spacing, -size), (i * spacing, size), "line" if major else "thin")
        if labels == "none" or labels == "ends" and 0 < i < divisions:
            continue
        if labels in (None, "all") and not (i in (0, divisions) or spacing >= 22 or
                                             major and (i % whole_every == 0)):
            continue
        c.tag(_tick_text(round(value, 9), f, i), i * spacing, size, 0, 1, 3, 12)
    for point in f.get("points") or []:
        x = (number(point["value"]) - start) / (end - start) * length
        c.circle(x, 0, 4.5, "dot")
        c.tag(point.get("label", ""), x, -8, 0, -1, 3)


def _array(c: _Canvas, f: dict) -> None:
    for row in range(f["rows"]):
        for col in range(f["columns"]):
            c.circle(col * 20.0, row * 20.0, 6, "dot")


def _area_model(c: _Canvas, f: dict) -> None:
    def sizes(values, total, least):
        whole = sum(values)
        return [max(least, total * v / whole) for v in values]
    widths, heights = sizes(f["columns"], 210, 48), sizes(f["rows"], 120, 34)
    xs = [sum(widths[:i]) for i in range(len(widths) + 1)]
    ys = [sum(heights[:i]) for i in range(len(heights) + 1)]
    c.poly([(0, 0), (xs[-1], 0), (xs[-1], ys[-1]), (0, ys[-1])], "shape")
    for x in xs[1:-1]:
        c.line((x, 0), (x, ys[-1]), "thin")
    for y in ys[1:-1]:
        c.line((0, y), (xs[-1], y), "thin")
    c.poly([(0, 0), (xs[-1], 0), (xs[-1], ys[-1]), (0, ys[-1])], "edge")
    for i, value in enumerate(f["columns"]):
        c.tag(fmt(value), (xs[i] + xs[i + 1]) / 2, 0, 0, -1, 5)
    for j, value in enumerate(f["rows"]):
        c.tag(fmt(value), 0, (ys[j] + ys[j + 1]) / 2, -1, 0, 6)
    cells = f.get("cells")
    for j, row in enumerate(f["rows"]):
        for i, col in enumerate(f["columns"]):
            text = fmt(_plain(row * col)) if cells == "products" else cells[j][i] if cells else ""
            c.text(text, (xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2, 13)


def _prism(c: _Canvas, f: dict) -> None:
    if f["shape"] == "cube":
        length = width = height = number(f.get("side")) or 1.0
    else:
        length = number(f.get("length")) or 1.6
        width, height = number(f.get("width")) or 1.0, number(f.get("height")) or 1.0
    dx, dy = width * .45 * math.cos(math.radians(32)), -width * .45 * math.sin(math.radians(32))
    front = [(0, height), (length, height), (length, 0), (0, 0)]
    p = _fit(front + [(x + dx, y + dy) for x, y in front], 180, 120)
    c.poly([p[0], p[1], p[2], p[3]], "shape")
    c.poly([p[3], p[2], p[6], p[7]], "shape top")
    c.poly([p[1], p[5], p[6], p[2]], "shape side")
    sizes = (length, width, height)
    cubes = f.get("cubes") and all(float(v).is_integer() and 1 <= v <= 12 for v in sizes)
    if cubes:
        across, deep, up = (int(v) for v in sizes)
        X = ((p[1][0] - p[0][0]) / across, (p[1][1] - p[0][1]) / across)
        D = ((p[4][0] - p[0][0]) / deep, (p[4][1] - p[0][1]) / deep)
        Y = ((p[3][0] - p[0][0]) / up, (p[3][1] - p[0][1]) / up)

        def at(origin, *steps):
            return (origin[0] + sum(s[0] * k for s, k in steps), origin[1] + sum(s[1] * k for s, k in steps))
        for i in range(1, across):
            c.line(at(p[0], (X, i)), at(p[3], (X, i)), "thin")
            c.line(at(p[3], (X, i)), at(p[3], (X, i), (D, deep)), "thin")
        for j in range(1, up):
            c.line(at(p[0], (Y, j)), at(p[1], (Y, j)), "thin")
            c.line(at(p[1], (Y, j)), at(p[1], (Y, j), (D, deep)), "thin")
        for k in range(1, deep):
            c.line(at(p[3], (D, k)), at(p[2], (D, k)), "thin")
            c.line(at(p[1], (D, k)), at(p[2], (D, k)), "thin")
    for end in (p[5], p[7], p[0]) if not cubes else ():  # a stack of cubes is solid
        c.line(p[4], end, "dash")
    if f["shape"] == "cube":
        c.tag(_label(f, "side"), *_mid(p[0], p[1]), 0, 1, 5)
        return
    c.tag(_label(f, "length"), *_mid(p[0], p[1]), 0, 1, 5)
    c.tag(_label(f, "height"), *_mid(p[0], p[3]), -1, 0, 5)
    c.tag(_label(f, "width"), *_mid(p[1], p[5]), -(p[5][1] - p[1][1]), p[5][0] - p[1][0], 4)


def _triangular_prism(c: _Canvas, f: dict) -> None:
    """The triangle in front and the prism's length going back to the upper right. Not to
    scale: the triangle is widened when its height label would not fit beside the height."""
    base, height, length = (number(f.get(key)) for key in ("base", "height", "length"))
    b = base or (height / .75 if height else 1.0)
    h = min(max(height or b * .75, b / 2.5), b * 1.5)
    right = f.get("kind") == "right"
    text = _label(f, "height") if "height" in f and not right else ""
    for widen in (1.0, 1.4, 2.0):
        w = b * widen
        span = max(w, h)
        depth = min(max(length or span * 1.5, span * .8), span * 3) * .5
        front = [(0.0, 0.0), (w, 0.0), (0.0 if right else w / 2, -h)]
        dx, dy = depth * math.cos(math.radians(32)), -depth * math.sin(math.radians(32))
        p = _fit(front + [(x + dx, y + dy) for x, y in front], 180, 120)
        near, far = p[:3], p[3:]
        shift = far[0][0] - near[0][0], far[0][1] - near[0][1]
        # A side face shows when the prism runs back out across that edge of the triangle.
        shown = [_outward(near, i)[0] * shift[0] + _outward(near, i)[1] * shift[1] > 1e-6
                 for i in range(3)]
        lines = [(near[i], near[(i + 1) % 3]) for i in range(3)]
        for i in range(3):
            if not shown[i]:
                lines.append((far[i], far[(i + 1) % 3]))
            if not shown[i] and not shown[i - 1]:
                lines.append((near[i], far[i]))
        foot = (near[2][0], near[0][1])
        mark = (foot[0] + 7, foot[1] - 7)  # the right-angle mark at the foot of the height
        # Beside the height line where no edge, hidden or not, runs through the label.
        spots = [(foot[0], foot[1] + (near[2][1] - foot[1]) * t, side)
                 for t in (.3, .2, .45, .6, .75) for side in (1, -1)]
        spot = next((spot for spot in spots if not any(
            _hits(c.spot(text, spot[0], spot[1], spot[2], 0, 4, 12), a, b)
            for a, b in [*lines, ((mark[0], foot[1]), mark), (mark, (foot[0], mark[1]))])),
            None) if text else None
        if spot or not text:
            break
    for i in range(3):
        if shown[i]:
            j = (i + 1) % 3
            nx, ny = _outward(near, i)
            c.poly([near[i], near[j], far[j], far[i]], "shape top" if -ny > abs(nx) else "shape side")
    c.poly(near, "shape")
    for a, b in lines[3:]:
        c.line(a, b, "dash")
    c.tag(_label(f, "base"), *_mid(near[0], near[1]), 0, 1, 5)
    if "height" in f and right:
        _corner(c, near[0], near[1], near[2], 8)
        c.tag(_label(f, "height"), *_mid(near[0], near[2]), -1, 0, 5)
    elif "height" in f:
        c.line(near[2], foot, "dash")
        _corner(c, foot, near[1], near[2], 7)
        x, y, side = spot or spots[0]
        c.tag(text, x, y, side, 0, 4, 12)
    if shown[0] or shown[1]:
        c.tag(_label(f, "length"), *_mid(near[1], far[1]), -shift[1], shift[0], 4)


def _coordinate_grid(c: _Canvas, f: dict) -> None:
    n = f.get("size", 10)
    s = min(16.0, 170.0 / n)
    for i in range(n + 1):
        c.line((i * s, 0), (i * s, -n * s), "grid")
        c.line((0, -i * s), (n * s, -i * s), "grid")
    c.line((0, 0), (n * s + 12, 0))
    c.line((0, 0), (0, -n * s - 12))
    _arrow(c, (n * s + 15, 0), (1, 0))
    _arrow(c, (0, -n * s - 15), (0, -1))
    for i in range(0, n + 1, 1 if n <= 10 else 2):
        c.tag(str(i), i * s, 0, 0, 1, 4, 10)
        if i:
            c.tag(str(i), 0, -i * s, -1, 0, 4, 10)
    points = f.get("points") or []
    spots = [(p["x"] * s, -p["y"] * s) for p in points]
    if f.get("connect") and len(spots) >= 2:
        c.poly(spots, "edge", closed=len(spots) >= 3)
    for point, (x, y) in zip(points, spots):
        c.circle(x, y, 3.5, "dot")
        c.tag(point.get("label", ""), x, y, 1, -1, 3, 12)


def _clock(c: _Canvas, f: dict) -> None:
    r = 54.0
    c.circle(0, 0, r, "plain")
    for m in range(60):
        t = math.radians(m * 6)
        inner = r - (7 if m % 5 == 0 else 3.5)
        c.line((inner * math.sin(t), -inner * math.cos(t)), (r * math.sin(t), -r * math.cos(t)),
               "line" if m % 5 == 0 else "thin")
    for hour in range(1, 13):
        t = math.radians(hour * 30)
        c.text(str(hour), (r - 17) * math.sin(t), -(r - 17) * math.cos(t), 11)
    for turn, length, cls in (((f["hour"] % 12 + f["minute"] / 60) * 30, 25, "hand"),
                              (f["minute"] * 6, 39, "line")):
        t = math.radians(turn)
        c.line((0, 0), (length * math.sin(t), -length * math.cos(t)), cls)
    c.circle(0, 0, 3, "dot")


def _base_ten(c: _Canvas, f: dict) -> None:
    u, x = 6.0, 0.0
    for k in range(f.get("hundreds", 0)):
        row, col = divmod(k, 3)
        x0, y0 = col * (10 * u + 6), row * (10 * u + 6)
        c.poly([(x0, y0), (x0 + 10 * u, y0), (x0 + 10 * u, y0 + 10 * u), (x0, y0 + 10 * u)], "shape")
        for i in range(1, 10):
            c.line((x0 + i * u, y0), (x0 + i * u, y0 + 10 * u), "grid")
            c.line((x0, y0 + i * u), (x0 + 10 * u, y0 + i * u), "grid")
        c.poly([(x0, y0), (x0 + 10 * u, y0), (x0 + 10 * u, y0 + 10 * u), (x0, y0 + 10 * u)], "edge")
        x = max(x, x0 + 10 * u + 14)
    start = x
    for k in range(f.get("tens", 0)):
        row, col = divmod(k, 10)
        x0, y0 = start + col * (u + 4), row * (10 * u + 6)
        c.poly([(x0, y0), (x0 + u, y0), (x0 + u, y0 + 10 * u), (x0, y0 + 10 * u)], "shape")
        for i in range(1, 10):
            c.line((x0, y0 + i * u), (x0 + u, y0 + i * u), "grid")
        c.poly([(x0, y0), (x0 + u, y0), (x0 + u, y0 + 10 * u), (x0, y0 + 10 * u)], "edge")
        x = max(x, x0 + u + 14)
    for k in range(f.get("ones", 0)):
        col, row = divmod(k, 5)
        x0, y0 = x + col * (u + 4), row * (u + 4)
        c.poly([(x0, y0), (x0 + u, y0), (x0 + u, y0 + u), (x0, y0 + u)], "shape")
        c.poly([(x0, y0), (x0 + u, y0), (x0 + u, y0 + u), (x0, y0 + u)], "edge")


def _bar_graph(c: _Canvas, f: dict) -> None:
    bars = f["bars"]
    top = max(bar["value"] for bar in bars) or 1
    step = next(m * 10 ** e for e in range(-2, 7) for m in (1, 2, 2.5, 5) if top / (m * 10 ** e) <= 6)
    ceiling = math.ceil(top / step - 1e-9) * step
    height = 110.0
    slot = max(34.0, max(mathtext.width(bar["label"], 10) for bar in bars) + 8)
    for k in range(int(round(ceiling / step)) + 1):
        y = -height * k * step / ceiling
        c.line((0, y), (slot * len(bars), y), "grid" if k else "line")
        c.tag(fmt(_plain(round(k * step, 6))), 0, y, -1, 0, 4, 10)
    c.line((0, 0), (0, -height - 6))
    for i, bar in enumerate(bars):
        h, x = height * bar["value"] / ceiling, i * slot + slot * .2
        if h > 0:
            c.poly([(x, -h), (x + slot * .6, -h), (x + slot * .6, 0), (x, 0)], "bar")
        c.tag(bar["label"], i * slot + slot / 2, 0, 0, 1, 4, 10)
    if f.get("title"):
        c.tag(f["title"], slot * len(bars) / 2, -height - 6, 0, -1, 6, 12)


_DRAWERS = {"rectangle": _rectangle, "square": _rectangle, "triangle": _triangle,
            "circle": _circle, "parallelogram": _parallelogram, "trapezoid": _trapezoid,
            "rhombus": _rhombus, "polygon": _polygon, **{name: _polygon for name in _POLYGONS},
            "angle": _angle, "lines": _lines, "fraction": _fraction, "number_line": _number_line,
            "array": _array, "area_model": _area_model, "prism": _prism, "cube": _prism,
            "triangular_prism": _triangular_prism,
            "coordinate_grid": _coordinate_grid, "clock": _clock, "base_ten": _base_ten,
            "bar_graph": _bar_graph}


def svg(figure: dict) -> tuple[str, int, int]:
    """Inline SVG for one canonical figure from :func:`parse`, with its width and height."""
    c = _Canvas()
    try:
        _DRAWERS[figure["shape"]](c, figure)
    except Impossible:
        raise
    except (KeyError, TypeError, ValueError, ZeroDivisionError, IndexError, StopIteration) as error:
        raise FigureError(f"could not draw the {figure.get('shape', 'figure')}: {error}") from error
    if figure.get("caption"):
        x0, _top, x1, bottom = c.box
        c.tag(figure["caption"], (x0 + x1) / 2, bottom, 0, 1, 6, 12)
    return c.markup()
