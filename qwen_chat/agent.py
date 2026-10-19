"""The tool-using conversation loop."""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from typing import Protocol

from .llm import Cancelled, ChatStream, LLMError, ServerManager
from .tools import ToolBox

MAX_TOOL_ROUNDS = 12
MAX_OUT_TOKENS = 4096      # per reply, tool call arguments included
CALL_CHARS = 8000          # what the model is told one tool call may hold (well under the cap)
CHARS_PER_TOKEN = 3.0      # conservative; real English text is ~3.5-4
OLD_TOOL_STUB = "[Earlier tool output removed to save context space.]"
LONG_ARG = 1500            # tool call strings longer than this get shortened when space is tight


class Events(Protocol):
    """Callbacks from the worker thread. The UI marshals them to the main loop."""
    def status(self, text: str) -> None: ...
    def round_begin(self) -> None: ...
    def reasoning(self, delta: str) -> None: ...
    def content(self, delta: str) -> None: ...
    def tool_begin(self, call_id: str, name: str, args: dict) -> None: ...
    def tool_end(self, call_id: str, name: str, args: dict, ok: bool, result: str) -> None: ...
    def round_end(self, message: dict, timings: dict) -> None: ...


def system_prompt(toolbox: ToolBox, model_name: str) -> str:
    now = time.localtime()
    parts = [
        f"You are Qwen, a helpful AI assistant ({model_name}) running locally on the user's "
        "phone GPU through llama.cpp.",
        # Date only: the prompt must stay identical between turns so llama-server can reuse
        # its cached prefix (re-reading ~1,400 tokens of instructions costs ~40 s here).
        f"Today is {time.strftime('%A, %B %-d, %Y', now)}.",
    ]
    if toolbox.web:
        parts.append(
            "\n## Internet\n"
            "You have live internet access through tools. Your built-in knowledge has a cutoff "
            "date, so for anything recent, time-sensitive, niche, or that you are unsure about, "
            "search with web_search, then read the most relevant result(s) with fetch_url before "
            "answering. Cite the pages you used as Markdown links like [Site name](URL); when a "
            "page lists links (for example news headlines), link each item to its own URL from "
            "that list rather than the page you were on, with the item's title as the link text, "
            "like [Headline](URL). Never invent URLs, quotes or numbers."
            + (" To save something from the web, use download_file." if toolbox.files else ""))
    else:
        parts.append("\nInternet access is turned off in this chat. If a question needs current "
                     "information, say that you can't check and suggest turning on Web.")
    if toolbox.files:
        parts.append(
            "\n## Workspace files\n"
            f"You can read and write files only inside the user's workspace folder "
            f"({toolbox.ws.root}). Always use paths relative to it, like 'notes/todo.md'. "
            "Nothing outside it is accessible. When the user asks you to create, save, write, "
            "export or edit a file, actually do it with the file tools (don't just show the "
            "content), then mention the file name. Read a file before changing it. Use clear "
            "file names with the right extension (.md, .txt, .csv, .py, .html, ...). One tool "
            f"call can hold at most about {CALL_CHARS:,} characters; longer calls get cut off and "
            "fail. Write a longer file in parts: write_file with the first part, then write_file "
            "with append: true for each next part.")
    else:
        parts.append("\nFile access is turned off in this chat.")
    if toolbox.files and toolbox.commands:
        parts.append(
            "\n## Programs\n"
            "You can run bash commands in the workspace with run_command, for example to convert "
            "images or documents, make or merge PDFs, run OCR, process data or run Python scripts. "
            "The user approves every command, so combine steps into one command or script and say "
            "why in `why`. Use check_programs to see what is installed. If a task needs a program "
            "or Python package that is missing, ask for it with install_packages (Debian packages "
            "with apt, Python packages with pip) and explain why. Don't tell the user a task is "
            "impossible just because a tool is missing. If the user says no to a command or "
            "install, don't try a workaround: stop and ask what they'd like instead. You can't see "
            "images: when a task depends on what images show (text, page or question numbers, "
            "order), read them first with OCR (`tesseract IMAGE -`; ask to install tesseract-ocr "
            "if it's missing). Never guess the order or contents from file names. Commands get "
            "no keyboard input, so use non-interactive options, and keep files in the workspace. "
            "For a long script, save it with write_file, then run it with run_command; keep it "
            "short by using loops, lists and helper functions instead of repeating code.")
    parts.append(
        "\n## Style\nAnswer in Markdown. Be direct and helpful; use headings, lists, tables and "
        "fenced code blocks (with a language) when they make the answer clearer.")
    return "\n".join(parts)


def _size(msg: dict) -> int:
    n = len(msg.get("content") or "") + 16
    for tc in msg.get("tool_calls") or []:
        n += len(tc["function"]["name"]) + len(tc["function"]["arguments"]) + 16
    return n


def _args_ok(raw) -> bool:
    """llama-server re-parses every earlier tool call; one broken call fails the whole request."""
    if isinstance(raw, dict):
        return True
    try:
        return isinstance(json.loads(raw), dict)
    except (TypeError, ValueError):
        return False


