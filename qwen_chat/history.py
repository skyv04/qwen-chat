"""What a chat has done so far, so a new message is read in context and stays in its own lane.

context_note() sums up the chat's recent requests, how each one ended and the files it made.
The agent attaches it to each new user message, so a short follow-up such as "try harder" is
understood even after older turns were trimmed out of the model's context.

ChatScope keeps other chats' files out of this one: list_files hides them and the file tools
refuse them, unless the user names them.

Everything is worked out from saved chat messages; nothing here imports the UI or the tools.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re

NOTE_HEAD = "Chat context (added by the app, not typed by the user):"
NOTE_SEP = "\n\n---\n"
SKIP_PARTS = ("__pycache__", "node_modules")
# Words too common in file, folder and chat names to show which one the user means.
GENERIC_WORDS = frozenset("""
    a an the of and or for with in on to my our your this that new old final draft copy improved
    better fixed updated version v1 v2 v3 v4 answer answers key keys quiz quizzes test tests exam
    worksheet worksheets sheet student students teacher parent output result results file files
    note notes image images picture pictures photo page pages document documents report data
    pdf json txt reading comprehension grade grades level first second third fourth fifth sixth
    seventh eighth 1st 2nd 3rd 4th 5th 6th kindergarten math english science homework practice
    assignment assignments exercise exercises lesson lessons class school work chat question
    questions story stories book books spec project projects misc temp tmp
""".split())
_AFFIRM = re.compile(r"(?:(?:yes|yeah|yep|yup|ok|okay|sure|please|go ahead|do it|correct|right|"
                     r"exactly|that one|those|use (?:it|that|them|those)|thanks|thank you)"
                     r"\b[\s.,!]*)+", re.I)
_CHANGED_LINE = re.compile(r"^Files created or changed in the workspace: (.*)$", re.M)
_DELETED_LINE = re.compile(r"^Files deleted: (.*)$", re.M)
_SIZED = re.compile(r"(?:^|, )(.+?) \([\d.]+ (?:bytes|KB|MB|GB)\)")
_READ_TOOLS = ("read_file", "read_quiz", "open_file")
_WRITE_VERBS = {"Created": "created", "Overwrote": "changed", "Appended to": "changed"}


def rel_path(path, root) -> str | None:
    """`path` relative to the workspace root, worked out without touching the disk, or None
    when it is not inside the workspace."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        return None
    p = os.path.expanduser(path.strip())
    if os.path.isabs(p):
        if root is None:
            return None
        base = os.path.normpath(str(root))
        p = os.path.normpath(p)
        if p != base and not p.startswith(base.rstrip(os.sep) + os.sep):
            return None
        p = os.path.relpath(p, base)
    p = os.path.normpath(p).replace(os.sep, "/")
    if p == ".." or p.startswith("../"):
        return None
    return p


def _visible(rel: str) -> bool:
    return rel != "." and not any(x.startswith(".") or x in SKIP_PARTS for x in rel.split("/"))


def changed_files_in(text: str) -> list[str]:
    """Files named by run_command's 'Files created or changed' line."""
    m = _CHANGED_LINE.search(text or "")
    return _SIZED.findall(m.group(1)) if m else []


