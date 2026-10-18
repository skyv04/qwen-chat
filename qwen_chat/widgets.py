"""GTK widgets for the chat view: Markdown, code blocks, tool cards and message rows."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
from gi.repository import Adw, Gdk, GLib, Gtk, GtkSource, Pango  # noqa: E402

from .markdown import Block, esc, inline_to_pango, parse_blocks  # noqa: E402

GtkSource.init()

TOOL_ICONS = {
    "web_search": "system-search-symbolic",
    "fetch_url": "qwen-chat-web-symbolic",
    "download_file": "folder-download-symbolic",
    "list_files": "folder-symbolic",
    "read_file": "text-x-generic-symbolic",
    "write_file": "document-edit-symbolic",
    "make_directory": "folder-new-symbolic",
    "move_path": "go-next-symbolic",
    "delete_path": "user-trash-symbolic",
    "run_command": "utilities-terminal-symbolic",
    "install_packages": "system-software-install-symbolic",
    "check_programs": "system-search-symbolic",
}

_LANG_ALIASES = {"py": "python3", "python": "python3", "js": "js", "javascript": "js",
                 "ts": "typescript", "sh": "sh", "bash": "sh", "shell": "sh", "zsh": "sh",
                 "console": "sh", "yml": "yaml", "c++": "cpp", "cs": "c-sharp",
                 "csharp": "c-sharp", "html": "html", "xml": "xml", "md": "markdown",
                 "rs": "rust", "kt": "kotlin", "rb": "ruby", "ps1": "powershell"}


def open_uri(uri: str) -> None:
    """Opens a URL, file or folder with the desktop's default app."""
    env = dict(os.environ)
    env.pop("GSK_RENDERER", None)
    try:
        subprocess.Popen(["xdg-open", uri], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    except OSError:
        Gtk.show_uri(None, uri, Gdk.CURRENT_TIME)


def copy_text(widget: Gtk.Widget, text: str) -> None:
    widget.get_clipboard().set(text)


def _on_link(label, uri) -> bool:
    open_uri(uri)
    return True


def md_label(markup: str, css: str | None = None, selectable: bool = True) -> Gtk.Label:
    lbl = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                    selectable=selectable, use_markup=True)
    lbl.set_markup(markup)
    lbl.connect("activate-link", _on_link)
    if css:
        lbl.add_css_class(css)
    return lbl


_A_OPEN = re.compile(r"<a\s[^>]*>")


def safe_markup(markup: str, fallback: str) -> str:
    # Gtk.Label understands <a href>, Pango does not, so check links as plain spans.
    try:
        Pango.parse_markup(_A_OPEN.sub("<span>", markup).replace("</a>", "</span>"), -1, "\0")
        return markup
    except GLib.Error:
        return esc(fallback)


def flat_button(icon: str, tooltip: str, callback=None) -> Gtk.Button:
    btn = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    btn.add_css_class("flat")
    btn.add_css_class("small-button")
    if callback:
        btn.connect("clicked", lambda *_: callback())
    return btn


def flash_copied(btn: Gtk.Button) -> None:
    btn.set_icon_name("object-select-symbolic")
    GLib.timeout_add(1200, lambda: (btn.set_icon_name("edit-copy-symbolic"), False)[1])


# ------------------------------------------------------------------ code blocks

