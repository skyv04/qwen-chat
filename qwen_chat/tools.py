"""Tools the model can call: web search/fetch/download, a sandboxed workspace folder, and
(when enabled) programs it runs or installs.

Every file operation goes through Workspace.resolve(), which follows symlinks and
".." with realpath() and rejects anything that does not land inside the workspace.
The skill tools also manage a fixed, shared library, with explicit approval for saves.
Deleted and replaced files go to the desktop Trash, so they can be restored.
Web requests are limited to public http(s) addresses, so a page cannot steer the
model into probing localhost services (the model server itself listens there).
Routine commands and small installs can run without asking; commands that look risky
(admin rights, files outside the workspace, stopping programs, ...) and big installs are
shown to the user verbatim and run only if they allow them.
"""
from __future__ import annotations

import codecs
import contextlib
import hashlib
import html
import inspect
import ipaddress
import itertools
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from .skills import SkillError, SkillRegistry, package_files, write_package

USER_AGENT = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TIMEOUT = 25
FETCH_CHUNK = 6000            # characters of page text per fetch_url call
MAX_PAGE_BYTES = 6 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
MAX_READ_CHARS = 12000
MAX_WRITE_BYTES = 4 * 1024 * 1024
MAX_LIST_ENTRIES = 300


class ToolError(Exception):
    """A failure the model is shown so it can recover (bad path, HTTP error, ...)."""


# The bracketed notes the chat history uses where it shortened something (see agent.py).
# A model can copy one into a file, which silently replaces real content, so writes refuse it.
CONTEXT_MARKER = re.compile(
    r"\[[^\[\]\n]{0,80}(?:to save context space|to fit the context window)[^\[\]\n]{0,120}\]")


def _refuse_placeholder(text: str, name: str, existed: bool) -> None:
    m = CONTEXT_MARKER.search(text)
    if m:
        raise ToolError(
            f"Nothing was saved: the new text contains {m[0]!r}, a note from shortened chat "
            "history, not real content. "
            + (f"{name} keeps its previous content. Use read_file to see it, then change part of "
               "it with edit_file or write the complete content." if existed else
               f"Write the complete content of {name} instead."))


def json_error(exc: json.JSONDecodeError) -> str:
    """Where JSON breaks (message, line, column, excerpt), so one edit can fix it."""
    lines = exc.doc.splitlines() if isinstance(exc.doc, str) else []
    text = lines[exc.lineno - 1] if 0 < exc.lineno <= len(lines) else ""
    start = max(0, exc.colno - 60)
    excerpt = text[start:start + 100].strip()
    if excerpt:
        excerpt = ("…" if start else "") + excerpt + ("…" if start + 100 < len(text) else "")
    return f"{exc.msg} at line {exc.lineno}, column {exc.colno}" + (f": {excerpt}" if excerpt else "")


def _json_fix_hint(exc: json.JSONDecodeError) -> str:
    doc = exc.doc if isinstance(exc.doc, str) else ""
    if CONTEXT_MARKER.search(doc):
        return ("The file contains a bracketed note copied from shortened chat history, so real "
                "content is missing. Write the complete JSON again with write_file.")
    if exc.pos >= len(doc.rstrip()):
        return "The JSON stops before it is complete: append the rest, or write the complete file."
    return ("Fix just that spot with edit_file (usually a missing comma, quote or bracket); do "
            "not rewrite the whole file.")


def _find_once(text: str, old: str, name: str) -> tuple[int, int]:
    """The one place `old` occurs: exactly, or else with any run of spaces/newlines matching."""
    count = text.count(old)
    if count == 1:
        start = text.index(old)
        return start, start + len(old)
    if not count:
        found = [m.span() for m in re.finditer(r"\s+".join(map(re.escape, old.split())), text)]
        if len(found) == 1:
            return found[0]
        count = len(found)
    if count:
        raise ToolError(f"old_text matches {count} places in {name}. Include more of the nearby "
                        "text so it matches exactly one place.")
    raise ToolError(f"old_text was not found in {name}. Use read_file and copy the current text "
                    "exactly; the file may have changed.")


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _home(p) -> str:
    s, home = str(p), os.path.expanduser("~")
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


def _duration(seconds: int) -> str:
    if seconds < 120:
        return f"{seconds} seconds"
    return f"{round(seconds / 60)} minutes"


def _text_page(text: str, name: str, start_line: int, tool: str = "read_file") -> str:
    lines = text.splitlines()
    if not lines:
        return f"{name} is empty."
    start = max(1, _as_int(start_line, 1))
    if start > len(lines):
        raise ToolError(f"start_line {start} is past the end ({len(lines)} lines).")
    out, used = [], 0
    for line in lines[start - 1:]:
        if len(line) > MAX_READ_CHARS:
            line = line[:MAX_READ_CHARS] + " [line truncated]"
        if out and used + len(line) > MAX_READ_CHARS:
            break
        out.append(line)
        used += len(line) + 1
    end = start + len(out) - 1
    footer = (f"\n[{len(lines) - end} more lines. Call {tool} with start_line={end + 1} to continue.]"
              if end < len(lines) else "")
    return f"{name} (lines {start}-{end} of {len(lines)}):\n" + "\n".join(out) + footer


# --------------------------------------------------------------------------- workspace

def trash_home() -> Path:
    """The freedesktop.org home Trash, which file managers show as "Trash"."""
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "Trash"