def deleted_files_in(text: str) -> list[str]:
    m = _DELETED_LINE.search(text or "")
    if not m:
        return []
    listed = re.sub(r" and \d+ more\.?$", "", m.group(1)).removesuffix(".")
    return [p for p in listed.split(", ") if p]


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def call_files(name: str, args: dict, content: str, ok: bool, meta: dict | None, root) -> dict:
    """The workspace files one tool call made, changed, read, removed or moved."""
    out: dict[str, list] = {"created": [], "changed": [], "read": [], "removed": [], "moved": []}
    rel = lambda p: rel_path(p, root)
    content = content if isinstance(content, str) else ""
    files = (meta or {}).get("files")
    if isinstance(files, dict):
        for kind in ("created", "changed", "removed"):
            out[kind] += [r for p in files.get(kind) or [] if (r := rel(p))]
    elif name == "run_command":  # older chats: read back from the result text
        out["created"] += [r for p in changed_files_in(content) if (r := rel(p))]
        out["removed"] += [r for p in deleted_files_in(content) if (r := rel(p))]
    if not ok:
        return out
    path = rel(args.get("path"))
    if name in _READ_TOOLS and path:
        out["read"].append(path)
    elif name == "write_file" and path:
        verb = next((kind for verb, kind in _WRITE_VERBS.items()
                     if content.startswith(verb + " ")), None)
        if verb:
            out[verb].append(path)
    elif name == "edit_file" and path and content.startswith("Edited "):
        out["changed"].append(path)
    elif name == "download_file":
        m = re.match(r"Saved \S+ to (.+?) \([^()]*\)\.", content)
        if m and (r := rel(m[1])):
            out["created"].append(r)
    elif name in ("render_worksheet", "render_answer_key"):
        if (spec := rel(args.get("spec_path"))):
            out["read"].append(spec)
        if not isinstance(files, dict):
            out["created"] += [r for key in ("path", "answer_key_path") if (r := rel(args.get(key)))]
    elif name == "move_path":
        source = rel(args.get("source"))
        lead = f"Moved {source} to "
        if source and content.startswith(lead) and content.endswith("."):
            if (dest := rel(content[len(lead):-1])):
                out["moved"].append((source, dest))
    elif name == "delete_path" and path:
        out["removed"].append(path)
    return out


@dataclass
class Activity:
    """Workspace paths a chat used, each with the index of the last message that used it."""
    created: dict[str, int] = field(default_factory=dict)
    touched: dict[str, int] = field(default_factory=dict)  # created or changed
    read: dict[str, int] = field(default_factory=dict)


def activity(messages: list[dict], root) -> Activity:
    act = Activity()
    calls: dict[str, tuple[str, dict]] = {}
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                calls[call.get("id")] = (fn.get("name") or "", _args(fn.get("arguments")))
            continue
        if m.get("role") != "tool":
            continue
        name, args = calls.get(m.get("tool_call_id"), (m.get("name") or "", {}))
        meta = m.get("_meta") or {}
        content = m.get("content") if isinstance(m.get("content"), str) else ""
        ok = meta.get("ok", not content.startswith("Error"))
        found = call_files(name, args, content, bool(ok), meta, root)
        for gone in found["removed"]:
            for book in (act.created, act.touched, act.read):
                for key in [k for k in book if k == gone or k.startswith(gone + "/")]:
                    del book[key]
        for source, dest in found["moved"]:
            for book in (act.created, act.touched, act.read):
                for key in [k for k in book if k == source or k.startswith(source + "/")]:
                    del book[key]
                    book[dest + key[len(source):]] = i
            act.touched[dest] = i
        for path in found["created"]:
            act.created[path] = act.touched[path] = i
        for path in found["changed"]:
            act.touched[path] = i
        for path in found["read"]:
            act.read[path] = i
    return act


