import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qwen_chat import tools
from qwen_chat.tools import ToolBox, ToolError, Workspace


class WorkspaceSandboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("top secret")
        self.ws = Workspace(base / "ws", trash_dir=base / "trash")
        self.box = ToolBox(self.ws, web=False, files=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, name, **args):
        return self.box.run(name, json.dumps(args))

    def test_rejects_escapes(self):
        secret = str(self.outside / "secret.txt")
        os.symlink(self.outside, self.ws.root / "link")
        for path in ("../outside/secret.txt", secret, "link/secret.txt", "~/.bashrc", "/etc/passwd"):
            ok, out = self.run_tool("read_file", path=path)
            self.assertFalse(ok, path)
            self.assertIn("outside the workspace", out)
        ok, _ = self.run_tool("write_file", path="link/new.txt", content="x")
        self.assertFalse(ok)
        self.assertFalse((self.outside / "new.txt").exists())
        ok, _ = self.run_tool("move_path", source="link/secret.txt", destination="stolen.txt")
        self.assertFalse(ok)
        self.assertTrue((self.outside / "secret.txt").exists())

    def test_cannot_delete_root_or_through_symlink(self):
        os.symlink(self.outside, self.ws.root / "link")
        self.assertFalse(self.run_tool("delete_path", path=".")[0])
        self.assertFalse(self.run_tool("delete_path", path="link/secret.txt")[0])
        self.assertTrue((self.outside / "secret.txt").exists())

    def test_write_read_list_move(self):
        ok, out = self.run_tool("write_file", path="notes/plan.md", content="line1\nline2\n")
        self.assertTrue(ok, out)
        self.assertIn("Created notes/plan.md", out)
        self.assertTrue(self.run_tool("write_file", path="notes/plan.md", content="line3", append=True)[0])
        ok, out = self.run_tool("read_file", path="notes/plan.md")
        self.assertIn("line1\nline2\nline3", out)
        ok, out = self.run_tool("list_files", recursive=True)
        self.assertIn("notes/", out)
        self.assertIn("notes/plan.md", out)
        self.assertTrue(self.run_tool("move_path", source="notes/plan.md", destination="plan.md")[0])
        self.assertTrue((self.ws.root / "plan.md").is_file())

    def test_overwrite_and_delete_need_confirmation(self):
        asked = []
        self.ws.confirm = lambda title, detail: asked.append(title) or False
        self.run_tool("write_file", path="a.txt", content="first")
        ok, out = self.run_tool("write_file", path="a.txt", content="second")
        self.assertFalse(ok)
        self.assertIn("declined", out)
        self.assertEqual((self.ws.root / "a.txt").read_text(), "first")
        ok, _ = self.run_tool("delete_path", path="a.txt")
        self.assertFalse(ok)
        self.assertTrue((self.ws.root / "a.txt").exists())
        self.assertEqual(len(asked), 2)

    def test_replaced_and_deleted_files_go_to_the_trash(self):
        trashed = []
        self.ws.on_trash = lambda original, t: trashed.append((original, t))
        (self.ws.root / "mine.txt").write_text("the user's")
        ok, out = self.run_tool("write_file", path="mine.txt", content="Qwen's")
        self.assertTrue(ok, out)
        self.assertIn("old version is in the Trash", out)
        original, t = trashed[0]
        self.assertEqual((original, t.read_text()), (self.ws.root / "mine.txt", "the user's"))
        info = (self.ws.trash_dir / "info" / (t.name + ".trashinfo")).read_text()
        self.assertIn("Path=" + str(self.ws.root / "mine.txt"), info)
        # Qwen's own unchanged file is replaced without a Trash copy.
        self.run_tool("write_file", path="mine.txt", content="Qwen's again")
        self.assertEqual(len(trashed), 1)
        ok, out = self.run_tool("delete_path", path="mine.txt")
        self.assertTrue(ok, out)
        self.assertIn("Trash", out)
        self.assertFalse((self.ws.root / "mine.txt").exists())
        # A second trashed file of the same name doesn't overwrite the first.
        self.assertEqual(len({t.name for _, t in trashed}), 2)
        self.ws.restore(*trashed[0])
        self.assertEqual((self.ws.root / "mine.txt").read_text(), "the user's")
        self.assertFalse(trashed[0][1].exists())

    def test_read_paging_and_binary(self):
        (self.ws.root / "big.txt").write_text("\n".join("x" * 100 for _ in range(500)))
        ok, out = self.run_tool("read_file", path="big.txt")
        self.assertIn("start_line=", out)
        (self.ws.root / "bin.dat").write_bytes(b"\x00\x01\x02")
        ok, out = self.run_tool("read_file", path="bin.dat")
        self.assertFalse(ok)
        self.assertIn("binary", out)

    def test_bad_arguments_and_disabled_tools(self):
        self.assertFalse(self.box.run("read_file", "{not json")[0])
        ok, out = self.box.run("read_file", "{}")
        self.assertFalse(ok)
        self.assertIn("Missing", out)
        ok, out = self.box.run("web_search", '{"query": "x"}')
        self.assertFalse(ok)
        self.assertIn("disabled", out)

    def test_edit_file_changes_exactly_one_place_and_keeps_json_valid(self):
        f = self.ws.root / "sheet.json"
        f.write_text('{\n  "title": "Farm day",\n  "theme": {"accent": "#D4A574"},\n  "questions": []\n}\n')
        ok, out = self.run_tool("edit_file", path="sheet.json", old_text='"#D4A574"', new_text='"#2F6B2F"')
        self.assertTrue(ok, out)
        self.assertIn("Edited sheet.json at line 3 (9 characters replaced with 9). The old version "
                      "is in the Trash.", out)
        # Different spacing still finds the one place, and the file keeps its own indentation.
        ok, out = self.run_tool("edit_file", path="sheet.json", old_text='  "title":   "Farm day",\n',
                                new_text='  "title": "Harvest",\n')
        self.assertTrue(ok, out)
        self.assertNotIn("Trash", out)  # now Qwen's own file: no second backup
        good = '{\n  "title": "Harvest",\n  "theme": {"accent": "#2F6B2F"},\n  "questions": []\n}\n'
        self.assertEqual(f.read_text(), good)
        self.assertEqual(len(list((self.ws.trash_dir / "files").iterdir())), 1)
        for old, new, error in (
                ("Farm day", "x", "old_text was not found"),
                ('"t', '"T', "matches 2 places"),
                ('"questions": []', '"questions": [', "Not changed: this edit would break the JSON"),
                ('"Harvest"', '"[... 1,412 more characters removed to save context space]"',
                 "Nothing was saved"),
                ("  ", "x", "old_text is empty")):
            with self.subTest(old=old):
                ok, out = self.run_tool("edit_file", path="sheet.json", old_text=old, new_text=new)
                self.assertFalse(ok)
                self.assertIn(error, out)
        self.assertEqual(f.read_text(), good)
        ok, out = self.run_tool("edit_file", path="sheet.json", old_text="Harvest", new_text="Harvest")
        self.assertIn("Unchanged", out)

    def test_json_drafts_say_where_they_break_until_fixed(self):
        ok, out = self.run_tool("write_file", path="broken.json", content='{"a": 1\n "b": 2}')
        self.assertTrue(ok, out)
        self.assertIn("Note: broken.json is not valid JSON yet (Expecting ',' delimiter at line 2, "
                      "column 2", out)
        ok, out = self.run_tool("edit_file", path="broken.json", old_text='"a": 1', new_text='"a": 1,')
        self.assertTrue(ok, out)
        self.assertIn("broken.json is now valid JSON.", out)
        ok, out = self.run_tool("write_file", path="parts.json", content='{"questions": [\n')
        self.assertIn("If more parts follow, append them", out)
        ok, out = self.run_tool("write_file", path="parts.json", content='{"prompt": "Q?"}]}', append=True)
        self.assertTrue(ok, out)
        self.assertNotIn("Note:", out)
        ok, out = self.run_tool("write_file", path="notes.txt", content='{"not": json')
        self.assertNotIn("Note:", out)  # only .json files are checked

    def test_notes_from_shortened_history_are_never_saved_as_content(self):
        f = self.ws.root / "sheet.json"
        f.write_text('{"title": "Farm day"}')
        for note in ("[... 1,412 more characters removed to save context space]",
                     "[Earlier tool output removed to save context space. Run the tool again if you need it.]",
                     "[... 200 more characters removed to fit the context window]"):
            with self.subTest(note=note):
                ok, out = self.run_tool("write_file", path="sheet.json", content='{"title": "x", ' + note + "}")
                self.assertFalse(ok)
                self.assertIn("Nothing was saved", out)
                self.assertIn("sheet.json keeps its previous content", out)
                ok, out = self.run_tool("write_file", path="new.json", content=note)
                self.assertFalse(ok)
                self.assertIn("Write the complete content of new.json", out)
        self.assertEqual(f.read_text(), '{"title": "Farm day"}')
        self.assertFalse((self.ws.root / "new.json").exists())
        self.assertFalse(self.ws.trash_dir.exists())
        ok, out = self.run_tool("write_file", path="ok.txt", content="Use [brackets] to save space.")
        self.assertTrue(ok, out)


