"""The main window: chat list, conversation view and composer."""
from __future__ import annotations

import datetime
import os
import shutil
import threading
import time
import traceback
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import APP_ID, APP_NAME  # noqa: E402
from .agent import Agent, repair_history, salvage_args  # noqa: E402
from .llm import Cancelled, LLMError, ServerError, gpu_busy_percent  # noqa: E402
from .store import ChatStore, title_from  # noqa: E402
from .tools import ToolBox, ToolError, Workspace  # noqa: E402
from .widgets import AssistantTurn, ConfirmCard, UserMessage, fmt_duration, open_uri  # noqa: E402

SUGGESTIONS = [
    ("system-search-symbolic", "What's in the news today?",
     "What are today's top news headlines? Give me a short summary of each with links."),
    ("qwen-chat-web-symbolic", "Explain a topic from the web",
     "Look up how the James Webb Space Telescope works and explain it simply, with sources."),
    ("document-edit-symbolic", "Write a file",
     "Plan healthy dinners for this week and save the plan with a shopping list as meal-plan.md."),
    ("folder-symbolic", "Look at my workspace",
     "List what's in my workspace and tell me what each file is."),
]


BIDI = GObject.BindingFlags.BIDIRECTIONAL
SYNC = GObject.BindingFlags.SYNC_CREATE
INVERT = GObject.BindingFlags.INVERT_BOOLEAN

RUN_TIP_AUTO = ("Let Qwen run programs and install software in the workspace. Routine commands "
                "and small installs run right away; it asks first before anything risky or big")
