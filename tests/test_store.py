import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from qwen_chat.store import ChatStore


class ChatRenameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ChatStore(Path(self.tmp.name))
        self.chat = self.store.new()
        self.chat["messages"] = [
            {"role": "user", "content": "/worksheet-pdf Make an ocean quiz.", "_meta": {"time": 1}},
            {"role": "assistant", "content": "Created quiz.pdf.", "_meta": {"time": 2}},
        ]
        self.chat["auto_run"] = True
        self.store.save(self.chat)

    def test_rename_persists_without_changing_messages_or_history_order(self):
        before = json.loads(json.dumps(self.chat))
        name = "An ocean reading quiz with a deliberately long, descriptive session name"
        self.assertEqual(self.store.rename(self.chat["id"], "  " + name + "  "), name)
        after = ChatStore(self.store.root).load(self.chat["id"])
        self.assertEqual(after, {**before, "title": name})
        self.assertEqual(self.store.list()[0]["title"], name)

    def test_blank_and_missing_chats_are_not_saved(self):
        original = self.store.load(self.chat["id"])
        for title in ("", "  \t\n"):
            with self.assertRaises(ValueError):
                self.store.rename(self.chat["id"], title)
        with self.assertRaises(FileNotFoundError):
            self.store.rename("missing-chat", "New name")
        self.assertEqual(self.store.load(self.chat["id"]), original)

    def test_save_failure_keeps_the_original_title(self):
        with mock.patch("qwen_chat.store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.store.rename(self.chat["id"], "Not saved")
        self.assertEqual(self.store.load(self.chat["id"])["title"], self.chat["title"])