class Workspace:
    def __init__(self, root: str | os.PathLike, confirm: Callable[[str, str], bool] | None = None,
                 trash_dir: str | os.PathLike | None = None):
        root = Path(os.path.expanduser(str(root)))
        root.mkdir(parents=True, exist_ok=True)
        self.root = Path(os.path.realpath(root))
        # confirm(title, detail) -> bool; asked before overwriting or deleting.
        self.confirm = confirm or (lambda title, detail: True)
        self.trash_dir = Path(trash_dir) if trash_dir else trash_home()
        # on_trash(original, trashed): a file went to the Trash (the UI offers Restore).
        self.on_trash: Callable[[Path, Path], None] = lambda original, trashed: None
        # Files Qwen wrote, with (mtime, size) right after: replacing one of those unchanged
        # needs no backup, so rewriting its own script doesn't fill the Trash.
        self._own: dict[Path, tuple[int, int]] = {}

    def mark_own(self, f: Path) -> None:
        try:
            st = os.lstat(f)
        except OSError:
            return
        self._own[f] = (st.st_mtime_ns, st.st_size)

    def _is_own(self, f: Path) -> bool:
        try:
            st = os.lstat(f)
        except OSError:
            return False
        return self._own.get(f) == (st.st_mtime_ns, st.st_size)

    def trash(self, p: Path) -> Path:
        """Moves p to the Trash (freedesktop.org spec) and returns where it went."""
        files, info = self.trash_dir / "files", self.trash_dir / "info"
        files.mkdir(parents=True, exist_ok=True)
        info.mkdir(parents=True, exist_ok=True)
        stem, suffix = (p.name, "") if p.is_dir() else (p.stem or p.name, p.suffix if p.stem else "")
        n = 1
        while True:
            name = p.name if n == 1 else f"{stem}.{n}{suffix}"
            record = info / (name + ".trashinfo")
            try:  # the .trashinfo file reserves the name
                fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                n += 1
                continue
            if os.path.lexists(files / name):
                os.close(fd)
                os.unlink(record)
                n += 1
                continue
            break
        with os.fdopen(fd, "w") as fh:
            fh.write("[Trash Info]\nPath=" + urllib.parse.quote(str(p)) + "\nDeletionDate="
                     + time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
        try:
            shutil.move(str(p), str(files / name))
        except BaseException:
            os.unlink(record)
            raise
        self._own.pop(p, None)
        self.on_trash(p, files / name)
        return files / name

    def restore(self, original: Path, trashed: Path) -> None:
        """Puts a trashed file back; whatever is at its place now goes to the Trash."""
        if not os.path.lexists(trashed):
            raise ToolError("It's no longer in the Trash.")
        if os.path.lexists(original):
            self.trash(original)
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(trashed), str(original))
        try:
            os.unlink(self.trash_dir / "info" / (trashed.name + ".trashinfo"))
        except OSError:
            pass

    def resolve(self, path) -> Path:
        if path is None:
            path = "."
        if not isinstance(path, str):
            raise ToolError("Paths must be strings.")
        p = path.strip() or "."
        if "\x00" in p:
            raise ToolError("Invalid path.")
        p = os.path.expanduser(p)
        candidate = p if os.path.isabs(p) else os.path.join(self.root, p)
        real = Path(os.path.realpath(candidate))
        if real != self.root and self.root not in real.parents:
            raise ToolError(f"Access denied: '{path}' is outside the workspace. "
                            "Only files inside the workspace folder can be used; "
                            "give paths relative to it, like 'notes/todo.md'.")
        return real

    def display(self, real: Path) -> str:
        rel = real.relative_to(self.root).as_posix()
        return "the workspace root" if rel == "." else rel

    # -- operations -------------------------------------------------------------------
    def list_files(self, path: str = ".", recursive: bool = False) -> str:
        d = self.resolve(path)
        if not d.exists():
            raise ToolError(f"'{path}' does not exist.")
        if not d.is_dir():
            raise ToolError(f"'{path}' is a file, not a folder.")
        entries: list[str] = []
        more = False

        def add(p: Path) -> bool:
            nonlocal more
            if len(entries) >= MAX_LIST_ENTRIES:
                more = True
                return False
            rel = p.relative_to(self.root).as_posix()
            if p.is_symlink():
                entries.append(f"{rel} (link)")
            elif p.is_dir():
                entries.append(f"{rel}/")
            else:
                try:
                    entries.append(f"{rel}  ({human_size(p.stat().st_size)})")
                except OSError:
                    entries.append(rel)
            return True

        if recursive:
            for top, dirs, files in os.walk(d, followlinks=False):
                dirs.sort()
                for name in dirs + sorted(files):
                    if not add(Path(top) / name):
                        break
                if more:
                    break
        else:
            for p in sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if not add(p):
                    break
        where = self.display(d)
        if not entries:
            return f"{where} is empty."
        out = [f"Contents of {where}:"] + entries
        if more:
            out.append(f"... more entries not shown (limit {MAX_LIST_ENTRIES}).")
        return "\n".join(out)

    def read_file(self, path: str, start_line: int = 1) -> str:
        f = self.resolve(path)
        if not f.exists():
            raise ToolError(f"'{path}' does not exist. Use list_files to see what is there.")
        if f.is_dir():
            raise ToolError(f"'{path}' is a folder. Use list_files to see its contents.")
        if f.suffix.lower() == ".pdf":
            text = pdf_to_text(f)
        else:
            with open(f, "rb") as fh:
                raw = fh.read(MAX_PAGE_BYTES)
            if b"\x00" in raw[:8192]:
                raise ToolError(f"'{path}' is a binary file ({human_size(f.stat().st_size)}); "
                                "its contents can't be shown as text.")
            text = raw.decode("utf-8", errors="replace")
        return _text_page(text, self.display(f), start_line)

    def write_file(self, path: str, content: str, append: bool = False) -> str:
        if not isinstance(content, str):
            content = json.dumps(content, indent=2) if isinstance(content, (dict, list)) else str(content)
        f = self.resolve(path)
        if f == self.root or f.is_dir():
            raise ToolError(f"'{path}' is a folder; give a file name.")
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolError(f"Content is too large ({human_size(len(data))}).")
        existed = f.exists()
        name = self.display(f)
        _refuse_placeholder(content, name, existed)
        if (existed and not append and f.is_file() and f.stat().st_size == len(data)
                and f.read_bytes() == data):
            return (f"Unchanged: {name} already has exactly this content. If a tool reported an "
                    "error for this file, change the value that error names before retrying.")
        backup = ""
        if existed and not append:
            if not self.confirm("Replace a file?",
                                f"Qwen wants to overwrite {name} ({human_size(f.stat().st_size)}) "
                                f"with new content ({human_size(len(data))}). The old version "
                                "goes to the Trash."):
                raise ToolError(f"The user declined overwriting {name}. "
                                "Ask what they want, or save under a new name.")
            if not self._is_own(f) and f.stat().st_size:
                self.trash(f)
                backup = " The old version is in the Trash."
        f.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if append else os.O_TRUNC)
        fd = os.open(f, flags, 0o644)
        with os.fdopen(fd, "wb") as fh:
            if append and existed and f.stat().st_size and not _ends_with_newline(f):
                fh.write(b"\n")
            fh.write(data)
        self.mark_own(f)
        verb = "Appended to" if append and existed else ("Overwrote" if existed else "Created")
        note = self._json_note(f, None if append else content)
        return f"{verb} {name} ({human_size(len(data))}).{backup}{note}"

    def _json_note(self, f: Path, text: str | None = None) -> str:
        """For a .json file that doesn't parse yet: where it breaks, so the fix is one edit."""
        if f.suffix.lower() != ".json":
            return ""
        try:
            if text is None:
                text = f.read_bytes()[:MAX_WRITE_BYTES + 1].decode("utf-8", errors="replace")
            json.loads(text, strict=False)
        except json.JSONDecodeError as exc:
            return (f" Note: {self.display(f)} is not valid JSON yet ({json_error(exc)}). If more "
                    "parts follow, append them; otherwise fix that spot with edit_file.")
        except (OSError, ValueError, RecursionError):
            pass
        return ""

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise ToolError("old_text and new_text must be text.")
        f = self.resolve(path)
        if not f.exists():
            raise ToolError(f"'{path}' does not exist. Use write_file to create it.")
        if not f.is_file():
            raise ToolError(f"'{path}' is a folder; give a file name.")
        name = self.display(f)
        if not old_text.strip():
            raise ToolError("old_text is empty. Copy the exact text to change from read_file, "
                            "with enough nearby words to match one place.")
        _refuse_placeholder(new_text, name, True)
        size = f.stat().st_size
        if size > MAX_WRITE_BYTES:
            raise ToolError(f"{name} is too large to edit ({human_size(size)}).")
        raw = f.read_bytes()
        try:
            if b"\x00" in raw[:8192]:
                raise UnicodeError
            text = raw.decode("utf-8")
        except UnicodeError:
            raise ToolError(f"{name} is not a text file, so edit_file can't change it.") from None
        start, end = _find_once(text, old_text, name)
        if text[start:end] != old_text:
            # Matched with different spacing: the file keeps its own surrounding whitespace.
            lead = old_text[:len(old_text) - len(old_text.lstrip())]
            trail = old_text[len(old_text.rstrip()):]
            if lead and new_text.startswith(lead):
                new_text = new_text[len(lead):]
            if trail and new_text.endswith(trail):
                new_text = new_text[:len(new_text) - len(trail)]
        new = text[:start] + new_text + text[end:]
        if new == text:
            return f"Unchanged: {name} already has this text."
        is_json = f.suffix.lower() == ".json"
        was_valid = False
        if is_json:
            try:
                json.loads(text, strict=False)
                was_valid = True
            except (ValueError, RecursionError):
                pass
            if was_valid:
                try:
                    json.loads(new, strict=False)
                except json.JSONDecodeError as exc:
                    raise ToolError(f"Not changed: this edit would break the JSON in {name} "
                                    f"({json_error(exc)}). Keep quotes, commas and brackets "
                                    "balanced in new_text.") from None
                except (ValueError, RecursionError):
                    pass
        if not self.confirm("Change a file?",
                            f"Qwen wants to edit {name} ({human_size(size)}). The old version "
                            "goes to the Trash."):
            raise ToolError(f"The user declined changing {name}. "
                            "Ask what they want, or save under a new name.")
        backup = ""
        if not self._is_own(f) and size:
            self.trash(f)
            backup = " The old version is in the Trash."
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(new.encode("utf-8"))
        self.mark_own(f)
        note = self._json_note(f, new)
        if is_json and not was_valid and not note:
            note = f" {name} is now valid JSON."
        left = CONTEXT_MARKER.search(new)
        if left:
            note += (f" {name} still contains {left[0]!r}, a note from shortened chat history; "
                     "replace it with the real content.")
        return (f"Edited {name} at line {text.count(chr(10), 0, start) + 1} "
                f"({end - start:,} characters replaced with {len(new_text):,}).{backup}{note}")

    def make_directory(self, path: str) -> str:
        d = self.resolve(path)
        if d.exists() and not d.is_dir():
            raise ToolError(f"'{path}' already exists as a file.")
        d.mkdir(parents=True, exist_ok=True)
        return f"Folder {self.display(d)} is ready."

    def move_path(self, source: str, destination: str) -> str:
        src, dst = self.resolve(source), self.resolve(destination)
        if src == self.root:
            raise ToolError("The workspace root can't be moved.")
        if not src.exists():
            raise ToolError(f"'{source}' does not exist.")
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            raise ToolError(f"'{self.display(dst)}' already exists. Pick another destination.")
        if dst == src or src in dst.parents:
            raise ToolError("A folder can't be moved into itself.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"Moved {self.display(src)} to {self.display(dst)}."

    def delete_path(self, path: str) -> str:
        p = self.resolve(path)
        if p == self.root:
            raise ToolError("The workspace root can't be deleted.")
        if not p.exists():
            raise ToolError(f"'{path}' does not exist.")
        name = self.display(p)
        if p.is_dir():
            count = sum(len(files) for _, _, files in os.walk(p))
            detail = f"Qwen wants to delete the folder {name} and the {count} file(s) in it."
        else:
            detail = f"Qwen wants to delete {name} ({human_size(p.stat().st_size)})."
        if not self.confirm("Delete from workspace?", detail + " It goes to the Trash."):
            raise ToolError(f"The user declined deleting {name}.")
        self.trash(p)
        return f"Moved {name} to the Trash."


def _ends_with_newline(f: Path) -> bool:
    with open(f, "rb") as fh:
        fh.seek(-1, os.SEEK_END)
        return fh.read(1) == b"\n"


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def pdf_to_text(path: Path, *, raw: bool = False) -> str:
    if not shutil.which("pdftotext"):
        raise ToolError("PDF support needs pdftotext (poppler-utils), which isn't installed.")
    try:
        res = subprocess.run(["pdftotext", *(["-raw"] if raw else []), "-q", str(path), "-"],
                             capture_output=True, timeout=90, stdin=subprocess.DEVNULL,
                             env=child_env())
    except subprocess.TimeoutExpired:
        raise ToolError("Extracting text from the PDF took too long.") from None
    if res.returncode:
        raise ToolError("PDF text extraction failed: "
                        + res.stderr.decode("utf-8", errors="replace")[-1000:])
    text = res.stdout.decode("utf-8", errors="replace")
    if not text.strip():
        raise ToolError("The PDF has no extractable text (it may be scanned images).")
    text = re.sub(r"[ \t]{2,}", " ", text if raw else text.replace("\f", "\n"))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", text)).strip()


# --------------------------------------------------------------------------- web

def check_public_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ToolError("Only http:// and https:// URLs are allowed.")
    host = parts.hostname
    if not host:
        raise ToolError(f"'{url}' has no host name.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise ToolError(f"'{url}' has an invalid port.") from None
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ToolError(f"Could not resolve {host}: {e.strerror}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise ToolError(f"Blocked: {host} is a local or private address ({ip}). "
                            "Only public internet sites can be fetched.")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


def _request(url: str, data: bytes | None = None, extra: dict | None = None):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    headers.update(extra or {})
    check_public_url(url)
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        return _opener.open(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP {e.code} {e.reason} from {url}") from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ToolError):
            raise reason from None
        raise ToolError(f"Could not reach {url}: {reason}") from None
    except (TimeoutError, socket.timeout):
        raise ToolError(f"{url} timed out.") from None
    except OSError as e:
        raise ToolError(f"Could not reach {url}: {e}") from None


_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "nav", "footer",
              "aside", "form", "button", "select", "object", "canvas"}
_BLOCK_TAGS = {"p", "div", "section", "article", "main", "header", "tr", "table", "ul", "ol",
               "dl", "dt", "dd", "blockquote", "pre", "figure", "figcaption", "hr", "address",
               "details", "summary", "caption"}
_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "col"}


_JUNK_ATTR = re.compile(
    r"(^|[\s_-])(nav|navbox|navbar|menu|sidebar|breadcrumbs?|cookie|banner|footer|toc|"
    r"catlinks|mw-editsection|mw-jump|interlanguage|language-?list|vector-|noprint|"
    r"share|social|subscribe|newsletter|advert|ads?|promo|related|comments?|skip-?link)([\s_-]|$)", re.I)
_JUNK_ROLES = {"navigation", "banner", "contentinfo", "search", "menu", "menubar", "dialog"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0              # depth inside _SKIP_TAGS
        self._junk: tuple[str, int] | None = None   # (tag, nesting) of a skipped junk element
        self.in_title = False
        self.title = ""
        self.main_ranges: list[tuple[int, int]] = []
        self._main_stack: list[tuple[str, int]] = []
        self.links: list[tuple[int, str, str]] = []   # (position in parts, text, href)
        self._link: tuple[str, int] | None = None

    @staticmethod
    def _is_junk(tag, attrs) -> bool:
        if tag in _VOID_TAGS or tag in ("html", "body", "main", "article"):
            return False
        a = dict(attrs)
        if "hidden" in a or a.get("aria-hidden") == "true" or (a.get("role") or "") in _JUNK_ROLES:
            return True
        return bool(_JUNK_ATTR.search(f"{a.get('class') or ''} {a.get('id') or ''}"))

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = not self.title  # svg <title>s later in the page are not the page title
            return
        if self._junk:
            if tag == self._junk[0]:
                self._junk = (tag, self._junk[1] + 1)
            return
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if self._is_junk(tag, attrs):
            self._junk = (tag, 1)
            return
        if tag in ("main", "article"):
            self._main_stack.append((tag, len(self.parts)))
        if tag == "a":
            href = dict(attrs).get("href")
            self._link = (href, len(self.parts)) if href else None
        if tag == "br":
            self.parts.append("\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
            return
        if self._junk:
            if tag == self._junk[0]:
                depth = self._junk[1] - 1
                self._junk = (tag, depth) if depth else None
            return
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "a" and self._link:
            href, start = self._link
            self._link = None
            lines = [re.sub(r"\s+", " ", ln).strip(" #-|") for ln in "".join(self.parts[start:]).split("\n")]
            lines = [ln for ln in lines if ln]
            # A card link often wraps headline + summary; the headline is the first line.
            text = next((ln for ln in lines if len(ln) >= 20), " ".join(lines))
            if len(text) >= 20:
                self.links.append((start, text, href))
        if tag in ("main", "article"):
            for i in range(len(self._main_stack) - 1, -1, -1):
                if self._main_stack[i][0] == tag:
                    _, start = self._main_stack.pop(i)
                    self.main_ranges.append((start, len(self.parts)))
                    break
        if re.fullmatch(r"h[1-6]", tag) or tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr") and not self.skip and not self._junk:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip and not self._junk:
            self.parts.append(data)


def _tidy(text: str) -> str:
    lines = [re.sub(r"[ \t\r\f\v\u00a0]+", " ", ln).strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln not in ("-", "|", "| |")]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _parse_html(page: str, base: str = "") -> tuple[str, str, list[tuple[str, str]]]:
    """Returns (title, readable text, [(link text, absolute URL)]) for an HTML page."""
    p = _TextExtractor()
    try:
        p.feed(page)
        p.close()
    except Exception:  # malformed markup: keep whatever was parsed
        pass
    full = _tidy("".join(p.parts))
    best, best_range = "", None
    for start, end in p.main_ranges:
        cand = _tidy("".join(p.parts[start:end]))
        if len(cand) > len(best):
            best, best_range = cand, (start, end)
    text = best if len(best) >= 400 else full
    if len(best) < 400:
        best_range = None
    # Headline-like links, those inside the main content first.
    here = urllib.parse.urldefrag(base)[0]
    seen, inside, outside = set(), [], []
    for pos, label, href in p.links:
        url = urllib.parse.urldefrag(urllib.parse.urljoin(base, href.strip()))[0]
        if not url.startswith(("http://", "https://")) or url == here or url in seen:
            continue
        seen.add(url)
        in_main = best_range is None or best_range[0] <= pos < best_range[1]
        (inside if in_main else outside).append((label, url))
    return _tidy(html.unescape(p.title)), text, inside + outside


def html_to_text(page: str) -> tuple[str, str]:
    title, text, _ = _parse_html(page)
    return title, text


def _links_section(links: list[tuple[str, str]], limit: int = 25, budget: int = 2600) -> str:
    out, used = [], 0
    for label, url in links[:limit]:
        label = label if len(label) <= 110 else label[:109] + "…"
        line = f"- {label}: {url}"
        if used + len(line) > budget:
            break
        out.append(line)
        used += len(line) + 1
    return ("\n\nLinks on this page:\n" + "\n".join(out)) if out else ""


class _DDGLiteParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._in_link = False
        self._in_snippet = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and "result-link" in cls:
            self._in_link = True
            self.results.append({"title": "", "url": a.get("href") or "", "snippet": ""})
        elif tag == "td" and "result-snippet" in cls and self.results:
            self._in_snippet = True

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_link = False
        elif tag == "td":
            self._in_snippet = False

    def handle_data(self, data):
        if self._in_link:
            self.results[-1]["title"] += data
        elif self._in_snippet:
            self.results[-1]["snippet"] += data


def _unwrap_ddg(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    parts = urllib.parse.urlsplit(url)
    if parts.hostname and parts.hostname.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        q = urllib.parse.parse_qs(parts.query)
        if q.get("uddg"):
            return q["uddg"][0]
    return url


def _search_ddg(query: str) -> list[dict]:
    body = urllib.parse.urlencode({"q": query}).encode()
    with _request("https://lite.duckduckgo.com/lite/", data=body,
                  extra={"Content-Type": "application/x-www-form-urlencoded",
                         "Referer": "https://lite.duckduckgo.com/"}) as r:
        page = r.read(3 * 1024 * 1024).decode("utf-8", errors="replace")
    parser = _DDGLiteParser()
    parser.feed(page)
    results = []
    for res in parser.results:
        url = _unwrap_ddg(res["url"].strip())
        if not url.startswith("http") or "duckduckgo.com/y.js" in url:
            continue  # ads
        results.append({"title": _tidy(res["title"]), "url": url, "snippet": _tidy(res["snippet"])})
    if not results and ("anomaly" in page or "challenge" in page):
        raise ToolError("DuckDuckGo is rate-limiting searches.")
    return results


def _search_wikipedia(query: str) -> list[dict]:
    qs = urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": query,
                                 "format": "json", "srlimit": 6, "utf8": 1})
    with _request("https://en.wikipedia.org/w/api.php?" + qs) as r:
        data = json.loads(r.read(2 * 1024 * 1024))
    out = []
    for item in data.get("query", {}).get("search", []):
        title = item.get("title", "")
        snippet = _tidy(html.unescape(re.sub(r"<[^>]+>", "", item.get("snippet", ""))))
        out.append({"title": title, "snippet": snippet,
                    "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))})
    return out


def web_search(query: str, max_results: int = 6) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ToolError("The search query is empty.")
    query = query.strip()
    n = min(max(_as_int(max_results, 6), 1), 10)
    note = ""
    try:
        results = _search_ddg(query)
    except ToolError as e:
        results, note = [], f"(DuckDuckGo failed: {e} Showing Wikipedia results instead.)\n"
    if not results:
        try:
            results = _search_wikipedia(query)
            if results and not note:
                note = "(No web results; showing Wikipedia results.)\n"
        except ToolError:
            pass
    if not results:
        return f"No results found for: {query}"
    lines = [note + f"Search results for: {query}", ""]
    for i, r in enumerate(results[:n], 1):
        lines += [f"{i}. {r['title']}", f"   URL: {r['url']}"]
        if r["snippet"]:
            lines.append(f"   {r['snippet']}")
        lines.append("")
    lines.append("Snippets can be stale or partial: use fetch_url to read the best result(s) "
                 "before answering.")
    return "\n".join(lines)


_page_cache: OrderedDict[str, tuple[str, str, str]] = OrderedDict()


def _load_page(url: str) -> tuple[str, str, str, list[tuple[str, str]]]:
    if url in _page_cache:
        _page_cache.move_to_end(url)
        return _page_cache[url]
    with _request(url) as r:
        data = r.read(MAX_PAGE_BYTES + 1)
        final = r.geturl()
        ctype = r.headers.get_content_type()
        charset = r.headers.get_content_charset() or "utf-8"
    data = data[:MAX_PAGE_BYTES]
    links: list[tuple[str, str]] = []
    if ctype == "application/pdf" or data[:5] == b"%PDF-":
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(data)
            tmp.flush()
            title, text = "", pdf_to_text(Path(tmp.name))
    elif ctype in ("text/html", "application/xhtml+xml") or data.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        title, text, links = _parse_html(data.decode(charset, errors="replace"), final)
    elif ctype.startswith("text/") or ctype.endswith(("json", "xml", "javascript")):
        title, text = "", data.decode(charset, errors="replace").strip()
    else:
        raise ToolError(f"{url} is a {ctype} file ({human_size(len(data))}), not a readable page. "
                        "Use download_file to save it to the workspace.")
    entry = (final, title, text, links)
    _page_cache[url] = entry
    while len(_page_cache) > 12:
        _page_cache.popitem(last=False)
    return entry


def fetch_url(url: str, start: int = 0) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ToolError("No URL given.")
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    final, title, text, links = _load_page(url)
    if not text:
        return f"URL: {final}\nThe page has no readable text (it may need JavaScript)."
    start = min(max(_as_int(start, 0), 0), len(text))
    chunk = text[start:start + FETCH_CHUNK]
    head = f"URL: {final}\n" + (f"Title: {title}\n" if title else "") + "\n"
    tail = ""
    if start + len(chunk) < len(text):
        tail = (f"\n\n[Showing characters {start}-{start + len(chunk)} of {len(text)}. "
                f"Call fetch_url with start={start + len(chunk)} to read more.]")
    if start == 0:
        tail += _links_section(links)
    return head + chunk + tail


def download_file(ws: Workspace, url: str, path: str = "") -> str:
    if not isinstance(url, str) or not url.strip():
        raise ToolError("No URL given.")
    url = url.strip()
    name = os.path.basename(urllib.parse.unquote(urllib.parse.urlsplit(url).path)) or "download"
    dest = ws.resolve(path or name)
    if dest.is_dir():
        dest = ws.resolve(os.path.join(ws.display(dest) if dest != ws.root else ".", name))
    if dest.exists():
        if not ws.confirm("Replace a file?", f"Qwen wants to download {url} over "
                          f"{ws.display(dest)}. The old version goes to the Trash."):
            raise ToolError(f"The user declined overwriting {ws.display(dest)}.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    fd, tmp = tempfile.mkstemp(prefix=".download-", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as out, _request(url, extra={"Accept": "*/*"}) as r:
            ctype = r.headers.get_content_type()
            while True:
                block = r.read(256 * 1024)
                if not block:
                    break
                total += len(block)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ToolError(f"The file is larger than {human_size(MAX_DOWNLOAD_BYTES)}.")
                out.write(block)
        backup = ""
        if dest.exists() and not ws._is_own(dest) and dest.stat().st_size:
            ws.trash(dest)
            backup = " The old version is in the Trash."
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    ws.mark_own(dest)
    return f"Saved {url} to {ws.display(dest)} ({human_size(total)}, {ctype}).{backup}"


# --------------------------------------------------------------------------- programs
#
# Commands and installs are not sandboxed beyond running in the workspace folder. Routine
# ones run without asking; command_risk() and the install size checks decide which ones the
# user sees (verbatim) and approves first.

TERMUX_PROOT = "/data/data/com.termux/files/usr/bin/proot"
RUN_TIMEOUT = 300
MAX_RUN_TIMEOUT = 1800
INSTALL_TIMEOUT = 1800
OUTPUT_HEAD = 1500
OUTPUT_TAIL = 6000
MAX_SNAPSHOT = 20000
APT_NAME = re.compile(r"^[a-z0-9][a-z0-9+.\-]{0,99}$")
PIP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,99}(\[[A-Za-z0-9._,\-]+\])?"
                      r"((==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!\-]{1,40})?$")
PROGRAM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,99}$")
MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]")
# Commands whose Debian package has a different name.
PROGRAM_PACKAGES = {
    "tesseract": "tesseract-ocr", "convert": "imagemagick", "magick": "imagemagick",
    "identify": "imagemagick", "mogrify": "imagemagick", "pdftotext": "poppler-utils",
    "pdfunite": "poppler-utils", "pdfseparate": "poppler-utils", "pdftoppm": "poppler-utils",
    "pdfinfo": "poppler-utils", "heif-convert": "libheif-examples", "gs": "ghostscript",
    "exiftool": "libimage-exiftool-perl", "7z": "p7zip-full", "node": "nodejs",
    "pdflatex": "texlive-latex-base", "latex": "texlive-latex-base", "soffice": "libreoffice",
    "cwebp": "webp", "dwebp": "webp", "rsvg-convert": "librsvg2-bin", "zbarimg": "zbar-tools",
}


def venv_dir() -> Path:
    from .config import HARNESS_DIR
    return HARNESS_DIR / "venv"


def child_env() -> dict:
    """Environment for programs Qwen runs: no LD_PRELOAD shims, Qwen's Python first on PATH."""
    env = dict(os.environ)
    env.pop("LD_PRELOAD", None)
    here = str(Path(__file__).resolve().parent.parent)
    pp = [p for p in env.pop("PYTHONPATH", "").split(":") if p and p != here]
    if pp:
        env["PYTHONPATH"] = ":".join(pp)
    path = env.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    vbin = venv_dir() / "bin"
    if vbin.is_dir():
        path = f"{vbin}:{path}"
    env.update(PATH=path, DEBIAN_FRONTEND="noninteractive", PAGER="cat", GIT_PAGER="cat",
               TERM="dumb", NO_COLOR="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    return env


def root_argv(argv: list[str]) -> list[str]:
    """argv run as root. Inside Termux's PRoot, `proot -0` fakes root for the guest system."""
    if os.geteuid() == 0:
        return argv
    if os.path.exists(TERMUX_PROOT):
        return [TERMUX_PROOT, "-0", "/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", *argv]
    return ["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", *argv]


def root_env() -> dict:
    env = child_env()
    if os.geteuid() != 0 and os.path.exists(TERMUX_PROOT):
        env.pop("LD_LIBRARY_PATH", None)   # the Termux proot binary needs its own libraries
    return env


def clean_output(text: str) -> str:
    """Drops colour codes and progress-bar redraws (keeps the last state of each line)."""
    text = _ANSI.sub("", text)
    return "\n".join(line.rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n")).strip("\n")


class _Output:
    """Keeps the start and the end of a long output."""

    def __init__(self):
        self.head = ""
        self.tail = ""
        self.dropped = 0

    def add(self, text: str) -> None:
        if len(self.head) < OUTPUT_HEAD:
            take = OUTPUT_HEAD - len(self.head)
            self.head += text[:take]
            text = text[take:]
        self.tail += text
        if len(self.tail) > 4 * OUTPUT_TAIL:
            cut = len(self.tail) - 2 * OUTPUT_TAIL
            self.dropped += cut
            self.tail = self.tail[cut:]

    def text(self) -> str:
        tail = clean_output(self.tail)
        if len(tail) > OUTPUT_TAIL:
            self.dropped += len(tail) - OUTPUT_TAIL
            tail = tail[-OUTPUT_TAIL:]
        if not self.dropped:
            return clean_output(self.head + self.tail)
        return (clean_output(self.head) + f"\n[... {self.dropped:,} characters of output skipped ...]\n"
                + tail)


def _last_line(text: str) -> str:
    for line in reversed(re.split(r"[\r\n]", _ANSI.sub("", text))):
        line = line.strip()
        if line:
            return line if len(line) <= 90 else line[:89] + "…"
    return ""


def _kill_group(proc: subprocess.Popen) -> None:
    for sig, wait in ((15, 3), (9, 3)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_streaming(argv: list[str], cwd, env: dict, timeout: float, cancel=None,
                  on_line: Callable[[str], None] | None = None,
                  killable: Callable[[], bool] | None = None,
                  on_chunk: Callable[[str], None] | None = None) -> tuple[int | None, str, str]:
    """Runs argv in its own process group with no stdin.

    Returns (exit code, output, reason) where reason is "", "timeout" or "cancelled".
    `on_line` gets the latest line for a status display (throttled); `on_chunk` gets all output.
    `killable()` returning False postpones a cancel (used while dpkg is unpacking).
    """
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    out = _Output()
    shown = {"t": 0.0, "line": ""}
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def reader():
        fd = proc.stdout.fileno()
        while True:
            try:
                block = os.read(fd, 8192)
            except OSError:
                break
            if not block:
                break
            text = decoder.decode(block)
            out.add(text)
            if on_chunk is not None:
                on_chunk(text)
            if on_line is not None:
                line = _last_line(text)
                now = time.monotonic()
                if line and line != shown["line"] and now - shown["t"] > 0.4:
                    shown.update(t=now, line=line)
                    on_line(line)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    deadline = time.monotonic() + timeout
    reason = ""
    while True:
        try:
            proc.wait(0.25)
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel is not None and cancel.is_set() and (killable is None or killable()):
            reason = "cancelled"
        elif time.monotonic() > deadline:
            reason = "timeout"
        if reason:
            _kill_group(proc)
            break
    # Background jobs the command left behind would hold the output pipe open (and memory).
    try:
        os.killpg(proc.pid, 15)
    except (ProcessLookupError, PermissionError):
        pass
    t.join(3)
    try:
        proc.stdout.close()
    except OSError:
        pass
    return proc.returncode, out.text(), reason


def _query(argv: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=child_env(), stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=timeout)


def apt_installed(pkgs: list[str]) -> set[str]:
    r = _query(["dpkg-query", "-W", "-f=${Package} ${db:Status-Abbrev}\n", *pkgs])
    return {line.split()[0] for line in r.stdout.splitlines()
            if len(line.split()) >= 2 and line.split()[1].startswith("ii")}


def apt_candidates(pkgs: list[str]) -> set[str]:
    """Packages apt could install (have a candidate version)."""
    r = _query(["apt-cache", "policy", *pkgs])
    found, current = set(), None
    for line in r.stdout.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            current = line.rstrip()[:-1].split(":")[0]
        elif current and line.strip().startswith("Candidate:"):
            if "(none)" not in line:
                found.add(current)
    return found


def apt_search(name: str, limit: int = 6) -> list[str]:
    try:
        r = _query(["apt-cache", "search", "--names-only", re.escape(name)])
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.split(" - ")[0] for line in r.stdout.splitlines()[:limit]]


def _apt_bytes(text: str) -> float | None:
    m = re.fullmatch(r"([\d.,]+) ?([kMG]?)B", text.strip())
    if not m:
        return None
    return float(m.group(1).replace(",", "")) * {"": 1, "k": 1e3, "M": 1e6, "G": 1e9}[m.group(2)]


def _apt_size(text: str) -> str:
    """'7,433 kB' -> '7.3 MB' (apt uses decimal units)."""
    n = _apt_bytes(text)
    if n is None:
        return text
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n:.0f} bytes"


def apt_plan(pkgs: list[str]) -> dict:
    """What `apt-get install pkgs` would do: counts, sizes and removals.

    --print-uris only lists what would be downloaded, and this runs as the normal user, so it
    can't install anything. (--assume-no is not safe here: apt skips its yes/no prompt, and
    installs, when nothing beyond the requested packages is needed.)
    """
    r = _query(["apt-get", "install", "--print-uris", "-q", *pkgs], timeout=120)
    text = r.stdout + r.stderr
    plan: dict = {}
    if m := re.search(r"(\d+) newly installed", text):
        plan["count"] = int(m.group(1))
    if m := re.search(r"Need to get ([\d.,]+ ?[kMG]?B)", text):
        plan["download"] = _apt_size(m.group(1))
        plan["download_bytes"] = _apt_bytes(m.group(1))
    if m := re.search(r"After this operation, ([\d.,]+ ?[kMG]?B) of additional disk space", text):
        plan["disk"] = _apt_size(m.group(1))
        plan["disk_bytes"] = _apt_bytes(m.group(1))
    if m := re.search(r"The following packages will be REMOVED:\n((?:\s+.*\n)+)", text):
        plan["remove"] = m.group(1).split()
    if "E: " in text and "count" not in plan:
        plan["error"] = "\n".join(line for line in text.splitlines() if line.startswith("E: "))
    return plan


_CMD_WORDS = {"{", "!", "do", "then", "else", "elif", "if", "while", "until", "time"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")


def _starts_command(tok: str) -> bool:
    """True if a command can follow this token (shlex merges runs of operators like ');')."""
    if tok in _CMD_WORDS:
        return True
    if not tok or tok.strip("();<>|&"):
        return False
    if "(" in tok or ";" in tok or "|" in tok:
        return True
    return "&" in tok and not any(c in tok for c in "<>")  # '&>' and '>&' are redirections


def _missing_programs(command: str) -> list[str]:
    """Programs a shell command starts that aren't installed (best effort, no false alarms)."""
    # Heredoc bodies, arithmetic, arrays, [[ ]] patterns and case labels aren't commands, and
    # this simple scan can't tell them apart, so such commands are left to the user.
    if re.search(r"<<|\(\(|=\(|\[\[|\bcase\b|>\|", command):
        return []
    text = command.replace("\\\n", " ").replace("\n", " ; ")
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []
    defined = {tokens[i + 1] for i, t in enumerate(tokens[:-1]) if t == "function"}
    defined |= {t for i, t in enumerate(tokens)
                if tokens[i + 1:i + 2] == ["()"] or tokens[i + 1:i + 3] == ["(", ")"]}
    names, expect = [], True
    for tok in tokens:
        if _starts_command(tok) and tok != "()":
            expect = True
        elif tok == "$":
            continue
        elif expect and _ASSIGNMENT.match(tok):
            continue
        elif expect:
            expect = False
            if PROGRAM_NAME.match(tok) and tok not in names and tok not in defined:
                names.append(tok)
    names = names[:20]
    if not names:
        return []
    try:  # bash knows its builtins and keywords; PATH is the one commands run with
        r = subprocess.run(["/bin/bash", "-c", 'for n; do type -t -- "$n" >/dev/null || echo "$n"; '
                            'done', "_", *names], env=child_env(), capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return [n for n in r.stdout.split() if n in names]


def _missing_hint(missing: list[str]) -> str:
    pkgs = {p: PROGRAM_PACKAGES.get(p, p) for p in missing}
    try:
        known = apt_candidates(list(dict.fromkeys(pkgs.values())))
    except (OSError, subprocess.SubprocessError):
        known = set()
    parts = [f"{p} (Debian package {pkg})" if pkg in known else p for p, pkg in pkgs.items()]
    return (f"Not installed: {', '.join(parts)}. Use install_packages to install it, or use a "
            "program that is installed.")


# --------------------------------------------------------------------------- what still asks
#
# Routine commands and small installs run without asking. These checks pick out the ones
# that still need the user's OK. They read the command as written; they are a safety net,
# not a sandbox (a script can do whatever it contains).

_ADMIN = {"sudo", "su", "doas", "pkexec", "runuser", "proot", "chroot"}
_SYSTEM = {"shutdown", "reboot", "poweroff", "halt", "systemctl", "service", "init", "telinit",
           "loginctl", "pkill", "killall", "xkill", "mkfs", "mkswap", "wipefs", "fdisk", "sfdisk",
           "gdisk", "parted", "mount", "umount", "losetup", "swapon", "swapoff", "shred",
           "blkdiscard"}
_ACCOUNTS = {"ssh", "scp", "sftp", "gh", "az", "aws", "gcloud", "kubectl", "crontab", "passwd",
             "chsh", "chpasswd", "useradd", "usermod", "userdel", "adb"}
_WRAPPERS = {"env", "nohup", "nice", "time", "timeout", "xargs", "exec", "command", "builtin",
             "stdbuf", "setsid", "ionice", "watch", "flock"}
_WRITERS = {"rm", "rmdir", "unlink", "mv", "chmod", "chown", "chgrp", "truncate", "touch", "tee",
            "mkdir"}
_DEST_LAST = {"cp", "rsync", "install", "ln"}
_REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<>", ">&"}
_SCRATCH = ("/tmp", "/var/tmp", "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/fd", "/proc/self")
_PRIVATE = ("/sdcard", "/storage", "/data", "/mnt", "/media", "/root", "/etc/shadow",
            "/etc/gshadow", "/etc/sudoers")
_WHOLE = {"*", ".", "./", "./*", "*/", ".*", "./.*"}
_PATH_END = r"(?=$|[\s'\"`;|&()<>])"
_PATH_REST = r"(?:/[^\s'\"`;|&()<>]*)?"


def _under(p: str, base: str) -> bool:
    return p == base or p.startswith(base.rstrip("/") + "/")


def _place(arg: str, cwd: str, root: str, home: str) -> str | None:
    """'workspace' (or a scratch place), 'private' (home, phone storage), 'system', or None
    when it can't be told (it starts with a variable)."""
    if arg.startswith("~"):
        if arg != "~" and not arg.startswith("~/"):
            return "private"  # another user's home
        arg = home + arg[1:]
    elif m := re.match(r"\$(HOME\b|\{HOME\})", arg):
        arg = home + arg[m.end():]
    elif arg.startswith(("$", "`")):
        return None
    p = os.path.normpath(os.path.join(cwd, arg))
    if _under(p, root) or _under(os.path.realpath(p), root) or any(_under(p, s) for s in _SCRATCH):
        return "workspace"
    if _under(p, home) or any(_under(p, s) for s in _PRIVATE):
        return "private"
    return "system"


def _strip_heredocs(text: str) -> str:
    """Drops here-document bodies (file contents, not commands)."""
    out, pending = [], []
    for line in text.split("\n"):
        if pending:
            delim, tabs = pending[0]
            if (line.lstrip("\t") if tabs else line) == delim:
                pending.pop(0)
            continue
        out.append(line)
        for m in re.finditer(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_][\w.-]*)\2", line):
            pending.append((m.group(3), m.group(1) == "-"))
    return "\n".join(out)


def _opt_value(args: list[str], flags: tuple[str, ...]) -> str:
    for i, a in enumerate(args):
        if a in flags and i + 1 < len(args):
            return args[i + 1]
        for f in flags:
            if f.startswith("--") and a.startswith(f + "="):
                return a[len(f) + 1:]
    return ""


def _short(path: str, cwd: str = "/") -> str:
    """A path as the user should read it: absolute, with ~ for home, at most 60 characters."""
    if not path.startswith(("$", "`", "~")):
        path = _home(os.path.normpath(os.path.join(cwd, path)))
    return path if len(path) <= 60 else path[:59] + "…"


def command_risk(command: str, root, home: str | None = None) -> str | None:
    """Why a command needs the user's OK, or None if it looks routine."""
    return _risk(command, str(root), home or os.path.expanduser("~"), 0)


def _risk(text: str, root: str, home: str, depth: int) -> str | None:
    if depth > 3:
        return None
    if re.search(r"\b(curl|wget)\b[^|;&\n]*\|\s*(sudo\s+)?(\w*sh|python3?)\b"
                 r"|\b(\w*sh|source|\.)\s+<\(\s*(curl|wget)\b", text):
        return "It runs a script straight from the internet."
    # Paths in the user's home or phone storage, wherever they appear (also inside quoted
    # scripts and here-documents).
    refs = [r"(?<![\w~./:$-])~" + _PATH_REST + _PATH_END,
            r"\$(?:HOME\b|\{HOME\})" + _PATH_REST,
            r"(?<![\w.:/-])" + re.escape(home) + _PATH_REST + _PATH_END if home not in ("", "/") else "",
            r"(?<![\w.:/~-])/(?:sdcard|storage|data|mnt|media|root|etc/shadow|etc/gshadow|"
            r"etc/sudoers)" + _PATH_REST + _PATH_END]
    for pattern in filter(None, refs):
        for m in re.finditer(pattern, text):
            if _place(m.group(0), root, root, home) == "private":
                return f"It uses files outside the workspace ({_short(m.group(0))})."
    try:
        lex = shlex.shlex(_strip_heredocs(text).replace("\\\n", " ").replace("\n", " ; "),
                          posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    cwd, words, targets = root, [], []
    i = 0
    while i <= len(tokens):
        tok = tokens[i] if i < len(tokens) else ";"
        if tok in _REDIRECTS and i + 1 < len(tokens):
            targets.append(tokens[i + 1])
            i += 2
            continue
        if tok == "$":
            i += 1
            continue
        if _starts_command(tok) or tok == ")":
            reason, cwd = _risk_simple(words, targets, cwd, root, home, depth)
            if reason:
                return reason
            words, targets = [], []
        else:
            words.append(tok)
        i += 1
    return None


def _risk_simple(words, targets, cwd, root, home, depth) -> tuple[str | None, str]:
    for t in targets:
        if not re.fullmatch(r"\d+|-", t) and _place(t, cwd, root, home) in ("private", "system"):
            return f"It writes to {_short(t, cwd)}, outside the workspace.", cwd
    while words and _ASSIGNMENT.match(words[0]):
        words = words[1:]
    while words and os.path.basename(words[0]) in _WRAPPERS:
        wrapper, words = os.path.basename(words[0]), words[1:]
        while words and (words[0].startswith("-") or _ASSIGNMENT.match(words[0])
                         or (wrapper == "timeout" and re.fullmatch(r"[\d.]+[smhd]?", words[0]))):
            words = words[1:]
    for w in words:
        for inner in re.findall(r"`([^`]*)`", w):
            if reason := _risk(inner, root, home, depth + 1):
                return reason, cwd
    if not words:
        return None, cwd
    prog, args = os.path.basename(words[0]), words[1:]
    opts = [a for a in args if a.startswith("-")]
    plain = [a for a in args if not a.startswith("-")]
    if prog in _ADMIN:
        return f"It asks for admin (root) rights with {prog}.", cwd
    if prog in _SYSTEM or prog.startswith("mkfs."):
        return f"It can stop programs or change the system ({prog}).", cwd
    if prog == "kill" and any(not re.fullmatch(r"\$!|%\d*|\"?\$\{?\w+\}?\"?", a) for a in plain):
        return "It stops a program that is running.", cwd
    if prog in _ACCOUNTS or (prog == "git" and plain[:1] == ["push"]):
        return f"It uses your accounts or another computer ({prog}).", cwd
    if prog == "git" and plain[:1] == ["config"] and ("--global" in opts or "--system" in opts):
        return "It changes your git settings.", cwd
    sub = plain[0] if plain else ""
    if ((prog in ("apt", "apt-get", "aptitude", "snap", "flatpak")
         and sub in ("install", "reinstall", "remove", "purge", "autoremove", "upgrade",
                     "full-upgrade", "dist-upgrade", "build-dep", "uninstall"))
            or (prog == "dpkg" and set(opts) & {"-i", "--install", "-r", "--remove", "-P",
                                                  "--purge", "--configure", "--unpack"})):
        return "It installs or removes system software.", cwd
    pip = prog in ("pip", "pip3") or (re.fullmatch(r"python[\d.]*", prog) and args[:2] == ["-m", "pip"])
    if pip and (plain[1:2] if prog.startswith("python") else plain[:1]) in (["install"], ["uninstall"]):
        return "It installs or removes Python packages.", cwd
    if (prog == "npm" and sub in ("install", "i", "uninstall") and set(opts) & {"-g", "--global"}) \
            or (prog in ("gem", "cargo") and sub in ("install", "uninstall")):
        return f"It installs software outside the workspace ({prog}).", cwd
    if prog in ("bash", "sh", "dash", "zsh") and "-c" in args[:-1]:
        if reason := _risk(args[args.index("-c") + 1], root, home, depth + 1):
            return reason, cwd
    if prog == "eval" and (reason := _risk(" ".join(args), root, home, depth + 1)):
        return reason, cwd
    if prog in ("cd", "pushd"):
        dest = plain[0] if plain else "~"
        where = _place(dest, cwd, root, home) if dest != "-" else None
        if where == "private":
            return f"It works in {_short(dest, cwd)}, outside the workspace.", cwd
        if where is not None:
            cwd = os.path.normpath(os.path.join(cwd, os.path.expanduser(dest)))
        return None, cwd
    if prog == "rm" and cwd == root and _WHOLE & set(plain):
        return "It deletes everything in the workspace.", cwd
    paths: list[str] = []
    if prog in _WRITERS:
        paths = plain
    elif prog in _DEST_LAST:
        paths = [_opt_value(args, ("-t", "--target-directory"))] if \
            _opt_value(args, ("-t", "--target-directory")) else plain[-1:]
    elif prog == "sed" and any(o.startswith(("-i", "--in-place")) for o in opts):
        paths = plain if "-e" in opts or "-f" in opts else plain[1:]
    elif prog == "dd":
        paths = [a[3:] for a in args if a.startswith("of=")]
    elif prog == "tar":
        paths = [_opt_value(args, ("-C", "--directory"))]
    elif prog == "unzip":
        paths = [_opt_value(args, ("-d",))]
    elif prog == "wget":
        paths = [_opt_value(args, ("-O", "-P", "--output-document", "--directory-prefix"))]
    elif prog == "curl":
        paths = [_opt_value(args, ("-o", "--output", "--output-dir"))]
    elif prog == "find" and ("-delete" in args or "-exec" in args or "-execdir" in args):
        paths = list(itertools.takewhile(lambda a: not a.startswith(("-", "(", "!")), args))
    for p in paths:
        if p and _place(p, cwd, root, home) in ("private", "system"):
            return f"It changes files outside the workspace ({_short(p, cwd)}).", cwd
    for a in args:
        if ".." in a.split("/") and _place(a, cwd, root, home) == "private":
            return f"It uses files outside the workspace ({_short(a, cwd)}).", cwd
    return None, cwd


# Big downloads ask first: these thresholds, plus pip packages that are known to be huge
# (or to pull in something huge, usually PyTorch) or to compile for a long time on ARM.
BIG_APT_DOWNLOAD = 100e6
BIG_APT_DISK = 300e6
BIG_PIP_BYTES = 50e6
BIG_PIP = {
    "torch", "torchvision", "torchaudio", "tensorflow", "tensorflow-cpu", "tf-nightly", "keras-nlp",
    "jax", "jaxlib", "paddlepaddle", "paddleocr", "mxnet", "onnxruntime-gpu", "opencv-python",
    "opencv-contrib-python", "opencv-python-headless", "opencv-contrib-python-headless",
    "pyspark", "vllm", "xformers", "triton", "easyocr", "ultralytics", "sentence-transformers",
    "openai-whisper", "accelerate", "timm", "mediapipe", "dlib", "llama-cpp-python", "playwright",
    "spacy", "gradio", "streamlit", "bitsandbytes", "deepspeed", "nemo-toolkit",
}


def apt_big_reason(plan: dict) -> str | None:
    if plan.get("remove"):
        return f"It would also remove {', '.join(plan['remove'][:8])}."
    if (plan.get("download_bytes") or 0) >= BIG_APT_DOWNLOAD:
        return f"It's a big download ({human_size(plan['download_bytes'])})."
    if (plan.get("disk_bytes") or 0) >= BIG_APT_DISK:
        return f"It takes a lot of space ({human_size(plan['disk_bytes'])})."
    return None


def _pip_norm(spec: str) -> str:
    return re.sub(r"[-_.]+", "-", re.split(r"[\[=<>~!;@ ]", spec, 1)[0]).lower()


def _pip_pick(files: list[dict]) -> tuple[int, bool]:
    """(size of the file pip would download here, whether it must be compiled)."""
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    wheels = [f for f in files if f.get("packagetype") == "bdist_wheel"]
    native = [f for f in wheels if "aarch64" in f["filename"] and "musllinux" not in f["filename"]
              and (tag in f["filename"] or "abi3" in f["filename"] or "-py3-none-" in f["filename"])]
    for pick in (native, [f for f in wheels if f["filename"].endswith("-none-any.whl")]):
        if pick:
            return max(int(f.get("size") or 0) for f in pick), False
    sdist = max((int(f.get("size") or 0) for f in files if f.get("packagetype") == "sdist"), default=0)
    return sdist, bool(wheels)  # built for other machines but not this one: pip compiles it


def _pip_info(name: str, timeout: float) -> tuple[int, bool] | None:
    """_pip_pick() for the latest release on PyPI, or None if PyPI doesn't answer."""
    try:
        req = urllib.request.Request(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json",
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read(16 * 1024 * 1024))
        return _pip_pick(data.get("urls") or [])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def pip_big_reason(pkgs: list[str], timeout: float = 6.0) -> str | None:
    names = list(dict.fromkeys(_pip_norm(p) for p in pkgs))
    known = [n for n in names if n in BIG_PIP]
    if known:
        return (f"{', '.join(known)} {'is' if len(known) == 1 else 'are'} large: often hundreds "
                "of MB with what it needs, and slow to install here.")
    found: dict[str, tuple[int, bool]] = {}

    def look(name: str) -> None:
        info = _pip_info(name, timeout)
        if info is not None:  # no answer: don't hold the install up
            found[name] = info

    threads = [threading.Thread(target=look, args=(n,), daemon=True) for n in names[:20]]
    for t in threads:
        t.start()
    end = time.monotonic() + timeout + 1
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    big = [f"{n} ({human_size(size)})" for n, (size, _) in found.items() if size >= BIG_PIP_BYTES]
    if big:
        return f"It's a big download: {', '.join(big)}."
    built = [n for n, (_, compiled) in found.items() if compiled]
    if built:
        return (f"{', '.join(built)} {'has' if len(built) == 1 else 'have'} no ready-made build "
                "for this phone, so pip compiles it, which can take a long time.")
    return None


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    files: dict[str, tuple[int, int]] = {}
    for top, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
        for n in names:
            p = os.path.join(top, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            files[os.path.relpath(p, root)] = (st.st_mtime_ns, st.st_size)
            if len(files) >= MAX_SNAPSHOT:
                return files
    return files


def _changes(before: dict, after: dict) -> tuple[list[str], list[str]]:
    changed = [p for p, v in after.items() if before.get(p) != v]
    changed.sort(key=lambda p: after[p][0], reverse=True)
    return changed, sorted(p for p in before if p not in after)


# --------------------------------------------------------------------------- toolbox

def _fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required}}}


_S = {"type": "string"}
WEB_TOOLS = [
    _fn("web_search", "Search the internet (DuckDuckGo). Returns titles, URLs and snippets.",
        {"query": {**_S, "description": "Search terms"},
         "max_results": {"type": "integer", "description": "1-10, default 6"}}, ["query"]),
    _fn("fetch_url", "Read a web page or online PDF as plain text, 6000 characters at a time.",
        {"url": {**_S, "description": "Full http(s) URL"},
         "start": {"type": "integer", "description": "Character offset to continue from (default 0)"}},
        ["url"]),
]
FILE_TOOLS = [
    _fn("save_skill", "Validate and save a workspace skill folder (SKILL.md plus optional "
        "scripts/references/assets) to the shared library for every chat. Load skill-creator "
        "first. Always asks for approval; an updated skill's old version goes to the Trash.",
        {"path": {**_S, "description": "Workspace folder, e.g. skills/ocean-quiz"}}, ["path"]),
    _fn("list_files", "List files and folders in the workspace.",
        {"path": {**_S, "description": "Folder relative to the workspace (default '.')"},
         "recursive": {"type": "boolean", "description": "Include subfolders"}}, []),
    _fn("read_file", "Read a text, code, Markdown, CSV or PDF file from the workspace.",
        {"path": {**_S, "description": "File path relative to the workspace"},
         "start_line": {"type": "integer", "description": "Line to start from (default 1)"}}, ["path"]),
    _fn("read_quiz", "Read the actual quiz before making an answer key. Returns exact questions, "
        "options, source excerpts and a source hash for render_answer_key. Supports worksheet "
        "JSON and text PDFs with numbered questions; checks matching companion JSON against "
        "the PDF. Read all continuation lines. Never derive a key from an older draft or memory.",
        {"path": {**_S, "description": "Existing source quiz .pdf or .json in the workspace"},
         "start_line": {"type": "integer", "description": "Continuation line, default 1"}}, ["path"]),
    _fn("write_file", "Create or overwrite a text file in the workspace (parent folders are created). "
        "Set append=true to add to the end instead, e.g. to write a long file in parts.",
        {"path": {**_S, "description": "File path relative to the workspace, e.g. 'notes/plan.md'"},
         "content": {**_S, "description": "Full text to write"},
         "append": {"type": "boolean", "description": "Append instead of replacing"}},
        ["path", "content"]),
    _fn("edit_file", "Change part of an existing text file: replaces old_text, which must match "
        "exactly one place, with new_text. Use it to fix one value or line instead of "
        "rewriting the whole file.",
        {"path": {**_S, "description": "File path relative to the workspace"},
         "old_text": {**_S, "description": "Exact current text to replace, copied from read_file, "
                                           "with enough nearby words to be unique"},
         "new_text": {**_S, "description": "Replacement text"}},
        ["path", "old_text", "new_text"]),
    _fn("make_directory", "Create a folder in the workspace.",
        {"path": {**_S, "description": "Folder path relative to the workspace"}}, ["path"]),
    _fn("move_path", "Move or rename a file or folder inside the workspace.",
        {"source": _S, "destination": _S}, ["source", "destination"]),
    _fn("delete_path", "Delete a file or folder from the workspace (it goes to the Trash).",
        {"path": _S}, ["path"]),
]
DOWNLOAD_TOOL = _fn(
    "download_file", "Download a file from the internet into the workspace.",
    {"url": {**_S, "description": "Full http(s) URL"},
     "path": {**_S, "description": "Destination path in the workspace (default: name from the URL)"}},
    ["url"])

FILE_RESULT_TOOLS = {"write_file", "edit_file", "download_file", "make_directory", "move_path",
                     "render_worksheet", "render_answer_key"}

_LIST = {"type": "array", "items": _S}
SKILL_TOOLS = [
    _fn("list_skills", "Discover reusable skills available in all chats. User-suggested /names "
        "are not exclusive: look for other useful skills too. Returns names and descriptions.",
        {"query": {**_S, "description": "Optional name or description filter"},
         "start": {"type": "integer", "description": "Result offset for the next page, default 0"}}, []),
    _fn("load_skill", "Read a saved or built-in skill's instructions. Skills are guidance, "
        "not exclusive tool selections or permission changes. Supporting files are copied "
        "into the workspace when Files is on; nothing is executed just by loading.",
        {"name": {**_S, "description": "Skill name from list_skills, without the leading /"}}, ["name"]),
]
PROGRAM_TOOLS = [
    _fn("render_worksheet", "Create a themed, portrait PDF from a workspace JSON specification. "
        "Load worksheet-pdf first for its format. Checks layout, page limit, color emoji and "
        "text preservation before saving. Pass the user's requested question counts, when given, "
        "to check the format. A page-limit conflict asks the user to allow the necessary pages; "
        "do not rewrite or remove content to work around it. For a NEW worksheet plus a parent "
        "or teacher answer key, put each question's answer in the same JSON and pass "
        "answer_key_path: one call checks every answer and creates both PDFs, with no answers "
        "on the student copy. For a key to an EXISTING quiz, use read_quiz and render_answer_key.",
        {"spec_path": {**_S, "description": "Workspace JSON file containing the worksheet data"},
         "path": {**_S, "description": "Destination .pdf file in the workspace"},
         "expected_questions": {"type": "integer", "minimum": 0, "maximum": 30,
                                "description": "Total questions requested by the user, excluding a bonus challenge"},
         "expected_multiple_choice": {"type": "integer", "minimum": 0, "maximum": 30,
                                      "description": "Multiple-choice question count requested by the user (not option count)"},
         "answer_key_path": {**_S, "description": "Optional second .pdf for the parent/teacher "
                                                  "answer key, built from the answers in the JSON"}},
        ["spec_path", "path"]),
    _fn("render_answer_key", "Create a teacher-facing answer-key PDF from source-bound JSON. "
        "Load answer-key and call read_quiz first. Requires one answer per source question, "
        "valid option letters and exact source evidence. Copies question/option text from "
        "the source; flags invalid questions rather than forcing wrong choices. Checks the "
        "source hash, coverage, layout, page limit and exported text before saving.",
        {"spec_path": {**_S, "description": "Answer-key JSON with source_path, source_sha256, answers "
                                             "and optional max_pages; see answer-key skill"},
         "path": {**_S, "description": "Destination .pdf file in the workspace"}},
        ["spec_path", "path"]),
    _fn("open_file", "Request that the desktop open an existing workspace file. Rejects missing "
        "or invalid PDFs and reports opener errors. Use instead of backgrounded xdg-open. "
        "Success confirms the launch request, not that a window was visibly displayed.",
        {"path": {**_S, "description": "Existing workspace file to open"}}, ["path"]),
    _fn("run_command", "Run a bash command in the workspace folder and get its output and exit "
        "code. Routine commands run right away; the user is asked first only for risky ones "
        "(admin rights, files outside the workspace, stopping programs). Do related steps in "
        "one command or script. There is no keyboard input; use non-interactive options.",
        {"command": {**_S, "description": "The bash command or script"},
         "why": {**_S, "description": "One short sentence telling the user what it does and why"},
         "timeout_seconds": {"type": "integer", "description": "Default 300, up to 1800 for slow jobs"}},
        ["command", "why"]),
    _fn("install_packages", "Install missing software: Debian packages with apt "
        "(programs such as tesseract-ocr, ffmpeg, pandoc, img2pdf) or Python packages with pip "
        "(into the Python that run_command's python3 uses). Small installs run right away; the "
        "user is asked first for big ones.",
        {"packages": {**_LIST, "description": "Package names, e.g. [\"tesseract-ocr\"]"},
         "manager": {**_S, "enum": ["apt", "pip"], "description": "Default apt"},
         "why": {**_S, "description": "One short sentence telling the user why they are needed"}},
        ["packages", "why"]),
    _fn("check_programs", "Check which command-line programs and Python modules are installed "
        "(no approval needed).",
        {"programs": {**_LIST, "description": "Command names, e.g. [\"tesseract\", \"pdfunite\"]"},
         "python_modules": {**_LIST, "description": "Module names, e.g. [\"PIL\"]"}}, []),
]


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[\s,]+", value)
    if not isinstance(value, list):
        raise ToolError("Give a list of names.")
    return [str(v).strip() for v in value if str(v).strip()]


class ToolBox:
    def __init__(self, workspace: Workspace, web: bool = True, files: bool = True,
                 commands: bool = False,
                 approve: Callable[..., bool] | None = None,
                 auto_approve: Callable[[], bool] | None = None,
                 skills: SkillRegistry | None = None):
        self.ws = workspace
        self.web = web
        self.files = files
        self.commands = commands
        self.skills = skills if skills is not None else SkillRegistry()
        # approve(title, detail, code, action_label, reason) -> bool; reason says why a
        # command or install needs an OK even when routine ones don't. Default: refuse.
        self.approve = approve or (lambda title, detail, code, action, reason="": False)
        # auto_approve() -> True lets routine commands and small installs run without asking.
        # Risky commands and big installs ask whatever it says. Default: ask for everything.
        self.auto_approve = auto_approve or (lambda: False)
        # Set by the agent for each turn.
        self.cancel: threading.Event | None = None
        self.status: Callable[[str], None] = lambda text: None
        # Once the user says no, nothing else is asked in that turn: the model has to reply.
        self.declined = False
        self.input_required = ""

    def schemas(self) -> list[dict]:
        out = list(SKILL_TOOLS)
        if self.web:
            out += WEB_TOOLS
        if self.files:
            out += FILE_TOOLS
        if self.web and self.files:
            out.append(DOWNLOAD_TOOL)
        if self.files and self.commands:
            out += PROGRAM_TOOLS
        return out

    def names(self) -> set[str]:
        return {t["function"]["name"] for t in self.schemas()}

    def _impl(self, name: str):
        ws = self.ws
        return {
            "web_search": web_search,
            "fetch_url": fetch_url,
            "download_file": lambda url, path="": download_file(ws, url, path),
            "list_files": ws.list_files,
            "read_file": ws.read_file,
            "read_quiz": self.read_quiz,
            "write_file": ws.write_file,
            "edit_file": ws.edit_file,
            "make_directory": ws.make_directory,
            "move_path": ws.move_path,
            "delete_path": ws.delete_path,
            "list_skills": self.list_skills,
            "load_skill": self.load_skill,
            "save_skill": self.save_skill,
            "render_worksheet": self.render_worksheet,
            "render_answer_key": self.render_answer_key,
            "open_file": self.open_file,
            "run_command": self.run_command,
            "install_packages": self.install_packages,
            "check_programs": self.check_programs,
        }.get(name)

    # -- programs -----------------------------------------------------------------------
    def _cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    def _ask(self, title: str, detail: str, code: str, action: str, refused: str,
             reason: str | None = None) -> None:
        self._check_declined()
        if reason is None and self.auto_approve():
            return
        if not self.approve(title, detail, code, action, reason or ""):
            self.declined = True
            raise ToolError(refused + " Don't try a workaround now; ask the user what they "
                            "would like to do.")

    def _check_declined(self) -> None:
        if self.declined:
            raise ToolError("Not done, and the user was not asked: they already said no in this "
                            "reply. Stop now and ask the user what they would like to do.")

    def list_skills(self, query: str = "", start: int = 0) -> str:
        self._check_declined()
        if not isinstance(query, str):
            raise ToolError("query must be text.")
        found, errors = self.skills.catalog(query)
        start = max(0, _as_int(start, 0))
        selected = found[start:start + 20]
        lines = ["Shared skills (suggestions are not exclusive):"]
        lines += [f"/{s.name} — {s.description} ({'built-in' if s.builtin else 'saved'})"
                  for s in selected]
        if not selected:
            lines.append("No matching skills." if not found else "No more results.")
        if start + len(selected) < len(found):
            lines.append(f"More results: call list_skills with start={start + len(selected)}.")
        if errors:
            lines += ["Some skill packages need attention:", *errors]
        return "\n".join(lines)

    def load_skill(self, name: str) -> str:
        self._check_declined()
        if self._cancelled():
            raise ToolError("Stopped by the user.")
        try:
            skill = self.skills.get(name)
            package = self.skills.package(skill.path, skill.builtin)
            skill = package.skill
        except SkillError as exc:
            raise ToolError(str(exc)) from None
        notes = [f"Skill /{skill.name}: {skill.description}",
                 "This is guidance, not an exclusive workflow. Use other useful skills/tools too; "
                 "normal permissions and approvals still apply."]
        if skill.compatibility:
            notes.append("Compatibility: " + skill.compatibility)
        if len(package.files) > 1:
            if not self.files:
                notes.append("This skill has supporting files. Enable Files before using them; "
                             "no resources were copied or executed.")
            else:
                for revision in itertools.count():
                    folder = package.digest[:12] + (f"-{revision}" if revision else "")
                    parent = self.ws.resolve(".qwen-skills/" + folder)
                    target = self.ws.resolve(str(parent / skill.name))
                    if not target.exists() and (not parent.exists() or parent.is_dir()):
                        break
                    try:
                        unchanged = package_files(target) == package.files
                    except SkillError:
                        unchanged = False
                    if unchanged:
                        break
                if revision:
                    notes.append("An earlier workspace copy was edited; those edits are preserved.")
                if not target.exists():
                    parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix=".loading-", dir=parent) as scratch:
                        staged = Path(scratch) / skill.name
                        write_package(package.files, staged)
                        if self._cancelled():
                            raise ToolError("Stopped by the user.")
                        os.replace(staged, target)
                    for relative in package.files:
                        self.ws.mark_own(target / relative)
                notes.append(f"Supporting files are in workspace folder `{self.ws.display(target)}`. "
                             "Relative paths in the skill refer to this folder; use read_file or "
                             "run_command there. No script has been executed.")
                notes.append("Files: " + ", ".join(sorted(package.files)))
        return "\n".join(notes) + "\n\n" + package.skill.instructions

    def save_skill(self, path: str) -> str:
        self._check_declined()
        if self._cancelled():
            raise ToolError("Stopped by the user.")
        source = self.ws.resolve(path)
        try:
            package = self.skills.package(source)
            previous = self.skills.version(package.skill.name)
        except SkillError as exc:
            raise ToolError(str(exc)) from None
        skill = package.skill
        action = "Update Skill" if previous is not None else "Save Skill"
        detail = (f"/{skill.name} — {skill.description}\n\n"
                  f"Source: {self.ws.display(source)}\n"
                  f"{len(package.files)} file(s), {human_size(sum(map(len, package.files.values())))}. "
                  "Available in every new and existing chat, not just this conversation. "
                  "Loading it will not run its scripts."
                  + (" The previous version goes to the Trash." if previous is not None else ""))
        preview = package.files["SKILL.md"].decode("utf-8")
        if len(package.files) > 1:
            preview += "\n\nIncluded files:\n" + "\n".join(sorted(package.files))
        self._ask("Save this reusable skill?", detail, preview, action,
                  "The user said no to saving this skill.",
                  reason="This changes the skill library shared by all chats. Review the instructions first.")
        if self._cancelled():
            raise ToolError("Stopped by the user.")
        try:
            old = self.skills.install(package, previous, self.ws.trash)
        except SkillError as exc:
            raise ToolError(str(exc)) from None
        return (f"Saved /{skill.name}. It is available now in every new and existing chat; "
                "no restart is needed. Suggest it with /" + skill.name + " or the Skills picker."
                + (" The previous version is in the Trash." if old is not None else ""))

    @staticmethod
    def _json_data(source: Path) -> tuple[dict, bytes]:
        from .worksheet_pdf import MAX_SPEC_BYTES
        if not source.is_file():
            raise ToolError("The specification must be an existing JSON file.")
        with source.open("rb") as fh:
            raw = fh.read(MAX_SPEC_BYTES + 1)
        if len(raw) > MAX_SPEC_BYTES:
            raise ToolError("The JSON specification is too large.")
        try:
            data = json.loads(raw, strict=False)
        except json.JSONDecodeError as exc:
            raise ToolError(f"Invalid JSON specification: {json_error(exc)}. "
                            + _json_fix_hint(exc)) from None
        except (ValueError, UnicodeError) as exc:
            raise ToolError(f"Invalid JSON specification: {exc}") from None
        if not isinstance(data, dict):
            raise ToolError("The JSON specification must be an object.")
        return data, raw

    def _quiz_source(self, path: str) -> tuple[dict, str, str, dict[Path, str]]:
        from .answer_key import quiz_from_pdf, quiz_from_spec
        from .worksheet_pdf import DesignError

        source = self.ws.resolve(path)
        if not source.is_file() or source.suffix.lower() not in (".pdf", ".json"):
            raise ToolError("read_quiz needs an existing quiz .pdf or worksheet .json file.")
        with source.open("rb") as fh:
            raw = fh.read(MAX_PAGE_BYTES + 1)
        if len(raw) > MAX_PAGE_BYTES:
            raise ToolError("The source quiz is too large; split it into shorter documents.")
        digest = hashlib.sha256(raw).hexdigest()
        snapshots = {source: digest}
        try:
            if source.suffix.lower() == ".json":
                quiz = quiz_from_spec(json.loads(raw, strict=False))
            else:
                if not raw.startswith(b"%PDF-"):
                    raise ToolError("The source file is not a PDF.")
                text = pdf_to_text(source, raw=True)
                companion = self.ws.resolve(str(source.with_suffix(".json")))
                if companion.is_file():
                    data, companion_raw = self._json_data(companion)
                    snapshots[companion] = hashlib.sha256(companion_raw).hexdigest()
                    quiz = quiz_from_spec(data, text)
                else:
                    quiz = quiz_from_pdf(text, source.stem.replace("_", " "))
        except (DesignError, ValueError, UnicodeError) as exc:
            raise ToolError(f"Cannot use this quiz source: {exc}") from None
        self._check_source_snapshots(snapshots)
        return quiz, self.ws.display(source), digest, snapshots

    def _check_source_snapshots(self, snapshots: dict[Path, str]) -> None:
        for source, digest in snapshots.items():
            if self.ws.resolve(str(source)) != source or not source.is_file():
                raise ToolError("The quiz source changed. Call read_quiz again before retrying.")
            with source.open("rb") as fh:
                raw = fh.read(MAX_PAGE_BYTES + 1)
            if len(raw) > MAX_PAGE_BYTES or hashlib.sha256(raw).hexdigest() != digest:
                raise ToolError("The quiz source changed during this operation. No key was saved. "
                                "Call read_quiz again before retrying.")

    def read_quiz(self, path: str, start_line: int = 1) -> str:
        self._check_declined()
        if self._cancelled():
            raise ToolError("Stopped by the user.")
        quiz, name, digest, _ = self._quiz_source(path)
        report = {
            "source_path": name, "source_sha256": digest, "title": quiz["title"],
            "source_excerpts": [
                {"number": i, "text": text} for i, text in enumerate(quiz["passages"], 1)],
            "questions": quiz["questions"],
        }
        return _text_page(json.dumps(report, ensure_ascii=False, indent=2), name, start_line, "read_quiz")

    def render_answer_key(self, spec_path: str, path: str) -> str:
        from .answer_key import key_document
        from .worksheet_pdf import DesignError, validate_spec

        self._check_declined()
        if self._cancelled():
            raise ToolError("Stopped by the user.")
        source = self.ws.resolve(spec_path)
        data, _ = self._json_data(source)
        if not isinstance(data, dict) or not isinstance(data.get("source_path"), str):
            raise ToolError("Load answer-key, then call read_quiz. The key JSON needs source_path, "
                            "source_sha256 and an answers list, not rewritten worksheet questions.")
        quiz, name, digest, snapshots = self._quiz_source(data["source_path"])
        target = self.ws.resolve(path)
        if target in snapshots or source in snapshots:
            raise ToolError("Save the answer key separately; never replace the source quiz.")
        try:
            document, audit = key_document(data, quiz, name, digest)
            spec = validate_spec(document)
        except DesignError as exc:
            raise ToolError(f"Invalid answer key: {exc}") from None
        result = self._render_pdf(source, target, path, document, spec, "answer key",
                                  lambda: self._check_source_snapshots(snapshots))
        return result + "\nSource checks: " + json.dumps(audit, ensure_ascii=False)

    def render_worksheet(self, spec_path: str, path: str, expected_questions: int | None = None,
                         expected_multiple_choice: int | None = None,
                         answer_key_path: str | None = None) -> str:
        from .answer_key import worksheet_key
        from .worksheet_pdf import DesignError, normalize_spec, validate_spec, without_answers

        self._check_declined()
        if self._cancelled():
            raise ToolError("Stopped by the user.")
        source, target = self.ws.resolve(spec_path), self.ws.resolve(path)
        data, _ = self._json_data(source)
        data, adjusted = normalize_spec(data)
        try:
            spec = validate_spec(data)
        except (DesignError, ValueError, UnicodeError) as exc:
            hint = (" This is answer-key JSON for an existing quiz: load answer-key and use "
                    "render_answer_key." if isinstance(data, dict)
                    and data.keys() & {"answers", "source_path", "source_sha256"} else
                    " Fix that spot with edit_file, then render again.")
            raise ToolError(f"Invalid worksheet specification: {exc}{hint}") from None
        for label, expected, actual in (
                ("questions", expected_questions, len(spec["questions"])),
                ("multiple-choice questions", expected_multiple_choice,
                 sum(bool(q["options"]) for q in spec["questions"]))):
            if expected is None:
                continue
            if type(expected) is not int or not 0 <= expected <= 30:
                raise ToolError(f"The expected number of {label} must be an integer from 0 to 30.")
            if actual != expected:
                raise ToolError(f"Question format mismatch: the request needs {expected} {label}, "
                                f"but the saved worksheet has {actual}. Correct the question mix "
                                "to match the request before rendering; do not change the expected "
                                "count to match a mistaken draft.")
        jobs = [(target, path, without_answers(data), spec, "worksheet")]
        key_audit = None
        if answer_key_path:
            key_target = self.ws.resolve(answer_key_path)
            if key_target in (source, target):
                raise ToolError("Use three different file names: the JSON, the student PDF and "
                                "the answer-key PDF.")
            try:
                document, key_audit = worksheet_key(spec)
                key_spec = validate_spec(document)
            except DesignError as exc:
                raise ToolError(f"The answer key is not ready: {exc}") from None
            jobs.append((key_target, answer_key_path, document, key_spec, "answer key"))
        result = self._render_pdfs(source, jobs)
        if adjusted:
            result += ("\nHandled automatically (no action needed; the JSON file was not changed): "
                       + "; ".join(adjusted) + ".")
        if key_audit:
            return (result + "\nAnswer checks: " + json.dumps(key_audit, ensure_ascii=False)
                    + "\nThe student PDF has no answers; the answer key has every answer.")
        if any(q["key"] for q in [*spec["questions"], *filter(None, [spec["challenge"]])]):
            result += ("\nThe JSON's answers were kept off this student PDF. For a parent answer "
                       "key, render again with answer_key_path.")
        return result

    def _render_pdf(self, source: Path, target: Path, path: str, data: dict, spec: dict,
                    kind: str = "worksheet", check_source: Callable[[], None] | None = None) -> str:
        return self._render_pdfs(source, [(target, path, data, spec, kind)], check_source)

    def _render_pdfs(self, source: Path, jobs: list[tuple[Path, str, dict, dict, str]],
                     check_source: Callable[[], None] | None = None) -> str:
        """Render every PDF to scratch files; publish them together only if all passed."""
        targets = [job[0] for job in jobs]
        for target in targets:
            if target.is_dir() or target.suffix.lower() != ".pdf":
                raise ToolError("Give a .pdf output file, not a folder.")
            if source == target:
                raise ToolError("The specification and PDF need different file names.")
        if len(set(targets)) != len(targets):
            raise ToolError("The worksheet and its answer key need different PDF file names.")

        def fingerprint(target: Path):
            if not target.exists():
                return None
            st = target.stat()
            return st.st_ino, st.st_size, st.st_mtime_ns

        before = [fingerprint(t) for t in targets]
        names = [self.ws.display(t) for t in targets]
        existing = " and ".join(n for n, old in zip(names, before) if old is not None)
        if existing and not self.ws.confirm(
                "Replace a file?", f"Qwen wants to replace {existing}. Old versions go to the "
                "Trash only after every new PDF passes its layout and text checks."):
            raise ToolError(f"The user declined overwriting {existing}. Ask what they want, "
                            "or save under a new name.")
        kind = jobs[0][4]
        call = {"spec_path": self.ws.display(source), "path": jobs[0][1]}
        if len(jobs) > 1:
            call["answer_key_path"] = jobs[1][1]
        self._ask(f"Create this {kind} PDF?" if len(jobs) == 1 else "Create the worksheet and answer key?",
                  f"Create {' and '.join(names)} from {self.ws.display(source)} using the bundled "
                  "local renderer. It uses a separate headless browser, not your open browser, "
                  "for up to 2 minutes per PDF.",
                  f"render_{kind.replace(' ', '_')}(" + json.dumps(call) + ")",
                  "Create PDF" if len(jobs) == 1 else "Create PDFs",
                  "The user said no to creating this PDF.")
        if self._cancelled():
            raise ToolError("Stopped by the user.")
        env = child_env()
        python = shutil.which("python3", path=env["PATH"])
        if not python:
            raise ToolError("Python 3 is missing from the command environment.")
        rendered = []
        with contextlib.ExitStack() as scratch:
            for target, _path, data, spec, kind in jobs:
                target.parent.mkdir(parents=True, exist_ok=True)
                root = Path(scratch.enter_context(
                    tempfile.TemporaryDirectory(prefix=".qwen-pdf-", dir=target.parent)))
                rendered.append((root / "worksheet.pdf",
                                 *self._render_one(python, env, root, data, spec, kind,
                                                   self.ws.display(target))))
            for (target, path, *_), old in zip(jobs, before):
                if self.ws.resolve(path) != target or fingerprint(target) != old:
                    raise ToolError("The destination changed while the PDF was being made. "
                                    "It was not replaced; read it again before retrying.")
            if not self.files or not self.commands or self._cancelled():
                raise ToolError("PDF rendering was stopped or disabled. No file was replaced.")
            if check_source:
                check_source()
            backups = []
            for target, (staged, *_) in zip(targets, rendered):
                backup = ""
                if target.exists() and not self.ws._is_own(target) and target.stat().st_size:
                    self.ws.trash(target)
                    backup = " The old version is in the Trash."
                os.replace(staged, target)
                backups.append(backup)
        lines = []
        for (target, _p, _d, spec, kind), (_s, report, approved), backup in zip(jobs, rendered, backups):
            self.ws.mark_own(target)
            if kind == "answer key":
                for field in ("questions", "multiple_choice", "challenge", "answers"):
                    report.pop(field, None)
                report["answer_entries"] = len(spec["reading"])
            lines.append(f"Created {self.ws.display(target)} ({human_size(target.stat().st_size)})."
                         f"{backup}\n"
                         + (f"The user approved {approved} pages for this export; the source JSON "
                            "was not changed.\n" if approved is not None else "")
                         + f"Layout report: {json.dumps(report, ensure_ascii=False)}")
        if any(report.get("icon_substitutions") for _s, report, _a in rendered):
            lines.append("Emoji the font cannot draw as one picture were replaced automatically "
                         "(see icon_substitutions).")
        return "\n".join(lines) + "\nThese are automated layout/text/emoji checks, not a visual review."

    def _render_one(self, python: str, env: dict, root: Path, data: dict, spec: dict,
                    kind: str, name: str) -> tuple[dict, int | None]:
        """Render one checked PDF into `root`; a page-limit conflict asks the user once."""
        from .worksheet_pdf import MAX_PAGES

        self.status(f"Laying out the {kind} and checking its pages…")
        approved_pages = None
        staged_spec, staged_pdf = root / "spec.json", root / "worksheet.pdf"
        staged_spec.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for attempt in range(2):
            if not self.files or not self.commands:
                raise ToolError("PDF rendering needs Files and Run enabled. No PDF was saved.")
            if self._cancelled():
                raise ToolError("Stopped by the user. The existing PDF was not changed.")
            rc, out, reason = run_streaming(
                [python, str(Path(__file__).with_name("worksheet_pdf.py")),
                 str(staged_spec), str(staged_pdf)],
                self.ws.root, env, 120, self.cancel)
            if reason == "cancelled" or self._cancelled():
                raise ToolError("Stopped by the user. The existing PDF was not changed.")
            if reason == "timeout":
                raise ToolError("The PDF renderer timed out and was stopped. "
                                "The existing PDF was not changed.")
            if rc not in (0, 2):
                raise ToolError(f"The {kind} PDF was not saved.\n{out[-3000:]}")
            try:
                report = json.loads(out)
            except ValueError:
                raise ToolError("The renderer returned no valid layout report; no PDF was saved.") from None
            if rc == 0:
                break
            if (not isinstance(report, dict) or report.get("code") != "page_limit"
                    or report.get("ok") is not False or type(report.get("pages")) is not int
                    or report.get("max_pages") != spec["max_pages"]
                    or report["pages"] <= spec["max_pages"]):
                raise ToolError("The renderer returned an invalid page-limit report; no PDF was saved.")
            pages = report["pages"]
            if attempt or pages > MAX_PAGES:
                self.input_required = (
                    f"The {kind} needs {pages} pages. No PDF was saved. "
                    "Ask the user how to split it into shorter documents; "
                    "do not remove questions or reduce writing space.")
                raise ToolError(self.input_required)
            count_summary = (
                f"{len(spec['reading'])} source-matched answers" if kind == "answer key" else
                f"{len(spec['questions'])} question{'s' if len(spec['questions']) != 1 else ''} "
                f"({sum(bool(q['options']) for q in spec['questions'])} multiple choice)"
                + (" + a bonus challenge" if spec["challenge"] else ""))
            self._ask(
                f"Use {pages} pages for this {kind}?",
                f"The saved specification limits {name} to {spec['max_pages']} pages, but "
                f"the complete {kind} needs {pages}. Keep all content "
                "at readable sizes. Only this export's page allowance changes; "
                "the source JSON stays untouched.",
                count_summary,
                f"Use {pages} Pages",
                "The user did not allow more pages. No PDF was saved. Ask how they want to "
                "revise the worksheet; do not cut questions or writing space on your own.",
                reason=f"The {kind} cannot fit its page limit without changing the content or readability.")
            if self._cancelled():
                raise ToolError("Stopped by the user. The existing PDF was not changed.")
            approved_pages = spec["max_pages"] = pages
            staged_spec.write_text(json.dumps({**data, "max_pages": pages}, ensure_ascii=False),
                                   encoding="utf-8")
            self.status(f"Creating the approved {pages}-page {kind}…")
        if (not isinstance(report, dict) or report.get("ok") is not True
                or report.get("text_preserved") is not True
                or report.get("layout_checked") is not True
                or report.get("portrait") is not True
                or type(report.get("pages")) is not int
                or not 1 <= report["pages"] <= spec["max_pages"]
                or type(report.get("color_emoji_checked")) is not int
                or report["color_emoji_checked"] < 1
                or not staged_pdf.is_file()):
            raise ToolError("The renderer did not confirm a valid PDF; no PDF was saved.")
        with staged_pdf.open("rb") as fh:
            if fh.read(5) != b"%PDF-":
                raise ToolError("The renderer output is not a PDF; no file was replaced.")
        return report, approved_pages

    def open_file(self, path: str) -> str:
        self._check_declined()
        target = self.ws.resolve(path)
        if not target.is_file() or not target.stat().st_size:
            raise ToolError(f"Cannot open {path}: the file is missing or empty. Create it successfully first.")
        def fingerprint():
            st = target.stat()
            return st.st_ino, st.st_size, st.st_mtime_ns, st.st_mode

        before = fingerprint()
        env = child_env()
        opener = shutil.which("gio", path=env["PATH"])
        if not opener:
            raise ToolError("The desktop opener is missing (gio from libglib2.0-bin).")
        if self._cancelled():
            raise ToolError("Stopped by the user.")
        if target.suffix.lower() == ".pdf":
            with target.open("rb") as fh:
                if fh.read(5) != b"%PDF-":
                    raise ToolError("Cannot open: this file is not a PDF.")
            if not shutil.which("pdfinfo", path=env["PATH"]):
                raise ToolError("Checking this PDF needs pdfinfo (poppler-utils).")
            try:
                info = subprocess.run(["pdfinfo", str(target)], env=env, capture_output=True,
                                      stdin=subprocess.DEVNULL, timeout=15)
            except subprocess.TimeoutExpired:
                raise ToolError("The PDF check timed out; no open request was sent.") from None
            if info.returncode:
                raise ToolError("Cannot open an invalid PDF: "
                                + info.stderr.decode("utf-8", errors="replace")[-1000:])
        executable = os.access(target, os.X_OK) or target.suffix.lower() == ".desktop"
        self._ask("Open this file?", f"Ask the desktop to open {self.ws.display(target)}.",
                  shlex.join([opener, "open", str(target)]), "Open",
                  "The user said no to opening this file.",
                  reason="This file can execute code." if executable else None)
        if self._cancelled() or not self.files or not self.commands:
            raise ToolError("Opening was stopped or disabled; no open request was sent.")
        if self.ws.resolve(path) != target or not target.is_file() or fingerprint() != before:
            raise ToolError("The file changed before opening. Read it again before retrying.")
        # Do not use run_streaming: its process-group cleanup would kill a newly opened viewer.
        with tempfile.TemporaryFile() as log:
            proc = subprocess.Popen([opener, "open", str(target)], cwd=self.ws.root, env=env,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            deadline = time.monotonic() + 15
            while proc.poll() is None:
                if self._cancelled() or not self.files or not self.commands or time.monotonic() >= deadline:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    raise ToolError("Opening was stopped or timed out. The desktop may already "
                                    "have received the request; window visibility is not verified.")
                time.sleep(.05)
            if proc.returncode:
                log.seek(0)
                detail = log.read(2000).decode("utf-8", errors="replace")
                raise ToolError(f"The desktop opener failed (exit {proc.returncode}): {detail}")
        return (f"Open requested for {self.ws.display(target)}. The file exists and the desktop "
                "opener accepted the request; whether a window appeared is not verified.")

    @staticmethod
    def open_command_path(command: str) -> str | None:
        if not isinstance(command, str):
            return None
        try:
            words = shlex.split(command)
        except ValueError:
            return None
        if words[-1:] == ["&"]:
            words = words[:-1]
        if ((len(words) == 2 and words[0] == "xdg-open")
                or (len(words) == 3 and words[:2] == ["gio", "open"])):
            return words[-1]
        return None

    def run_command(self, command: str, why: str = "", timeout_seconds: int = RUN_TIMEOUT) -> str:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("No command given.")
        self._check_declined()
        open_path = self.open_command_path(command)
        if open_path is not None:
            return self.open_file(open_path)
        if len(command) > 20000:
            raise ToolError("The command is too long. Save it as a script with write_file and run that.")
        try:
            timeout = max(5, min(MAX_RUN_TIMEOUT, int(timeout_seconds or RUN_TIMEOUT)))
        except (TypeError, ValueError):
            timeout = RUN_TIMEOUT
        why = str(why or "").strip() or "Qwen didn't say why."
        detail = (f"{why}\n\nIt runs in {_home(self.ws.root)} with your account's access, "
                  f"for up to {_duration(timeout)}.")
        missing = _missing_programs(command)
        if missing:
            raise ToolError(_missing_hint(missing) + " The command was not run.")
        self._ask("Run this command?", detail, command.strip(), "Run",
                  "The user said no to this command.", command_risk(command, self.ws.root))
        if self._cancelled():
            raise ToolError("Stopped by the user.")
        before = _snapshot(self.ws.root)
        self.status("Running the command…")
        rc, out, reason = run_streaming(
            ["/bin/bash", "-c", command], self.ws.root, child_env(), timeout, self.cancel,
            lambda line: self.status(f"Running… {line}"))
        changed, deleted = _changes(before, _snapshot(self.ws.root))
        notes = []
        if changed:
            shown = ", ".join(f"{p} ({human_size(self._size(p))})" for p in changed[:15])
            more = f" and {len(changed) - 15} more" if len(changed) > 15 else ""
            notes.append(f"Files created or changed in the workspace: {shown}{more}.")
            for p in changed[:200]:
                self.ws.mark_own(self.ws.root / p)
        if deleted:
            notes.append(f"Files deleted: {', '.join(deleted[:15])}"
                         + (f" and {len(deleted) - 15} more." if len(deleted) > 15 else "."))
        body = ("Output:\n" + out) if out.strip() else "(no output)"
        tail = ("\n" + "\n".join(notes)) if notes else ""
        if reason == "cancelled":
            raise ToolError(f"Stopped by the user.\n{body}{tail}")
        if reason == "timeout":
            raise ToolError(f"Timed out after {_duration(timeout)} and was stopped. For slow jobs "
                            f"pass a larger timeout_seconds (up to {MAX_RUN_TIMEOUT}).\n{body}{tail}")
        if rc != 0:
            hint = ""
            if rc == 127 or re.search(r"command not found|No such file or directory", out[-600:]):
                hint = ("\nIf a program is missing, check_programs shows what's installed and "
                        "install_packages can install it.")
            raise ToolError(f"The command failed with exit code {rc}.\n{body}{tail}{hint}")
        return f"Exit code 0.\n{body}{tail}"

    def _size(self, rel: str) -> int:
        try:
            return (self.ws.root / rel).stat().st_size
        except OSError:
            return 0

    def install_packages(self, packages, why: str = "", manager: str = "apt") -> str:
        pkgs = list(dict.fromkeys(_as_list(packages)))
        if not pkgs:
            raise ToolError("No packages given.")
        self._check_declined()  # before apt-get update or planning, which run without asking
        if len(pkgs) > 20:
            raise ToolError("Too many packages at once (20 at most).")
        manager = str(manager or "apt").strip().lower()
        why = str(why or "").strip() or "Qwen didn't say why."
        if manager in ("apt", "apt-get", "debian", "system"):
            return self._install_apt(pkgs, why)
        if manager in ("pip", "pip3", "python"):
            return self._install_pip(pkgs, why)
        raise ToolError("manager must be 'apt' or 'pip'.")

    def _install_apt(self, pkgs: list[str], why: str) -> str:
        bad = [p for p in pkgs if not APT_NAME.match(p)]
        if bad:
            raise ToolError(f"Not valid Debian package names: {', '.join(bad)}. Use lowercase "
                            "names like 'tesseract-ocr'.")
        missing = [p for p in pkgs if p not in apt_installed(pkgs)]
        if not missing:
            return f"Already installed: {', '.join(pkgs)}. {self._programs_of(pkgs)}".strip()
        unknown = [p for p in missing if p not in apt_candidates(missing)]
        if unknown:
            self.status("Updating the list of available software…")
            run_streaming(root_argv(["apt-get", "update", "-q"]), "/", root_env(), 600, self.cancel)
            if self._cancelled():
                raise ToolError("Stopped by the user.")
            unknown = [p for p in missing if p not in apt_candidates(missing)]
        if unknown:
            hints = []
            for p in unknown:
                similar = [s for s in apt_search(p) if s != p]
                hints.append(f"'{p}'" + (f" (similar: {', '.join(similar)})" if similar else ""))
            raise ToolError(f"Debian has no package named {', '.join(hints)}. Check the name and "
                            "try again.")
        self.status("Checking what needs to be installed…")
        plan = apt_plan(missing)
        if plan.get("error"):
            raise ToolError(f"apt can't install this:\n{plan['error']}")
        facts = []
        if plan.get("count"):
            extra = plan["count"] - len(missing)
            facts.append(f"{plan['count']} package{'s' if plan['count'] != 1 else ''}"
                         + (f" including {extra} it depends on" if extra > 0 else ""))
        if plan.get("download"):
            facts.append(f"{plan['download']} download")
        if plan.get("disk"):
            facts.append(f"{plan['disk']} of space")
        detail = why + "\n\n" + "From Debian" + (f": {', '.join(facts)}." if facts else ".")
        if plan.get("remove"):
            detail += f"\nWarning: this would also remove {', '.join(plan['remove'])}."
        self._ask("Install software?", detail, "apt install " + " ".join(missing), "Install",
                  f"The user said no to installing {', '.join(missing)}.", apt_big_reason(plan))
        # Once apt has downloaded everything, dpkg changes the system: Stop must not cut it off.
        # Checked on every chunk of output, since the status line only shows some lines.
        state = {"dpkg": False, "tail": ""}
        dpkg_started = re.compile(r"Fetched |Extracting templates|Preconfiguring|Selecting "
                                  r"previously|Preparing to unpack|Unpacking|Setting up")

        def on_chunk(text: str) -> None:
            if not state["dpkg"] and dpkg_started.search(state["tail"] + text):
                state["dpkg"] = True
            state["tail"] = text[-40:]

        self.status(f"Installing {', '.join(missing)}…")
        rc, out, reason = run_streaming(
            root_argv(["apt-get", "install", "-y", "-q", "-o", "Dpkg::Use-Pty=0", *missing]),
            "/", root_env(), INSTALL_TIMEOUT, self.cancel,
            lambda line: self.status(f"Installing… {line}"),
            killable=lambda: not state["dpkg"], on_chunk=on_chunk)
        done = apt_installed(missing)
        if reason == "cancelled" and not done:
            raise ToolError("Stopped by the user before the install finished.")
        if reason == "timeout" or rc != 0 or set(missing) - done:
            raise ToolError(f"The install failed (exit code {rc}).\n{out[-3000:]}")
        return f"Installed {', '.join(missing)}. {self._programs_of(missing)}".strip()

    @staticmethod
    def _programs_of(pkgs: list[str]) -> str:
        """Commands the packages provide, so the model knows what to run."""
        try:
            r = _query(["dpkg", "-L", *pkgs])
        except (OSError, subprocess.SubprocessError):
            return ""
        progs = sorted({os.path.basename(p) for p in r.stdout.splitlines()
                        if re.match(r"^/usr/(local/)?s?bin/[^/]+$", p)})
        if not progs:
            return ""
        more = f" and {len(progs) - 25} more" if len(progs) > 25 else ""
        return f"Commands available: {', '.join(progs[:25])}{more}."

    def _install_pip(self, pkgs: list[str], why: str) -> str:
        bad = [p for p in pkgs if not PIP_NAME.match(p)]
        if bad:
            raise ToolError(f"Not valid pip package names: {', '.join(bad)}.")
        # pip installs a local archive with a matching file name instead of the PyPI package.
        files = [p for p in pkgs if re.search(r"\.(whl|zip|tar|tgz|tbz2?|txz|gz|bz2|xz|egg)$",
                                              re.split(r"[\[=<>~!]", p)[0], re.I)]
        if files:
            raise ToolError(f"Give PyPI package names, not file names: {', '.join(files)}.")
        venv = venv_dir()
        py = venv / "bin" / "python"
        if py.exists():
            bare = [p for p in pkgs if re.fullmatch(r"[A-Za-z0-9._\-]+", p)]
            if len(bare) == len(pkgs):
                r = _query([str(py), "-c", "import importlib.metadata as m, sys\n"
                            "for n in sys.argv[1:]:\n"
                            "    try: m.version(n)\n"
                            "    except m.PackageNotFoundError: print(n)", *pkgs])
                if r.returncode == 0 and not r.stdout.strip():
                    return f"Already installed for python3: {', '.join(pkgs)}."
        detail = (f"{why}\n\nFrom PyPI, into Qwen's own Python environment "
                  f"({_home(venv)}); the rest of the system is not changed.")
        reason = None
        if self.auto_approve():  # only worth a lookup when the answer decides whether to ask
            self.status("Checking the download size…")
            reason = pip_big_reason(pkgs)
        self._ask("Install Python packages?", detail, "pip install " + " ".join(pkgs), "Install",
                  f"The user said no to installing {', '.join(pkgs)}.", reason)
        if not py.exists():
            self.status("Creating Qwen's Python environment…")
            self._make_venv(venv)
        self.status(f"Installing {', '.join(pkgs)}…")
        # Run from the venv, not the workspace, so nothing there can be taken for a package.
        rc, out, reason = run_streaming(
            [str(py), "-m", "pip", "install", "--no-input", "--progress-bar", "off", *pkgs],
            str(venv), child_env(), INSTALL_TIMEOUT, self.cancel,
            lambda line: self.status(f"Installing… {line}"))
        if reason == "cancelled":
            raise ToolError("Stopped by the user before the install finished.")
        if reason == "timeout" or rc != 0:
            raise ToolError(f"pip failed (exit code {rc}).\n{out[-3000:]}")
        return (f"Installed {', '.join(pkgs)}. In run_command, python3 and pip now use this "
                "environment.")

    def _make_venv(self, venv: Path) -> None:
        venv.parent.mkdir(parents=True, exist_ok=True)
        errors = []
        for base in dict.fromkeys(filter(None, ["/usr/local/bin/python3", shutil.which("python3"),
                                                "/usr/bin/python3"])):
            if not os.path.exists(base):
                continue
            r = subprocess.run([base, "-m", "venv", "--system-site-packages", str(venv)],
                               env=child_env(), stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=300)
            if r.returncode == 0 and (venv / "bin" / "python").exists():
                return
            errors.append(f"{base}: {(r.stderr or r.stdout).strip()[-300:]}")
            shutil.rmtree(venv, ignore_errors=True)
        raise ToolError("Couldn't create a Python environment.\n" + "\n".join(errors))

    def check_programs(self, programs=None, python_modules=None) -> str:
        progs, mods = _as_list(programs), _as_list(python_modules)
        if not progs and not mods:
            raise ToolError("Give programs and/or python_modules to check.")
        lines = []
        path = child_env()["PATH"]
        missing = [p for p in progs if PROGRAM_NAME.match(p) and not shutil.which(p, path=path)]
        known = apt_candidates([PROGRAM_PACKAGES.get(p, p) for p in missing]) if missing else set()
        for p in progs[:30]:
            if not PROGRAM_NAME.match(p):
                lines.append(f"{p}: not a valid program name")
            elif found := shutil.which(p, path=path):
                lines.append(f"{p}: installed ({found})")
            else:
                pkg = PROGRAM_PACKAGES.get(p, p)
                lines.append(f"{p}: not installed" + (f" (Debian package: {pkg})" if pkg in known else ""))
        if mods:
            py = venv_dir() / "bin" / "python"
            py = str(py) if py.exists() else (shutil.which("python3", path=path) or "python3")
            ok = [m for m in mods[:30] if MODULE_NAME.match(m)]
            try:
                r = _query([py, "-c", "import importlib.util, sys\nfor n in sys.argv[1:]:\n"
                            "    try: print(n, bool(importlib.util.find_spec(n)))\n"
                            "    except Exception: print(n, False)", *ok])
                have = {ln.split()[0] for ln in r.stdout.splitlines() if ln.endswith(" True")}
            except (OSError, subprocess.SubprocessError):
                have = set()
            for m in mods[:30]:
                if m not in ok:
                    lines.append(f"python module {m}: not a valid module name")
                else:
                    lines.append(f"python module {m}: " + ("installed" if m in have else
                                 "not installed (install_packages with manager 'pip')"))
        return "\n".join(lines)

    @staticmethod
    def parse_args(arguments) -> dict:
        if isinstance(arguments, dict):
            return arguments
        if not arguments or not str(arguments).strip():
            return {}
        try:
            val = json.loads(arguments)
        except ValueError:
            raise ToolError("The tool arguments were not valid JSON.") from None
        if not isinstance(val, dict):
            raise ToolError("Tool arguments must be a JSON object.")
        return val

    def run(self, name: str, arguments) -> tuple[bool, str]:
        """Run a tool call; returns (ok, text for the model). Never raises ToolError."""
        try:
            self._check_declined()
            if self.input_required:
                raise ToolError("Wait for the user's decision before using more tools. " + self.input_required)
            if name not in self.names():
                raise ToolError(f"Unknown or disabled tool '{name}'. Available: {', '.join(sorted(self.names())) or 'none'}.")
            args = self.parse_args(arguments)
            fn = self._impl(name)
            params = inspect.signature(fn).parameters
            args = {k: v for k, v in args.items() if k in params}
            missing = [p.name for p in params.values()
                       if p.default is inspect.Parameter.empty and p.name not in args]
            if missing:
                raise ToolError(f"Missing argument(s): {', '.join(missing)}.")
            return True, fn(**args)
        except ToolError as e:
            return False, f"Error: {e}"
        except PermissionError as e:
            return False, f"Error: permission denied ({e.filename})."
        except OSError as e:
            return False, f"Error: {e.strerror or e}"
        except Exception as e:  # a tool bug must not end the conversation
            return False, f"Error: {type(e).__name__}: {e}"

    @staticmethod
    def describe(name: str, args: dict, running: bool = False, ok: bool = True) -> str:
        """Short human description for the UI, e.g. 'Searching the web for “x”'."""
        a = args or {}

        def q(key, limit=70):
            v = " ".join(str(a.get(key, "") or "").split())
            return v if len(v) <= limit else v[:limit - 1] + "…"
        path = q("path") or "the workspace"

        def names(key, fallback):
            try:
                v = ", ".join(_as_list(a.get(key)))
            except ToolError:
                v = ""
            v = v or fallback
            return v if len(v) <= 70 else v[:69] + "…"
        pkgs = names("packages", "software")
        checked = ", ".join(filter(None, (names("programs", ""), names("python_modules", ""))))
        verbs = {
            "web_search": ("Searching the web for", "Searched the web for", f"“{q('query')}”"),
            "fetch_url": ("Reading", "Read", q("url")),
            "download_file": ("Downloading", "Downloaded", q("url", 60)),
            "list_files": ("Looking in", "Looked in", path),
            "read_file": ("Reading", "Read", path),
            "read_quiz": ("Reading source quiz", "Read source quiz", path),
            "write_file": (("Appending to", "Appended to") if a.get("append") else ("Writing", "Wrote")) + (path,),
            "edit_file": ("Editing", "Edited", path),
            "make_directory": ("Creating folder", "Created folder", path),
            "move_path": ("Moving", "Moved", f"{q('source', 40)} → {q('destination', 40)}"),
            "delete_path": ("Deleting", "Deleted", path),
            "run_command": ("Running", "Ran", q("command", 70)),
            "install_packages": ("Installing", "Installed", pkgs),
            "check_programs": ("Checking for", "Checked for", checked or "programs"),
            "load_skill": ("Reading skill", "Read skill", q("name")),
            "list_skills": ("Finding skills", "Found skills", q("query") or "in the shared library"),
            "save_skill": ("Saving skill", "Saved skill", path),
            "render_worksheet": ("Designing PDF", "Created PDF",
                                 path + (" + answer key" if a.get("answer_key_path") else "")),
            "render_answer_key": ("Checking and creating answer key", "Created answer-key PDF", path),
            "open_file": ("Requesting desktop open", "Requested desktop open", path),
        }
        tried = {"run_command": "Tried to run", "install_packages": "Tried to install",
                 "render_worksheet": "Could not create PDF", "load_skill": "Could not read skill",
                 "render_answer_key": "Could not create answer key",
                 "read_quiz": "Could not read source quiz", "open_file": "Could not open",
                 "save_skill": "Could not save skill", "list_skills": "Could not list skills",
                 "edit_file": "Tried to edit",
                 "write_file": "Tried to append to" if a.get("append") else "Tried to write"}
        if name not in verbs:
            return f"Using {name}" if running else f"Used {name}"
        now, done, obj = verbs[name]
        if name in ("write_file", "edit_file") and not a.get("path"):
            obj = "a file"
        if not running and not ok and name in tried:
            return f"{tried[name]} {obj}"
        return f"{now if running else done} {obj}"

    def label(self, name: str, args: dict, running: bool = False, ok: bool = True) -> str:
        """describe(), with workspace paths shown relative to it."""
        text = self.describe(name, args, running, ok)
        root = str(self.ws.root)
        return text.replace(root + "/", "").replace(root, "the workspace").replace(
            os.path.expanduser("~") + "/", "~/")

    def result_paths(self, name: str, args: dict, result: str = "") -> list[Path]:
        """Workspace files or folders a successful tool call produced, for 'Open' buttons."""
        if name == "run_command":
            # Read back from the result, so this also works for chats opened later.
            m = re.search(r"^Files created or changed in the workspace: (.*)$", result or "", re.M)
            found = []
            for rel in re.findall(r"(?:^|, )(.+?) \([\d.]+ (?:bytes|KB|MB|GB)\)", m.group(1) if m else ""):
                parts = Path(rel).parts
                if any(x.startswith(".") or x in ("__pycache__", "node_modules") for x in parts):
                    continue
                p = self.ws.root / rel
                if p.is_file() and p.resolve().is_relative_to(self.ws.root):
                    found.append(p)
            return found[:5]
        if name == "render_worksheet" and (args or {}).get("answer_key_path"):
            return [p for key_path in (args.get("path"), args["answer_key_path"])
                    for p in self.result_paths("render_answer_key", {"path": key_path})]
        key = {"write_file": "path", "edit_file": "path", "make_directory": "path",
               "move_path": "destination", "download_file": "path", "render_worksheet": "path",
               "render_answer_key": "path"}.get(name)
        if not key:
            return []
        val = (args or {}).get(key) or ""
        if name == "download_file" and not val:
            val = os.path.basename(urllib.parse.urlsplit(str(args.get("url", ""))).path)
        try:
            p = self.ws.resolve(str(val))
        except ToolError:
            return []
        if name == "move_path" and p.is_dir():  # moved into a folder
            src = os.path.basename(str((args or {}).get("source", "")).rstrip("/"))
            if src and (p / src).exists():
                p = p / src
        return [p] if p.exists() else []

    def result_path(self, name: str, args: dict, result: str = "") -> Path | None:
        paths = self.result_paths(name, args, result)
        return paths[0] if paths else None