class CodeBlock(Gtk.Box):
    def __init__(self, code: str, lang: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("code-block")
        self.lang = lang
        header = Gtk.Box(spacing=6)
        header.add_css_class("code-header")
        self.lang_label = Gtk.Label(label=lang or "code", xalign=0, hexpand=True)
        self.lang_label.add_css_class("caption")
        header.append(self.lang_label)
        self.copy_btn = flat_button("edit-copy-symbolic", "Copy code", self._copy)
        header.append(self.copy_btn)
        self.append(header)

        self.buffer = GtkSource.Buffer()
        manager = GtkSource.LanguageManager.get_default()
        language = manager.get_language(_LANG_ALIASES.get(lang, lang)) if lang else None
        if language is not None:
            self.buffer.set_language(language)
        self.buffer.set_highlight_matching_brackets(False)
        self.view = GtkSource.View(buffer=self.buffer, editable=False, cursor_visible=False,
                                   monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                   top_margin=8, bottom_margin=10, left_margin=12, right_margin=12)
        self.view.add_css_class("code-view")
        self.append(self.view)
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *_: self._apply_scheme())
        self._apply_scheme()
        self.set_code(code)

    def _apply_scheme(self) -> None:
        dark = Adw.StyleManager.get_default().get_dark()
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme("Adwaita-dark" if dark else "Adwaita")
        if scheme is not None:
            self.buffer.set_style_scheme(scheme)

    def set_code(self, code: str) -> None:
        code = code.rstrip("\n")
        if self.buffer.get_property("text") != code:
            self.buffer.set_text(code)

    def _copy(self) -> None:
        copy_text(self, self.buffer.get_property("text"))
        flash_copied(self.copy_btn)


# ------------------------------------------------------------------ markdown view

