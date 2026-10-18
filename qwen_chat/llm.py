"""The llama.cpp server process and a streaming OpenAI-style chat client."""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from .config import CACHE_DIR


class ServerError(Exception):
    pass


class LLMError(Exception):
    def __init__(self, message: str, context_overflow: bool = False):
        super().__init__(message)
        self.context_overflow = context_overflow


class Cancelled(Exception):
    pass


GPU_BUSY = Path("/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage")


def gpu_busy_percent() -> int | None:
    try:
        return int(GPU_BUSY.read_text().strip().rstrip("%").strip())
    except (OSError, ValueError):
        return None


class ServerManager:
    """Reuses a running llama-server or starts one through the qwen-server launcher."""

    def __init__(self, settings):
        self.settings = settings
        self.url = settings["server_url"].rstrip("/")
        self.proc: subprocess.Popen | None = None
        self.n_ctx = int(settings["context_size"])
        self.model_name = "Qwen"
        self.log_path = CACHE_DIR / "server.log"
        self._lock = threading.Lock()
        self._token_ids: dict[str, int | None] = {}

    @property
    def owned(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def health(self) -> str:
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=3) as r:
                return "ok" if r.status == 200 else "loading"
        except urllib.error.HTTPError as e:
            return "loading" if e.code == 503 else "down"
        except (urllib.error.URLError, OSError):
            return "down"

    def _log_tail(self, n: int = 6) -> str:
        try:
            lines = self.log_path.read_text(errors="replace").strip().splitlines()
        except OSError:
            return ""
        keep = [ln for ln in lines if ln.startswith("qwen:") or "error" in ln.lower()
                or "Close some" in ln or "QWEN_SKIP" in ln]
        return "\n".join((keep or lines)[-n:])

    def ensure_running(self, on_status: Callable[[str], None] = lambda s: None,
                       cancel: threading.Event | None = None) -> None:
        with self._lock:
            state = self.health()
            if state == "down" and not self.owned:
                cmd = [os.path.expanduser(c) for c in self.settings["server_command"]]
                if not os.access(cmd[0], os.X_OK):
                    raise ServerError(f"Model launcher not found: {cmd[0]}")
                env = dict(os.environ)
                env.pop("LD_PRELOAD", None)
                env["QWEN_CTX"] = str(self.settings["context_size"])
                port = urllib.parse.urlsplit(self.url).port or 8080
                if port != 8080:
                    cmd += ["--port", str(port)]
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                on_status("Starting the model on the GPU…")
                with open(self.log_path, "wb") as log:
                    self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                                 stderr=subprocess.STDOUT, env=env,
                                                 start_new_session=True)
            deadline = time.monotonic() + 420
            while True:
                state = self.health()
                if state == "ok":
                    break
                if self.proc is not None and self.proc.poll() is not None:
                    self.proc = None
                    raise ServerError(self._log_tail() or "The model server exited during startup.")
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if time.monotonic() > deadline:
                    raise ServerError("The model took more than 7 minutes to load.")
                on_status("Loading Qwen onto the GPU…")
                time.sleep(0.8)
            self._read_props()

    def _read_props(self) -> None:
        self._token_ids.clear()
        try:
            with urllib.request.urlopen(self.url + "/props", timeout=5) as r:
                props = json.loads(r.read())
        except (OSError, ValueError):
            return
        self.n_ctx = int(props.get("default_generation_settings", {}).get("n_ctx") or self.n_ctx)
        path = props.get("model_alias") or props.get("model_path") or ""
        name = os.path.basename(path).removesuffix(".gguf")
        if name:
            # "Qwen3.5-9B-Q4_K_M" -> "Qwen3.5-9B"
            parts = name.split("-")
            if len(parts) > 2 and parts[-1][:1] in "QIF" and "_" in parts[-1]:
                name = "-".join(parts[:-1])
            self.model_name = name

    def token_id(self, text: str) -> int | None:
        """Id of a special token such as <tool_call>, or None if the model has no such token."""
        if text not in self._token_ids:
            tid = None
            req = urllib.request.Request(
                self.url + "/tokenize", data=json.dumps({"content": text}).encode(),
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    toks = json.loads(r.read()).get("tokens") or []
                if len(toks) == 1:
                    tid = int(toks[0]["id"] if isinstance(toks[0], dict) else toks[0])
            except (OSError, ValueError, KeyError, TypeError):
                pass
            self._token_ids[text] = tid
        return self._token_ids[text]

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


class ChatStream:
    """One streaming /v1/chat/completions request that can be aborted from another thread."""

    def __init__(self, base_url: str):
        parts = urllib.parse.urlsplit(base_url)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 80
        self._conn: http.client.HTTPConnection | None = None
        self._aborted = False

    def abort(self) -> None:
        self._aborted = True
        conn = self._conn
        if conn is not None and conn.sock is not None:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def run(self, payload: dict, on_reasoning: Callable[[str], None],
            on_content: Callable[[str], None],
            on_progress: Callable[[int, int], None] = lambda done, total: None,
            on_tool_draft: Callable[[str, int], None] = lambda name, chars: None) -> dict:
        """Returns {"message": assistant message, "timings": {...}, "finish_reason": str}."""
        payload = dict(payload, stream=True, return_progress=True)
        conn = http.client.HTTPConnection(self.host, self.port, timeout=900)
        self._conn = conn
        if self._aborted:
            raise Cancelled()
        content, reasoning = [], []
        calls: dict[int, dict] = {}
        timings: dict = {}
        finish = None
        done = False
        try:
            conn.request("POST", "/v1/chat/completions", body=json.dumps(payload).encode(),
                         headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
            resp = conn.getresponse()
            if resp.status != 200:
                self._raise_error(resp.status, resp.read())
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith(b"error:"):
                    self._raise_error(500, line[6:].strip())
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    done = True
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if "error" in chunk:
                    self._raise_error(400, data)
                if chunk.get("timings"):
                    timings = chunk["timings"]
                prog = chunk.get("prompt_progress")
                if prog and prog.get("total"):
                    on_progress(int(prog.get("processed", 0)), int(prog["total"]))
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                        on_reasoning(delta["reasoning_content"])
                    if delta.get("content"):
                        content.append(delta["content"])
                        on_content(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        c["id"] += tc.get("id") or ""
                        fn = tc.get("function") or {}
                        c["name"] += fn.get("name") or ""
                        c["arguments"] += fn.get("arguments") or ""
                        if c["name"]:
                            on_tool_draft(c["name"], len(c["arguments"]))
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        except (OSError, http.client.HTTPException) as e:
            if self._aborted:
                raise Cancelled() from None
            raise LLMError(f"Lost connection to the model server: {e}") from None
        finally:
            conn.close()
            self._conn = None
        if self._aborted:
            raise Cancelled()
        if not done and finish is None:
            raise LLMError("The model server stopped unexpectedly. Details are in "
                           "~/.cache/qwen-chat/server.log; send the message again to restart it.")
        msg: dict = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        if calls:
            msg["tool_calls"] = [
                {"id": c["id"] or f"call_{i}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                for i, c in sorted(calls.items())]
        return {"message": msg, "timings": timings, "finish_reason": finish}

    @staticmethod
    def _raise_error(status: int, body: bytes) -> None:
        try:
            err = json.loads(body).get("error", {})
        except ValueError:
            err = {"message": body.decode(errors="replace")[:300]}
        if isinstance(err, str):
            err = {"message": err}
        msg = err.get("message") or f"HTTP {status}"
        overflow = err.get("type") == "exceed_context_size_error" or "context" in msg.lower() and "exceed" in msg.lower()
        raise LLMError(msg, context_overflow=overflow)
