"""Opt-in native tests: QWEN_TEST_GUI=1 xvfb-run -a python3 -m unittest tests.test_window_ui."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

if os.environ.get("QWEN_TEST_GUI") != "1":
    raise unittest.SkipTest("Native tests require an explicitly isolated Xvfb display.")
if not os.environ.get("DISPLAY") or re.search(r":0(?:\.\d+)?$", os.environ["DISPLAY"]):
    raise unittest.SkipTest("Do not run native tests on the user's display.")

_profile = tempfile.TemporaryDirectory(prefix="qwen-skill-ui-")
ROOT = Path(_profile.name)
for variable, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                          ("XDG_CACHE_HOME", "cache")):
    os.environ[variable] = str(ROOT / folder)
os.environ["QWEN_CHAT_NON_UNIQUE"] = "1"
os.environ["GSK_RENDERER"] = "cairo"
os.environ["GTK_A11Y"] = "none"
os.environ["GIO_USE_VFS"] = "local"
os.environ["GSETTINGS_BACKEND"] = "memory"

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GdkX11", "4.0")
from gi.repository import GLib, Gtk

from qwen_chat.app import QwenChatApp
from qwen_chat.config import DATA_DIR
from qwen_chat.window import ChatWindow, apply_color_scheme
from tests.test_agent import _Server
from tests.test_skills import skill_text

if not DATA_DIR.is_relative_to(ROOT):
    raise RuntimeError("Run this GUI module in its own process to isolate application data.")


def pump(seconds=0.25):
    until = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < until:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


class WindowUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QwenChatApp()
        cls.app.register(None)
        cls.app.settings["workspace"] = str(ROOT / "workspace")
        cls.app.settings["web"] = False
        cls.app.settings["stop_server_on_exit"] = False

    @classmethod
    def tearDownClass(cls):
        cls.app.quit()
        _profile.cleanup()

    def setUp(self):
        self.app.settings["files"] = True
        self.app.settings["commands"] = True
        self.app.settings["web"] = False
        self.app.settings["thinking"] = False
        self.app.settings["ask_commands"] = False
        self.app.settings["window_size"] = [1180, 820]
        apply_color_scheme("light")
        with mock.patch.object(ChatWindow, "preload_model"):
            self.win = ChatWindow(self.app, self.app.settings, _Server())
        self.app.window = self.win
        self.win._server_state = "ready"
        self.win._set_status("Ready")
        self.win._start_turn = mock.Mock()
        self.win.present()
        self.win.textview.grab_focus()
        pump()
        subprocess.run(["xdotool", "windowfocus", "--sync", str(self.win.get_surface().get_xid())],
                       check=True, timeout=5)
        pump(0.1)

    def tearDown(self):
        self.win.cancel.set()
        self.win.running = False
        # Finish pointer-leave events while native popup surfaces still exist.
        geometry = self.win.get_display().get_monitors().get_item(0).get_geometry()
        subprocess.run(["xdotool", "mousemove", str(geometry.width - 1), str(geometry.height - 1)],
                       check=True, timeout=5)
        pump(0.1)
        dialog = getattr(self.win, "_dialog", None)
        if isinstance(dialog, Gtk.Window):
            dialog.destroy()
        self.win.skill_picker.popdown()
        self.win.tools_popover.popdown()
        self.win.destroy()
        self.app.window = None
        pump(0.1)

    def draft(self, text):
        self.win.buffer.set_text(text)
        self.win.buffer.place_cursor(self.win.buffer.get_end_iter())
        self.win.textview.grab_focus()
        pump()

    def key(self, key):
        subprocess.run(["xdotool", "key", "--clearmodifiers", key], check=True, timeout=5)
        pump()

    def click(self, widget):
        native = widget.get_native()
        ok, bounds = widget.compute_bounds(native)
        self.assertTrue(ok)
        geometry = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", str(native.get_surface().get_xid())],
            check=True, timeout=5, capture_output=True, text=True)
        values = dict(line.split("=", 1) for line in geometry.stdout.splitlines())
        x = int(values["X"]) + int(bounds.get_x() + bounds.get_width() / 2)
        y = int(values["Y"]) + int(bounds.get_y() + bounds.get_height() / 2)
        subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"],
                       check=True, timeout=5)
        pump()

    def capture(self, name):
        folder = os.environ.get("QWEN_GUI_ARTIFACT_DIR")
        if folder:
            path = Path(folder) / (name + ".png")
            path.parent.mkdir(parents=True, exist_ok=True)
            pump()
            subprocess.run(["import", "-display", os.environ["DISPLAY"], "-window", "root", str(path)],
                           check=True, timeout=15)

    def assert_dialog_closed(self, dialog):
        deadline = time.monotonic() + 2
        while dialog.get_visible() and time.monotonic() < deadline:
            pump(0.05)
        self.assertFalse(dialog.get_visible())

    def test_button_search_and_insertion_preserve_the_draft(self):
        self.draft("Make a reading quiz about coral reefs.")
        self.key("ctrl+k")
        picker = self.win.skill_picker
        self.assertTrue(picker.get_visible())
        self.assertFalse(picker.inline)
        self.assertEqual(picker.get_position(), Gtk.PositionType.TOP)
        self.assertGreater(len(picker.catalog), 0)
        picker.search.set_text("worksheet")
        pump(0.4)
        self.assertEqual(picker.list.get_selected_row().skill.name, "worksheet-pdf")
        self.capture("skill-picker")
        self.key("Return")
        self.assertEqual(self.win._text(), "Make a reading quiz about coral reefs. /worksheet-pdf ")
        self.assertFalse(picker.get_visible())
        self.assertTrue(self.win.textview.has_focus())
        self.win._start_turn.assert_not_called()

    def test_inline_enter_selects_before_sending_and_queue_keeps_mentions(self):
        self.draft("Make /work")
        picker = self.win.skill_picker
        self.assertTrue(picker.get_visible())
        self.assertTrue(picker.inline)
        self.assertTrue(self.win.textview.has_focus())
        self.key("Down")
        self.assertEqual(picker.list.get_selected_row().skill.name, "skill-creator")
        self.key("Up")
        self.assertEqual(picker.list.get_selected_row().skill.name, "worksheet-pdf")
        self.assertEqual(self.win._text(), "Make /work")
        self.capture("skill-completion")
        self.key("Return")
        self.assertEqual(self.win._text(), "Make /worksheet-pdf ")
        self.win._start_turn.assert_not_called()
        self.win.running = True
        self.win._update_composer()
        self.key("Return")
        self.assertEqual(self.win._queued, ("Make /worksheet-pdf", []))
        self.assertEqual(self.win._text(), "")
        self.win._edit_queued()
        self.assertEqual(self.win._text(), "Make /worksheet-pdf")
        self.win.running = False
        self.win.send()
        saved = self.win.store.load(self.win.chat["id"])
        self.assertEqual(saved["messages"][-1]["content"], "Make /worksheet-pdf")
        self.win._start_turn.assert_called_once()

    def test_escape_closes_completion_without_stopping_an_answer(self):
        self.win.running = True
        self.win.stop = mock.Mock()
        self.draft("Next /work")
        self.key("Escape")
        self.assertFalse(self.win.skill_picker.get_visible())
        self.win.stop.assert_not_called()
        self.win._update_composer()
        pump()
        self.assertFalse(self.win.skill_picker.get_visible())
        self.key("Escape")
        self.win.stop.assert_called_once()

    def test_tab_completes_inside_a_sentence_without_reopening(self):
        self.draft("Make /work with an ocean theme.")
        self.win.buffer.place_cursor(self.win.buffer.get_iter_at_offset(8))
        pump()
        self.key("Tab")
        self.assertEqual(self.win._text(), "Make /worksheet-pdf with an ocean theme.")
        self.assertFalse(self.win.skill_picker.get_visible())
        self.assertEqual(self.win.buffer.get_iter_at_mark(self.win.buffer.get_insert()).get_offset(), 20)
        self.win._start_turn.assert_not_called()

    def test_mouse_picker_works_without_sending(self):
        self.draft("Make a quiz.")
        self.click(self.win.skills_button)
        picker = self.win.skill_picker
        self.assertTrue(picker.get_visible())
        picker.search.set_text("worksheet")
        pump(0.4)
        self.click(picker.list.get_selected_row())
        self.assertEqual(self.win._text(), "Make a quiz. /worksheet-pdf ")
        self.assertFalse(picker.get_visible())
        self.win._start_turn.assert_not_called()

    def test_authoring_prefills_without_destroying_or_sending_the_draft(self):
        self.draft("Use colorful examples.")
        self.win._skill_prompt(False)
        self.assertTrue(self.win._text().startswith("Use colorful examples.\n\n/skill-creator"))
        self.win._start_turn.assert_not_called()
        self.win.chat = self.win.store.new()
        self.win.chat["messages"] = [{"role": "user", "content": "Create a quiz."},
                                     {"role": "assistant", "content": "Created quiz.pdf."}]
        self.draft("Keep the page checks.")
        self.win._skill_prompt(True)
        self.assertTrue(self.win._text().startswith("Keep the page checks.\n\n/skill-creator"))
        self.assertIn("successful workflow", self.win._text())
        before = self.win._text()
        self.win.toolbox.files = False
        self.win._skill_prompt(False)
        self.assertEqual(self.win._text(), before)
        self.win._start_turn.assert_not_called()

    def test_save_card_and_new_and_existing_chats_share_the_library(self):
        self.win._submit("Save our ocean quiz workflow as a reusable skill.", [])
        chat_id = self.win.chat["id"]
        folder = self.win.workspace.root / "skills" / "ocean-ui"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_bytes(skill_text("ocean-ui"))
        turn = self.win._new_turn()
        self.win.turn = turn
        self.win.running = True
        args = {"path": str(folder)}
        self.win._ev_tool_begin(turn, "save1", "save_skill", args)
        results = []
        worker = threading.Thread(target=lambda: results.append(
            self.win.toolbox.run("save_skill", json.dumps(args))), daemon=True)
        worker.start()
        deadline = time.monotonic() + 5
        while self.win._pending_confirm is None and time.monotonic() < deadline:
            pump(0.05)
        card = self.win._pending_confirm
        self.assertIsNotNone(card)
        self.assertFalse(card.routine)
        self.win.scroll_to_bottom(force=True)
        self.capture("skill-save-approval")
        button = card.buttons.get_last_child()
        self.assertEqual(button.get_label(), "Save Skill")
        button.emit("clicked")
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0][0], results[0][1])
        self.win.running = False
        self.win.turn = None
        self.win.new_chat()
        self.win._show_skills()
        pump()
        self.assertIn("ocean-ui", {s.name for s in self.win.skill_picker.catalog})
        self.win.skill_picker.popdown()
        row = self.win.chat_list.get_row_at_index(0)
        self.assertEqual(row.chat_id, chat_id)
        self.win.chat_list.select_row(row)
        self.assertEqual(self.win.chat["id"], chat_id)
        self.win._show_skills()
        pump()
        self.assertIn("ocean-ui", {s.name for s in self.win.skill_picker.catalog})

    def test_picker_fits_a_narrow_window(self):
        self.win.set_default_size(680, 640)
        pump()
        self.key("ctrl+k")
        self.assertEqual(self.win.get_surface().get_width(), 680)
        self.assertTrue(self.win.skill_picker.get_visible())
        self.assertLess(self.win.skill_picker.get_width(), self.win.get_width())
        self.capture("skill-picker-narrow")

    def test_compact_tools_menu_preserves_draft_and_permission_settings(self):
        self.draft("Make a new worksheet with three multiple-choice questions.")
        draft = self.win._text()
        self.assertFalse(self.win.web_toggle.get_mapped())
        self.assertTrue(self.win.ctx_label.get_mapped())
        self.assertLessEqual(self.win.composer.get_height(), 125)
        self.capture("composer-clean")
        self.click(self.win.tools_button)
        self.assertTrue(self.win.web_toggle.get_mapped())
        self.assertTrue(self.win.run_toggle.get_sensitive())
        self.capture("composer-tools")
        self.click(self.win.files_toggle)
        self.assertFalse(self.win.toolbox.files)
        self.assertFalse(self.win.attach_btn.get_sensitive())
        self.assertFalse(self.win.run_toggle.get_sensitive())
        self.assertNotIn("render_worksheet", self.win.toolbox.names())
        self.assertNotIn("write_file", self.win.toolbox.names())
        self.assertIn("load_skill", self.win.toolbox.names())
        self.click(self.win.web_toggle)
        self.click(self.win.think_toggle)
        self.assertTrue(self.win.toolbox.web)
        self.assertTrue(self.win.settings["thinking"])
        self.assertIn("Think", self.win.tools_summary.get_label())
        saved = json.loads(self.win.settings.path.read_text())
        self.assertFalse(saved["files"])
        self.assertTrue(saved["web"])
        self.assertTrue(saved["thinking"])
        self.key("Escape")
        self.assertFalse(self.win.tools_popover.get_visible())
        self.assertEqual(self.win._text(), draft)
        self.win._start_turn.assert_not_called()

    def test_context_counter_stays_visible_while_answering_and_resets_for_new_chat(self):
        self.win.chat = self.win.store.new()
        self.win.chat["messages"] = [
            {"role": "assistant", "content": "Previous answer.",
             "_meta": {"timings": {"cache_n": 12000, "prompt_n": 1700, "predicted_n": 800}}}
        ]
        self.win._update_context()
        self.win.running = True
        self.draft("A follow-up while Qwen is answering.")
        self.assertFalse(self.win.tools_popover.get_visible())
        self.assertTrue(self.win.ctx_label.get_mapped())
        self.assertIs(self.win.ctx_label.get_parent(), self.win.send_btn.get_parent())
        self.assertEqual(self.win.ctx_label.get_label(), "14.5k / 16k")
        self.assertTrue(self.win.ctx_label.has_css_class("warning"))
        self.assertTrue(self.win.send_btn.get_mapped())
        self.assertTrue(self.win.stop_btn.get_mapped())
        self.capture("composer-context-warning")
        self.win.running = False
        self.win.new_chat()
        pump()
        self.assertTrue(self.win.ctx_label.get_mapped())
        self.assertEqual(self.win.ctx_label.get_label(), "0 / 16k")
        self.assertFalse(self.win.ctx_label.has_css_class("warning"))

    def test_clicking_send_queues_a_follow_up_with_its_attachment(self):
        source = ROOT / "composer-attachment.txt"
        source.write_text("A passage for the next worksheet.")
        self.win.attach(source)
        self.draft("Use this passage next.")
        attachments = list(self.win.attachments)
        self.win.running = True
        self.win.stop = mock.Mock()
        self.win._update_composer()
        self.assertTrue(self.win.send_btn.get_visible())
        self.assertTrue(self.win.send_btn.get_sensitive())
        self.assertTrue(self.win.stop_btn.get_visible())
        self.capture("composer-follow-up")
        self.click(self.win.send_btn)
        self.assertEqual(self.win._queued, ("Use this passage next.", attachments))
        self.assertEqual(self.win._text(), "")
        self.assertFalse(self.win.send_btn.get_visible())
        self.assertTrue(self.win.stop_btn.get_visible())
        self.win.stop.assert_not_called()
        self.win._start_turn.assert_not_called()
        self.win._edit_queued()
        self.assertEqual(self.win._text(), "Use this passage next.")
        self.assertEqual(self.win.attachments, attachments)
        self.click(self.win.tools_button)
        self.key("Escape")
        self.win.stop.assert_not_called()
        self.click(self.win.stop_btn)
        self.win.stop.assert_called_once()

    def test_composer_and_tools_fit_a_smaller_dark_window(self):
        apply_color_scheme("dark")
        self.win.set_default_size(680, 640)
        self.draft("A short message.")
        self.assertLessEqual(self.win.composer.get_height(), 125)
        self.assertTrue(self.win.ctx_label.get_mapped())
        ok, bounds = self.win.ctx_label.compute_bounds(self.win.composer)
        self.assertTrue(ok)
        self.assertGreaterEqual(bounds.get_x(), 0)
        self.assertLessEqual(bounds.get_x() + bounds.get_width(), self.win.composer.get_width())
        self.capture("composer-dark")
        self.click(self.win.tools_button)
        self.assertLess(self.win.tools_popover.get_width(), self.win.get_width())
        self.assertLess(self.win.tools_popover.get_height(), self.win.get_height())
        self.capture("composer-tools-dark")

    def test_page_budget_conflict_is_a_decision_not_a_failed_retry(self):
        self.win._submit("Make the worksheet PDF.", [])
        source = self.win.workspace.root / "page-budget.json"
        original = '{"title":"Moon quiz","max_pages":3,"questions":[{"prompt":"Explain."}]}'
        source.write_text(original)
        turn = self.win._new_turn()
        self.win.turn = turn
        self.win.running = True
        self.win._update_composer()
        args = {"spec_path": "page-budget.json", "path": "page-budget.pdf"}
        self.win._ev_tool_begin(turn, "pdf1", "render_worksheet", args)
        results = []

        def render(argv, *_):
            spec = json.loads(Path(argv[-2]).read_text())
            if spec["max_pages"] == 3:
                return 2, json.dumps({"ok": False, "code": "page_limit", "pages": 5, "max_pages": 3}), ""
            Path(argv[-1]).write_bytes(b"%PDF-1.7\nfixture")
            return 0, json.dumps({"ok": True, "pages": 5, "text_preserved": True,
                                  "layout_checked": True, "portrait": True, "color_emoji_checked": 4}), ""

        with mock.patch("qwen_chat.tools.run_streaming", side_effect=render):
            worker = threading.Thread(target=lambda: results.append(
                self.win.toolbox.run("render_worksheet", json.dumps(args))), daemon=True)
            worker.start()
            deadline = time.monotonic() + 5
            while self.win._pending_confirm is None and time.monotonic() < deadline:
                pump(0.05)
            card = self.win._pending_confirm
            self.assertIsNotNone(card)
            self.assertFalse(card.routine)
            self.win.scroll_to_bottom(force=True)
            self.capture("worksheet-page-approval")
            button = card.buttons.get_last_child()
            self.assertEqual(button.get_label(), "Use 5 Pages")
            self.click(button)
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0][0], results[0][1])
        self.assertIn("user approved 5 pages", results[0][1])
        self.assertEqual(source.read_text(), original)
        self.assertTrue((self.win.workspace.root / "page-budget.pdf").exists())

    def test_rename_another_chat_preserves_selection_draft_and_messages(self):
        self.win._submit("The active conversation.", [])
        active = self.win.chat
        other = self.win.store.new()
        other["title"] = "Old quiz title"
        other["messages"] = [{"role": "user", "content": "A different quiz."},
                              {"role": "assistant", "content": "Saved the quiz."}]
        self.win.store.save(other)
        before = json.loads(json.dumps(other))
        self.win._reload_chat_list(active["id"])
        self.draft("Unsent follow-up.")
        row = self.win.chat_list.get_row_at_index(0)
        self.assertEqual(row.chat_id, other["id"])
        rename = row.get_child().get_first_child().get_next_sibling()
        self.assertEqual(rename.get_tooltip_text(), "Rename chat")
        self.click(rename)
        pump()
        dialog = self.win._dialog
        entry = dialog.get_extra_child().get_row_at_index(0)
        entry.set_text("Ocean comprehension practice")
        self.capture("chat-rename")
        self.key("Return")
        self.assert_dialog_closed(dialog)
        self.assertEqual(self.win.store.load(other["id"]), {**before, "title": "Ocean comprehension practice"})
        self.assertIs(self.win.chat, active)
        self.assertEqual(self.win.chat_list.get_selected_row().chat_id, active["id"])
        self.assertEqual(self.win._text(), "Unsent follow-up.")

    def test_renaming_an_active_chat_survives_a_later_save_and_cancel_is_safe(self):
        self.win._submit("The title will change.", [])
        active = self.win.chat
        self.win.running = True
        self.win._queued = ("Keep this queued message.", [])
        self.key("F2")
        dialog = self.win._dialog
        entry = dialog.get_extra_child().get_row_at_index(0)
        entry.set_text(" ")
        self.assertFalse(dialog.get_response_enabled("rename"))
        entry.set_text("Renamed active chat")
        self.key("Return")
        self.assert_dialog_closed(dialog)
        self.assertIs(self.win.chat, active)
        self.assertEqual(self.win.title.get_title(), "Renamed active chat")
        self.assertEqual(self.win._queued, ("Keep this queued message.", []))
        self.capture("chat-renamed")
        self.win.store.save(active)
        self.assertEqual(self.win.store.load(active["id"])["title"], "Renamed active chat")
        self.key("F2")
        self.win._dialog.get_extra_child().get_row_at_index(0).set_text("Discard this edit")
        self.key("Escape")
        self.assert_dialog_closed(self.win._dialog)
        self.assertEqual(active["title"], "Renamed active chat")
        self.assertFalse(self.win.cancel.is_set())


if __name__ == "__main__":
    unittest.main()