class MarkdownView(Gtk.Box):
    """Renders Markdown as widgets; re-rendering reuses blocks that did not change."""

    def __init__(self, text: str = ""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("markdown")
        self._items: list[tuple[Block, Gtk.Widget]] = []
        self.text = ""
        if text:
            self.set_markdown(text)

    def set_markdown(self, text: str) -> None:
        if text == self.text:
            return
        self.text = text
        blocks = parse_blocks(text)
        keep = 0
        for i, block in enumerate(blocks):
            if i >= len(self._items):
                break
            old, widget = self._items[i]
            if old == block:
                keep = i + 1
                continue
            if old.kind == block.kind and self._update(widget, block):
                self._items[i] = (block, widget)
                keep = i + 1
                continue
            break
        for _, widget in self._items[keep:]:
            self.remove(widget)
        self._items = self._items[:keep]
        for block in blocks[keep:]:
            widget = self._build(block)
            self.append(widget)
            self._items.append((block, widget))

    # Blocks that can be updated in place while streaming (avoids flicker).
    def _update(self, widget: Gtk.Widget, block: Block) -> bool:
        if block.kind == "paragraph":
            widget.set_markup(safe_markup(inline_to_pango(block.text), block.text))
            return True
        if block.kind == "heading" and widget.level == block.level:
            widget.set_markup(self._heading_markup(block))
            return True
        if block.kind == "code" and widget.lang == block.lang:
            widget.set_code(block.text)
            return True
        if block.kind == "quote":
            widget.inner.set_markdown(block.text)
            return True
        if block.kind == "table" and widget.aligns == block.aligns and \
                widget.rows and widget.rows[0] == block.rows[0]:
            widget.set_rows(block.rows)
            return True
        return False

    @staticmethod
    def _heading_markup(block: Block) -> str:
        inner = safe_markup(inline_to_pango(block.text), block.text)
        size = {1: "x-large", 2: "large", 3: "medium"}.get(block.level, "medium")
        return f'<span size="{size}" weight="bold">{inner}</span>'

    def _build(self, block: Block) -> Gtk.Widget:
        kind = block.kind
        if kind == "paragraph":
            return md_label(safe_markup(inline_to_pango(block.text), block.text))
        if kind == "heading":
            lbl = md_label(self._heading_markup(block), "md-heading")
            lbl.level = block.level
            return lbl
        if kind == "code":
            return CodeBlock(block.text, block.lang)
        if kind == "rule":
            sep = Gtk.Separator()
            sep.set_margin_top(4)
            sep.set_margin_bottom(4)
            return sep
        if kind == "quote":
            box = Gtk.Box()
            box.add_css_class("md-quote")
            box.inner = MarkdownView(block.text)
            box.inner.set_hexpand(True)
            box.append(box.inner)
            return box
        if kind == "list":
            return self._build_list(block)
        if kind == "table":
            return self._build_table(block)
        return md_label(esc(block.text))

    @staticmethod
    def _build_list(block: Block) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        base = min((it[0] for it in block.items), default=0)
        levels = sorted({it[0] for it in block.items})
        for indent, marker, text in block.items:
            depth = levels.index(indent) if indent >= base else 0
            row = Gtk.Box(spacing=8, margin_start=4 + depth * 20)
            ordered = marker[0].isdigit()
            bullet = marker if ordered else ("•", "◦", "▪")[depth % 3]
            mark = Gtk.Label(label=bullet, xalign=1, valign=Gtk.Align.START,
                             width_chars=3 if ordered else 1)
            mark.add_css_class("md-bullet")
            row.append(mark)
            task = None
            if text.startswith(("[ ] ", "[x] ", "[X] ")):
                task, text = text[1] != " ", text[4:]
                mark.set_label("☑" if task else "☐")
            lbl = md_label(safe_markup(inline_to_pango(text), text))
            lbl.set_hexpand(True)
            row.append(lbl)
            box.append(row)
        return box

    @staticmethod
    def _build_table(block: Block) -> Gtk.Widget:
        return TableView(block)


class TableView(Gtk.Box):
    """A Markdown table; rows can be added and edited in place while it streams."""

    def __init__(self, block: Block):
        super().__init__()
        self.add_css_class("md-table-frame")
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.grid = Gtk.Grid(hexpand=True)
        self.append(self.grid)
        self.aligns = list(block.aligns)
        self.rows: list[list[str]] = []
        self.set_rows(block.rows)

    def set_rows(self, rows: list[list[str]]) -> None:
        xalign = {"left": 0.0, "center": 0.5, "right": 1.0}
        for r, row in enumerate(rows):
            old = self.rows[r] if r < len(self.rows) else None
            for c, cell in enumerate(row):
                if old is not None and c < len(old) and old[c] == cell:
                    continue
                markup = safe_markup(inline_to_pango(cell), cell)
                if r == 0:
                    markup = f"<b>{markup}</b>"
                lbl = self.grid.get_child_at(c, r)
                if lbl is None:
                    lbl = md_label(markup)
                    lbl.set_xalign(xalign.get(self.aligns[c] if c < len(self.aligns) else "left", 0.0))
                    lbl.set_hexpand(True)
                    lbl.add_css_class("md-cell")
                    if r == 0:
                        lbl.add_css_class("md-head")
                    elif r % 2 == 0:
                        lbl.add_css_class("md-alt")
                    self.grid.attach(lbl, c, r, 1, 1)
                else:
                    lbl.set_markup(markup)
        for r in range(len(rows), len(self.rows)):
            for c in range(len(self.rows[r])):
                child = self.grid.get_child_at(c, r)
                if child is not None:
                    self.grid.remove(child)
        self.rows = [list(r) for r in rows]


# ------------------------------------------------------------------ thinking

class ThinkingView(Gtk.Box):
    def __init__(self, expanded: bool = True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("thinking")
        self.text = ""
        self.button = Gtk.ToggleButton(active=expanded, halign=Gtk.Align.START)
        self.button.add_css_class("flat")
        self.button.add_css_class("thinking-toggle")
        inner = Gtk.Box(spacing=6)
        inner.append(Gtk.Image(icon_name="emoji-objects-symbolic"))
        self.title = Gtk.Label(label="Thinking…")
        inner.append(self.title)
        self.arrow = Gtk.Image(icon_name="pan-down-symbolic" if expanded else "pan-end-symbolic")
        inner.append(self.arrow)
        self.button.set_child(inner)
        self.append(self.button)
        self.revealer = Gtk.Revealer(reveal_child=expanded)
        self.label = md_label("", "thinking-text")
        self.revealer.set_child(self.label)
        self.append(self.revealer)
        self.button.connect("toggled", self._toggled)

    def _toggled(self, btn) -> None:
        self.revealer.set_reveal_child(btn.get_active())
        self.arrow.set_from_icon_name("pan-down-symbolic" if btn.get_active() else "pan-end-symbolic")

    def set_text(self, text: str) -> None:
        self.text = text
        self.label.set_markup(esc(text.strip()))

    def finish(self, seconds: float | None = None) -> None:
        self.title.set_label(f"Thought for {seconds:.0f} s" if seconds else "Thoughts")
        self.button.set_active(False)


# ------------------------------------------------------------------ tool cards

class ToolCard(Gtk.Box):
    def __init__(self, name: str, args: dict, describe, result_path=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("tool-card")
        self.name, self.args, self._describe = name, args, describe
        self._result_path = result_path
        self.header = Gtk.ToggleButton()
        self.header.add_css_class("flat")
        self.header.add_css_class("tool-header")
        row = Gtk.Box(spacing=8)
        icon = Gtk.Image(icon_name=TOOL_ICONS.get(name, "applications-science-symbolic"))
        icon.add_css_class("tool-icon")
        row.append(icon)
        running = describe(name, args, True)
        self.title = Gtk.Label(label=running if running.endswith("…") else running + "…", xalign=0,
                               hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        row.append(self.title)
        self.state = Gtk.Stack()
        self.spinner = Gtk.Spinner(spinning=True)
        self.state.add_named(self.spinner, "busy")
        self.ok_icon = Gtk.Image(icon_name="emblem-ok-symbolic")
        self.ok_icon.add_css_class("success")
        self.state.add_named(self.ok_icon, "ok")
        self.err_icon = Gtk.Image(icon_name="dialog-warning-symbolic")
        self.err_icon.add_css_class("warning")
        self.state.add_named(self.err_icon, "error")
        row.append(self.state)
        self.arrow = Gtk.Image(icon_name="pan-end-symbolic")
        self.arrow.add_css_class("dim-label")
        row.append(self.arrow)
        self.header.set_child(row)
        self.header.connect("toggled", self._toggled)
        self.append(self.header)

        self.revealer = Gtk.Revealer()
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.add_css_class("tool-body")
        self.result = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                selectable=True)
        self.result.add_css_class("tool-result")
        body.append(self.result)
        self.actions = Gtk.Box(spacing=6)
        body.append(self.actions)
        self.revealer.set_child(body)
        self.append(self.revealer)

    def _toggled(self, btn) -> None:
        self.revealer.set_reveal_child(btn.get_active())
        self.arrow.set_from_icon_name("pan-down-symbolic" if btn.get_active() else "pan-end-symbolic")

    def finish(self, ok: bool, result: str) -> None:
        self.title.set_label(self._describe(self.name, self.args, False, ok))
        self.state.set_visible_child_name("ok" if ok else "error")
        self.spinner.set_spinning(False)
        text = result.strip()
        if len(text) > 4000:
            text = text[:4000] + f"\n… ({len(result) - 4000:,} more characters)"
        self.result.set_label(text or "(no output)")
        if not ok:
            self.add_css_class("failed")
        path = self._result_path(self.name, self.args) if (ok and self._result_path) else None
        if path is not None:
            self._add_open_buttons(Path(path))

    def _add_open_buttons(self, path: Path) -> None:
        if path.is_file():
            b = Gtk.Button(label="Open")
            b.connect("clicked", lambda *_: open_uri(path.as_uri()))
            self.actions.append(b)
        b = Gtk.Button(label="Show in folder")
        b.connect("clicked", lambda *_: open_uri((path if path.is_dir() else path.parent).as_uri()))
        self.actions.append(b)
        for b in (self.actions.get_first_child(), self.actions.get_last_child()):
            b.add_css_class("pill-small")
        quick = flat_button("folder-open-symbolic", "Show in folder",
                            lambda: open_uri((path if path.is_dir() else path.parent).as_uri()))
        self.header.get_child().insert_child_after(quick, self.title)


class ConfirmCard(Gtk.Box):
    """Asks the user to allow a file change, command or install. `on_answer(bool)` runs once."""

    def __init__(self, title: str, detail: str, on_answer, code: str | None = None,
                 allow_label: str = "Allow", icon: str = "dialog-warning-symbolic"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_css_class("confirm-card")
        self._on_answer = on_answer
        head = Gtk.Box(spacing=8)
        head.append(Gtk.Image(icon_name=icon))
        t = Gtk.Label(label=title, xalign=0)
        t.add_css_class("heading")
        head.append(t)
        self.append(head)
        d = Gtk.Label(label=detail, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.append(d)
        if code:
            # The exact command, so what runs is what the user read.
            sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                    propagate_natural_height=True, max_content_height=260)
            sw.add_css_class("confirm-code")
            c = Gtk.Label(label=code, xalign=0, yalign=0, wrap=True, wrap_mode=Pango.WrapMode.CHAR,
                          selectable=True)
            c.add_css_class("monospace")
            sw.set_child(c)
            self.append(sw)
        self.buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        deny = Gtk.Button(label="Don't allow")
        deny.connect("clicked", lambda *_: self.answer(False))
        allow = Gtk.Button(label=allow_label)
        allow.add_css_class("suggested-action")
        allow.connect("clicked", lambda *_: self.answer(True))
        self.buttons.append(deny)
        self.buttons.append(allow)
        self.append(self.buttons)
        self.result = Gtk.Label(xalign=1)
        self.result.add_css_class("dim-label")
        self.result.set_visible(False)
        self.append(self.result)

    def answer(self, allowed: bool) -> None:
        if self._on_answer is None:
            return
        cb, self._on_answer = self._on_answer, None
        self.buttons.set_visible(False)
        self.result.set_label("Allowed" if allowed else "Not allowed")
        self.result.set_visible(True)
        self.add_css_class("answered")
        cb(allowed)


# ------------------------------------------------------------------ messages

class UserMessage(Gtk.Box):
    def __init__(self, text: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2, halign=Gtk.Align.END)
        self.add_css_class("user-row")
        self.text = text
        bubble = Gtk.Label(label=text, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                           selectable=True)
        bubble.add_css_class("user-bubble")
        self.append(bubble)
        tools = Gtk.Box(halign=Gtk.Align.END)
        tools.add_css_class("msg-tools")
        self.copy_btn = flat_button("edit-copy-symbolic", "Copy", self._copy)
        tools.append(self.copy_btn)
        self.append(tools)

    def _copy(self) -> None:
        copy_text(self, self.text)
        flash_copied(self.copy_btn)


class AssistantTurn(Gtk.Box):
    """Everything the assistant produced for one user message: thoughts, tools, answer."""

    def __init__(self, model_name: str, describe, result_path):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("assistant-row")
        self._describe, self._result_path = describe, result_path
        head = Gtk.Box(spacing=8)
        avatar = Gtk.Image(icon_name="io.github.skyv04.QwenChat", pixel_size=22)
        avatar.add_css_class("avatar")
        head.append(avatar)
        name = Gtk.Label(label="Qwen", xalign=0)
        name.add_css_class("heading")
        head.append(name)
        self.model_label = Gtk.Label(label=model_name, xalign=0)
        self.model_label.add_css_class("dim-label")
        self.model_label.add_css_class("caption")
        head.append(self.model_label)
        self.append(head)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.body.add_css_class("assistant-body")
        self.append(self.body)
        self.tools: dict[str, ToolCard] = {}
        self._md: MarkdownView | None = None
        self._think: ThinkingView | None = None
        self._content = ""
        self._reasoning = ""
        self.round_open = False

        self.status_box = Gtk.Box(spacing=8)
        self.status_box.add_css_class("turn-status")
        self.spinner = Gtk.Spinner(spinning=True)
        self.status_box.append(self.spinner)
        self.status_label = Gtk.Label(label="", xalign=0)
        self.status_label.add_css_class("dim-label")
        self.status_box.append(self.status_label)
        self.append(self.status_box)
        self.status_box.set_visible(False)

        self.footer = Gtk.Box(spacing=2)
        self.footer.add_css_class("msg-tools")
        self.copy_btn = flat_button("edit-copy-symbolic", "Copy answer", self._copy)
        self.footer.append(self.copy_btn)
        self.regen_btn = flat_button("view-refresh-symbolic", "Regenerate")
        self.footer.append(self.regen_btn)
        self.stats = Gtk.Label(xalign=0, margin_start=6)
        self.stats.add_css_class("dim-label")
        self.stats.add_css_class("caption")
        self.footer.append(self.stats)
        self.append(self.footer)
        self.footer.set_visible(False)
        self.answer_texts: list[str] = []

    # -- streaming
    def begin_round(self) -> None:
        self._md = None
        self._think = None
        self._content = ""
        self._reasoning = ""
        self.round_open = True

    def has_content(self) -> bool:
        return bool(self._content.strip())

    def partial_content(self) -> str:
        return self._content.strip()

    def add_reasoning(self, delta: str, live: bool = True) -> None:
        if self._think is None:
            self._think = ThinkingView(expanded=live)
            self.body.append(self._think)
        self._reasoning += delta
        self._think.set_text(self._reasoning)

    def add_content(self, delta: str) -> None:
        if not self._content and not delta.strip():
            return
        if self._think is not None and self._content == "":
            self._think.finish()
        self._content += delta
        if self._md is None:
            self._md = MarkdownView()
            self.body.append(self._md)

    def flush(self) -> None:
        if self._md is not None:
            self._md.set_markdown(self._content.strip())

    def end_round(self, message: dict) -> None:
        self.round_open = False
        if self._think is not None:
            self._think.finish()
        text = (message.get("content") or "").strip()
        if text:
            if self._md is None:
                self._md = MarkdownView()
                self.body.append(self._md)
            self._md.set_markdown(text)
            self.answer_texts.append(text)
        elif self._md is not None:
            self.body.remove(self._md)
            self._md = None

    def add_tool(self, call_id: str, name: str, args: dict) -> ToolCard:
        card = ToolCard(name, args, self._describe, self._result_path)
        self.tools[call_id] = card
        self.body.append(card)
        return card

    def end_tool(self, call_id: str, ok: bool, result: str) -> None:
        card = self.tools.get(call_id)
        if card is not None:
            card.finish(ok, result)

    def add_widget(self, widget: Gtk.Widget) -> None:
        self.body.append(widget)

    def set_status(self, text: str | None) -> None:
        self.status_box.set_visible(bool(text))
        self.spinner.set_spinning(bool(text))
        if text:
            self.status_label.set_label(text)

    def show_error(self, text: str, retry=None) -> None:
        box = Gtk.Box(spacing=10)
        box.add_css_class("error-box")
        box.append(Gtk.Image(icon_name="dialog-error-symbolic", valign=Gtk.Align.START))
        lbl = Gtk.Label(label=text, xalign=0, wrap=True, hexpand=True, selectable=True,
                        wrap_mode=Pango.WrapMode.WORD_CHAR)
        box.append(lbl)
        if retry is not None:
            b = Gtk.Button(label="Try again", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: retry())
            box.append(b)
        self.body.append(box)

    def add_note(self, text: str) -> None:
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.add_css_class("dim-label")
        lbl.add_css_class("caption")
        self.body.append(lbl)

    def finish(self, stats: str = "", can_regenerate: bool = True) -> None:
        self.set_status(None)
        self.stats.set_label(stats)
        self.copy_btn.set_visible(bool(self.answer_texts))
        self.regen_btn.set_visible(can_regenerate)
        self.footer.set_visible(True)

    def _copy(self) -> None:
        copy_text(self, "\n\n".join(self.answer_texts))
        flash_copied(self.copy_btn)
