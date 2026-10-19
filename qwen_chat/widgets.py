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
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, GtkSource, Pango  # noqa: E402

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

# How a finished group of steps is summed up: (one, several).
STEP_WORDS = {
    "web_search": ("searched the web", "searched the web {n} times"),
    "fetch_url": ("read a page", "read {n} pages"),
    "download_file": ("downloaded a file", "downloaded {n} files"),
    "list_files": ("looked at the files", "looked at the files"),
    "read_file": ("read a file", "read {n} files"),
    "write_file": ("wrote a file", "wrote {n} files"),
    "make_directory": ("made a folder", "made {n} folders"),
    "move_path": ("moved a file", "moved {n} files"),
    "delete_path": ("deleted a file", "deleted {n} files"),
    "run_command": ("ran a command", "ran {n} commands"),
    "install_packages": ("installed software", "installed software"),
    "check_programs": ("checked for programs", "checked for programs"),
}


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s" if s % 60 else f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60} min"


def steps_summary(names: list[str]) -> str:
    counts: dict[str, int] = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    parts = []
    for name, n in counts.items():
        one, many = STEP_WORDS.get(name, (f"used {name}", f"used {name} {{n}} times"))
        parts.append(one if n == 1 else many.format(n=n))
    if len(parts) > 4:
        parts = parts[:3] + ["more"]
    text = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    return text[:1].upper() + text[1:]


def _file_icon(path: Path) -> str:
    try:
        ctype, _ = Gio.content_type_guess(path.name, None)
        icon = Gio.content_type_get_symbolic_icon(ctype)
        names = icon.get_names() if hasattr(icon, "get_names") else []
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        for n in names:
            if theme.has_icon(n):
                return n
    except (TypeError, GLib.Error, AttributeError):
        pass
    return "text-x-generic-symbolic"


