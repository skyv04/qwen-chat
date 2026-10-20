import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from qwen_chat import agent
from qwen_chat.agent import Agent, clean_for_api, fit_context
from qwen_chat.tools import ToolBox, Workspace

CUT = '{"path": "practice/make_pdf.py", "content": "#!/usr/bin/env python3\\nimport os\\nstory = [\\"'


class _Server:
    url = "http://127.0.0.1:1"
    n_ctx = 16384
    model_name = "test"

    def ensure_running(self, *_):
        pass

    def token_id(self, _text):
        return None


class _Events:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _call(args: str, cid="c1", name="write_file") -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def _reply(msg: dict, finish="stop") -> dict:
    return {"message": {"role": "assistant", "content": "", **msg}, "timings": {}, "finish_reason": finish}


class CutOffToolCallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.box = ToolBox(Workspace(self.root), web=False, files=True)
        self.payloads = []

    def tearDown(self):
        self.tmp.cleanup()

    def _respond(self, replies, messages):
        payloads = self.payloads

        class Stream:
            def __init__(self, _url):
                pass

            def run(self, payload, *_):
                payloads.append(json.loads(json.dumps(payload)))
                return replies.pop(0)

        with mock.patch.object(agent, "ChatStream", Stream):
            Agent(_Server(), self.box).respond(messages, _Events(), threading.Event())

    def test_cut_off_call_is_reported_and_never_stored_broken(self):
        msgs = [{"role": "user", "content": "make the pdf"}]
        self._respond([_reply({"tool_calls": [_call(CUT)]}, finish="length"),
                       _reply({"content": "I'll write it in parts."})], msgs)
        stored = msgs[1]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(stored), {"path": "practice/make_pdf.py"})
        tool = msgs[2]
        self.assertFalse(tool["_meta"]["ok"])
        self.assertIn("cut off", tool["content"])
        self.assertIn("append: true", tool["content"])
        self.assertFalse((self.root / "practice/make_pdf.py").exists())
        # The follow-up request must be one llama-server can parse.
        for m in self.payloads[1]["messages"]:
            for c in m.get("tool_calls") or []:
                self.assertIsInstance(json.loads(c["function"]["arguments"]), dict)
        self.assertEqual(self.payloads[0]["max_tokens"], agent.MAX_OUT_TOKENS)

    def test_worksheet_skill_is_loaded_on_demand(self):
        self.box.commands = True
        msgs = [{"role": "user", "content": "Improve this worksheet's appearance, not its content."}]
        self._respond([
            _reply({"tool_calls": [_call('{"name":"worksheet-pdf"}', name="load_skill")]}),
            _reply({"content": "I'll preserve the wording and use the worksheet renderer."}),
        ], msgs)
        prompt = self.payloads[0]["messages"][0]["content"]
        self.assertIn("load_skill", prompt)
        self.assertIn("preserve the original passage", prompt)
        self.assertNotIn("Use about 4-6 answer lines", prompt)
        skill_reply = next(m for m in self.payloads[1]["messages"] if m["role"] == "tool")
        self.assertIn("Use about 4-6 answer lines", skill_reply["content"])
        self.assertTrue(next(m for m in msgs if m["role"] == "tool")["_meta"]["ok"])
        self.box.commands = False
        self.assertIn("load_skill", agent.system_prompt(self.box, "test"))
        self.assertNotIn("call load_skill with name 'worksheet-pdf'", agent.system_prompt(self.box, "test"))

    def test_suggested_skills_stay_in_history_without_limiting_tools(self):
        self.box.commands = True
        original = "/worksheet-pdf Make a new ocean quiz. Use any other useful skills too."
        msgs = [{"role": "user", "content": original}]
        self._respond([
            _reply({"tool_calls": [_call("{}", name="list_skills")]}),
            _reply({"tool_calls": [_call('{"name":"worksheet-pdf"}', "c2", name="load_skill")]}),
            _reply({"content": "I'll use the worksheet design skill for the ocean theme."}),
        ], msgs)
        prompt = self.payloads[0]["messages"][0]["content"]
        self.assertIn("not mandatory or exclusive", prompt)
        self.assertIn("other relevant skills", prompt)
        for payload in self.payloads:
            self.assertEqual(next(m["content"] for m in payload["messages"] if m["role"] == "user"),
                             original)
            names = {t["function"]["name"] for t in payload["tools"]}
            self.assertTrue({"list_skills", "load_skill", "save_skill",
                             "write_file", "run_command", "render_worksheet"} <= names)
        self.assertTrue(all(m["_meta"]["ok"] for m in msgs if m["role"] == "tool"))

    def test_refused_page_increase_stops_tools_instead_of_cutting_questions(self):
        self.box.commands = True
        self.box.auto_approve = lambda: True
        source = self.root / "quiz.json"
        source.write_text('{"title":"Quiz","max_pages":3,"questions":[{"prompt":"Explain."}]}')
        original = source.read_bytes()
        failure = json.dumps({"ok": False, "code": "page_limit", "pages": 5, "max_pages": 3})
        msgs = [{"role": "user", "content": "Make the worksheet PDF."}]
        with mock.patch("qwen_chat.tools.run_streaming", return_value=(2, failure, "")):
            self._respond([
                _reply({"tool_calls": [_call('{"spec_path":"quiz.json","path":"quiz.pdf"}',
                                            name="render_worksheet")]}),
                _reply({"tool_calls": [_call('{"path":"quiz.json","content":"cut questions"}',
                                            "c2", name="write_file")]}),
                _reply({"content": "The complete worksheet needs five pages. How would you like to revise it?"}),
            ], msgs)
        self.assertEqual(self.payloads[1]["tool_choice"], "none")
        self.assertEqual(self.payloads[2]["tool_choice"], "none")
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse((self.root / "quiz.pdf").exists())
        self.assertTrue(all(not m["_meta"]["ok"] for m in msgs if m["role"] == "tool"))

    def test_oversized_worksheet_pauses_until_the_user_can_choose_a_split(self):
        self.box.commands = True
        self.box.auto_approve = lambda: True
        source = self.root / "quiz.json"
        source.write_text('{"title":"Quiz","questions":[{"prompt":"Explain."}]}')
        original = source.read_bytes()
        failure = json.dumps({"ok": False, "code": "page_limit", "pages": 12, "max_pages": 10})
        msgs = [{"role": "user", "content": "Make the worksheet PDF."}]
        with mock.patch("qwen_chat.tools.run_streaming", return_value=(2, failure, "")):
            self._respond([
                _reply({"tool_calls": [_call('{"spec_path":"quiz.json","path":"quiz.pdf"}',
                                            name="render_worksheet")]}),
                _reply({"tool_calls": [_call('{"path":"quiz.json","content":"cut questions"}',
                                            "c2", name="write_file")]}),
                _reply({"content": "How would you like the worksheet split?"}),
            ], msgs)
        self.assertEqual(self.payloads[1]["tool_choice"], "none")
        self.assertEqual(source.read_bytes(), original)
        self.assertIn("12 pages", self.box.input_required)
        msgs.append({"role": "user", "content": "Keep the draft for now. List the files."})
        self._respond([
            _reply({"tool_calls": [_call("{}", "c3", name="list_files")]}),
            _reply({"content": "The draft is still in quiz.json."}),
        ], msgs)
        self.assertEqual(self.box.input_required, "")
        self.assertNotIn("tool_choice", self.payloads[-2])
        self.assertTrue(next(m for m in msgs if m.get("tool_call_id") == "c3")["_meta"]["ok"])


