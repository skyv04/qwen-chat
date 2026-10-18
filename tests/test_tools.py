import json
import os
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
        self.ws = Workspace(base / "ws")
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


if __name__ == "__main__":
    unittest.main()