class ToolCard(Gtk.Box):
    def __init__(self, name: str, args: dict, describe, result_paths=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("tool-card")
        self.name, self.args, self._describe = name, args or {}, describe
        self._result_paths = result_paths
        self.ok: bool | None = None
        self.paths: list[Path] = []
        self.has_restore = False
        command = self.args.get("command") if name == "run_command" else None
        command = command.strip() if isinstance(command, str) else ""
        why = self.args.get("why") if name == "run_command" else None
        # A command is shown by what it's for, with the command itself underneath.
        self._why = " ".join(why.split()).rstrip(".") if isinstance(why, str) and command else ""

        # A plain box that toggles on click, not a button, so the Restore and folder buttons
        # inside it get their own clicks.
        row = self.header = Gtk.Box(spacing=8)
        row.add_css_class("tool-header")
        row.set_cursor_from_name("pointer")
        icon = Gtk.Image(icon_name=TOOL_ICONS.get(name, "applications-science-symbolic"),
                         valign=Gtk.Align.CENTER)
        icon.add_css_class("tool-icon")
        row.append(icon)
        self._labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                               valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self._labels.append(self.title)
        if self._why:
            sub = Gtk.Label(label=" ".join(command.split()), xalign=0,
                            ellipsize=Pango.EllipsizeMode.END)
            sub.add_css_class("monospace")
            sub.add_css_class("tool-subtitle")
            self._labels.append(sub)
        row.append(self._labels)
        self.state = Gtk.Stack(valign=Gtk.Align.CENTER)
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
        self.expander = Gtk.ToggleButton(child=self.arrow, valign=Gtk.Align.CENTER,
                                         tooltip_text="Show details")
        self.expander.add_css_class("flat")
        self.expander.add_css_class("small-button")
        self.expander.add_css_class("tool-expander")
        self.expander.connect("toggled", self._toggled)
        row.append(self.expander)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self.expander.set_active(not self.expander.get_active()))
        row.add_controller(click)
        self.append(row)
        self._set_title(running=True, ok=True)

        self.revealer = Gtk.Revealer()
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.add_css_class("tool-body")
        if command:
            # The whole command, also when it ran without a confirmation card.
            cmd = Gtk.Label(label=command, xalign=0, wrap=True,
                            wrap_mode=Pango.WrapMode.CHAR, selectable=True)
            cmd.add_css_class("monospace")
            cmd.add_css_class("tool-command")
            body.append(cmd)
        self.result = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                selectable=True)
        self.result.add_css_class("tool-result")
        body.append(self.result)
        self.actions = Gtk.Box(spacing=6)
        self.actions.set_visible(False)
        body.append(self.actions)
        self.revealer.set_child(body)
        self.append(self.revealer)

    def _set_title(self, running: bool, ok: bool) -> None:
        if self._why:
            text = self._why[:1].upper() + self._why[1:]
        else:
            text = self._describe(self.name, self.args, running, ok)
        if running and not text.endswith("…"):
            text += "…"
        self.title.set_label(text)
        self.title.set_tooltip_text(text if len(text) > 60 else None)

    def title_text(self) -> str:
        return self.title.get_label()

    def _toggled(self, btn) -> None:
        self.revealer.set_reveal_child(btn.get_active())
        self.arrow.set_from_icon_name("pan-down-symbolic" if btn.get_active() else "pan-end-symbolic")

    def finish(self, ok: bool, result: str) -> None:
        self.ok = ok
        self._set_title(running=False, ok=ok)
        self.state.set_visible_child_name("ok" if ok else "error")
        self.spinner.set_spinning(False)
        text = result.strip()
        if len(text) > 4000:
            text = text[:4000] + f"\n… ({len(result) - 4000:,} more characters)"
        self.result.set_label(text or "(no output)")
        if not ok:
            self.add_css_class("failed")
        if ok and self._result_paths:
            try:
                self.paths = [Path(p) for p in self._result_paths(self.name, self.args, result)]
            except Exception:
                self.paths = []
        if self.paths:
            self._add_open_buttons(self.paths)

    def _add_action(self, label: str, callback) -> Gtk.Button:
        b = Gtk.Button(label=label)
        b.add_css_class("pill-small")
        b.connect("clicked", lambda *_: callback())
        self.actions.append(b)
        self.actions.set_visible(True)
        return b

    def _add_open_buttons(self, paths: list[Path]) -> None:
        files = [p for p in paths if p.is_file()]
        for p in files[:4]:
            b = self._add_action("Open" if len(files) == 1 else f"Open {p.name}",
                                 lambda p=p: open_uri(p.as_uri()))
            b.get_child().set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            b.get_child().set_max_width_chars(28)
        first = paths[0]
        folder = first if first.is_dir() else first.parent
        self._add_action("Show in folder", lambda: open_uri(folder.as_uri()))
        quick = flat_button("folder-open-symbolic", "Show in folder",
                            lambda: open_uri(folder.as_uri()))
        self.header.insert_child_after(quick, self._labels)

    def add_restore(self, on_restore) -> None:
        """Offers to undo a replace or delete; on_restore() -> bool puts the old file back."""
        btn = Gtk.Button(label="Restore", valign=Gtk.Align.CENTER,
                         tooltip_text="Put back the version that went to the Trash")
        btn.add_css_class("pill-small")
        btn.add_css_class("restore-button")

        def clicked(_b):
            if on_restore():
                btn.set_label("Restored")
                btn.set_sensitive(False)

        btn.connect("clicked", clicked)
        self.header.insert_child_after(btn, self._labels)
        self.has_restore = True