RUN_TIP_ASK = ("Let Qwen run programs and install software in the workspace. It asks before "
               "every command and install (change this in the main menu)")


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _home(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


def _age_group(ts) -> str:
    try:
        days = (datetime.date.today() - datetime.date.fromtimestamp(float(ts))).days
    except (TypeError, ValueError, OverflowError, OSError):
        return "Older"
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return "Previous 7 Days" if days < 7 else "Previous 30 Days" if days < 30 else "Older"


class _UiEvents:
    """Agent events from the worker thread, forwarded to the GTK main loop."""

    def __init__(self, win: "ChatWindow", turn: AssistantTurn):
        self.win, self.turn = win, turn

    def _post(self, fn, *args):
        GLib.idle_add(lambda: (fn(self.turn, *args), False)[1])

    def status(self, text):
        self._post(self.win._ev_status, text)

    def round_begin(self):
        self._post(self.win._ev_round_begin)

    def reasoning(self, delta):
        self._post(self.win._ev_reasoning, delta)

    def content(self, delta):
        self._post(self.win._ev_content, delta)

    def tool_begin(self, call_id, name, args):
        self._post(self.win._ev_tool_begin, call_id, name, args)

    def tool_end(self, call_id, name, args, ok, result):
        self._post(self.win._ev_tool_end, call_id, ok, result)

    def round_end(self, message, timings):
        self._post(self.win._ev_round_end, message)


class ChatWindow(Adw.ApplicationWindow):
    def __init__(self, app, settings, server):
        super().__init__(application=app, title=APP_NAME)
        self.settings = settings
        self.server = server
        self.store = ChatStore()
        self.workspace = Workspace(settings.workspace_path, confirm=self._confirm)
        self.workspace.on_trash = self._on_trash
        settings.workspace_path.mkdir(parents=True, exist_ok=True)
        self.toolbox = ToolBox(self.workspace, web=settings["web"], files=settings["files"],
                               commands=settings["commands"], approve=self._approve,
                               auto_approve=self._auto_approve)
        self.agent = Agent(server, self.toolbox)
        self.chat: dict | None = None
        self.running = False
        self.cancel = threading.Event()
        self.turn: AssistantTurn | None = None
        self.last_turn: AssistantTurn | None = None
        self.attachments: list[str] = []
        self._status = ""
        self._server_state = "starting"
        self._flush_pending = False
        self._stick = True
        self._loading_list = False
        self._deleted: dict | None = None
        self._pending_confirm: ConfirmCard | None = None
        self._queued: tuple[str, list[str]] | None = None   # sent when the current answer ends
        self._turn_started = 0.0

        w, h = (settings["window_size"] + [1180, 820])[:2]
        self.set_default_size(int(w), int(h))
        self.set_size_request(360, 480)
        self.set_icon_name(APP_ID)

        self.toasts = Adw.ToastOverlay()
        self.flap = Adw.Flap(fold_policy=Adw.FlapFoldPolicy.AUTO, swipe_to_open=True,
                             swipe_to_close=True)
        self.flap.set_flap(self._build_sidebar())
        self.flap.set_separator(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.flap.set_content(self._build_content())
        self.toasts.set_child(self.flap)
        self.set_content(self.toasts)

        self._install_actions()
        self.connect("close-request", self._on_close)
        self._reload_chat_list()
        self._show_empty()
        self._update_composer()
        self.preload_model()

    # ================================================================== layout
    def _build_sidebar(self) -> Gtk.Widget:
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=272)
        side.add_css_class("sidebar-pane")
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        title = Gtk.Label(label="Chats")
        title.add_css_class("heading")
        hb.set_title_widget(title)
        self.side_header = hb
        side.append(hb)

        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.chat_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.chat_list.add_css_class("navigation-sidebar")
        self.chat_list.connect("row-selected", self._on_row_selected)
        self.chat_list.set_header_func(self._row_header)
        placeholder = Gtk.Label(label="No chats yet", margin_top=24)
        placeholder.add_css_class("dim-label")
        self.chat_list.set_placeholder(placeholder)
        scroller.set_child(self.chat_list)
        side.append(scroller)

        ws_btn = Gtk.Button(tooltip_text="Open the workspace folder. Qwen can only read and write files here.")
        ws_btn.add_css_class("flat")
        ws_btn.add_css_class("workspace-button")
        box = Gtk.Box(spacing=10)
        box.append(Gtk.Image(icon_name="folder-symbolic"))
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        t = Gtk.Label(label="Workspace", xalign=0)
        t.add_css_class("caption-heading")
        labels.append(t)
        p = Gtk.Label(label=_home(self.settings.workspace_path), xalign=0,
                      ellipsize=Pango.EllipsizeMode.MIDDLE)
        p.add_css_class("caption")
        p.add_css_class("dim-label")
        labels.append(p)
        box.append(labels)
        ws_btn.set_child(box)
        ws_btn.connect("clicked", lambda *_: self.open_workspace())
        side.append(ws_btn)
        return side

    def _build_content(self) -> Gtk.Widget:
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.header = Adw.HeaderBar()
        self.flap_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Show chats (F9)")
        self.flap.bind_property("reveal-flap", self.flap_btn, "active",
                                BIDI | SYNC)
        self.header.pack_start(self.flap_btn)
        self.new_btn = Gtk.Button(icon_name="document-new-symbolic", tooltip_text="New chat (Ctrl+N)",
                                  action_name="win.new-chat")
        self.header.pack_start(self.new_btn)
        self.flap.bind_property("reveal-flap", self.header, "show-start-title-buttons",
                                SYNC | INVERT)
        self.flap.bind_property("reveal-flap", self.side_header, "show-start-title-buttons",
                                SYNC)
        self.title = Adw.WindowTitle(title=APP_NAME, subtitle="Starting…")
        self.header.set_title_widget(self.title)
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=self._build_menu(),
                                  tooltip_text="Menu", primary=True)
        self.header.pack_end(menu_btn)
        self.gpu_label = Gtk.Label(tooltip_text="GPU load")
        self.gpu_label.add_css_class("gpu-pill")
        self.gpu_label.set_visible(False)
        self.header.pack_end(self.gpu_label)
        main.append(self.header)

        # Banner for model-server problems.
        self.banner = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        bbox = Gtk.Box(spacing=12)
        bbox.add_css_class("app-banner")
        bbox.append(Gtk.Image(icon_name="dialog-warning-symbolic", valign=Gtk.Align.START))
        self.banner_label = Gtk.Label(xalign=0, hexpand=True, wrap=True, selectable=True,
                                      wrap_mode=Pango.WrapMode.WORD_CHAR)
        bbox.append(self.banner_label)
        retry = Gtk.Button(label="Try again", valign=Gtk.Align.CENTER)
        retry.connect("clicked", lambda *_: self.preload_model())
        bbox.append(retry)
        self.banner.set_child(bbox)
        main.append(self.banner)

        # Conversation / empty state.
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.stack.add_named(self._build_empty(), "empty")
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=820, tightening_threshold=640)
        self.msg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        self.msg_box.add_css_class("messages")
        clamp.set_child(self.msg_box)
        self.scroller.set_child(clamp)
        adj = self.scroller.get_vadjustment()
        adj.connect("value-changed", self._on_scrolled)
        adj.connect("notify::upper", self._on_content_grew)
        adj.connect("notify::page-size", self._on_content_grew)
        self.stack.add_named(self.scroller, "chat")
        overlay = Gtk.Overlay(child=self.stack)
        self.jump_btn = Gtk.Button(icon_name="go-bottom-symbolic", tooltip_text="Jump to the latest",
                                   halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=12)
        self.jump_btn.add_css_class("osd")
        self.jump_btn.add_css_class("circular")
        self.jump_btn.set_visible(False)
        self.jump_btn.connect("clicked", lambda *_: self.scroll_to_bottom(force=True))
        overlay.add_overlay(self.jump_btn)
        main.append(overlay)
        main.append(self._build_composer())
        return main

    def _build_empty(self) -> Gtk.Widget:
        page = Adw.StatusPage(icon_name=APP_ID, title="What can I help with?")
        page.add_css_class("empty-page")
        page.set_description(
            "Qwen runs privately on this phone's GPU. It can search and read the web, "
            f"create and edit files in {_home(self.settings.workspace_path)}, and run or "
            "install programs there. It asks you first before anything risky.")
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           max_children_per_line=2, min_children_per_line=1,
                           column_spacing=10, row_spacing=10, halign=Gtk.Align.CENTER)
        grid.set_size_request(min(640, 640), -1)
        for icon, label, prompt in SUGGESTIONS:
            btn = Gtk.Button()
            btn.add_css_class("suggestion")
            box = Gtk.Box(spacing=10)
            box.append(Gtk.Image(icon_name=icon, valign=Gtk.Align.START))
            lbl = Gtk.Label(label=label, xalign=0, wrap=True, hexpand=True)
            box.append(lbl)
            btn.set_child(box)
            btn.connect("clicked", lambda _b, p=prompt: self.send(p))
            grid.append(btn)
        page.set_child(Adw.Clamp(maximum_size=640, child=grid))
        return page

    def _build_composer(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.add_css_class("composer-area")

        # A message written while Qwen works waits here and goes out when the answer ends.
        self.queue_bar = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        qbox = Gtk.Box(spacing=8)
        qbox.add_css_class("queue-bar")
        qbox.append(Gtk.Image(icon_name="mail-send-symbolic"))
        q_title = Gtk.Label(label="Next:")
        q_title.add_css_class("heading")
        qbox.append(q_title)
        self.queue_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        qbox.append(self.queue_label)
        q_hint = Gtk.Label(label="sends when Qwen finishes")
        q_hint.add_css_class("dim-label")
        q_hint.add_css_class("caption")
        qbox.append(q_hint)
        q_edit = Gtk.Button(label="Edit", action_name="win.queue-edit", valign=Gtk.Align.CENTER,
                            tooltip_text="Put it back in the message box")
        q_edit.add_css_class("pill-small")
        qbox.append(q_edit)
        q_drop = Gtk.Button(icon_name="window-close-symbolic", action_name="win.queue-discard",
                            valign=Gtk.Align.CENTER, tooltip_text="Don't send it")
        q_drop.add_css_class("flat")
        q_drop.add_css_class("circular")
        q_drop.add_css_class("small-button")
        qbox.append(q_drop)
        self.queue_bar.set_child(Adw.Clamp(maximum_size=820, tightening_threshold=640, child=qbox))
        outer.append(self.queue_bar)

        clamp = Adw.Clamp(maximum_size=820, tightening_threshold=640)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("composer")

        self.chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                                 column_spacing=6, row_spacing=6)
        self.chips.add_css_class("chips")
        self.chips.set_visible(False)
        card.append(self.chips)

        tv_overlay = Gtk.Overlay()
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                max_content_height=220)
        self.textview = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
                                     top_margin=8, bottom_margin=8, left_margin=10, right_margin=10)
        self.textview.add_css_class("composer-text")
        self.buffer = self.textview.get_buffer()
        self.buffer.connect("changed", lambda *_: self._update_composer())
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.textview.add_controller(keys)
        sw.set_child(self.textview)
        tv_overlay.set_child(sw)
        self.placeholder = Gtk.Label(label="Message Qwen…", halign=Gtk.Align.START,
                                     valign=Gtk.Align.START, margin_start=12, margin_top=8,
                                     can_target=False)
        self.placeholder.add_css_class("placeholder")
        tv_overlay.add_overlay(self.placeholder)
        card.append(tv_overlay)

        row = Gtk.Box(spacing=6)
        self.web_toggle = self._pill("qwen-chat-web-symbolic", "Web", self.settings["web"],
                                     "Let Qwen search and read the internet")
        self.web_toggle.connect("toggled", self._on_web_toggled)
        row.append(self.web_toggle)
        self.files_toggle = self._pill("folder-symbolic", "Files", self.settings["files"],
                                       f"Let Qwen read and write files in {_home(self.settings.workspace_path)}")
        self.files_toggle.connect("toggled", self._on_files_toggled)
        row.append(self.files_toggle)
        self.run_toggle = self._pill("utilities-terminal-symbolic", "Run", self.settings["commands"],
                                     RUN_TIP_AUTO)
        self.run_toggle.connect("toggled", self._on_run_toggled)
        row.append(self.run_toggle)
        self.think_toggle = self._pill("emoji-objects-symbolic", "Think", self.settings["thinking"],
                                       "Reason step by step before answering (slower, better for hard questions)")
        self.think_toggle.connect("toggled", self._on_think_toggled)
        row.append(self.think_toggle)
        self.attach_btn = Gtk.Button(icon_name="mail-attachment-symbolic", valign=Gtk.Align.CENTER,
                                     tooltip_text="Attach a file (copied into the workspace)")
        self.attach_btn.add_css_class("flat")
        self.attach_btn.add_css_class("circular")
        self.attach_btn.connect("clicked", lambda *_: self.pick_attachment())
        row.append(self.attach_btn)
        row.append(Gtk.Box(hexpand=True))
        self.ctx_label = Gtk.Label(valign=Gtk.Align.CENTER)
        self.ctx_label.add_css_class("ctx-label")
        self.ctx_label.add_css_class("caption")
        row.append(self.ctx_label)
        self.action_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.send_btn = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Send (Enter)",
                                   valign=Gtk.Align.CENTER)
        self.send_btn.add_css_class("circular")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.add_css_class("send-button")
        self.send_btn.connect("clicked", lambda *_: self.send())
        self.action_stack.add_named(self.send_btn, "send")
        self.stop_btn = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop (Esc)",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.add_css_class("circular")
        self.stop_btn.add_css_class("stop-button")
        self.stop_btn.connect("clicked", lambda *_: self.stop())
        self.action_stack.add_named(self.stop_btn, "stop")
        row.append(self.action_stack)
        card.append(row)
        clamp.set_child(card)
        outer.append(clamp)
        self.caption = Gtk.Label(label=f"Files stay in {_home(self.settings.workspace_path)} · "
                                       "Qwen can make mistakes, so check important facts.",
                                 wrap=True, justify=Gtk.Justification.CENTER)
        self.caption.add_css_class("caption")
        self.caption.add_css_class("dim-label")
        self.caption.add_css_class("composer-caption")
        outer.append(self.caption)

        try:
            drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            drop.connect("drop", self._on_drop)
            outer.add_controller(drop)
        except (TypeError, AttributeError):
            pass
        return outer

    @staticmethod
    def _pill(icon: str, label: str, active: bool, tooltip: str) -> Gtk.ToggleButton:
        btn = Gtk.ToggleButton(active=active, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
        btn.add_css_class("pill-toggle")
        box = Gtk.Box(spacing=5)
        box.append(Gtk.Image(icon_name=icon))
        box.append(Gtk.Label(label=label))
        btn.set_child(box)
        return btn

    def _build_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        s1 = Gio.Menu()
        s1.append("New Chat", "win.new-chat")
        s1.append("Open Workspace Folder", "win.open-workspace")
        s1.append("Restart Model", "win.restart-model")
        menu.append_section(None, s1)
        look = Gio.Menu()
        look.append("Follow System", "win.color-scheme::system")
        look.append("Light", "win.color-scheme::light")
        look.append("Dark", "win.color-scheme::dark")
        s2 = Gio.Menu()
        s2.append_submenu("Appearance", look)
        s2.append("Ask Before Every Command", "win.ask-commands")
        s2.append("Ask Before Replacing or Deleting Files", "win.ask-file-changes")
        s2.append("Unload Model When Closing", "win.stop-on-exit")
        menu.append_section(None, s2)
        s3 = Gio.Menu()
        s3.append("Keyboard Shortcuts", "win.shortcuts")
        s3.append(f"About {APP_NAME}", "app.about")
        s3.append("Quit", "app.quit")
        menu.append_section(None, s3)
        return menu

    def _install_actions(self) -> None:
        def add(name, cb, param=None):
            act = Gio.SimpleAction.new(name, param)
            act.connect("activate", cb)
            self.add_action(act)
            return act

        add("new-chat", lambda *_: self.new_chat())
        add("open-workspace", lambda *_: self.open_workspace())
        add("restart-model", lambda *_: self.restart_model())
        self.stop_action = add("stop", lambda *_: self.stop())
        add("toggle-sidebar", lambda *_: self.flap.set_reveal_flap(not self.flap.get_reveal_flap()))
        add("focus-composer", lambda *_: self.textview.grab_focus())
        add("undo-delete", lambda *_: self._undo_delete())
        add("shortcuts", lambda *_: self._show_shortcuts())
        scheme = Gio.SimpleAction.new_stateful("color-scheme", GLib.VariantType.new("s"),
                                               GLib.Variant("s", self.settings["color_scheme"]))
        scheme.connect("activate", self._on_color_scheme)
        self.add_action(scheme)
        for name, key in (("ask-commands", "ask_commands"), ("ask-file-changes", "ask_file_changes"),
                          ("stop-on-exit", "stop_server_on_exit")):
            act = Gio.SimpleAction.new_stateful(name, None, GLib.Variant("b", bool(self.settings[key])))
            act.connect("activate", self._on_bool_setting, key)
            self.add_action(act)
        add("queue-edit", lambda *_: self._edit_queued())
        add("queue-discard", lambda *_: self._discard_queued())

    # ================================================================== settings
    def _on_color_scheme(self, action, value) -> None:
        action.set_state(value)
        self.settings["color_scheme"] = value.get_string()
        self.settings.save()
        apply_color_scheme(self.settings["color_scheme"])

    def _on_bool_setting(self, action, _param, key) -> None:
        new = not action.get_state().get_boolean()
        action.set_state(GLib.Variant("b", new))
        self.settings[key] = new
        self.settings.save()
        if key == "ask_commands":
            if new and self.chat is not None and self.chat.get("auto_run"):
                self.chat["auto_run"] = False  # the menu wins over "Allow All in This Chat"
                self.store.save(self.chat)
            self._sync_run_tip()
            self.toast("Qwen will ask before every command and install." if new else
                       "Routine commands and small installs now run without asking. "
                       "Risky ones still ask.")
            if not new:
                self._answer_routine_card()
        elif key == "ask_file_changes":
            self.toast("Qwen will ask before replacing or deleting files." if new else
                       "Replaced and deleted files go to the Trash, so you can get them back.")

    # Routine commands and small installs run without asking unless "Ask Before Every Command"
    # is on; "Allow All in This Chat" on a question card turns that off again for one chat.
    def _auto_approve(self) -> bool:
        return not self.settings["ask_commands"] or bool(self.chat and self.chat.get("auto_run"))

    def _sync_run_tip(self) -> None:
        self.run_toggle.set_tooltip_text(RUN_TIP_AUTO if self._auto_approve() else RUN_TIP_ASK)

    def _allow_all_in_chat(self) -> None:
        if self.chat is not None:
            self.chat["auto_run"] = True
            self.store.save(self.chat)
        self._sync_run_tip()
        self.toast("Commands and small installs in this chat now run without asking. "
                   "Risky ones still ask.")

    def _answer_routine_card(self) -> None:
        card = self._pending_confirm
        if card is not None and getattr(card, "routine", False) and getattr(card, "kind", "") in ("Run", "Install"):
            card.answer(True)

    def _on_web_toggled(self, btn) -> None:
        self.settings["web"] = self.toolbox.web = btn.get_active()
        self.settings.save()

    def _on_files_toggled(self, btn) -> None:
        self.settings["files"] = self.toolbox.files = btn.get_active()
        self.settings.save()
        self._update_composer()

    def _on_run_toggled(self, btn) -> None:
        self.settings["commands"] = self.toolbox.commands = btn.get_active()
        self.settings.save()

    def _on_think_toggled(self, btn) -> None:
        self.settings["thinking"] = btn.get_active()
        self.settings.save()

    # ================================================================== model server
    def preload_model(self) -> None:
        self.banner.set_reveal_child(False)
        self._server_state = "starting"
        self._set_status("Starting…")

        def work():
            try:
                self.server.ensure_running(lambda s: GLib.idle_add(self._set_status_idle, s))
                GLib.idle_add(self._server_ready)
            except ServerError as e:
                GLib.idle_add(self._server_failed, str(e))
            except Exception as e:  # never leave the UI stuck on "Starting…"
                GLib.idle_add(self._server_failed, f"{type(e).__name__}: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _set_status_idle(self, text):
        if not self.running:
            self._set_status(text)
        return False

    def _server_ready(self):
        self._server_state = "ready"
        self.banner.set_reveal_child(False)
        if not self.running:
            self._set_status(self._ready_text())
        self._update_context()
        return False

    def _ready_text(self) -> str:
        return f"{self.server.model_name} · on GPU · ready"

    def _server_failed(self, text):
        self._server_state = "failed"
        self.banner_label.set_label("The model could not start.\n" + text)
        self.banner.set_reveal_child(True)
        self._set_status("Model not running")
        return False

    def restart_model(self) -> None:
        if self.running:
            self.toast("Stop the current answer first.")
            return
        if not self.server.owned and self.server.health() == "ok":
            self.toast("The model server was started outside Qwen Chat. Restart it there.")
            return
        self._set_status("Restarting the model…")

        def work():
            self.server.stop()
            GLib.idle_add(self.preload_model)

        threading.Thread(target=work, daemon=True).start()

    def _set_status(self, text: str) -> None:
        self._status = text
        self.title.set_subtitle(text)

    def _tick(self) -> bool:
        if not self.running:
            self.gpu_label.set_visible(False)
            return False
        if self.turn is not None:
            self.turn.set_elapsed(time.monotonic() - self._turn_started)
        pct = gpu_busy_percent()
        if pct is not None:
            self.gpu_label.set_label(f"GPU {pct}%")
            self.gpu_label.set_visible(True)
        return True

    # ================================================================== chat list
    def _reload_chat_list(self, select: str | None = None) -> None:
        self._loading_list = True
        try:
            while (row := self.chat_list.get_row_at_index(0)) is not None:
                self.chat_list.remove(row)
            for info in self.store.list():
                row = Gtk.ListBoxRow()
                row.chat_id = info["id"]
                row.group = _age_group(info.get("updated"))
                row.add_css_class("chat-row")
                box = Gtk.Box(spacing=6)
                lbl = Gtk.Label(label=info["title"], xalign=0, hexpand=True,
                                ellipsize=Pango.EllipsizeMode.END, tooltip_text=info["title"])
                box.append(lbl)
                delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete chat",
                                    valign=Gtk.Align.CENTER)
                delete.add_css_class("flat")
                delete.add_css_class("circular")
                delete.add_css_class("row-delete")
                delete.connect("clicked", lambda _b, cid=info["id"]: self.delete_chat(cid))
                box.append(delete)
                row.set_child(box)
                self.chat_list.append(row)
                if select and info["id"] == select:
                    self.chat_list.select_row(row)
        finally:
            self._loading_list = False

    @staticmethod
    def _row_header(row, before) -> None:
        group = getattr(row, "group", "")
        if before is not None and getattr(before, "group", "") == group:
            row.set_header(None)
            return
        current = row.get_header()
        if current is None or current.get_label() != group:
            label = Gtk.Label(label=group, xalign=0)
            label.add_css_class("chat-group-header")
            row.set_header(label)

    def _on_row_selected(self, _list, row) -> None:
        if self._loading_list or row is None:
            return
        if self.chat is not None and row.chat_id == self.chat["id"]:
            return
        chat = self.store.load(row.chat_id)
        if chat is None:
            self.toast("That chat could not be opened.")
            self._reload_chat_list()
            return
        self.chat = chat
        self._render_chat()
        if self.flap.get_folded():
            self.flap.set_reveal_flap(False)

    def new_chat(self) -> None:
        if self.running:
            return
        self.chat = None
        self.chat_list.unselect_all()
        self._show_empty()
        self.textview.grab_focus()
        if self.flap.get_folded():
            self.flap.set_reveal_flap(False)

    def delete_chat(self, chat_id: str) -> None:
        if self.running and self.chat and self.chat["id"] == chat_id:
            self.toast("Stop the current answer first.")
            return
        self._deleted = self.store.load(chat_id)
        self.store.delete(chat_id)
        if self.chat is not None and self.chat["id"] == chat_id:
            self.chat = None
            self._show_empty()
        self._reload_chat_list(self.chat["id"] if self.chat else None)
        toast = Adw.Toast(title="Chat deleted", button_label="Undo", action_name="win.undo-delete",
                          timeout=6)
        self.toasts.add_toast(toast)

    def _undo_delete(self) -> None:
        if self._deleted:
            self.store.save(self._deleted)
            self._reload_chat_list(self.chat["id"] if self.chat else None)
            self._deleted = None

    def open_workspace(self) -> None:
        self.settings.workspace_path.mkdir(parents=True, exist_ok=True)
        open_uri(self.settings.workspace_path.as_uri())

    # ================================================================== rendering
    def _clear_messages(self) -> None:
        while (child := self.msg_box.get_first_child()) is not None:
            self.msg_box.remove(child)
        self.last_turn = None

    def _show_empty(self) -> None:
        self._clear_messages()
        self.stack.set_visible_child_name("empty")
        self.title.set_title(APP_NAME)
        self._sync_run_tip()
        self._update_context()

    def _new_turn(self) -> AssistantTurn:
        if self.last_turn is not None:
            self.last_turn.regen_btn.set_visible(False)
        turn = AssistantTurn(self.server.model_name, self.toolbox.label, self.toolbox.result_paths)
        turn.regen_btn.connect("clicked", lambda *_: self.regenerate())
        self.msg_box.append(turn)
        self.last_turn = turn
        return turn

    def _render_chat(self) -> None:
        self._clear_messages()
        self.title.set_title(self.chat.get("title") or APP_NAME)
        msgs = self.chat.get("messages", [])
        if not msgs:
            self._show_empty()
            return
        self.stack.set_visible_child_name("chat")
        self._sync_run_tip()
        turn = None
        turns: list[list] = []   # [turn, last assistant message, when it was asked]
        error = None
        asked_at = None

        def show_error(last: bool):
            # A turn that failed is saved as an error on its user message, so it stays visible.
            nonlocal turn
            if not error:
                return
            if turn is None:
                turn = self._new_turn()
                turns.append([turn, None, None])
            turn.show_error(error, retry=self.regenerate if last else None)

        for m in msgs:
            role = m.get("role")
            if role == "user":
                show_error(False)
                self.msg_box.append(UserMessage(m.get("content") or ""))
                turn = None
                error = (m.get("_meta") or {}).get("error")
                asked_at = (m.get("_meta") or {}).get("time")
            elif role == "assistant":
                if turn is None:
                    turn = self._new_turn()
                    turns.append([turn, None, asked_at])
                turn.begin_round()
                if m.get("reasoning_content"):
                    turn.add_reasoning(m["reasoning_content"], live=False)
                turn.end_round(m)
                for call in m.get("tool_calls") or []:
                    try:
                        args = ToolBox.parse_args(call["function"].get("arguments"))
                    except Exception:
                        args = salvage_args(call["function"].get("arguments"))
                    turn.add_tool(call.get("id"), call["function"].get("name", ""), args)
                if m.get("_meta", {}).get("stopped"):
                    turn.add_note("Stopped")
                turns[-1][1] = m
            elif role == "tool" and turn is not None:
                turn.end_tool(m.get("tool_call_id"), bool(m.get("_meta", {}).get("ok", True)),
                              m.get("content") or "")
        show_error(True)
        for i, (t, last, start) in enumerate(turns):
            end = ((last or {}).get("_meta") or {}).get("time")
            took = end - start if start and end and 0 < end - start < 86400 else None
            t.finish(self._stats_text(last, took),
                     can_regenerate=(i == len(turns) - 1 and t is self.last_turn))
        self._update_context()
        self._stick = True
        GLib.idle_add(lambda: (self.scroll_to_bottom(force=True), False)[1])

    @staticmethod
    def _stats_text(msg: dict | None, took: float | None = None) -> str:
        t = (msg or {}).get("_meta", {}).get("timings") or {}
        tps, n = t.get("predicted_per_second"), t.get("predicted_n")
        parts = [f"Took {fmt_duration(took)}"] if took and took >= 1 else []
        if tps:
            parts.append(f"{tps:.1f} tokens/s")
        return " · ".join(parts)

    # ================================================================== scrolling
    def _at_bottom(self) -> bool:
        adj = self.scroller.get_vadjustment()
        return adj.get_value() + adj.get_page_size() >= adj.get_upper() - 48

    def _on_scrolled(self, _adj) -> None:
        self._stick = self._at_bottom()
        self.jump_btn.set_visible(not self._stick and self.stack.get_visible_child_name() == "chat")

    def _on_content_grew(self, *_):
        if self._stick:
            GLib.idle_add(lambda: (self.scroll_to_bottom(), False)[1])

    def scroll_to_bottom(self, force: bool = False) -> None:
        if force:
            self._stick = True
        if self._stick:
            adj = self.scroller.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())

    # ================================================================== composer
    def _text(self) -> str:
        start, end = self.buffer.get_bounds()
        return self.buffer.get_text(start, end, False)

    def _update_composer(self) -> None:
        text = self._text()
        self.placeholder.set_visible(not text)
        self.placeholder.set_label("Write a follow-up; Enter queues it for when Qwen finishes…"
                                   if self.running else "Message Qwen…")
        self.send_btn.set_sensitive(bool(text.strip() or self.attachments) and not self.running)
        self.action_stack.set_visible_child_name("stop" if self.running else "send")
        self.stop_action.set_enabled(self.running)
        self.attach_btn.set_sensitive(self.files_toggle.get_active())
        # Commands run in the workspace, so they go together with file access.
        self.run_toggle.set_sensitive(self.files_toggle.get_active())
        self.new_btn.set_sensitive(not self.running)
        self.chat_list.set_sensitive(not self.running)

    def _on_key(self, _ctrl, keyval, _code, state) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK):
                if state & Gdk.ModifierType.CONTROL_MASK:
                    self.send()
                    return True
                return False
            self.send()
            return True
        return False

    def _update_context(self) -> None:
        if not self.chat or not self.chat.get("messages"):
            self.ctx_label.set_label("")
            return
        used = 0
        for m in reversed(self.chat["messages"]):
            t = m.get("_meta", {}).get("timings") if m.get("role") == "assistant" else None
            if t and t.get("prompt_n") is not None:
                used = (t.get("cache_n") or 0) + (t.get("prompt_n") or 0) + (t.get("predicted_n") or 0)
                break
        if not used:
            used = sum(len(str(m.get("content") or "")) for m in self.chat["messages"]) // 3
        total = self.server.n_ctx
        self.ctx_label.set_label(f"{_k(used)} / {total // 1000}k")
        full = used > total * 0.8
        self.ctx_label.set_tooltip_text(
            "Context in use (tokens)." + (" The oldest messages are now left out to make room; "
                                         "start a new chat for a fresh context." if full else ""))
        if full:
            self.ctx_label.add_css_class("warning")
        else:
            self.ctx_label.remove_css_class("warning")

    # ---------------------------------------------------------------- attachments
    def pick_attachment(self) -> None:
        dialog = Gtk.FileChooserNative(title="Attach a file", transient_for=self,
                                       action=Gtk.FileChooserAction.OPEN, accept_label="Attach",
                                       modal=True)
        dialog.set_select_multiple(True)
        downloads = Path(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or Path.home())
        try:
            dialog.set_current_folder(Gio.File.new_for_path(str(downloads)))
        except GLib.Error:
            pass

        def done(dlg, response):
            if response == Gtk.ResponseType.ACCEPT:
                files = dlg.get_files()
                for i in range(files.get_n_items()):
                    path = files.get_item(i).get_path()
                    if path:
                        self.attach(Path(path))
            dlg.destroy()

        dialog.connect("response", done)
        self._dialog = dialog
        dialog.show()

    def _on_drop(self, _target, value, _x, _y) -> bool:
        try:
            files = value.get_files()
        except AttributeError:
            return False
        for f in files:
            if f.get_path():
                self.attach(Path(f.get_path()))
        return True

    def attach(self, src: Path) -> None:
        if not self.files_toggle.get_active():
            self.toast("Turn on Files to attach files.")
            return
        try:
            ws = self.settings.workspace_path.resolve()
            src_real = src.resolve()
            if src_real.is_dir():
                self.toast("Folders can't be attached; attach the files inside instead.")
                return
            if src_real.stat().st_size > 300 * 1024 * 1024:
                self.toast("That file is larger than 300 MB.")
                return
            if src_real == ws or ws in src_real.parents:
                rel = src_real.relative_to(ws).as_posix()
            else:
                dest_dir = ws / "attachments"
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / src.name
                n = 2
                while dest.exists():
                    dest = dest_dir / f"{src.stem} ({n}){src.suffix}"
                    n += 1
                shutil.copyfile(src_real, dest)
                rel = dest.relative_to(ws).as_posix()
        except OSError as e:
            self.toast(f"Could not attach {src.name}: {e.strerror or e}")
            return
        if rel not in self.attachments:
            self.attachments.append(rel)
            self._add_chip(rel)
        self._update_composer()

    def _add_chip(self, rel: str) -> None:
        chip = Gtk.Box(spacing=4)
        chip.add_css_class("chip")
        chip.append(Gtk.Image(icon_name="mail-attachment-symbolic"))
        chip.append(Gtk.Label(label=os.path.basename(rel), ellipsize=Pango.EllipsizeMode.MIDDLE,
                              max_width_chars=28, tooltip_text=rel))
        rm = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Remove")
        rm.add_css_class("flat")
        rm.add_css_class("circular")
        chip.append(rm)
        self.chips.append(chip)
        child = chip.get_parent()
        rm.connect("clicked", lambda *_: self._remove_chip(rel, child))
        self.chips.set_visible(True)

    def _remove_chip(self, rel: str, child) -> None:
        if rel in self.attachments:
            self.attachments.remove(rel)
        self.chips.remove(child)
        self.chips.set_visible(bool(self.attachments))
        self._update_composer()

    def _clear_chips(self) -> None:
        self.attachments = []
        while (c := self.chips.get_first_child()) is not None:
            self.chips.remove(c)
        self.chips.set_visible(False)

    # ================================================================== sending
    def send(self, text: str | None = None) -> None:
        typed = text is None
        text = (self._text() if typed else text).strip()
        atts = list(self.attachments)
        if not text and not atts:
            return
        if typed:
            self.buffer.set_text("")
        self._clear_chips()
        if self.running:
            self._queue(text, atts)
        else:
            self._submit(text, atts)

    def _queue(self, text: str, atts: list[str]) -> None:
        if self._queued is not None:
            old, old_atts = self._queued
            text = "\n\n".join(t for t in (old, text) if t)
            atts = old_atts + [a for a in atts if a not in old_atts]
        self._queued = (text, atts)
        preview = " ".join(text.split()) or ", ".join(os.path.basename(a) for a in atts)
        if text and atts:
            preview += f"  (+{len(atts)} file{'s' if len(atts) > 1 else ''})"
        self.queue_label.set_label(preview)
        self.queue_label.set_tooltip_text(text[:600] or None)
        self.queue_bar.set_reveal_child(True)

    def _take_queued(self) -> tuple[str, list[str]] | None:
        queued, self._queued = self._queued, None
        self.queue_bar.set_reveal_child(False)
        return queued

    def _edit_queued(self) -> None:
        queued = self._take_queued()
        if queued is None:
            return
        text, atts = queued
        current = self._text().strip()
        self.buffer.set_text("\n\n".join(t for t in (text, current) if t))
        for a in atts:
            if a not in self.attachments:
                self.attachments.append(a)
                self._add_chip(a)
        self._update_composer()
        self.textview.grab_focus()

    def _discard_queued(self) -> None:
        if self._take_queued() is not None:
            self.toast("The next message won't be sent.")

    def _submit(self, text: str, atts: list[str]) -> None:
        content = text
        if atts:
            listed = ", ".join(f"`{a}`" for a in atts)
            content = (text + "\n\n" if text else "") + f"Attached files (in the workspace): {listed}"
            if not text:
                content += "\nPlease look at them."
        if self.chat is None:
            self.chat = self.store.new()
            self._clear_messages()
        if not self.chat["messages"]:
            self.chat["title"] = title_from(text or os.path.basename(content))
        self.chat["messages"].append({"role": "user", "content": content, "_meta": {"time": time.time()}})
        self.store.save(self.chat)
        self._reload_chat_list(self.chat["id"])
        self.title.set_title(self.chat["title"])
        self.stack.set_visible_child_name("chat")
        self.msg_box.append(UserMessage(content))
        self._start_turn()

    def regenerate(self) -> None:
        if self.running or not self.chat:
            return
        msgs = self.chat["messages"]
        last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=None)
        if last_user is None:
            return
        del msgs[last_user + 1:]
        if self.last_turn is not None:
            self.msg_box.remove(self.last_turn)
            self.last_turn = None
        self._start_turn()

    def _start_turn(self) -> None:
        turn = self._new_turn()
        turn.set_status("Starting…")
        self.turn = turn
        self.running = True
        self._turn_started = time.monotonic()
        self.cancel = threading.Event()
        self._stick = True
        self._update_composer()
        self.scroll_to_bottom(force=True)
        GLib.timeout_add(1000, self._tick)
        last_user = next((m for m in reversed(self.chat["messages"]) if m.get("role") == "user"), None)
        if last_user is not None:
            (last_user.get("_meta") or {}).pop("error", None)
        work = [dict(m) for m in self.chat["messages"]]
        thinking = self.think_toggle.get_active()
        chat = self.chat
        cancel = self.cancel

        def worker():
            err = None
            cancelled = False
            try:
                self.agent.respond(work, _UiEvents(self, turn), cancel, thinking=thinking)
            except Cancelled:
                cancelled = True
            except (ServerError, LLMError) as e:
                err = str(e)
            except Exception as e:  # keep the app usable whatever happens
                traceback.print_exc()
                err = f"Something went wrong: {type(e).__name__}: {e}"
            if cancel.is_set():
                cancelled, err = True, None
            GLib.idle_add(lambda: (self._turn_done(chat, turn, work, cancelled, err), False)[1])

        threading.Thread(target=worker, daemon=True).start()

    def stop(self) -> None:
        if not self.running:
            return
        self.cancel.set()
        self.agent.abort()
        if self.turn is not None:
            self.turn.set_status("Stopping…")

    def _turn_done(self, chat, turn, work, cancelled, err) -> None:
        if self._pending_confirm is not None:
            self._pending_confirm.answer(False)
            self._pending_confirm = None
        self._flush(turn)
        if cancelled and turn.round_open:
            partial = turn.partial_content()
            if partial:
                work.append({"role": "assistant", "content": partial,
                             "_meta": {"stopped": True, "time": time.time()}})
                turn.end_round(work[-1])
        repair_history(work)
        if err:
            asked = next((m for m in reversed(work) if m.get("role") == "user"), None)
            if asked is not None:
                asked["_meta"] = {**(asked.get("_meta") or {}), "error": err}
        chat["messages"] = work
        self.store.save(chat)
        last = next((m for m in reversed(work) if m.get("role") == "assistant"), None)
        if err:
            turn.show_error(err, retry=self.regenerate)
        elif cancelled:
            turn.add_note("Stopped")
        took = time.monotonic() - self._turn_started
        turn.finish(self._stats_text(last, took) if not err else "", can_regenerate=True)
        self.running = False
        self.turn = None
        self._update_composer()
        if self._queued is not None:
            if err or cancelled:
                # Don't pile a new question onto a failed or stopped answer; let the user decide.
                self._edit_queued()
                self.toast("Your next message is back in the box. Send it when you're ready.")
            else:
                text, atts = self._take_queued()
                GLib.idle_add(lambda: (self._submit(text, atts), False)[1])
        self._update_context()
        if self._server_state == "ready" or self.server.health() == "ok":
            self._server_state = "ready"
            self._set_status(self._ready_text())
        else:
            self._set_status("Model not running")
        self.gpu_label.set_visible(False)
        if not self.is_active() and not cancelled:
            self._notify_done(chat)

    def _notify_done(self, chat) -> None:
        app = self.get_application()
        if app is None:
            return
        n = Gio.Notification.new("Qwen finished answering")
        n.set_body(chat.get("title") or "")
        app.send_notification("answer-done", n)

    # ---------------------------------------------------------------- agent events (main thread)
    def _ev_status(self, turn, text) -> None:
        turn.set_status(text)
        self._set_status(text)

    def _ev_round_begin(self, turn) -> None:
        turn.begin_round()

    def _ev_reasoning(self, turn, delta) -> None:
        turn.set_status("Thinking…")
        turn.add_reasoning(delta)

    def _ev_content(self, turn, delta) -> None:
        turn.add_content(delta)
        if turn.has_content():
            turn.set_status(None)
            if self._status != "Writing…":
                self._set_status("Writing…")
        if not self._flush_pending:
            self._flush_pending = True
            GLib.timeout_add(90, lambda: (self._flush(turn), False)[1])

    def _flush(self, turn) -> None:
        self._flush_pending = False
        turn.flush()

    def _ev_tool_begin(self, turn, call_id, name, args) -> None:
        self._flush(turn)
        card = turn.add_tool(call_id, name, args)
        msg = card.title_text()
        turn.set_status(msg)
        self._set_status(msg)

    def _ev_tool_end(self, turn, call_id, ok, result) -> None:
        turn.end_tool(call_id, ok, result)
        turn.set_status("Reading the results…")

    def _ev_round_end(self, turn, message) -> None:
        self._flush(turn)
        turn.end_round(message)

    # ---------------------------------------------------------------- confirmations (worker thread)
    def _approve(self, title: str, detail: str, code: str, action: str, reason: str = "") -> bool:
        """Commands and installs the ToolBox wants an OK for: risky or big ones (with a reason),
        or every one when "Ask Before Every Command" is on."""
        icon = "system-software-install-symbolic" if action == "Install" else "utilities-terminal-symbolic"
        extra = None if reason else ("Allow All in This Chat", self._allow_all_in_chat)
        return self._confirm(title, detail, code=code, allow_label=action, icon=icon, always=True,
                             reason=reason, extra=extra)

    def _confirm(self, title: str, detail: str, code: str | None = None, allow_label: str = "Allow",
                 icon: str = "dialog-warning-symbolic", always: bool = False, reason: str = "",
                 extra=None) -> bool:
        if not always and not self.settings["ask_file_changes"]:
            return True
        answered = threading.Event()
        result = {"ok": False}
        cancel = self.cancel

        def on_answer(ok):
            result["ok"] = ok
            answered.set()

        def show():
            turn = self.turn
            if turn is None:
                on_answer(False)
                return False
            card = ConfirmCard(title, detail, on_answer, code=code, allow_label=allow_label,
                               icon=icon, reason=reason, extra=extra)
            card.kind = allow_label
            self._pending_confirm = card
            turn.add_confirm(card)
            turn.set_status("Waiting for your answer…")
            self._set_status("Waiting for your answer…")
            if not self.is_active():
                app = self.get_application()
                n = Gio.Notification.new("Qwen is asking permission")
                n.set_body(f"{title} {code}" if code else detail)
                app.send_notification("confirm", n)
            return False

        GLib.idle_add(show)
        while not answered.wait(0.2):
            if cancel.is_set():
                return False
        GLib.idle_add(self._confirm_answered)
        return result["ok"]

    def _confirm_answered(self):
        self._pending_confirm = None
        return False

    # ---------------------------------------------------------------- Trash
    def _on_trash(self, original: Path, trashed: Path) -> None:
        # Tools run on the worker thread; a trash on the main thread comes from Restore itself.
        if threading.current_thread() is not threading.main_thread():
            GLib.idle_add(self._offer_restore, original, trashed)

    def _offer_restore(self, original: Path, trashed: Path) -> bool:
        card = self.turn.last_tool_card() if self.turn is not None else None
        if card is not None:
            card.add_restore(lambda: self._restore(original, trashed))
        return False

    def _restore(self, original: Path, trashed: Path) -> bool:
        try:
            self.workspace.restore(original, trashed)
        except (OSError, ToolError) as e:
            self.toast(f"Couldn't restore {original.name}: {getattr(e, 'strerror', None) or e}")
            return False
        self.toast(f"Restored {original.name}")
        return True

    # ================================================================== misc
    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def _show_shortcuts(self) -> None:
        rows = [("Send message", "Enter"), ("New line", "Shift+Enter"), ("Stop answering", "Esc"),
                ("New chat", "Ctrl+N"), ("Focus the message box", "Ctrl+L"),
                ("Show or hide chats", "F9"), ("Quit", "Ctrl+Q")]
        dlg = Adw.MessageDialog(transient_for=self, heading="Keyboard Shortcuts")
        grid = Gtk.Grid(column_spacing=24, row_spacing=8)
        for i, (what, keys) in enumerate(rows):
            grid.attach(Gtk.Label(label=what, xalign=0), 0, i, 1, 1)
            k = Gtk.Label(label=keys, xalign=1)
            k.add_css_class("dim-label")
            grid.attach(k, 1, i, 1, 1)
        dlg.set_extra_child(grid)
        dlg.add_response("close", "Close")
        dlg.present()

    def _on_close(self, *_):
        if not self.is_maximized():
            self.settings["window_size"] = [self.get_width(), self.get_height()]
            self.settings.save()
        if self.running:
            self.cancel.set()
            self.agent.abort()
        return False


def apply_color_scheme(name: str) -> None:
    Adw.StyleManager.get_default().set_color_scheme({
        "light": Adw.ColorScheme.FORCE_LIGHT,
        "dark": Adw.ColorScheme.FORCE_DARK,
    }.get(name, Adw.ColorScheme.DEFAULT))