_SALVAGE = re.compile(r'"(path)"\s*:\s*("(?:[^"\\]|\\.)*")|"(append)"\s*:\s*(true|false)')


def salvage_args(raw) -> dict:
    """The small fields that survive in broken (usually cut-off) arguments, for the UI and history."""
    out = {}
    for m in _SALVAGE.finditer(raw if isinstance(raw, str) else ""):
        key, val = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        try:
            out.setdefault(key, json.loads(val))
        except ValueError:
            pass
    return out


def _api_call(call: dict) -> dict:
    fn = call.get("function") or {}
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        raw = json.dumps(raw, ensure_ascii=False)
    elif not _args_ok(raw):
        raw = json.dumps(salvage_args(raw), ensure_ascii=False)
    return {**call, "function": {**fn, "arguments": raw}}


def clean_for_api(messages: list[dict]) -> list[dict]:
    """Drops UI-only fields; earlier reasoning is not sent back (Qwen's template ignores it)."""
    out = []
    for m in messages:
        c = {k: v for k, v in m.items() if not k.startswith("_") and k != "reasoning_content"}
        if c.get("role") == "assistant" and c.get("content") is None:
            c["content"] = ""
        if c.get("tool_calls"):
            c["tool_calls"] = [_api_call(tc) for tc in c["tool_calls"]]
        out.append(c)
    return out


