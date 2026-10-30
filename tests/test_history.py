import json
import tempfile
import unittest
from pathlib import Path

from qwen_chat.history import NOTE_HEAD, Activity, ChatFiles, ChatScope, call_files, context_note


def _user(text):
    return {"role": "user", "content": text}


def _answer(text):
    return {"role": "assistant", "content": text}


def _call(cid, name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _result(cid, name, content, ok=True, files=None):
    return {"role": "tool", "tool_call_id": cid, "name": name, "content": content,
            "_meta": {"ok": ok, **({"files": files} if files else {})}}


class ContextNoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_follow_up_gets_the_earlier_requests_how_they_ended_and_their_files(self):
        (self.root / "quiz.json").write_text("{}")
        (self.root / "quiz_v1.pdf").write_bytes(b"%PDF")
        (self.root / "notes.txt").write_text("ideas")
        messages = [
            _user("Make a fifth grade reading quiz about the woods, with a parent answer key."),
            _call("c0", "read_file", path="notes.txt"),
            _result("c0", "read_file", "ideas"),
            _call("c1", "write_file", path="quiz.json", content="{}"),
            _result("c1", "write_file", "Created quiz.json (2 bytes)."),
            _call("c2", "render_worksheet", spec_path="quiz.json", path="quiz_v1.pdf",
                  answer_key_path="quiz_v1-key.pdf"),
            _result("c2", "render_worksheet", "Created quiz_v1.pdf (80.0 KB).",
                    files={"created": ["quiz_v1.pdf", "quiz_v1-key.pdf"], "changed": []}),
            _answer("Done! Your quiz is **quiz_v1.pdf** and the key is quiz_v1-key.pdf."),
            _user("Keep it the same, but with a mermaid and a rainbow background."),
            _call("c3", "render_worksheet", spec_path="quiz.json", path="quiz_mermaid.pdf"),
            _result("c3", "render_worksheet", "Error: Invalid worksheet specification.", ok=False),
            _answer("**Task incomplete.** The following operations did not succeed; do not treat "
                    "their drafts as finished or opened PDFs.\n\n- Create PDF "
                    "`practice/quiz_mermaid.pdf`: Error: Invalid worksheet specification: "
                    "theme.background must be plain or rainbow."),
            _user("try harder"),
        ]
        note = context_note(messages, self.root)
        self.assertTrue(note.startswith(NOTE_HEAD + "\n"), note)
        self.assertIn('1. "Make a fifth grade reading quiz about the woods, with a parent answer '
                      'key." → answered: Done! Your quiz is quiz_v1.pdf and the key is '
                      'quiz_v1-key.pdf.', note)
        self.assertIn('2. "Keep it the same, but with a mermaid and a rainbow background." → ended '
                      'incomplete: Create PDF quiz_mermaid.pdf: Error: Invalid worksheet '
                      'specification: theme.background must be plain or rainbow.', note)
        self.assertIn("Files this chat made or changed, newest first: quiz_v1.pdf, "
                      "quiz_v1-key.pdf (now missing), quiz.json\n", note)
        self.assertIn("Files this chat read: notes.txt\n", note)
        self.assertNotIn("try harder", note)  # only what came before the new message
        self.assertNotIn("notes.txt", context_note(messages, self.root, hidden={"notes.txt"}))
        # A read from another chat's folder is left out unless the user named that file.
        (self.root / "grade1").mkdir()
        (self.root / "grade1" / "harvest.json").write_text("{}")
        wandered = messages[:3] + [_call("c9", "read_file", path="grade1/harvest.json"),
                                   _result("c9", "read_file", "{}")] + messages[3:]
        hidden = {"grade1/space-key.pdf"}
        self.assertIn("Files this chat read: notes.txt\n", context_note(wandered, self.root, hidden))
        wandered[0] = _user(wandered[0]["content"] + " Use grade1/harvest.json too.")
        self.assertIn("Files this chat read: grade1/harvest.json, notes.txt\n",
                      context_note(wandered, self.root, hidden))
        # The same request sent again counts once, with its latest outcome.
        again = messages[:8] + [_user(messages[0]["content"]), _answer("Here it is again."),
                                _user("thanks")]
        self.assertIn('\n1. "Make a fifth grade reading quiz about the woods, with a parent answer '
                      'key." → answered: Here it is again.\n', context_note(again, self.root))
        self.assertNotIn("\n2. ", context_note(again, self.root))

    def test_nothing_is_added_without_earlier_work_to_continue(self):
        self.assertEqual(context_note([_user("hi")], self.root), "")
        self.assertEqual(context_note([_user("hi"), _answer("Hello!"), _user("2+2?")], self.root), "")
        stopped = [_user("Write a poem."), {**_answer("Roses"), "_meta": {"stopped": True}},
                   _user("go on")]
        self.assertIn("→ stopped by the user", context_note(stopped, self.root))


class ChatScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / "homework/first-grade"
        self.folder.mkdir(parents=True)
        for name in ("space-adventure.json", "space-adventure-key.pdf", "harvest-math.json"):
            (self.folder / name).write_text("x")
        (self.root / "apples.png").write_bytes(b"png")
        self.grade1 = Activity(created={"apples.png": 3,
                                        "homework/first-grade/space-adventure.json": 5,
                                        "homework/first-grade/space-adventure-key.pdf": 6})

    def tearDown(self):
        self.tmp.cleanup()

    def foreign(self, messages):
        return set(ChatScope.build(self.root, messages, [("grade1", self.grade1)]).foreign)

    def test_other_chats_files_stay_hidden_until_the_user_names_them(self):
        everything = set(self.grade1.created)
        asks = lambda text: [_user(text)]
        # Words every worksheet shares, like "first grade" or "homework", don't name a file.
        self.assertEqual(self.foreign(asks("Make a first grade math quiz in my homework folder.")),
                         everything)
        self.assertEqual(self.foreign(asks("Use the Space Adventure quiz again.")), {"apples.png"})
        self.assertEqual(self.foreign(asks("Put apples.png on the cover.")),
                         everything - {"apples.png"})
        self.assertEqual(self.foreign(asks("Do what we did in grade1.")), set())
        offered = [_user("Make a fruit quiz."),
                   _answer("Should I use apples.png from your other chat?"), _user("Yes please")]
        self.assertEqual(self.foreign(offered), everything - {"apples.png"})
        made_here = [_user("Draw some fruit."), _call("c1", "run_command", command="draw"),
                     _result("c1", "run_command", "(no output)",
                             files={"created": ["apples.png"], "changed": [], "removed": []})]
        self.assertNotIn("apples.png", self.foreign(made_here))

    def test_a_folder_is_another_chats_only_when_all_its_files_are(self):
        scope = ChatScope(self.root, {rel: "grade1" for rel in self.grade1.created})
        self.assertEqual(scope.owner(self.folder / "space-adventure.json"), "grade1")
        self.assertIsNone(scope.owner(self.folder / "harvest-math.json"))
        self.assertIsNone(scope.owner(self.folder))  # harvest-math.json belongs to no chat
        # Moving or deleting the folder would take the other chat's files along.
        self.assertEqual(scope.inside(self.folder), "grade1")
        self.assertEqual(scope.inside(self.root / "homework"), "grade1")
        (self.folder / "harvest-math.json").unlink()
        self.assertEqual(scope.owner(self.folder), "grade1")
        scope.claim(["homework/first-grade/space-adventure.json"])
        self.assertIsNone(scope.owner(self.folder / "space-adventure.json"))
        (self.root / "apples.png").unlink()
        self.assertIsNone(scope.owner(self.root / "apples.png"))  # a new file may take the name


class CallFilesTest(unittest.TestCase):
    def test_files_come_from_the_saved_meta_or_an_older_results_text(self):
        root = "/ws"
        output = ("Output:\nok\nFiles created or changed in the workspace: out/a b.png (1.2 KB), "
                  "notes.txt (10 bytes).\nFiles deleted: old.txt, x.txt and 3 more.")
        self.assertEqual(call_files("run_command", {}, output, True, {"ok": True}, root),
                         {"created": ["out/a b.png", "notes.txt"], "changed": [], "read": [],
                          "removed": ["old.txt", "x.txt"], "moved": []})
        found = call_files("render_worksheet", {"spec_path": "/ws/quiz.json", "path": "q.pdf"},
                           "Created q.pdf.", True,
                           {"files": {"created": ["q.pdf", "q.json"], "changed": ["k.pdf"]}}, root)
        self.assertEqual((found["created"], found["changed"], found["read"]),
                         (["q.pdf", "q.json"], ["k.pdf"], ["quiz.json"]))
        self.assertEqual(call_files("move_path", {"source": "a.txt", "destination": "b"},
                                    "Moved a.txt to b/a.txt.", True, None, root)["moved"],
                         [("a.txt", "b/a.txt")])
        for path, ok in (("/etc/passwd", True), ("../up.txt", True), ("notes.txt", False)):
            with self.subTest(path=path):
                self.assertEqual(call_files("read_file", {"path": path}, "text", ok, None,
                                            root)["read"], [])


class ChatFilesTest(unittest.TestCase):
    def test_other_chats_are_read_once_and_again_after_they_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            chats = Path(tmp)

            def save(chat_id, title, made):
                messages = [_user("go"), _call("c1", "write_file", path=made, content="x"),
                            _result("c1", "write_file", f"Created {made} (1 bytes).")]
                (chats / f"{chat_id}.json").write_text(json.dumps(
                    {"id": chat_id, "title": title, "messages": messages}))

            save("me", "this chat", "mine.txt")
            save("other", "grade1", "apples.png")
            (chats / "broken.json").write_text("{")
            files = ChatFiles(chats)
            (title, act), = files.others("me", "/ws")
            self.assertEqual((title, list(act.created)), ("grade1", ["apples.png"]))
            self.assertIs(files.others("me", "/ws")[0][1], act)  # unchanged, so not read again
            save("other", "grade1", "carrots.png")
            self.assertEqual(list(files.others("me", "/ws")[0][1].created), ["carrots.png"])


if __name__ == "__main__":
    unittest.main()