class WebGuardTest(unittest.TestCase):
    def test_blocks_local_and_non_http(self):
        for url in ("http://127.0.0.1:8080/health", "http://localhost/", "http://192.168.1.1/",
                    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "file:///etc/passwd", "ftp://example.com/"):
            with self.assertRaises(ToolError, msg=url):
                tools.check_public_url(url)

    def test_redirect_to_local_is_blocked(self):
        handler = tools._SafeRedirect()
        with self.assertRaises(ToolError):
            handler.redirect_request(mock.Mock(), None, 302, "Found", {}, "http://127.0.0.1:8080/props")


class HtmlExtractTest(unittest.TestCase):
    def test_prefers_article_and_drops_chrome(self):
        body = "<p>" + "Real content sentence. " * 40 + "</p>"
        page = (f"<html><head><title>T &amp; Co</title><style>x{{}}</style></head><body>"
                f"<nav>Menu Home About</nav><article><h1>Heading</h1>{body}</article>"
                f"<footer>Copyright</footer><script>alert(1)</script></body></html>")
        title, text = tools.html_to_text(page)
        self.assertEqual(title, "T & Co")
        self.assertIn("# Heading", text)
        self.assertNotIn("Menu", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("Copyright", text)

    def test_ddg_lite_parse(self):
        page = ('<a rel="nofollow" href="https://example.com/a" class=\'result-link\'>Example A</a>'
                '<td class=\'result-snippet\'>Snippet <b>A</b></td>'
                '<a href="https://duckduckgo.com/y.js?ad=1" class=\'result-link\'>Ad</a>')
        p = tools._DDGLiteParser()
        p.feed(page)
        self.assertEqual(p.results[0]["url"], "https://example.com/a")
        self.assertEqual(p.results[0]["snippet"], "Snippet A")
        self.assertEqual(tools._unwrap_ddg("//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.org%2F"), "https://x.org/")


class ProgramToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name) / "ws", trash_dir=Path(self.tmp.name) / "trash")
        self.asked = []
        self.reasons = []
        self.allow = True
        self.box = ToolBox(self.ws, web=False, files=True, commands=True, approve=self._approve)

    def tearDown(self):
        self.tmp.cleanup()

    def _approve(self, title, detail, code, action, reason=""):
        self.asked.append((title, detail, code, action))
        self.reasons.append(reason)
        return self.allow

    def run_tool(self, tool_name, **args):
        return self.box.run(tool_name, json.dumps(args))

    def test_offered_only_with_files_and_commands_on(self):
        names = lambda box: {t["function"]["name"] for t in box.schemas()}
        for name in ("run_command", "render_worksheet", "render_answer_key", "open_file"):
            self.assertIn(name, names(self.box))
            self.assertNotIn(name, names(ToolBox(self.ws, web=True, files=True)))
            self.assertNotIn(name, names(ToolBox(self.ws, files=False, commands=True)))
        self.assertNotIn("install_packages", names(ToolBox(self.ws, files=False, commands=True)))

    def test_command_runs_only_when_approved(self):
        self.allow = False
        cmd = "touch should-not-exist"
        ok, out = self.run_tool("run_command", command=cmd, why="test")
        self.assertFalse(ok)
        self.assertIn("said no", out)
        self.assertEqual(self.asked[0][2], cmd)
        self.assertFalse((self.ws.root / "should-not-exist").exists())
        # Approval is required even without a GUI callback.
        ok, _ = ToolBox(self.ws, files=True, commands=True).run(
            "run_command", json.dumps({"command": cmd, "why": "x"}))
        self.assertFalse(ok)
        self.assertFalse((self.ws.root / "should-not-exist").exists())

    def test_nothing_more_is_asked_after_a_no_in_the_same_turn(self):
        self.allow = False
        self.run_tool("run_command", command="touch a", why="test")
        self.allow = True
        ok, out = self.run_tool("run_command", command="touch b", why="workaround")
        self.assertFalse(ok)
        self.assertIn("already said no", out)
        self.assertEqual(len(self.asked), 1)
        self.assertFalse((self.ws.root / "b").exists())
        self.box.declined = False  # the agent resets this for each new turn
        ok, _ = self.run_tool("run_command", command="touch b", why="next turn")
        self.assertTrue(ok)

    def test_missing_program_is_reported_without_asking(self):
        ok, out = self.run_tool("run_command", command="cd . && qc-no-such-prog-1 x", why="test")
        self.assertFalse(ok)
        self.assertIn("Not installed: qc-no-such-prog-1", out)
        self.assertIn("install_packages", out)
        self.assertEqual(self.asked, [])
        # Builtins, shell functions and redirections are not mistaken for programs.
        ok, _ = self.run_tool("run_command", why="test",
                              command="f() { echo hi; }; f &> log.txt; [ -f log.txt ] && cd .")
        self.assertTrue(ok)
        # Neither are arithmetic, arrays or [[ ]] patterns.
        for cmd in ("n=$((n+1)); echo $n", "for ((i=0;i<2;i++)); do :; done",
                    "arr=(one two)", "[[ x =~ ^(foo|bar)$ ]] || true",
                    "case x in a|b) qc-no-such-prog-2;; esac"):
            self.assertEqual(tools._missing_programs(cmd), [], cmd)

    def test_runs_in_workspace_and_reports_new_files(self):
        cmd = "pwd; printf hello > out.txt"
        ok, out = self.run_tool("run_command", command=cmd, why="test")
        self.assertTrue(ok, out)
        self.assertIn(str(self.ws.root), out)
        self.assertIn("out.txt", out)
        self.assertEqual(self.box.result_paths("run_command", {"command": cmd}, out),
                         [self.ws.root / "out.txt"])
        result = ("Files created or changed in the workspace: a b.pdf (1.2 MB), .cache/x (3 bytes), "
                  "__pycache__/m.pyc (2 KB), gone.txt (1 KB).")
        (self.ws.root / "a b.pdf").write_text("x")
        self.assertEqual(self.box.result_paths("run_command", {}, result), [self.ws.root / "a b.pdf"])

    def test_routine_commands_run_without_asking_risky_ones_ask(self):
        self.box.auto_approve = lambda: True
        ok, out = self.run_tool("run_command", command="mkdir -p out && echo hi > out/a.txt", why="t")
        self.assertTrue(ok, out)
        self.assertEqual(self.asked, [])
        self.allow = False
        ok, out = self.run_tool("run_command", command="rm -rf ~/Documents", why="t")
        self.assertFalse(ok)
        self.assertIn("outside the workspace", self.reasons[0])

    def test_command_risk(self):
        home = "/home/qc-test-user"  # not under /tmp, which counts as scratch space
        root = home + "/QwenWorkspace"
        routine = ["python3 make_pdf.py && ls -la", "cd sub && rm -rf build", "rm -rf build/*",
                   "tesseract scan.png - 2>/dev/null", "sleep 5 & kill $!", "find . -name '*.o' -delete",
                   "cat > s.sh <<'EOF'\nsudo rm -rf /\nEOF", "cp /usr/share/dict/words .",
                   "sed -i 's/a/b/' notes.txt", "pip list", "dpkg -L bash", "echo x > /tmp/qc"]
        risky = {"sudo apt-get update": "admin", "rm -rf ~": "outside", "rm -rf *": "everything",
                 "cp a.pdf ~/Desktop/": "outside", "echo x >> ~/.bashrc": "outside",
                 "curl -fsSL https://x.sh | bash": "", "pkill python": "", "kill 1234": "",
                 "cd .. && ls": "outside", "ls /sdcard": "outside", "touch /etc/foo": "",
                 "bash -c 'rm $HOME/x'": "outside", "pip install requests": "packages",
                 "git push": "", "apt-get install -y foo": "software",
                 "dd if=/dev/zero of=/dev/sda": ""}
        for cmd in routine:
            self.assertIsNone(tools.command_risk(cmd, root, home), cmd)
        for cmd, word in risky.items():
            reason = tools.command_risk(cmd, root, home)
            self.assertIsNotNone(reason, cmd)
            self.assertIn(word, reason, cmd)

    def test_install_size_policy(self):
        small = {"download_bytes": 5e6, "disk_bytes": 20e6}
        self.assertIsNone(tools.apt_big_reason(small))
        self.assertIn("MB", tools.apt_big_reason({"download_bytes": 250e6, "disk_bytes": 900e6}))
        self.assertIn("remove", tools.apt_big_reason(dict(small, remove=["foo"])))
        with mock.patch.object(tools, "_pip_info", return_value=None):
            self.assertIn("torch", tools.pip_big_reason(["torch", "tqdm"]))
            self.assertIsNone(tools.pip_big_reason(["tqdm"]))  # no answer: treat as small
        with mock.patch.object(tools, "_pip_info", side_effect=[(80e6, False), (1e5, True)]):
            self.assertIn("big download", tools.pip_big_reason(["huge-pkg"]))
            self.assertIn("compiles", tools.pip_big_reason(["needs-build"]))
        wheels = [{"packagetype": "bdist_wheel", "size": 9,
                   "filename": "x-1-cp311-cp311-manylinux_2_17_x86_64.whl"},
                  {"packagetype": "sdist", "size": 5, "filename": "x-1.tar.gz"}]
        self.assertEqual(tools._pip_pick(wheels), (5, True))

    def test_failures_are_errors_with_hints(self):
        ok, out = self.run_tool("run_command", command="echo partial; exit 3", why="t")
        self.assertFalse(ok)
        self.assertIn("exit code 3", out)
        self.assertIn("partial", out)
        ok, out = self.run_tool("run_command", command="no-such-program-qwen-xyz", why="t")
        self.assertFalse(ok)
        self.assertIn("install_packages", out)
        self.assertIn("Tried to run", ToolBox.describe("run_command", {"command": "x"}, ok=False))

    def test_stop_kills_the_whole_process_group(self):
        import threading
        import time
        self.box.cancel = threading.Event()
        threading.Timer(0.6, self.box.cancel.set).start()
        start = time.monotonic()
        ok, out = self.run_tool("run_command", command="sleep 30 & sleep 30", why="t")
        self.assertLess(time.monotonic() - start, 8)
        self.assertFalse(ok)
        self.assertIn("Stopped by the user", out)

    def test_timeout_stops_the_command(self):
        import time
        start = time.monotonic()
        rc, out, reason = tools.run_streaming(["bash", "-c", "echo started; sleep 30"],
                                              self.ws.root, tools.child_env(), 1)
        self.assertEqual(reason, "timeout")
        self.assertIn("started", out)
        self.assertLess(time.monotonic() - start, 8)

    def test_long_output_keeps_start_and_end(self):
        ok, out = self.run_tool("run_command", command="echo FIRST; seq 1 50000; echo LAST", why="t")
        self.assertTrue(ok)
        self.assertIn("FIRST", out)
        self.assertIn("LAST", out)
        self.assertIn("skipped", out)
        self.assertLess(len(out), 10000)

    def test_clean_output_keeps_final_progress_state(self):
        self.assertEqual(tools.clean_output("\x1b[32m10%\r50%\r100%\x1b[0m\nDone\r\n"), "100%\nDone")

    def test_bad_package_names_are_refused_before_asking(self):
        for name, mgr in (("foo; rm -rf ~", "apt"), ("Tesseract", "apt"), ("x && y", "pip"),
                          ("--index-url=http://evil", "pip")):
            ok, out = self.run_tool("install_packages", packages=[name], manager=mgr, why="t")
            self.assertFalse(ok, name)
        self.assertEqual(self.asked, [])

    def test_apt_install_shows_plan_and_respects_no(self):
        self.allow = False
        with mock.patch.object(tools, "apt_installed", return_value=set()), \
             mock.patch.object(tools, "apt_candidates", return_value={"foo"}), \
             mock.patch.object(tools, "apt_plan", return_value={"count": 3, "download": "1.2 MB",
                                                                "disk": "4 MB"}), \
             mock.patch.object(tools, "run_streaming") as run:
            ok, out = self.run_tool("install_packages", packages=["foo"], why="OCR the scans")
        self.assertFalse(ok)
        self.assertIn("said no", out)
        run.assert_not_called()
        title, detail, code, action = self.asked[0]
        self.assertEqual((code, action), ("apt install foo", "Install"))
        self.assertIn("OCR the scans", detail)
        self.assertIn("3 packages including 2 it depends on, 1.2 MB download, 4 MB of space", detail)

    def test_apt_plan_cannot_install(self):
        # apt skips its yes/no prompt (and installs) when nothing extra is needed, so the
        # plan must use a mode that never installs, and must not run as root.
        with mock.patch.object(tools, "_query") as q:
            q.return_value = subprocess.CompletedProcess([], 0, "0 upgraded, 1 newly installed, "
                                                         "0 to remove\nNeed to get 52.1 kB of "
                                                         "archives.\n", "")
            plan = tools.apt_plan(["bc"])
        argv = q.call_args.args[0]
        self.assertIn("--print-uris", argv)
        self.assertNotIn("proot", argv)
        self.assertNotIn("sudo", argv)
        self.assertEqual((plan["count"], plan["download"]), (1, "52.1 KB"))

    def test_no_apt_work_after_a_no(self):
        self.box.declined = True
        with mock.patch.object(tools, "apt_plan") as plan, \
             mock.patch.object(tools, "run_streaming") as run:
            ok, out = self.run_tool("install_packages", packages=["no-such-pkg-qc"], why="t")
        self.assertFalse(ok)
        self.assertIn("already said no", out)
        plan.assert_not_called()
        run.assert_not_called()

    def test_pip_refuses_file_names(self):
        ok, out = self.run_tool("install_packages", packages=["evil-1.0.tar.gz"], manager="pip",
                                why="t")
        self.assertFalse(ok)
        self.assertIn("not file names", out)
        self.assertEqual(self.asked, [])

    def test_already_installed_needs_no_approval(self):
        ok, out = self.run_tool("install_packages", packages=["bash"], why="t")
        self.assertTrue(ok, out)
        self.assertIn("Already installed", out)
        self.assertEqual(self.asked, [])

    def test_check_programs(self):
        with mock.patch.object(tools, "apt_candidates", return_value={"tesseract-ocr"}):
            ok, out = self.run_tool("check_programs", programs=["bash", "tesseract"],
                                    python_modules=["json", "no_such_module_qwen"])
        self.assertTrue(ok, out)
        self.assertRegex(out, r"bash: installed")
        if "tesseract: not installed" in out:
            self.assertIn("Debian package: tesseract-ocr", out)
        self.assertIn("python module json: installed", out)
        self.assertIn("python module no_such_module_qwen: not installed", out)

    def _worksheet_spec(self):
        (self.ws.root / "quiz.json").write_text(json.dumps({
            "title": "A quiz", "questions": [{"prompt": "What did you find?", "lines": 4}],
        }))

    @staticmethod
    def _render_ok(argv, *_args):
        Path(argv[-1]).write_bytes(b"%PDF-1.7\nrendered")
        return 0, json.dumps({"ok": True, "text_preserved": True, "portrait": True,
                              "layout_checked": True, "pages": 1, "color_emoji_checked": 4}), ""

    def test_skill_name_is_registered_not_a_path(self):
        ok, text = self.run_tool("load_skill", name="worksheet-pdf")
        self.assertTrue(ok, text)
        self.assertIn("render_worksheet", text)
        for name in ("../../tools.py", "/etc/passwd", "unknown"):
            ok, text = self.run_tool("load_skill", name=name)
            self.assertFalse(ok)
            self.assertIn("Unknown skill", text)

    def test_worksheet_paths_and_approval_are_enforced(self):
        self._worksheet_spec()
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.ws.root / "link").symlink_to(outside, target_is_directory=True)
        with mock.patch.object(tools, "run_streaming") as run:
            for spec, output in (("../outside/quiz.json", "quiz.pdf"),
                                 ("quiz.json", "link/quiz.pdf"),
                                 ("quiz.json", "../outside/quiz.pdf")):
                ok, text = self.run_tool("render_worksheet", spec_path=spec, path=output)
                self.assertFalse(ok)
                self.assertIn("outside the workspace", text)
            self.allow = False
            ok, text = self.run_tool("render_worksheet", spec_path="quiz.json", path="quiz.pdf")
            self.assertFalse(ok)
            self.assertIn("said no", text)
            self.allow = True
            ok, text = self.run_tool("render_worksheet", spec_path="quiz.json", path="other.pdf")
            self.assertFalse(ok)
            self.assertIn("already said no", text)
            self.assertEqual(len(self.asked), 1)
            run.assert_not_called()

    def test_worksheet_checks_requested_question_counts_before_rendering(self):
        self._worksheet_spec()
        with mock.patch.object(tools, "run_streaming") as run:
            for expected, message in (({"expected_multiple_choice": 3}, "needs 3 multiple-choice questions"),
                                      ({"expected_questions": 5}, "needs 5 questions"),
                                      ({"expected_multiple_choice": True}, "must be an integer")):
                ok, text = self.run_tool("render_worksheet", spec_path="quiz.json", path="quiz.pdf",
                                        **expected)
                self.assertFalse(ok, text)
                self.assertIn(message, text)
            run.assert_not_called()
        self.assertEqual(self.asked, [])

    @staticmethod
    def _page_limit(pages=5, limit=3):
        return 2, json.dumps({"ok": False, "code": "page_limit", "pages": pages, "max_pages": limit}), ""

    def test_page_limit_asks_once_and_exports_without_rewriting_content(self):
        self._worksheet_spec()
        source = self.ws.root / "quiz.json"
        data = json.loads(source.read_text())
        data["max_pages"] = 3
        source.write_text(json.dumps(data))
        before = source.read_bytes()
        rendered = []
        self.box.auto_approve = lambda: True

        def render(argv, *_args):
            rendered.append(json.loads(Path(argv[-2]).read_text()))
            if len(rendered) == 1:
                return self._page_limit()
            rc, out, reason = self._render_ok(argv)
            return rc, json.dumps({**json.loads(out), "pages": 5}), reason

        with mock.patch.object(tools, "run_streaming", side_effect=render):
            ok, text = self.run_tool("render_worksheet", spec_path="quiz.json", path="quiz.pdf",
                                    expected_questions=1, expected_multiple_choice=0)
        self.assertTrue(ok, text)
        self.assertEqual(rendered, [data, {**data, "max_pages": 5}])
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.asked[0][3], "Use 5 Pages")
        self.assertIn("without changing", self.reasons[0])
        self.assertIn("user approved 5 pages", text)
        self.assertTrue((self.ws.root / "quiz.pdf").is_file())

    def test_refusing_more_pages_blocks_rewrites_and_preserves_existing_pdf(self):
        self._worksheet_spec()
        source = self.ws.root / "quiz.json"
        data = {**json.loads(source.read_text()), "max_pages": 3}
        source.write_text(json.dumps(data))
        before = source.read_bytes()
        target = self.ws.root / "quiz.pdf"
        target.write_bytes(b"original PDF")
        self.box.auto_approve = lambda: True
        self.allow = False
        with mock.patch.object(tools, "run_streaming", return_value=self._page_limit()) as run:
            ok, text = self.run_tool("render_worksheet", spec_path="quiz.json", path="quiz.pdf")
        self.assertFalse(ok)
        self.assertIn("did not allow more pages", text)
        self.assertTrue(self.box.declined)
        self.assertEqual(run.call_count, 1)
        ok, text = self.run_tool("write_file", path="quiz.json", content="remove questions")
        self.assertFalse(ok)
        self.assertIn("already said no", text)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(target.read_bytes(), b"original PDF")
        self.assertFalse(self.ws.trash_dir.exists())

    def test_stopping_during_page_approval_never_restarts_the_renderer(self):
        import threading
        self._worksheet_spec()
        source = self.ws.root / "quiz.json"
        source.write_text(json.dumps({**json.loads(source.read_text()), "max_pages": 3}))
        self.box.auto_approve = lambda: True
        self.box.cancel = threading.Event()
        self.box.approve = lambda *_: self.box.cancel.set() or True
        with mock.patch.object(tools, "run_streaming", return_value=self._page_limit()) as run:
            ok, text = self.run_tool("render_worksheet", spec_path="quiz.json", path="quiz.pdf")
        self.assertFalse(ok)
        self.assertIn("Stopped", text)
        self.assertEqual(run.call_count, 1)
        self.assertFalse((self.ws.root / "quiz.pdf").exists())

    def test_disabling_run_during_page_approval_prevents_a_second_render(self):
        self._worksheet_spec()
        source = self.ws.root / "quiz.json"
        source.write_text(json.dumps({**json.loads(source.read_text()), "max_pages": 3}))
        self.box.auto_approve = lambda: True

        def disable_run(*_):
            self.box.commands = False
            return True

        self.box.approve = disable_run
        with mock.patch.object(tools, "run_streaming", return_value=self._page_limit()) as run:
            ok, text = self.run_tool("render_worksheet", spec_path="quiz.json", path="quiz.pdf")
        self.assertFalse(ok)
        self.assertIn("Files and Run enabled", text)
        self.assertEqual(run.call_count, 1)
        self.assertFalse((self.ws.root / "quiz.pdf").exists())

    def test_failed_or_stopped_render_never_replaces_an_existing_pdf(self):
        self._worksheet_spec()
        target = self.ws.root / "quiz.pdf"
        target.write_bytes(b"original document")
        self.box.auto_approve = lambda: True
        for result, message in (((1, "Needs 4 pages", ""), "not saved"),
                                ((-15, "", "cancelled"), "Stopped"),
                                ((-15, "", "timeout"), "timed out"),
                                ((0, "not a report", ""), "no valid layout report"),
                                ((0, '{"ok": true}', ""), "did not confirm")):
            with self.subTest(result=result):
                with mock.patch.object(tools, "run_streaming", return_value=result):
                    ok, text = self.run_tool("render_worksheet", spec_path="quiz.json", path="quiz.pdf")
                self.assertFalse(ok, text)
                self.assertIn(message, text)
                self.assertEqual(target.read_bytes(), b"original document")
                self.assertFalse(self.ws.trash_dir.exists())
                self.assertEqual(list(self.ws.root.glob(".qwen-pdf-*")), [])

    def test_successful_pdf_has_open_chip_and_recoverable_original(self):
        self._worksheet_spec()
        target = self.ws.root / "quiz.pdf"
        target.write_bytes(b"the user's original")
        trashed = []
        self.ws.on_trash = lambda original, backup: trashed.append((original, backup))
        self.box.auto_approve = lambda: True
        with mock.patch.object(tools, "run_streaming", side_effect=self._render_ok):
            ok, text = self.run_tool("render_worksheet", spec_path="quiz.json", path="quiz.pdf")
        self.assertTrue(ok, text)
        self.assertIn("old version is in the Trash", text)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.box.result_paths("render_worksheet", {"path": "quiz.pdf"}, text), [target])
        self.assertEqual(trashed[0][1].read_bytes(), b"the user's original")
        self.ws.restore(*trashed[0])
        self.assertEqual(target.read_bytes(), b"the user's original")

    def test_pdf_overwrite_refusal_and_concurrent_user_edit_are_preserved(self):
        self._worksheet_spec()
        target = self.ws.root / "quiz.pdf"
        target.write_bytes(b"original")
        self.ws.confirm = lambda *_: False
        self.box.auto_approve = lambda: True
        with mock.patch.object(tools, "run_streaming") as run:
            ok, text = self.run_tool("render_worksheet", spec_path="quiz.json", path="quiz.pdf")
            self.assertFalse(ok)
            self.assertIn("declined overwriting", text)
            run.assert_not_called()
        self.ws.confirm = lambda *_: True

        def edited_during_render(*args):
            target.write_bytes(b"edited by the user during rendering")
            return self._render_ok(*args)

        with mock.patch.object(tools, "run_streaming", side_effect=edited_during_render):
            ok, text = self.run_tool("render_worksheet", spec_path="quiz.json", path="quiz.pdf")
        self.assertFalse(ok)
        self.assertIn("destination changed", text)
        self.assertEqual(target.read_bytes(), b"edited by the user during rendering")
        self.assertFalse(self.ws.trash_dir.exists())

    def _homework_spec(self):
        data = {
            "title": "Moon homework", "reading": ["Mia flew her rocket all the way to the Moon."],
            "questions": [
                {"prompt": "Where did Mia fly?", "options": ["To the Moon", "To the beach"],
                 "choice": "A", "evidence": "Mia flew her rocket all the way to the Moon."},
                {"prompt": "3 + 2 = __", "answer": "5", "lines": 1},
            ],
        }
        (self.ws.root / "moon.json").write_text(json.dumps(data))
        return data

    def test_homework_and_answer_key_are_checked_then_created_together(self):
        data = self._homework_spec()
        staged = []

        def render(argv, *_args):
            staged.append(json.loads(Path(argv[-2]).read_text()))
            return self._render_ok(argv)

        with mock.patch.object(tools, "run_streaming", side_effect=render):
            ok, text = self.run_tool("render_worksheet", spec_path="moon.json", path="moon.pdf",
                                     answer_key_path="moon-key.pdf", expected_questions=2)
        self.assertTrue(ok, text)
        self.assertEqual([a[3] for a in self.asked], ["Create PDFs"])
        self.assertIn('"answer_key_path": "moon-key.pdf"', self.asked[0][2])
        student, key = staged
        self.assertEqual(student, {**data, "questions": [
            {k: v for k, v in q.items() if k not in ("choice", "answer", "evidence")}
            for q in data["questions"]]})
        key_text = json.dumps(key["reading"], ensure_ascii=False)
        self.assertIn("Answer: A. To the Moon", key_text)
        self.assertIn("Answer: 5", key_text)
        self.assertIn('"arithmetic_checked": 1', text)
        self.assertIn('"answer_entries": 2', text)
        self.assertIn("student PDF has no answers", text)
        both = [self.ws.root / "moon.pdf", self.ws.root / "moon-key.pdf"]
        self.assertEqual(self.box.result_paths(
            "render_worksheet", {"path": "moon.pdf", "answer_key_path": "moon-key.pdf"}, text), both)
        self.assertTrue(all(p.read_bytes().startswith(b"%PDF-") for p in both))

    def test_worksheet_near_misses_render_and_are_reported_without_touching_the_json(self):
        raw = ('{"title": "Harvest math", "theme": {"motif": "pumpkins", "accent": "#D4A574"},\n'
               ' "questions": [\n'
               '  {"question": "1. Count the apples.\nHow many?", "type": "short", "lines": 0, "answer": "5"},\n'
               '  {"text": "2. Which is more?", "choices": ["A. 3", "B. 4"], "correct_answer": "B"}]}')
        (self.ws.root / "harvest.json").write_text(raw)  # note the raw newline inside a string
        staged = []

        def render(argv, *_args):
            staged.append(json.loads(Path(argv[-2]).read_text()))
            return self._render_ok(argv)

        with mock.patch.object(tools, "run_streaming", side_effect=render):
            ok, text = self.run_tool("render_worksheet", spec_path="harvest.json", path="harvest.pdf",
                                     answer_key_path="harvest-key.pdf", expected_questions=2,
                                     expected_multiple_choice=1)
        self.assertTrue(ok, text)
        student, key = staged
        self.assertEqual(student, {"title": "Harvest math", "theme": {"motif": "dots", "accent": "#8A6B4B"},
                                   "questions": [{"prompt": "Count the apples.\nHow many?", "lines": 1},
                                                 {"prompt": "Which is more?", "options": ["3", "4"]}]})
        self.assertIn("Answer: B. 4", json.dumps(key["reading"]))
        self.assertIn("Handled automatically (no action needed; the JSON file was not changed): ", text)
        for note in ("theme.motif 'pumpkins' is not available, so 'dots' was used",
                     "theme.accent #D4A574 was too pale for headings, so #8A6B4B was used",
                     "read question field 'choices' as 'options' (Question 2)",
                     "removed typed numbers from prompts"):
            self.assertIn(note, text)
        self.assertEqual((self.ws.root / "harvest.json").read_text(), raw)

    def test_broken_worksheet_json_names_the_spot_and_the_right_fix(self):
        f = self.ws.root / "sheet.json"
        for content, expected in (
                ('{\n "title": "Farm day",\n "questions": [\n  {"prompt": "3 + 4 = __"}\n'
                 '  {"prompt": "5 + 1 = __"}\n ]\n}\n',
                 'Expecting \',\' delimiter at line 5, column 3: {"prompt": "5 + 1 = __"}. Fix just '
                 'that spot with edit_file'),
                ('{"title": "Farm day", "questions": [{"prompt": "3 + 4 = __"},\n'
                 '[... 1,412 more characters removed to save context space]\n]}',
                 "copied from shortened chat history, so real content is missing. Write the "
                 "complete JSON again"),
                ('{"title": "Farm day", "questions": [', "The JSON stops before it is complete")):
            with self.subTest(expected=expected):
                f.write_text(content)
                ok, text = self.run_tool("render_worksheet", spec_path="sheet.json", path="sheet.pdf")
                self.assertFalse(ok)
                self.assertIn("Invalid JSON specification: ", text)
                self.assertIn(expected, text)
        self.assertEqual(self.asked, [])
        self.assertFalse((self.ws.root / "sheet.pdf").exists())

    def test_answer_problems_and_failed_key_render_leave_every_file_unchanged(self):
        data = self._homework_spec()
        data["questions"][1]["answer"] = "6"
        (self.ws.root / "moon.json").write_text(json.dumps(data))
        with mock.patch.object(tools, "run_streaming") as run:
            ok, text = self.run_tool("render_worksheet", spec_path="moon.json", path="moon.pdf",
                                     answer_key_path="moon-key.pdf")
            self.assertFalse(ok)
            self.assertIn("answer key is not ready", text)
            self.assertIn("3 + 2 = __ gives 5", text)
            for key_path in ("moon.json", "moon.pdf"):
                ok, text = self.run_tool("render_worksheet", spec_path="moon.json", path="moon.pdf",
                                         answer_key_path=key_path)
                self.assertIn("three different file names", text)
            run.assert_not_called()
        self.assertEqual(self.asked, [])
        self._homework_spec()
        student = self.ws.root / "moon.pdf"
        student.write_bytes(b"the user's original")
        self.box.auto_approve = lambda: True
        results = [self._render_ok, lambda *_: (1, "Chromium crashed", "")]
        with mock.patch.object(tools, "run_streaming", side_effect=lambda *a: results.pop(0)(*a)):
            ok, text = self.run_tool("render_worksheet", spec_path="moon.json", path="moon.pdf",
                                     answer_key_path="moon-key.pdf")
        self.assertFalse(ok)
        self.assertIn("answer key PDF was not saved", text)
        self.assertEqual(student.read_bytes(), b"the user's original")
        self.assertFalse((self.ws.root / "moon-key.pdf").exists())
        self.assertFalse(self.ws.trash_dir.exists())
        self.assertEqual(list(self.ws.root.glob(".qwen-pdf-*")), [])

    def test_answer_key_hint_and_unchanged_rewrites_point_at_the_real_fix(self):
        (self.ws.root / "quiz.json").write_text(json.dumps(
            {"title": "Quiz", "theme": {"icons": ["rocket"]}, "questions": [{"prompt": "Q?"}]}))
        ok, text = self.run_tool("render_worksheet", spec_path="quiz.json", path="quiz.pdf")
        self.assertIn("theme.icons[0] must be one emoji", text)
        self.assertNotIn("render_answer_key", text)
        (self.ws.root / "key.json").write_text('{"source_path": "quiz.pdf", "answers": []}')
        ok, text = self.run_tool("render_worksheet", spec_path="key.json", path="key.pdf")
        self.assertIn("use render_answer_key", text)
        content = (self.ws.root / "quiz.json").read_text()
        ok, text = self.run_tool("write_file", path="quiz.json", content=content)
        self.assertTrue(ok, text)
        self.assertIn("Unchanged", text)
        self.assertEqual(self.asked, [])
        self.assertFalse(self.ws.trash_dir.exists())

    def _key_spec(self):
        (self.ws.root / "quiz.json").write_text(json.dumps({
            "title": "The umbrella", "reading": ["Nora left her wet umbrella beside the door."],
            "questions": [
                {"prompt": "What did Nora leave?", "options": ["An umbrella", "A notebook"]},
                {"prompt": "What might the wet umbrella suggest?", "lines": 4},
            ],
        }))
        ok, result = self.run_tool("read_quiz", path="quiz.json")
        self.assertTrue(ok, result)
        source = json.loads(result.split("\n", 1)[1])
        key = {
            "source_path": source["source_path"], "source_sha256": source["source_sha256"],
            "answers": [
                {"question": 1, "choice": "A", "explanation": "The passage names the object.",
                 "evidence": ["Nora left her wet umbrella beside the door."]},
                {"question": 2, "answer": "It might have been raining outside.",
                 "evidence": ["her wet umbrella beside the door."]},
            ],
        }
        (self.ws.root / "key.json").write_text(json.dumps(key))
        return key

    def test_key_exports_source_questions_with_the_shared_pdf_safeguards(self):
        self._key_spec()
        self.box.auto_approve = lambda: True
        target = self.ws.root / "key.pdf"
        target.write_bytes(b"previous answer key")
        drafts = []

        def render(argv, *_args):
            drafts.append(json.loads(Path(argv[-2]).read_text()))
            return self._render_ok(argv)

        with mock.patch.object(tools, "run_streaming", side_effect=render):
            ok, result = self.run_tool("render_answer_key", spec_path="key.json", path="key.pdf")
        self.assertTrue(ok, result)
        self.assertIn('"questions_answered": 2', result)
        self.assertIn('"answer_entries": 2', result)
        self.assertIn('"semantic_accuracy_verified": false', result)
        self.assertEqual(drafts[0]["reading"][0]["heading"], "Question 1:")
        self.assertIn("What did Nora leave?", drafts[0]["reading"][0]["text"])
        self.assertIn("Answer: A. An umbrella", drafts[0]["reading"][0]["text"])
        self.assertEqual(self.box.result_paths("render_answer_key", {"path": "key.pdf"}), [target])
        self.assertEqual((self.ws.trash_dir / "files/key.pdf").read_bytes(), b"previous answer key")

    def test_stale_and_wrong_story_keys_do_not_touch_existing_outputs(self):
        key = self._key_spec()
        target = self.ws.root / "key.pdf"
        target.write_bytes(b"existing teacher key")
        self.box.auto_approve = lambda: True
        key["answers"][0]["evidence"] = ["Detective Martinez found a rope ladder."]
        (self.ws.root / "key.json").write_text(json.dumps(key))
        with mock.patch.object(tools, "run_streaming") as render:
            ok, result = self.run_tool("render_answer_key", spec_path="key.json", path="key.pdf")
            self.assertFalse(ok)
            self.assertIn("not in the source", result)
            self._key_spec()
            source = self.ws.root / "quiz.json"
            source.write_text(source.read_text().replace("Nora", "Ada"))
            ok, result = self.run_tool("render_answer_key", spec_path="key.json", path="key.pdf")
            self.assertFalse(ok)
            self.assertIn("current source", result)
            render.assert_not_called()
        self.assertEqual(target.read_bytes(), b"existing teacher key")
        self.assertFalse(self.ws.trash_dir.exists())

    def test_source_changed_during_key_render_prevents_publication(self):
        self._key_spec()
        self.box.auto_approve = lambda: True
        target = self.ws.root / "key.pdf"
        target.write_bytes(b"previous key")

        def edit_source(argv, *_args):
            source = self.ws.root / "quiz.json"
            source.write_text(source.read_text().replace("Nora", "Ada"))
            return self._render_ok(argv)

        with mock.patch.object(tools, "run_streaming", side_effect=edit_source):
            ok, result = self.run_tool("render_answer_key", spec_path="key.json", path="key.pdf")
        self.assertFalse(ok)
        self.assertIn("source changed", result)
        self.assertEqual(target.read_bytes(), b"previous key")
        self.assertFalse(self.ws.trash_dir.exists())
        self.assertIn("Ada", (self.ws.root / "quiz.json").read_text())

    def test_answer_key_page_allowance_keeps_the_source_and_key_draft_unchanged(self):
        key = self._key_spec()
        key["max_pages"] = 1
        source = self.ws.root / "key.json"
        source.write_text(json.dumps(key))
        original = source.read_bytes()
        self.box.auto_approve = lambda: True
        drafts = []

        def render(argv, *_args):
            drafts.append(json.loads(Path(argv[-2]).read_text()))
            if len(drafts) == 1:
                return self._page_limit(2, 1)
            rc, report, reason = self._render_ok(argv)
            return rc, json.dumps({**json.loads(report), "pages": 2}), reason

        with mock.patch.object(tools, "run_streaming", side_effect=render):
            ok, result = self.run_tool("render_answer_key", spec_path="key.json", path="key.pdf")
        self.assertTrue(ok, result)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(drafts[0]["reading"], drafts[1]["reading"])
        self.assertEqual(self.asked[0][3], "Use 2 Pages")
        self.assertIn("2 source-matched answers", self.asked[0][2])

    def test_reader_pages_are_fresh_and_companion_paths_stay_in_workspace(self):
        self._key_spec()
        self.box.commands = False
        with mock.patch.object(tools, "MAX_READ_CHARS", 350):
            ok, first = self.run_tool("read_quiz", path="quiz.json")
        self.assertTrue(ok, first)
        self.assertIn("Call read_quiz with start_line=", first)
        (self.ws.root / "quiz.pdf").write_bytes(b"%PDF-1.7\nsource")
        companion = self.ws.root / "quiz.json"
        companion.unlink()
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text("{}")
        companion.symlink_to(outside)
        with mock.patch.object(tools, "pdf_to_text", return_value="Question 1:\nExplain."):
            ok, result = self.run_tool("read_quiz", path="quiz.pdf")
        self.assertFalse(ok)
        self.assertIn("outside the workspace", result)

    def test_answer_key_cannot_replace_its_source_pdf(self):
        from qwen_chat.worksheet_pdf import source_texts, validate_spec
        key = self._key_spec()
        source_text = "\n".join(source_texts(validate_spec(json.loads(
            (self.ws.root / "quiz.json").read_text()))))
        target = self.ws.root / "quiz.pdf"
        target.write_bytes(b"%PDF-1.7\nsource")
        with mock.patch.object(tools, "pdf_to_text", return_value=source_text), \
             mock.patch.object(tools, "run_streaming") as render:
            ok, result = self.run_tool("read_quiz", path="quiz.pdf")
            self.assertTrue(ok, result)
            source = json.loads(result.split("\n", 1)[1])
            key.update(source_path=source["source_path"], source_sha256=source["source_sha256"])
            (self.ws.root / "key.json").write_text(json.dumps(key))
            ok, result = self.run_tool("render_answer_key", spec_path="key.json", path="quiz.pdf")
            self.assertFalse(ok)
            self.assertIn("never replace the source quiz", result)
            render.assert_not_called()
        self.assertEqual(target.read_bytes(), b"%PDF-1.7\nsource")

    def test_open_missing_or_invalid_pdf_never_launches_a_viewer(self):
        (self.ws.root / "bad.pdf").write_bytes(b"plain text is not a PDF")
        with mock.patch.object(tools.subprocess, "Popen") as launch:
            for name in ("missing.pdf", "bad.pdf", "../outside.pdf"):
                ok, result = self.run_tool("open_file", path=name)
                self.assertFalse(ok, result)
            ok, result = self.run_tool("run_command", command="xdg-open missing.pdf &", why="Open")
            self.assertFalse(ok)
            self.assertIn("missing or empty", result)
            launch.assert_not_called()
        self.assertEqual(self.asked, [])

    def test_open_reports_launch_acceptance_and_real_opener_errors(self):
        target = self.ws.root / "a file.txt"
        target.write_text("A document")
        self.box.auto_approve = lambda: True
        process = mock.Mock(returncode=0)
        process.poll.return_value = 0
        with mock.patch.object(tools.subprocess, "Popen", return_value=process) as launch:
            ok, result = self.run_tool("open_file", path="a file.txt")
            self.assertTrue(ok, result)
            self.assertIn("Open requested", result)
            self.assertIn("not verified", result)
            self.assertEqual(launch.call_args.args[0][-2:], ["open", str(target)])
            self.assertTrue(launch.call_args.kwargs["start_new_session"])
            process.returncode = process.poll.return_value = 1
            ok, result = self.run_tool("open_file", path="a file.txt")
            self.assertFalse(ok)
            self.assertIn("opener failed", result)

    def test_open_refusal_changed_file_and_cancellation_do_not_hide_failure(self):
        import threading
        target = self.ws.root / "key.txt"
        target.write_text("Before approval")
        with mock.patch.object(tools.subprocess, "Popen") as launch:
            self.allow = False
            self.assertFalse(self.run_tool("open_file", path="key.txt")[0])
            self.box.declined = False

            def edit(*_):
                target.write_text("Edited during approval")
                return True

            self.box.approve = edit
            ok, result = self.run_tool("open_file", path="key.txt")
            self.assertFalse(ok)
            self.assertIn("changed before opening", result)
            launch.assert_not_called()
            self.box.auto_approve = lambda: True
            self.box.cancel = threading.Event()
            process = launch.return_value
            process.poll.side_effect = lambda: self.box.cancel.set()
            ok, result = self.run_tool("open_file", path="key.txt")
            self.assertFalse(ok)
            self.assertIn("stopped or timed out", result)
            process.terminate.assert_called_once()

    def test_pdf_extraction_does_not_accept_partial_stdout_on_failure(self):
        failure = subprocess.CompletedProcess([], 1, b"Only part of the quiz", b"Damaged PDF")
        with mock.patch.object(tools.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(ToolError, "PDF text extraction failed"):
                tools.pdf_to_text(self.ws.root / "source.pdf")


if __name__ == "__main__":
    unittest.main()