def _shrink_call(call: dict) -> bool:
    """Shortens long strings (like a whole file) in a tool call's arguments. True if it changed."""
    try:
        args = json.loads(call["function"]["arguments"])
    except (KeyError, TypeError, ValueError):
        return False
    if not isinstance(args, dict):
        return False
    changed = False
    for k, v in args.items():
        if isinstance(v, str) and len(v) > LONG_ARG:
            args[k] = v[:300] + f"\n[... {len(v) - 300:,} more characters removed to save context space]"
            changed = True
    if changed:
        call["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
    return changed


def fit_context(messages: list[dict], budget_chars: int) -> list[dict]:
    """Shrinks history to fit: first old tool outputs and arguments, then whole old turns, then the
    current turn's tool outputs, then its tool arguments."""
    msgs = copy.deepcopy(messages)
    total = lambda: sum(_size(m) for m in msgs)
    if total() <= budget_chars:
        return msgs
    last_user = max((i for i, m in enumerate(msgs) if m["role"] == "user"), default=0)
    for i, m in enumerate(msgs[:last_user]):
        if m["role"] == "tool" and len(m.get("content") or "") > len(OLD_TOOL_STUB):
            m["content"] = OLD_TOOL_STUB
        for call in m.get("tool_calls") or []:
            _shrink_call(call)
    # Drop whole turns from the front (a turn = a user message and everything up to the next).
    while total() > budget_chars:
        users = [i for i, m in enumerate(msgs) if m["role"] == "user"]
        if len(users) < 2:
            break
        del msgs[users[0]:users[1]]
    # Still too big: trim the current turn's tool outputs, largest first.
    while total() > budget_chars:
        tools = [m for m in msgs if m["role"] == "tool" and len(m.get("content") or "") > 600]
        if not tools:
            break
        big = max(tools, key=lambda m: len(m["content"]))
        over = total() - budget_chars
        keep = max(400, len(big["content"]) - over - 200)
        big["content"] = big["content"][:keep] + "\n[...truncated to fit the context window]"
    # Last resort: the current turn's long tool arguments (e.g. files written in parts), oldest first.
    for m in msgs[last_user:]:
        if total() <= budget_chars:
            break
        for call in m.get("tool_calls") or []:
            _shrink_call(call)
    return msgs


def repair_history(messages: list[dict]) -> None:
    """After a stop or error: give every tool call a result so the history stays valid."""
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    i = 0
    while i < len(messages):
        m = messages[i]
        i += 1
        if m.get("role") != "assistant":
            continue
        for call in m.get("tool_calls") or []:
            if call.get("id") not in answered:
                messages.insert(i, {"role": "tool", "tool_call_id": call.get("id"),
                                    "name": call.get("function", {}).get("name", ""),
                                    "content": "Stopped by the user before this ran.",
                                    "_meta": {"ok": False}})
                answered.add(call.get("id"))
                i += 1


_DRAFTING = {"write_file": "Drafting the file", "web_search": "Choosing what to search",
             "fetch_url": "Choosing a page", "download_file": "Choosing a download",
             "run_command": "Writing a command", "install_packages": "Choosing software"}


def _drafting(name: str, chars: int) -> str:
    """Status while the model streams a tool call's arguments (e.g. a long file)."""
    what = _DRAFTING.get(name, "Preparing a step")
    return f"{what}… {chars:,} characters" if chars >= 200 else f"{what}…"


def _broken_call_note(name: str, chars: int, cut_off: bool) -> str:
    """Tool result for a call whose arguments couldn't be read, telling the model how to recover."""
    if cut_off:
        return (f"Error: this {name} call was cut off after {chars:,} characters because the reply "
                "reached its length limit, so nothing was done. Keep each tool call under about "
                f"{CALL_CHARS:,} characters: write a long file in parts (write_file with the first "
                "part, then write_file with append: true for each next part), and keep scripts "
                "short with loops and helper functions.")
    return (f"Error: the {name} arguments were not valid JSON, so nothing was done. Try again with "
            "valid JSON (escape quotes, backslashes and newlines inside strings).")


class Agent:
    def __init__(self, server: ServerManager, toolbox: ToolBox):
        self.server = server
        self.toolbox = toolbox
        self.stream: ChatStream | None = None

    def abort(self) -> None:
        if self.stream is not None:
            self.stream.abort()

    def respond(self, messages: list[dict], events: Events, cancel: threading.Event,
                thinking: bool = False) -> None:
        """Runs until the model gives a final answer. Appends assistant/tool messages to `messages`."""
        self.server.ensure_running(events.status, cancel)
        self.toolbox.cancel = cancel
        self.toolbox.status = events.status
        self.toolbox.declined = False
        seen: dict[str, str] = {}
        tools = self.toolbox.schemas()
        sys_msg = {"role": "system", "content": system_prompt(self.toolbox, self.server.model_name)}
        n_ctx = self.server.n_ctx
        max_out = min(MAX_OUT_TOKENS + (2048 if thinking else 0), n_ctx // 3)
        tool_chars = len(json.dumps(tools)) if tools else 0
        budget = int((n_ctx - max_out) * CHARS_PER_TOKEN) - len(sys_msg["content"]) - tool_chars
        rounds = 0
        while True:
            if cancel.is_set():
                raise Cancelled()
            rounds += 1
            use_tools = tools and rounds <= MAX_TOOL_ROUNDS
            history = fit_context(clean_for_api(messages), budget)
            payload = {
                "messages": [sys_msg] + history,
                "max_tokens": max_out,
                "chat_template_kwargs": {"enable_thinking": bool(thinking)},
                "temperature": 0.6 if thinking else 0.7,
                "top_p": 0.95 if thinking else 0.8,
                "top_k": 20,
                "min_p": 0.0,
                "timings_per_token": False,
            }
            if use_tools:
                payload["tools"] = tools
                if self.toolbox.declined:
                    # The user said no: the model has to answer in words now. The tools stay in
                    # the request so the cached prompt is reused; calling them is switched off.
                    payload["tool_choice"] = "none"
                    tid = self.server.token_id("<tool_call>")
                    if tid is not None:
                        payload["logit_bias"] = [[tid, False]]
            events.round_begin()
            events.status("Thinking…" if thinking else "Writing…")
            self.stream = ChatStream(self.server.url)
            if cancel.is_set():
                raise Cancelled()
            try:
                result = self.stream.run(
                    payload, events.reasoning, events.content,
                    lambda done, total: events.status(f"Reading {total:,} tokens… {100 * done // total}%"),
                    lambda name, chars: events.status(_drafting(name, chars)))
            except LLMError as e:
                if e.context_overflow and budget > 6000:
                    budget = int(budget * 0.7)
                    continue
                raise
            finally:
                self.stream = None
            msg, timings = result["message"], result["timings"]
            cut_off = result["finish_reason"] == "length"
            if cut_off and not msg.get("tool_calls"):
                msg["content"] = (msg.get("content") or "") + "\n\n*[Stopped: reached the response length limit.]*"
            # Broken arguments (usually a call cut off by the length limit) must not be stored:
            # llama-server would reject every later request in this chat.
            broken: dict[str, int] = {}
            for call in msg.get("tool_calls") or []:
                fn = call["function"]
                if not _args_ok(fn.get("arguments")):
                    broken[call["id"]] = len(fn.get("arguments") or "")
                    fn["arguments"] = json.dumps(salvage_args(fn.get("arguments")), ensure_ascii=False)
            msg["_meta"] = {"timings": {k: timings.get(k) for k in
                                        ("predicted_per_second", "predicted_n", "prompt_n",
                                         "prompt_per_second", "cache_n")},
                            "time": time.time()}
            messages.append(msg)
            events.round_end(msg, timings)
            calls = msg.get("tool_calls") or []
            if not calls:
                return
            for call in calls:
                if cancel.is_set():
                    raise Cancelled()
                name = call["function"]["name"]
                raw = call["function"]["arguments"]
                try:
                    args = ToolBox.parse_args(raw)
                except Exception:
                    args = {}
                events.tool_begin(call["id"], name, args)
                key = name + ":" + json.dumps(args, sort_keys=True)
                if call["id"] in broken:
                    ok, out = False, _broken_call_note(name, broken[call["id"]], cut_off)
                elif key in seen and name in ("web_search", "fetch_url", "list_files", "read_file"):
                    ok, out = True, seen[key] + "\n[You already ran this exact call; use this result.]"
                else:
                    ok, out = self.toolbox.run(name, raw)
                    if ok:
                        seen[key] = out
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": name,
                                 "content": out, "_meta": {"ok": ok}})
                events.tool_end(call["id"], name, args, ok, out)