class ToolGroup(Gtk.Box):
    """Consecutive steps. Once the answer is done, three or more fold into one summary line."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("tool-group")
        self.cards: list[ToolCard] = []
        self.header = Gtk.ToggleButton(visible=False)
        self.header.add_css_class("flat")
        self.header.add_css_class("group-header")
        row = Gtk.Box(spacing=8)
        icon = Gtk.Image(icon_name="view-list-symbolic", valign=Gtk.Align.CENTER)
        icon.add_css_class("tool-icon")
        row.append(icon)
        self.count = Gtk.Label(xalign=0)
        self.count.add_css_class("group-count")
        row.append(self.count)
        self.summary = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.summary.add_css_class("dim-label")
        row.append(self.summary)
        self.failed = Gtk.Label(visible=False)
        self.failed.add_css_class("group-failed")
        row.append(self.failed)
        self.arrow = Gtk.Image(icon_name="pan-end-symbolic", valign=Gtk.Align.CENTER)
        self.arrow.add_css_class("dim-label")
        row.append(self.arrow)
        self.header.set_child(row)
        self.header.connect("toggled", self._toggled)
        self.append(self.header)
        self.revealer = Gtk.Revealer(reveal_child=True)
        self.inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.inner.add_css_class("group-steps")
        self.revealer.set_child(self.inner)
        self.append(self.revealer)

    def add(self, widget: Gtk.Widget) -> None:
        self.inner.append(widget)
        if isinstance(widget, ToolCard):
            self.cards.append(widget)

    def _toggled(self, btn) -> None:
        self.revealer.set_reveal_child(btn.get_active())
        self.arrow.set_from_icon_name("pan-down-symbolic" if btn.get_active() else "pan-end-symbolic")

    def finish(self) -> None:
        cards = self.cards
        if len(cards) < 3 or any(c.has_restore or c.ok is None for c in cards):
            return
        self.count.set_label(f"{len(cards)} steps")
        self.summary.set_label(steps_summary([c.name for c in cards]))
        failed = sum(1 for c in cards if c.ok is False)
        self.failed.set_label(f"{failed} failed")
        self.failed.set_visible(bool(failed))
        self.header.set_visible(True)
        self.add_css_class("folded")
        kind = self.revealer.get_transition_type()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.NONE)
        self.revealer.set_reveal_child(False)
        self.revealer.set_transition_type(kind)


class ConfirmCard(Gtk.Box):
    """Asks the user to allow a file change, command or install. `on_answer(bool)` runs once.
    `reason` says why this one needs an OK; `extra` = (label, callback) adds a button that
    also allows it."""

    def __init__(self, title: str, detail: str, on_answer, code: str | None = None,
                 allow_label: str = "Allow", icon: str = "dialog-warning-symbolic",
                 reason: str = "", extra=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_css_class("confirm-card")
        self.add_css_class("risky" if reason or allow_label == "Allow" else "routine")
        self.routine = not reason
        self._on_answer = on_answer
        head = Gtk.Box(spacing=8)
        head.append(Gtk.Image(icon_name=icon))
        t = Gtk.Label(label=title, xalign=0, hexpand=True)
        t.add_css_class("heading")
        head.append(t)
        self.result = Gtk.Label(xalign=1)
        self.result.add_css_class("dim-label")
        self.result.add_css_class("caption")
        self.result.set_visible(False)
        head.append(self.result)
        self.append(head)
        self.details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        if reason:
            r = Gtk.Box(spacing=8)
            r.add_css_class("confirm-reason")
            r.append(Gtk.Image(icon_name="dialog-warning-symbolic", valign=Gtk.Align.START))
            r.append(Gtk.Label(label=reason, xalign=0, wrap=True, hexpand=True,
                               wrap_mode=Pango.WrapMode.WORD_CHAR))
            self.details.append(r)
        d = Gtk.Label(label=detail, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.details.append(d)
        if code:
            # The exact command, so what runs is what the user read. Only long ones scroll.
            c = Gtk.Label(label=code, xalign=0, yalign=0, wrap=True, wrap_mode=Pango.WrapMode.CHAR,
                          selectable=True)
            c.add_css_class("monospace")
            if code.count("\n") < 12 and len(code) < 900:
                c.add_css_class("confirm-code")
                self.details.append(c)
            else:
                sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, min_content_height=200,
                                        max_content_height=260, propagate_natural_height=True)
                sw.add_css_class("confirm-code")
                sw.set_child(c)
                self.details.append(sw)
        self.append(self.details)
        self.buttons = Gtk.Box(spacing=8)
        if extra:
            label, callback = extra
            more = Gtk.Button(label=label)
            more.add_css_class("flat")
            more.connect("clicked", lambda *_: (callback(), self.answer(True, "Allowed for this chat")))
            self.buttons.append(more)
        self.buttons.append(Gtk.Box(hexpand=True))
        deny = Gtk.Button(label="Don't allow")
        deny.connect("clicked", lambda *_: self.answer(False))
        allow = Gtk.Button(label=allow_label)
        allow.add_css_class("suggested-action")
        allow.connect("clicked", lambda *_: self.answer(True))
        self.buttons.append(deny)
        self.buttons.append(allow)
        self.append(self.buttons)

    def answer(self, allowed: bool, note: str | None = None) -> None:
        if self._on_answer is None:
            return
        cb, self._on_answer = self._on_answer, None
        # Once answered, fold to one line; the step's card still shows the command.
        self.buttons.set_visible(False)
        self.details.set_visible(False)
        self.result.set_label(note or ("Allowed" if allowed else "Not allowed"))
        self.result.set_visible(True)
        self.add_css_class("answered")
        # A plain OK needs no trace: the step's card shows what ran.
        self.set_visible(not allowed or bool(note))
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

    def __init__(self, model_name: str, describe, result_paths):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("assistant-row")
        self._describe, self._result_paths = describe, result_paths
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
        self._group: ToolGroup | None = None
        self._last_card: ToolCard | None = None
        self._md: MarkdownView | None = None
        self._think: ThinkingView | None = None
        self._content = ""
        self._reasoning = ""
        self.round_open = False

        self.status_box = Gtk.Box(spacing=8)
        self.status_box.add_css_class("turn-status")
        self.spinner = Gtk.Spinner(spinning=True)
        self.status_box.append(self.spinner)
        self.status_label = Gtk.Label(label="", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status_label.add_css_class("dim-label")
        self.status_box.append(self.status_label)
        self.elapsed = Gtk.Label(xalign=0)
        self.elapsed.add_css_class("dim-label")
        self.elapsed.add_css_class("elapsed")
        self.status_box.append(self.elapsed)
        self.append(self.status_box)
        self.status_box.set_visible(False)

        self.files = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.files.add_css_class("turn-files")
        self.files.set_visible(False)
        self.append(self.files)

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

    def _current_group(self) -> ToolGroup:
        # Steps with nothing else in between share a group.
        if self._group is None or self.body.get_last_child() is not self._group:
            self._group = ToolGroup()
            self.body.append(self._group)
        return self._group

    def add_tool(self, call_id: str, name: str, args: dict) -> ToolCard:
        card = ToolCard(name, args, self._describe, self._result_paths)
        self.tools[call_id] = card
        self._current_group().add(card)
        self._last_card = card
        return card

    def last_tool_card(self) -> ToolCard | None:
        return self._last_card

    def end_tool(self, call_id: str, ok: bool, result: str) -> None:
        card = self.tools.get(call_id)
        if card is not None:
            card.finish(ok, result)

    def add_widget(self, widget: Gtk.Widget) -> None:
        self.body.append(widget)

    def add_confirm(self, card: Gtk.Widget) -> None:
        """A question about the step that is running: shown right under that step."""
        if self._group is not None and self.body.get_last_child() is self._group:
            self._group.add(card)
        else:
            self.body.append(card)

    def set_status(self, text: str | None) -> None:
        self.status_box.set_visible(bool(text))
        self.spinner.set_spinning(bool(text))
        if text:
            self.status_label.set_label(text)

    def set_elapsed(self, seconds: float | None) -> None:
        self.elapsed.set_label(f"· {fmt_duration(seconds)}" if seconds and seconds >= 3 else "")

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

    def _show_files(self) -> None:
        """The files this answer made, as one row of buttons under it."""
        while (c := self.files.get_first_child()) is not None:
            self.files.remove(c)
        found: list[Path] = []
        for card in self.tools.values():
            for p in card.paths:
                if p in found:
                    found.remove(p)
                found.append(p)
        found = [p for p in found if p.is_file()]
        self.files.set_visible(bool(found))
        if not found:
            return
        shown = found[-6:]
        head = Gtk.Label(label="Files", xalign=0)
        head.add_css_class("caption-heading")
        head.add_css_class("dim-label")
        self.files.append(head)
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8,
                           column_spacing=6, row_spacing=6, homogeneous=False)
        for p in shown:
            b = Gtk.Button(tooltip_text=f"Open {p.name}")
            b.add_css_class("file-chip")
            box = Gtk.Box(spacing=6)
            box.append(Gtk.Image(icon_name=_file_icon(p)))
            box.append(Gtk.Label(label=p.name, ellipsize=Pango.EllipsizeMode.MIDDLE,
                                 max_width_chars=26))
            b.set_child(box)
            b.connect("clicked", lambda _b, p=p: open_uri(p.as_uri()))
            flow.append(b)
        folder = shown[-1].parent
        more = f"{len(found) - len(shown)} more · " if len(found) > len(shown) else ""
        b = Gtk.Button(tooltip_text=f"{more}Show in folder")
        b.add_css_class("file-chip")
        b.add_css_class("folder-chip")
        b.set_child(Gtk.Image(icon_name="folder-open-symbolic"))
        b.connect("clicked", lambda *_: open_uri(folder.as_uri()))
        flow.append(b)
        self.files.append(flow)

    def finish(self, stats: str = "", can_regenerate: bool = True) -> None:
        self.set_status(None)
        self.set_elapsed(None)
        for child in list(_children(self.body)):
            if isinstance(child, ToolGroup):
                child.finish()
        self._show_files()
        self.stats.set_label(stats)
        self.copy_btn.set_visible(bool(self.answer_texts))
        self.regen_btn.set_visible(can_regenerate)
        self.footer.set_visible(True)

    def _copy(self) -> None:
        copy_text(self, "\n\n".join(self.answer_texts))
        flash_copied(self.copy_btn)


def _children(widget: Gtk.Widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()
