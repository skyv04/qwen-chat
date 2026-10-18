"""The tool-using conversation loop."""
from __future__ import annotations

import copy
import json
import threading
import time
from typing import Protocol

from .llm import Cancelled, ChatStream, LLMError, ServerManager
from .tools import ToolBox

MAX_TOOL_ROUNDS = 12
CHARS_PER_TOKEN = 3.0      # conservative; real English text is ~3.5-4
OLD_TOOL_STUB = "[Earlier tool output removed to save context space.]"


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
            "file names with the right extension (.md, .txt, .csv, .py, .html, ...).")
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
            "no keyboard input, so use non-interactive options, and keep files in the workspace.")
    parts.append(
        "\n## Style\nAnswer in Markdown. Be direct and helpful; use headings, lists, tables and "
        "fenced code blocks (with a language) when they make the answer clearer.")
    return "\n".join(parts)


def _size(msg: dict) -> int:
    n = len(msg.get("content") or "") + 16
    for tc in msg.get("tool_calls") or []:
        n += len(tc["function"]["name"]) + len(tc["function"]["arguments"]) + 16
    return n


def clean_for_api(messages: list[dict]) -> list[dict]:
    """Drops UI-only fields; earlier reasoning is not sent back (Qwen's template ignores it)."""
    out = []
    for m in messages:
        c = {k: v for k, v in m.items() if not k.startswith("_") and k != "reasoning_content"}
        if c.get("role") == "assistant" and c.get("content") is None:
            c["content"] = ""
        out.append(c)
    return out


def fit_context(messages: list[dict], budget_chars: int) -> list[dict]:
    """Shrinks history to fit: first old tool outputs, then whole old turns, then current tool outputs."""
    msgs = copy.deepcopy(messages)
    total = lambda: sum(_size(m) for m in msgs)
    if total() <= budget_chars:
        return msgs
    last_user = max((i for i, m in enumerate(msgs) if m["role"] == "user"), default=0)
    for i, m in enumerate(msgs[:last_user]):
        if m["role"] == "tool" and len(m.get("content") or "") > len(OLD_TOOL_STUB):
            m["content"] = OLD_TOOL_STUB
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
        max_out = min(4096 if thinking else 3072, n_ctx // 3)
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
            if result["finish_reason"] == "length" and not msg.get("tool_calls"):
                msg["content"] = (msg.get("content") or "") + "\n\n*[Stopped: reached the response length limit.]*"
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
                if key in seen and name in ("web_search", "fetch_url", "list_files", "read_file"):
                    ok, out = True, seen[key] + "\n[You already ran this exact call; use this result.]"
                else:
                    ok, out = self.toolbox.run(name, raw)
                    if ok:
                        seen[key] = out
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": name,
                                 "content": out, "_meta": {"ok": ok}})
                events.tool_end(call["id"], name, args, ok, out)