class HistoryRepairTest(unittest.TestCase):
    def test_broken_arguments_in_saved_chats_are_made_valid(self):
        msgs = [{"role": "user", "content": "x"},
                {"role": "assistant", "content": None,
                 "tool_calls": [_call(CUT), _call("", "c2", "list_files")]},
                {"role": "tool", "tool_call_id": "c1", "content": "Error"},
                {"role": "tool", "tool_call_id": "c2", "content": "a.txt"}]
        out = clean_for_api(msgs)
        args = [json.loads(c["function"]["arguments"]) for c in out[1]["tool_calls"]]
        self.assertEqual(args, [{"path": "practice/make_pdf.py"}, {}])
        self.assertEqual(msgs[1]["tool_calls"][0]["function"]["arguments"], CUT)  # saved copy untouched

    def test_long_file_contents_in_old_turns_are_shortened_first(self):
        big = json.dumps({"path": "a.py", "content": "x = 1\n" * 2000})
        msgs = clean_for_api([
            {"role": "user", "content": "write it"},
            {"role": "assistant", "content": "", "tool_calls": [_call(big)]},
            {"role": "tool", "tool_call_id": "c1", "content": "Wrote a.py"},
            {"role": "assistant", "content": "Done."},
            {"role": "user", "content": "now run it"}])
        out = fit_context(msgs, 6000)
        self.assertEqual(len(out), len(msgs))  # nothing dropped
        args = json.loads(out[1]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["path"], "a.py")
        self.assertIn("more characters removed", args["content"])
        self.assertLess(len(args["content"]), 500)


if __name__ == "__main__":
    unittest.main()
