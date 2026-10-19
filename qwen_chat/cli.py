"""Terminal mode: `qwen-chat --ask "question"` runs one agent turn and prints it."""
from __future__ import annotations

import sys
import threading

from .agent import Agent
from .config import Settings
from .llm import Cancelled, LLMError, ServerError, ServerManager
from .tools import ToolBox, Workspace


def run(question: str, web: bool, files: bool, think: bool, keep_server: bool, yes: bool) -> int:
    settings = Settings()
    tty = sys.stdin.isatty()

    def confirm(title: str, detail: str) -> bool:
        if yes:
            return True
        if not tty:
            print(f"\n[denied: {detail} (run with --yes to allow)]", file=sys.stderr)
            return False
        return input(f"\n{title} {detail} [y/N] ").strip().lower() in ("y", "yes")

    # Same rules as the app: replaced and deleted files go to the Trash, and only risky
    # commands and big installs ask, unless the settings say to ask for everything.
    ws = Workspace(settings.workspace_path,
                   confirm=confirm if settings["ask_file_changes"] else None)
    server = ServerManager(settings)

    def approve(title: str, detail: str, code: str, action: str, reason: str = "") -> bool:
        why = f"{reason}\n" if reason else ""
        return confirm(title, f"{why}{detail}\n\n    {code}\n\n{action}?")

    toolbox = ToolBox(ws, web=web, files=files, commands=settings["commands"], approve=approve,
                      auto_approve=lambda: not settings["ask_commands"])
    agent = Agent(server, toolbox)
    out = sys.stdout

    class Events:
        def status(self, text):
            print(f"\x1b[2m{text}\x1b[0m", file=sys.stderr)

        def round_begin(self):
            self._thinking = False

        def reasoning(self, delta):
            if not self._thinking:
                out.write("\x1b[2m[thinking] ")
                self._thinking = True
            out.write(delta)
            out.flush()

        def content(self, delta):
            if self._thinking:
                out.write("\x1b[0m\n\n")
                self._thinking = False
            out.write(delta)
            out.flush()

        def tool_begin(self, call_id, name, args):
            out.write(f"\n\x1b[36m⚙ {ToolBox.describe(name, args, running=True)}…\x1b[0m\n")

        def tool_end(self, call_id, name, args, ok, result):
            first = result.strip().splitlines()[0] if result.strip() else ""
            mark = "\x1b[32m✓" if ok else "\x1b[31m✗"
            out.write(f"  {mark} {first[:110]}\x1b[0m\n")

        def round_end(self, message, timings):
            if self._thinking:
                out.write("\x1b[0m")
            tps = timings.get("predicted_per_second")
            if tps and not message.get("tool_calls"):
                out.write(f"\n\n\x1b[2m[{tps:.1f} tok/s]\x1b[0m\n")

    messages = [{"role": "user", "content": question}]
    cancel = threading.Event()
    try:
        agent.respond(messages, Events(), cancel, thinking=think)
        return 0
    except (ServerError, LLMError) as e:
        print(f"\nError: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, Cancelled):
        cancel.set()
        agent.abort()
        return 130
    finally:
        if server.owned and not keep_server:
            server.stop()
