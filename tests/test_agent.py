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
