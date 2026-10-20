import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from qwen_chat import skills
from qwen_chat.skills import SkillError, SkillRegistry, parse_skill, slash_query
from qwen_chat.tools import ToolBox, Workspace


def skill_text(name="ocean-quiz", body="Make a themed quiz. Check the finished pages."):
    return (f"---\nname: {name}\ndescription: Make ocean reading quizzes for learners.\n"
            f"---\n\n{body}\n").encode()


class SkillLibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = SkillRegistry(self.root / "library")
        self.ws = Workspace(self.root / "workspace", trash_dir=self.root / "trash")
        self.approve = mock.Mock(return_value=True)
        self.box = ToolBox(self.ws, web=False, files=True, commands=False,
                           skills=self.registry, approve=self.approve, auto_approve=lambda: True)

    def draft(self, name="ocean-quiz", body="Make a themed quiz.", resource=False):
        folder = self.ws.root / "skills" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_bytes(skill_text(name, body))
        if resource:
            (folder / "scripts").mkdir(exist_ok=True)
            (folder / "scripts" / "example.py").write_text("print('example')\n")
        return folder

    def run_tool(self, tool_name, **args):
        return self.box.run(tool_name, json.dumps(args))

    def save(self, folder):
        ok, text = self.run_tool("save_skill", path=str(folder))
        self.assertTrue(ok, text)
        return text

    def test_yaml_frontmatter_and_invalid_packages(self):
        raw = (b"---\nname: ocean-quiz\ndescription: >-\n  Ocean questions:\n  use for reading.\n"
               b"compatibility: Files on\n---\nDo the task.\n")
        parsed = parse_skill(raw, Path("ocean-quiz"))
        self.assertEqual(parsed.description, "Ocean questions: use for reading.")
        self.assertEqual(parsed.instructions, "Do the task.")
        invalid = [
            b"Missing frontmatter",
            b"---\nname: ocean-quiz\n",
            skill_text("other-name"),
            skill_text().replace(b"name: ocean-quiz", b"name: Bad--Name"),
            skill_text().replace(b"Make ocean reading quizzes for learners.", b""),
            skill_text().replace(b"Make ocean reading quizzes for learners.", b"x" * 1025),
            skill_text().replace(b"description:", b"compatibility: [bad]\ndescription:"),
            skill_text(body=""),
            skill_text(body="x" * skills.MAX_SKILL_BYTES),
            b"---\n!!python/object:builtins.str {}\n---\nDo the task.",
        ]
        for data in invalid:
            with self.subTest(data=data[:70]), self.assertRaises(SkillError):
                parse_skill(data, Path("ocean-quiz"))

    def test_read_only_discovery_without_file_or_command_permission(self):
        self.box.files = False
        self.assertTrue({"list_skills", "load_skill"} <= self.box.names())
        self.assertFalse({"save_skill", "run_command", "render_worksheet"} & self.box.names())
        ok, text = self.run_tool("list_skills", query="WORKSHEET")
        self.assertTrue(ok, text)
        self.assertIn("/worksheet-pdf", text)
        ok, text = self.run_tool("load_skill", name="worksheet-pdf")
        self.assertTrue(ok, text)
        self.assertIn("render_worksheet", text)
        self.approve.assert_not_called()
        self.assertEqual(list(self.ws.root.iterdir()), [])

    def test_save_is_visible_to_existing_and_new_readers(self):
        existing = ToolBox(Workspace(self.root / "second-workspace"),
                           skills=SkillRegistry(self.registry.user_root))
        self.assertNotIn("ocean-quiz", existing.list_skills())
        self.save(self.draft())
        self.assertIn("/ocean-quiz", existing.list_skills())
        self.assertIn("Make a themed quiz.", existing.load_skill("ocean-quiz"))
        fresh = SkillRegistry(self.registry.user_root)
        self.assertEqual(fresh.get("ocean-quiz").instructions, "Make a themed quiz.")
        self.assertTrue(self.registry.get("worksheet-pdf").builtin)
        self.assertFalse(fresh.get("ocean-quiz").builtin)
        (self.registry.user_root / "broken").mkdir()
        found, errors = self.registry.catalog()
        self.assertIn("ocean-quiz", {s.name for s in found})
        self.assertTrue(any("broken" in error for error in errors))
        self.assertIn("need attention", existing.list_skills())

    def test_save_always_needs_explicit_approval_and_respects_refusal(self):
        draft = self.draft(resource=True)
        self.approve.return_value = False
        ok, text = self.run_tool("save_skill", path=str(draft))
        self.assertFalse(ok, text)
        self.assertIn("said no", text)
        title, detail, preview, action, reason = self.approve.call_args.args
        self.assertEqual(action, "Save Skill")
        self.assertIn("every new and existing chat", detail)
        self.assertIn("name: ocean-quiz", preview)
        self.assertIn("scripts/example.py", preview)
        self.assertTrue(reason)
        self.assertFalse(self.registry.user_root.exists())
        self.approve.return_value = True
        ok, text = self.run_tool("save_skill", path=str(draft))
        self.assertFalse(ok)
        self.assertIn("already said no", text)
        self.assertEqual(self.approve.call_count, 1)
        unattended = ToolBox(self.ws, skills=self.registry, auto_approve=lambda: True)
        ok, text = unattended.run("save_skill", json.dumps({"path": str(draft)}))
        self.assertFalse(ok)
        self.assertFalse(self.registry.user_root.exists())

    def test_stopping_during_approval_saves_nothing(self):
        self.box.cancel = threading.Event()
        self.approve.side_effect = lambda *_: self.box.cancel.set() or True
        ok, text = self.run_tool("save_skill", path=str(self.draft()))
        self.assertFalse(ok)
        self.assertIn("Stopped", text)
        self.assertFalse(self.registry.user_root.exists())

    def test_approval_saves_the_reviewed_snapshot_not_later_source_edits(self):
        draft = self.draft(resource=True)
        reviewed = self.registry.package(draft).files

        def edit_source(*_):
            (draft / "SKILL.md").write_bytes(skill_text(body="Changed after review."))
            (draft / "scripts" / "example.py").write_text("print('changed')\n")
            return True

        self.approve.side_effect = edit_source
        self.save(draft)
        self.assertEqual(self.registry.package(self.registry.user_root / draft.name).files, reviewed)

    def test_update_keeps_a_recoverable_previous_version(self):
        draft = self.draft()
        self.save(draft)
        backups = []
        self.ws.on_trash = lambda original, backup: backups.append((original, backup))
        (draft / "SKILL.md").write_bytes(skill_text(body="Updated reusable method."))
        text = self.save(draft)
        self.assertIn("previous version is in the Trash", text)
        self.assertEqual(self.approve.call_args.args[3], "Update Skill")
        self.assertEqual(self.registry.get("ocean-quiz").instructions, "Updated reusable method.")
        old = backups[0]
        self.ws.restore(*old)
        self.assertEqual(self.registry.get("ocean-quiz").instructions, "Make a themed quiz.")
        self.assertFalse(old[1].exists())

    def test_concurrent_library_edit_is_not_overwritten(self):
        draft = self.draft()
        self.save(draft)
        saved = self.registry.user_root / draft.name / "SKILL.md"
        (draft / "SKILL.md").write_bytes(skill_text(body="Proposed update."))

        def edit_library(*_):
            saved.write_bytes(skill_text(body="Someone else's newer version."))
            return True

        self.approve.side_effect = edit_library
        ok, text = self.run_tool("save_skill", path=str(draft))
        self.assertFalse(ok)
        self.assertIn("changed while you were reviewing", text)
        self.assertEqual(self.registry.get(draft.name).instructions, "Someone else's newer version.")
        self.assertFalse(self.ws.trash_dir.exists())
        self.assertEqual(list(self.registry.user_root.glob(".saving-*")), [])

    def test_failed_promotion_restores_previous_skill_and_reports_failure(self):
        draft = self.draft()
        self.save(draft)
        (draft / "SKILL.md").write_bytes(skill_text(body="Proposed update."))
        with mock.patch.object(skills.os, "replace", side_effect=OSError("simulated disk error")):
            ok, text = self.run_tool("save_skill", path=str(draft))
        self.assertFalse(ok)
        self.assertIn("simulated disk error", text)
        self.assertEqual(self.registry.get(draft.name).instructions, "Make a themed quiz.")
        self.assertTrue((self.ws.trash_dir / "files" / draft.name / "SKILL.md").is_file())

    def test_cannot_replace_builtins_escape_workspace_or_package_links(self):
        for path in (str(self.draft(name="worksheet-pdf")), "../outside"):
            ok, text = self.run_tool("save_skill", path=path)
            self.assertFalse(ok, text)
        draft = self.draft()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "data.txt").write_text("do not copy")
        (draft / "linked").symlink_to(outside, target_is_directory=True)
        ok, text = self.run_tool("save_skill", path=str(draft))
        self.assertFalse(ok)
        self.assertIn("symlinks", text)
        self.approve.assert_not_called()
        self.assertFalse(self.registry.user_root.exists())

    def test_package_limits_and_hidden_files_fail_before_approval(self):
        draft = self.draft()
        (draft / "first.txt").write_text("one")
        (draft / "second.txt").write_text("two")
        with mock.patch.object(skills, "MAX_PACKAGE_FILES", 2):
            self.assertFalse(self.run_tool("save_skill", path=str(draft))[0])
        with mock.patch.object(skills, "MAX_PACKAGE_BYTES", 32):
            self.assertFalse(self.run_tool("save_skill", path=str(draft))[0])
        (draft / ".secret").write_text("not reusable")
        ok, text = self.run_tool("save_skill", path=str(draft))
        self.assertFalse(ok)
        self.assertIn("hidden files", text)
        self.approve.assert_not_called()

    def test_supporting_files_are_safe_copies_not_automatically_executed(self):
        draft = self.draft(resource=True)
        package = self.registry.package(draft)
        self.save(draft)
        self.box.files = False
        ok, text = self.run_tool("load_skill", name=draft.name)
        self.assertTrue(ok, text)
        self.assertIn("Enable Files", text)
        self.assertFalse((self.ws.root / ".qwen-skills").exists())
        self.box.files = True
        ok, text = self.run_tool("load_skill", name=draft.name)
        self.assertTrue(ok, text)
        target = self.ws.root / ".qwen-skills" / package.digest[:12] / draft.name
        self.assertIn(self.ws.display(target), text)
        self.assertEqual(self.registry.package(target).files, package.files)
        self.assertFalse(os.access(target / "scripts" / "example.py", os.X_OK))
        self.assertIn("No script has been executed", text)
        (target / "scripts" / "example.py").write_text("user's local edit\n")
        ok, text = self.run_tool("load_skill", name=draft.name)
        self.assertTrue(ok, text)
        self.assertIn("edits are preserved", text)
        self.assertEqual((target / "scripts" / "example.py").read_text(), "user's local edit\n")
        copied = self.ws.root / ".qwen-skills" / (package.digest[:12] + "-1") / draft.name
        self.assertEqual(self.registry.package(copied).files, package.files)
        ok, text = self.run_tool("load_skill", name=draft.name)
        self.assertTrue(ok, text)
        self.assertIn(self.ws.display(copied), text)
        self.assertEqual(len(list((self.ws.root / ".qwen-skills").iterdir())), 2)

    def test_resource_cache_cannot_escape_workspace(self):
        self.save(self.draft(resource=True))
        outside = self.root / "outside"
        outside.mkdir()
        (self.ws.root / ".qwen-skills").symlink_to(outside, target_is_directory=True)
        ok, text = self.run_tool("load_skill", name="ocean-quiz")
        self.assertFalse(ok)
        self.assertIn("outside the workspace", text)
        self.assertEqual(list(outside.iterdir()), [])


class SlashCompletionTest(unittest.TestCase):
    def test_completion_replaces_only_the_current_token(self):
        text = "Make /work a quiz."
        self.assertEqual(slash_query(text, 10), (5, 10, "work"))
        self.assertEqual(slash_query(text, 8), (5, 10, "wo"))
        self.assertEqual(slash_query("/WORK", 5), (0, 5, "work"))
        self.assertEqual(slash_query("/worksheet-pdf /", 16), (15, 16, ""))

    def test_urls_paths_and_code_do_not_trigger_completion(self):
        for text in ("https://example.com/work", "/tmp/work", "notes/work",
                     "Use /tmp/file.txt", "```sh\n/work", "Use `/work",
                     "/work.py", "/work:", "Use /" + "a" * 65):
            with self.subTest(text=text):
                self.assertIsNone(slash_query(text, len(text)))
        self.assertIsNone(slash_query("/tmp/file", 4))


if __name__ == "__main__":
    unittest.main()
