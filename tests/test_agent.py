import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from qwen_chat import agent
from qwen_chat.agent import Agent, clean_for_api, fit_context
from qwen_chat.tools import CONTEXT_MARKER, ToolBox, Workspace

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

    def _respond(self, replies, messages, events=None):
        payloads = self.payloads

        class Stream:
            def __init__(self, _url):
                pass

            def run(self, payload, _reasoning, content, *_):
                payloads.append(json.loads(json.dumps(payload)))
                result = replies.pop(0)
                content(result["message"].get("content") or "")
                return result

        with mock.patch.object(agent, "ChatStream", Stream):
            Agent(_Server(), self.box).respond(messages, events or _Events(), threading.Event())

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

    def test_reads_after_a_change_are_fresh_and_exact_repeats_are_marked(self):
        read = '{"path":"n.json"}'
        msgs = [{"role": "user", "content": "make n.json"}]
        self._respond([
            _reply({"tool_calls": [_call('{"path":"n.json","content":"{\\"n\\": 1}"}', "c1")]}),
            _reply({"tool_calls": [_call(read, "c2", name="read_file")]}),
            _reply({"tool_calls": [_call(read, "c3", name="read_file")]}),
            _reply({"tool_calls": [_call('{"path":"n.json","old_text":"1","new_text":"2"}', "c4",
                                         name="edit_file")]}),
            _reply({"tool_calls": [_call(read, "c5", name="read_file")]}),
            _reply({"content": "Done."}),
        ], msgs)
        out = [m["content"] for m in msgs if m["role"] == "tool"]
        self.assertIn('{"n": 1}', out[1])
        self.assertEqual(out[2], out[1] + agent.CACHE_NOTE)
        self.assertIn("Edited n.json at line 1", out[3])
        self.assertIn('{"n": 2}', out[4])
        self.assertNotIn(agent.CACHE_NOTE, out[4])
        self.assertEqual((self.root / "n.json").read_text(), '{"n": 2}')

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

    def test_tool_limit_pauses_without_asking_for_a_toolless_response(self):
        replies = [
            _reply({"tool_calls": [_call("{}", "c1", "list_files")]}),
            _reply({"tool_calls": [_call("{}", "c2", "list_files")]}),
            _reply({"content": "<tool_call>pretend command</tool_call>"}),
        ]
        messages = [{"role": "user", "content": "Make the PDF."}]
        with mock.patch.object(agent, "MAX_TOOL_ROUNDS", 2):
            self._respond(replies, messages)
        self.assertEqual(len(self.payloads), 2)
        self.assertEqual(len(replies), 1)
        self.assertIn("Paused after 2 tool rounds", messages[-1]["content"])
        self.assertIn("Reply **continue**", messages[-1]["content"])
        self.assertNotIn("<tool_call>", messages[-1]["content"])

    def test_identical_tool_error_is_flagged_then_stops_instead_of_looping(self):
        self.box.commands = True
        (self.root / "quiz.json").write_text(json.dumps(
            {"title": "Quiz", "theme": {"icons": ["rocket"]}, "questions": [{"prompt": "Q?"}]}))
        render = '{"spec_path":"quiz.json","path":"quiz.pdf"}'
        replies = [_reply({"tool_calls": [_call(render, f"c{i}", "render_worksheet")]})
                   for i in range(4)]
        messages = [{"role": "user", "content": "Make the quiz PDF."}]
        self._respond(replies, messages)
        self.assertEqual(len(self.payloads), 3)
        results = [m["content"] for m in messages if m["role"] == "tool"]
        self.assertNotIn("Same error as before", results[0])
        self.assertIn("Same error as before", results[1])
        final = messages[-1]["content"]
        for expected in ("Task incomplete", "theme.icons[0] must be one emoji",
                         "same error repeated 3 times", "Reply **continue**"):
            self.assertIn(expected, final)

    def test_failed_pdf_cannot_stream_or_store_a_false_success(self):
        self.box.commands = True
        (self.root / "key.json").write_text(json.dumps({
            "source_path": "quiz.pdf", "answers": [{"question": 1, "choice": "B"}]}))
        messages = [{"role": "user", "content": "Make the answer key PDF."}]
        events = mock.Mock()
        false_success = "Your PDF is ready: key.pdf"
        self._respond([
            _reply({"tool_calls": [_call('{"spec_path":"key.json","path":"key.pdf"}',
                                        name="render_worksheet")]}),
            _reply({"content": false_success}),
        ], messages, events)
        self.assertIn("Task incomplete", messages[-1]["content"])
        self.assertIn("render_answer_key", messages[-1]["content"])
        self.assertNotIn(false_success, "".join(call.args[0] for call in events.content.call_args_list))
        self.assertFalse((self.root / "key.pdf").exists())

    def test_backgrounded_missing_file_cannot_claim_it_opened(self):
        self.box.commands = True
        messages = [{"role": "user", "content": "Open the answer key."}]
        self._respond([
            _reply({"tool_calls": [_call('{"command":"xdg-open key.pdf &","why":"Open key"}',
                                        name="run_command")]}),
            _reply({"content": "The answer key opened successfully."}),
        ], messages)
        self.assertIn("Task incomplete", messages[-1]["content"])
        self.assertIn("missing or empty", messages[-1]["content"])
        self.assertFalse(messages[2]["_meta"]["ok"])

    def test_successful_retry_clears_only_the_resolved_failure(self):
        self.box.commands = True
        self.box.auto_approve = lambda: True
        (self.root / "quiz.json").write_text('{"title":"Quiz","questions":[{"prompt":"Explain."}]}')
        messages = [{"role": "user", "content": "Make the quiz."}]

        def render(argv, *_):
            if not hasattr(render, "attempted"):
                render.attempted = True
                return 1, "Temporary renderer failure", ""
            Path(argv[-1]).write_bytes(b"%PDF-1.7\nrendered")
            return 0, json.dumps({"ok": True, "text_preserved": True, "portrait": True,
                                  "layout_checked": True, "pages": 1, "color_emoji_checked": 4}), ""

        with mock.patch("qwen_chat.tools.run_streaming", side_effect=render):
            self._respond([
                _reply({"tool_calls": [_call('{"spec_path":"quiz.json","path":"quiz.pdf"}',
                                            name="render_worksheet")]}),
                _reply({"tool_calls": [_call('{"spec_path":"quiz.json","path":"quiz.pdf"}',
                                            "c2", "render_worksheet")]}),
                _reply({"content": "Created quiz.pdf."}),
            ], messages)
        self.assertEqual(messages[-1]["content"], "Created quiz.pdf.")
        self.assertTrue((self.root / "quiz.pdf").is_file())


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
            {"role": "tool", "name": "load_skill", "content": "Skill steps. " * 200},
            {"role": "assistant", "content": "Done."},
            {"role": "user", "content": "now run it"}])
        out = fit_context(msgs, 6000)
        self.assertEqual(len(out), len(msgs))  # nothing dropped
        args = json.loads(out[1]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["path"], "a.py")
        # No fragment is kept: the model would continue it and copy it into a file.
        self.assertEqual(args["content"], "[12,000 characters omitted here to save context space]")
        self.assertEqual(out[3]["content"], agent.SKILL_STUB)
        # Every note is one the write tools refuse to save.
        for note in (args["content"], agent.OLD_TOOL_STUB, agent.SKILL_STUB, agent.REPEAT_STUB,
                     agent.CUT_NOTE):
            self.assertTrue(CONTEXT_MARKER.search(note), note)

    def test_current_source_survives_before_large_draft_arguments(self):
        source = "The real source passage and question inventory. " * 100
        messages = [
            {"role": "user", "content": "Make its answer key."},
            {"role": "tool", "name": "read_quiz", "content": source},
            {"role": "assistant", "content": "", "tool_calls": [
                _call(json.dumps({"path": "key.json", "content": "draft text " * 1500}))]},
            {"role": "tool", "name": "write_file", "content": "Created key.json"},
        ]
        trimmed = fit_context(messages, 6500)
        self.assertEqual(trimmed[1]["content"], source)
        self.assertIn("to save context space", trimmed[2]["tool_calls"][0]["function"]["arguments"])

    def test_skill_text_outlasts_stale_outputs_of_the_same_turn(self):
        skill = "Worksheet rules. " * 300
        messages = [{"role": "user", "content": "Make the worksheet."},
                    {"role": "tool", "name": "load_skill", "content": skill}]
        messages += [{"role": "tool", "name": "list_files", "content": f"listing {i} " * 150}
                     for i in range(6)]
        trimmed = fit_context(messages, 12000)
        self.assertEqual(trimmed[1]["content"], skill)
        self.assertEqual([m["content"] for m in trimmed[2:4]], [agent.OLD_TOOL_STUB] * 2)
        self.assertEqual(trimmed[-4:], messages[-4:])  # the latest results stay whole

    def test_trimmed_history_only_grows_until_it_must_be_trimmed_again(self):
        fit, budget = agent.ContextFitter(), 10_000
        size = lambda ms: sum(agent._size(m) for m in ms)
        turn = lambda i: [{"role": "user", "content": f"question {i} " + "x" * 900},
                          {"role": "assistant", "content": f"answer {i} " + "y" * 900}]
        msgs = [m for i in range(8) for m in turn(i)]
        prev = fit(msgs, budget)
        self.assertLessEqual(size(prev), budget * agent.TRIM_TARGET)
        retrims = 0
        for i in range(8, 20):
            msgs += turn(i)
            out = fit(msgs, budget)
            self.assertLessEqual(size(out), budget)
            self.assertEqual(out[-2:], msgs[-2:])
            if out[:len(prev)] != prev:  # an earlier message changed: the server re-reads
                retrims += 1
                self.assertLessEqual(size(out), budget * agent.TRIM_TARGET)
            prev = out
        self.assertLessEqual(retrims, 5)  # re-trimming every round would be 12


if __name__ == "__main__":
    unittest.main()