def _content(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c if isinstance(c, str) else ""


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _plain_line(text: str) -> str:
    """The first line of a reply without Markdown marks, keeping names like quiz_v2.pdf whole."""
    for line in text.splitlines():
        line = re.sub(r"\*+|`+|(?<!\w)_+|_+(?!\w)", "", line)
        line = re.sub(r"^[\s#>+-]+|[\s:]+$", "", line)
        if line:
            return line
    return ""


def outcome(turn: list[dict]) -> str:
    """How one turn (a user message and everything after it) ended, in a few words."""
    error = (turn[0].get("_meta") or {}).get("error") if turn else None
    if error:
        return "failed with an error: " + _short(error, 100)
    last = turn[-1] if len(turn) > 1 else None
    if last is None:
        return "no reply"
    if last.get("role") != "assistant" or last.get("tool_calls"):
        return "stopped before finishing"
    if (last.get("_meta") or {}).get("stopped"):
        return "stopped by the user"
    text = _content(last)
    if text.startswith("**Task incomplete.**"):
        detail = next((line[2:] for line in text.splitlines() if line.startswith(("- ", "> "))), "")
        detail = re.sub(r"`([^`]*)`", lambda m: m[1].rsplit("/", 1)[-1], detail)
        return "ended incomplete" + (": " + _short(detail, 150) if detail else "")
    if re.search(r"Paused after \d+ tool rounds", text):
        return "paused after too many steps"
    return "answered: " + _short(_plain_line(text), 110)


def _turns(messages: list[dict]) -> list[list[dict]]:
    starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    return [messages[s:e] for s, e in zip(starts, starts[1:] + [len(messages)])]


def context_note(messages: list[dict], root=None, hidden=()) -> str:
    """A short summary of the chat before its last user message, or "" when there is nothing
    worth noting (a first message, or plain answers with no files)."""
    turns = _turns(messages)[:-1]
    if not turns:
        return ""
    merged: list[list[dict]] = []
    for turn in turns:  # the same message sent twice in a row is one request
        if merged and " ".join(_content(merged[-1][0]).split()) == " ".join(_content(turn[0]).split()):
            merged[-1] = turn
        else:
            merged.append(turn)
    recent = [(turn, outcome(turn)) for turn in merged[-3:]]
    made: list[str] = []
    read: list[str] = []
    if root is not None:
        act = activity([m for turn in turns for m in turn], root)
        newest = lambda book: [r for r, _ in sorted(book.items(), key=lambda kv: -kv[1])
                               if _visible(r) and r not in hidden]
        made = [r + ("" if os.path.lexists(os.path.join(root, r)) else " (now missing)")
                for r in newest(act.touched)[:6]]
        # A read from another chat's folder was a wrong turn unless the user named the file.
        crowded = {os.path.dirname(r) for r in hidden} - {""}
        text = user_text(messages)
        read = [r for r in newest(act.read) if r not in act.touched
                and (os.path.dirname(r) not in crowded or mentions(r, text))][:3]
    if not made and not read and all(o.startswith("answered") for _, o in recent):
        return ""
    lines = [NOTE_HEAD, "Earlier requests in this chat, oldest first:"]
    lines += [f'{n}. "{_short(_content(turn[0]), 140)}" → {o}' for n, (turn, o) in enumerate(recent, 1)]
    if made:
        lines.append("Files this chat made or changed, newest first: " + ", ".join(made))
    if read:
        lines.append("Files this chat read: " + ", ".join(read))
    lines.append("If the new message is about this earlier work (a fix, 'try again', 'make it "
                 "better', a short reply), continue it with these requests and files instead of "
                 "starting over or looking in other folders.")
    return "\n".join(lines)


# -------------------------------------------------------------------------- chat scope

def _words(name: str) -> list[str]:
    return [w for w in re.split(r"[\s_\-.]+", name.casefold()) if w]


def _specific(words: list[str]) -> bool:
    return any(len(w) >= 3 and not w.isdigit() and re.sub(r"\d+", "", w) not in GENERIC_WORDS
               and w not in GENERIC_WORDS for w in words)


def _token(token: str, text: str) -> bool:
    return bool(re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", text))


def _phrase(words: list[str], text: str) -> bool:
    return bool(re.search(r"(?<![\w-])" + r"[\s_-]+".join(map(re.escape, words)) + r"(?![\w-])", text))


def mentions(rel: str, text: str) -> bool:
    """Whether casefolded user text names this workspace file: by its path, its file name, a
    folder path, or a distinctive name (for example "space adventure" for
    homework/space-adventure-key.pdf). Generic names like "first grade" don't count."""
    rel = rel.casefold()
    if rel in text:
        return True
    parts = rel.split("/")
    if _token(parts[-1], text):
        return True
    if any(_token("/".join(parts[:k]), text) for k in range(2, len(parts))):
        return True
    stem = parts[-1].rsplit(".", 1)[0] if "." in parts[-1].lstrip(".") else parts[-1]
    for name in [*parts[:-1], stem]:
        words = _words(name)
        if len(words) == 1:
            if len(name) >= 5 and _specific(words) and _token(name, text):
                return True
            continue
        if name == stem and _token(name, text):  # a whole file name, typed without extension
            return True
        for k in range(len(words), 1, -1):
            if _specific(words[:k]) and _phrase(words[:k], text):
                return True
    return False


def names_chat(title: str, text: str) -> bool:
    """Whether casefolded user text names another chat by a distinctive title."""
    title = re.sub(r"[\s.…]+$", "", (title or "").casefold()).strip()
    words = _words(title)
    if not words:
        return False
    if len(words) == 1:
        return len(title) >= 4 and (any(c.isdigit() for c in title) or _specific(words)) \
            and _token(title, text)
    return _specific(words) and _phrase(words, text)


def user_text(messages: list[dict]) -> str:
    """Everything the user typed in a chat, casefolded. A short "yes" also counts the reply it
    answers, so "yes" to "Should I use apples.png from your other chat?" names apples.png."""
    parts = []
    for i, m in enumerate(messages):
        if m.get("role") != "user":
            continue
        text = _content(m)
        parts.append(text)
        if len(text) <= 60 and _AFFIRM.fullmatch(text.strip()):
            asked = next((_content(p) for p in reversed(messages[:i]) if p.get("role") == "assistant"
                          and _content(p).strip()), "")
            parts.append(asked)
    return "\n".join(parts).casefold()


class ChatScope:
    """Other chats' files, hidden from this chat unless its user names them."""

    def __init__(self, root, foreign: dict[str, str] | None = None):
        self.root = Path(root)
        self.foreign = dict(foreign or {})  # workspace-relative path -> owning chat's title

    @classmethod
    def build(cls, root, messages: list[dict], others: list[tuple[str, Activity]]) -> "ChatScope":
        """`others`: (title, activity) of every other chat, most recently used first."""
        mine = activity(messages, root)
        text = user_text(messages)
        foreign: dict[str, str] = {}
        for title, act in others:
            if names_chat(title, text):
                continue
            for rel in act.created:
                if rel not in foreign and rel not in mine.touched and not mentions(rel, text):
                    foreign[rel] = title
        return cls(root, foreign)

    def _rel(self, real) -> str | None:
        rel = rel_path(str(real), self.root)
        return None if rel in (None, ".") else rel

    def owner(self, real) -> str | None:
        """The other chat that made this file, or every file in this folder."""
        rel = self._rel(real)
        if rel is None:
            return None
        if rel in self.foreign:
            return self.foreign[rel] if os.path.lexists(real) else None
        if not os.path.isdir(real) or not any(r.startswith(rel + "/") for r in self.foreign):
            return None
        owners: list[str] = []
        for top, dirs, files in os.walk(real):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if name.startswith("."):
                    continue
                owner = self.foreign.get(self._rel(os.path.join(top, name)) or "")
                if owner is None or len(owners) >= 2000:
                    return None
                owners.append(owner)
        return max(set(owners), key=owners.count) if owners else None

    def inside(self, real) -> str | None:
        """For deleting or moving: the other chat that made this file or any file in it."""
        rel = rel_path(str(real), self.root)
        if rel is None:
            return None
        if rel in self.foreign:
            return self.foreign[rel] if os.path.lexists(real) else None
        prefix = "" if rel == "." else rel + "/"
        return next((title for r, title in self.foreign.items()
                     if r.startswith(prefix) and os.path.lexists(self.root / r)), None)

    def claim(self, paths) -> None:
        """Files this chat just made or changed are its own from now on."""
        for p in paths:
            self.foreign.pop(p, None)


class ChatFiles:
    """Every saved chat's file activity, re-read only when a chat file changes."""

    def __init__(self, chats_dir):
        self.dir = Path(chats_dir)
        self._cache: dict[tuple[str, str], tuple[tuple[int, int], str, Activity]] = {}

    def others(self, exclude_id: str, root) -> list[tuple[str, Activity]]:
        found = []
        for f in self.dir.glob("*.json"):
            if f.stem == exclude_id:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            found.append((st.st_mtime_ns, f, (st.st_mtime_ns, st.st_size)))
        out = []
        for _, f, stamp in sorted(found, key=lambda x: x[0], reverse=True):
            key = (f.name, str(root))
            hit = self._cache.get(key)
            if hit is None or hit[0] != stamp:
                try:
                    chat = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(chat, dict) or not isinstance(chat.get("messages"), list):
                    continue
                hit = self._cache[key] = (stamp, str(chat.get("title") or f.stem),
                                          activity(chat["messages"], root))
            if hit[2].created:
                out.append((hit[1], hit[2]))
        return out
