"""Tools the model can call: web search/fetch/download and a sandboxed workspace folder.

Every file operation goes through Workspace.resolve(), which follows symlinks and
".." with realpath() and rejects anything that does not land inside the workspace.
Web requests are limited to public http(s) addresses, so a page cannot steer the
model into probing localhost services (the model server itself listens there).
"""
from __future__ import annotations

import html
import inspect
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

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


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# --------------------------------------------------------------------------- workspace

class Workspace:
    def __init__(self, root: str | os.PathLike, confirm: Callable[[str, str], bool] | None = None):
        root = Path(os.path.expanduser(str(root)))
        root.mkdir(parents=True, exist_ok=True)
        self.root = Path(os.path.realpath(root))
        # confirm(title, detail) -> bool; asked before overwriting or deleting.
        self.confirm = confirm or (lambda title, detail: True)

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
        lines = text.splitlines()
        if not lines:
            return f"{self.display(f)} is empty."
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
        header = f"{self.display(f)} (lines {start}-{end} of {len(lines)}):\n"
        footer = ""
        if end < len(lines):
            footer = (f"\n[{len(lines) - end} more lines. Call read_file with "
                      f"start_line={end + 1} to continue.]")
        return header + "\n".join(out) + footer

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
        if existed and not append:
            if not self.confirm("Replace a file?",
                                f"Qwen wants to overwrite {name} ({human_size(f.stat().st_size)}) "
                                f"with new content ({human_size(len(data))})."):
                raise ToolError(f"The user declined overwriting {name}. "
                                "Ask what they want, or save under a new name.")
        f.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if append else os.O_TRUNC)
        fd = os.open(f, flags, 0o644)
        with os.fdopen(fd, "wb") as fh:
            if append and existed and f.stat().st_size and not _ends_with_newline(f):
                fh.write(b"\n")
            fh.write(data)
        verb = "Appended to" if append and existed else ("Overwrote" if existed else "Created")
        return f"{verb} {name} ({human_size(len(data))})."

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
        if not self.confirm("Delete from workspace?", detail):
            raise ToolError(f"The user declined deleting {name}.")
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return f"Deleted {name}."


def _ends_with_newline(f: Path) -> bool:
    with open(f, "rb") as fh:
        fh.seek(-1, os.SEEK_END)
        return fh.read(1) == b"\n"


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def pdf_to_text(path: Path) -> str:
    if not shutil.which("pdftotext"):
        raise ToolError("PDF support needs pdftotext (poppler-utils), which isn't installed.")
    try:
        res = subprocess.run(["pdftotext", "-q", str(path), "-"],
                             capture_output=True, timeout=90, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ToolError("Extracting text from the PDF took too long.") from None
    text = res.stdout.decode("utf-8", errors="replace")
    if not text.strip():
        raise ToolError("The PDF has no extractable text (it may be scanned images).")
    text = re.sub(r"[ \t]{2,}", " ", text.replace("\f", "\n"))
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
        if not ws.confirm("Replace a file?", f"Qwen wants to download {url} over {ws.display(dest)}."):
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
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return f"Saved {url} to {ws.display(dest)} ({human_size(total)}, {ctype})."


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
    _fn("list_files", "List files and folders in the workspace.",
        {"path": {**_S, "description": "Folder relative to the workspace (default '.')"},
         "recursive": {"type": "boolean", "description": "Include subfolders"}}, []),
    _fn("read_file", "Read a text, code, Markdown, CSV or PDF file from the workspace.",
        {"path": {**_S, "description": "File path relative to the workspace"},
         "start_line": {"type": "integer", "description": "Line to start from (default 1)"}}, ["path"]),
    _fn("write_file", "Create or overwrite a text file in the workspace (parent folders are created). "
        "Set append=true to add to the end instead.",
        {"path": {**_S, "description": "File path relative to the workspace, e.g. 'notes/plan.md'"},
         "content": {**_S, "description": "Full text to write"},
         "append": {"type": "boolean", "description": "Append instead of replacing"}},
        ["path", "content"]),
    _fn("make_directory", "Create a folder in the workspace.",
        {"path": {**_S, "description": "Folder path relative to the workspace"}}, ["path"]),
    _fn("move_path", "Move or rename a file or folder inside the workspace.",
        {"source": _S, "destination": _S}, ["source", "destination"]),
    _fn("delete_path", "Delete a file or folder from the workspace (the user is asked first).",
        {"path": _S}, ["path"]),
]
DOWNLOAD_TOOL = _fn(
    "download_file", "Download a file from the internet into the workspace.",
    {"url": {**_S, "description": "Full http(s) URL"},
     "path": {**_S, "description": "Destination path in the workspace (default: name from the URL)"}},
    ["url"])

FILE_RESULT_TOOLS = {"write_file", "download_file", "make_directory", "move_path"}


class ToolBox:
    def __init__(self, workspace: Workspace, web: bool = True, files: bool = True):
        self.ws = workspace
        self.web = web
        self.files = files

    def schemas(self) -> list[dict]:
        out = []
        if self.web:
            out += WEB_TOOLS
        if self.files:
            out += FILE_TOOLS
        if self.web and self.files:
            out.append(DOWNLOAD_TOOL)
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
            "write_file": ws.write_file,
            "make_directory": ws.make_directory,
            "move_path": ws.move_path,
            "delete_path": ws.delete_path,
        }.get(name)

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
    def describe(name: str, args: dict, running: bool = False) -> str:
        """Short human description for the UI, e.g. 'Searching the web for “x”'."""
        a = args or {}

        def q(key, limit=70):
            v = str(a.get(key, "") or "")
            return v if len(v) <= limit else v[:limit - 1] + "…"
        path = q("path") or "the workspace"
        verbs = {
            "web_search": ("Searching the web for", "Searched the web for", f"“{q('query')}”"),
            "fetch_url": ("Reading", "Read", q("url")),
            "download_file": ("Downloading", "Downloaded", q("url", 60)),
            "list_files": ("Looking in", "Looked in", path),
            "read_file": ("Reading", "Read", path),
            "write_file": (("Appending to", "Appended to") if a.get("append") else ("Writing", "Wrote")) + (path,),
            "make_directory": ("Creating folder", "Created folder", path),
            "move_path": ("Moving", "Moved", f"{q('source', 40)} → {q('destination', 40)}"),
            "delete_path": ("Deleting", "Deleted", path),
        }
        if name not in verbs:
            return f"Using {name}" if running else f"Used {name}"
        now, done, obj = verbs[name]
        return f"{now if running else done} {obj}"

    def result_path(self, name: str, args: dict) -> Path | None:
        """Workspace path a successful tool call produced, for 'Open' buttons."""
        key = {"write_file": "path", "make_directory": "path", "move_path": "destination",
               "download_file": "path"}.get(name)
        if not key:
            return None
        val = (args or {}).get(key) or ""
        if name == "download_file" and not val:
            val = os.path.basename(urllib.parse.urlsplit(str(args.get("url", ""))).path)
        try:
            p = self.ws.resolve(str(val))
        except ToolError:
            return None
        return p if p.exists() else None
