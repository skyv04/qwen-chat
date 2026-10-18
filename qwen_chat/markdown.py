"""A small Markdown parser for chat messages: block structure plus inline Pango markup.

Streaming-friendly: an unclosed code fence is still a code block, and inline markers
without a closing partner are left as literal text instead of producing broken markup.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^\s`]*)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_LIST = re.compile(r"^(\s*)([-*+•]|\d{1,3}[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _is_table_sep(line: str) -> bool:
    # A pipe is required so "text | more" over a "---" rule is not a table.
    return "|" in line and bool(_TABLE_SEP_RE.match(line))


@dataclass
class Block:
    kind: str                      # heading | paragraph | code | list | quote | rule | table
    text: str = ""
    level: int = 0                 # heading level
    lang: str = ""                 # code language
    closed: bool = True            # code fence closed?
    items: list = field(default_factory=list)   # list: (indent, marker, text)
    rows: list = field(default_factory=list)    # table: list of cell lists (row 0 = header)
    aligns: list = field(default_factory=list)  # table: "left" | "center" | "right"


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _starts_block(line: str, nxt: str | None) -> bool:
    return bool(_FENCE.match(line) or _HEADING.match(line) or _RULE.match(line)
                or _QUOTE.match(line) or _LIST.match(line)
                or ("|" in line and nxt is not None and _is_table_sep(nxt)))


def parse_blocks(md: str) -> list[Block]:
    lines = md.replace("\r\n", "\n").split("\n")
    blocks: list[Block] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < n else None
        m = _FENCE.match(line)
        if m:
            indent, fence, lang = m.groups()
            body, i, closed = [], i + 1, False
            while i < n:
                s = lines[i]
                if s.strip().startswith(fence[0] * len(fence)) and not s.strip().strip(fence[0]):
                    closed = True
                    i += 1
                    break
                body.append(s[len(indent):] if s.startswith(indent) else s.lstrip())
                i += 1
            blocks.append(Block("code", "\n".join(body), lang=lang.lower(), closed=closed))
            continue
        if not line.strip():
            i += 1
            continue
        m = _HEADING.match(line)
        if m:
            blocks.append(Block("heading", m.group(2), level=len(m.group(1))))
            i += 1
            continue
        if _RULE.match(line):
            blocks.append(Block("rule"))
            i += 1
            continue
        if "|" in line and nxt is not None and _is_table_sep(nxt):
            header = _split_row(line)
            aligns = []
            for c in _split_row(nxt):
                c = c.strip()
                aligns.append("center" if c.startswith(":") and c.endswith(":")
                              else "right" if c.endswith(":") else "left")
            rows, i = [header], i + 2
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1
            width = len(header)
            rows = [(r + [""] * width)[:width] for r in rows]
            blocks.append(Block("table", rows=rows, aligns=(aligns + ["left"] * width)[:width]))
            continue
        if _QUOTE.match(line):
            body = []
            while i < n and lines[i].strip() and (_QUOTE.match(lines[i]) or not _starts_block(lines[i], None)):
                q = _QUOTE.match(lines[i])
                body.append(q.group(1) if q else lines[i])
                i += 1
            blocks.append(Block("quote", "\n".join(body)))
            continue
        if _LIST.match(line):
            items = []
            while i < n:
                s = lines[i]
                lm = _LIST.match(s)
                if lm:
                    items.append([len(lm.group(1).expandtabs(4)), lm.group(2), lm.group(3)])
                    i += 1
                elif s.strip() and items and (s.startswith((" ", "\t")) or not _starts_block(s, None)) \
                        and not _FENCE.match(s):
                    items[-1][2] += "\n" + s.strip()
                    i += 1
                elif not s.strip() and i + 1 < n and _LIST.match(lines[i + 1]):
                    i += 1
                else:
                    break
            blocks.append(Block("list", items=[tuple(it) for it in items]))
            continue
        body = []
        while i < n and lines[i].strip():
            nx = lines[i + 1] if i + 1 < n else None
            if body and _starts_block(lines[i], nx):
                break
            body.append(lines[i].strip())
            i += 1
        blocks.append(Block("paragraph", "\n".join(body)))
    return blocks


# ------------------------------------------------------------------ inline → Pango markup

_PH = "\ue000{}\ue001"
_PH_RE = re.compile("\ue000(\\d+)\ue001")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+|mailto:[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_AUTOLINK = re.compile(r"<?(https?://[^\s<>()\[\]]*[^\s<>()\[\].,;:!?'\"*_])>?")
_BOLD = [re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")]
_ITALIC = [re.compile(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])"),
           re.compile(r"(?<![_\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![_\w])")]
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def _attr(text: str) -> str:
    return html.escape(text, quote=True)


def inline_to_pango(text: str, code_bg: str = "#80808030") -> str:
    """Converts inline Markdown to Pango markup. Always returns balanced markup."""
    slots: list[str] = []

    def stash(markup: str) -> str:
        slots.append(markup)
        return _PH.format(len(slots) - 1)

    color, alpha = code_bg[:7], code_bg[7:] or "30"
    alpha_pct = f"{round(int(alpha, 16) * 100 / 255)}%"

    def code(m):
        return stash(f'<span font_family="monospace" background="{color}" bgalpha="{alpha_pct}">'
                     f"{esc(m.group(2).strip() if m.group(2).strip() else m.group(2))}</span>")

    text = _CODE_SPAN.sub(code, text)
    text = _LINK.sub(lambda m: stash(f'<a href="{_attr(m.group(2))}">{_style(esc(m.group(1)))}</a>'), text)
    text = _AUTOLINK.sub(lambda m: stash(f'<a href="{_attr(m.group(1))}">{esc(m.group(1))}</a>'), text)
    text = text.replace("\\*", stash("*")).replace("\\_", stash("_")).replace("\\`", stash("`"))
    out = _style(esc(text))
    while _PH_RE.search(out):
        out = _PH_RE.sub(lambda m: slots[int(m.group(1))], out)
    return out


_TAG = re.compile(r"<(/?)([bis])>")


def _balanced(s: str) -> bool:
    stack = []
    for m in _TAG.finditer(s):
        if not m.group(1):
            stack.append(m.group(2))
        elif not stack or stack.pop() != m.group(2):
            return False
    return not stack


def _wrap(tag: str):
    # Crossing markers like "**a *b** c*" stay literal instead of producing <b><i></b></i>.
    return lambda m: f"<{tag}>{m.group(1)}</{tag}>" if _balanced(m.group(1)) else m.group(0)


def _style(s: str) -> str:
    for rx in _BOLD:
        s = rx.sub(_wrap("b"), s)
    for rx in _ITALIC:
        s = rx.sub(_wrap("i"), s)
    return _STRIKE.sub(_wrap("s"), s)


def plain_text(md: str) -> str:
    """Markdown without the markers, for copying a message as text."""
    return md.strip()
