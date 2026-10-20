"""Conversations saved as JSON files."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from .config import DATA_DIR


def title_from(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:57] + "…") if len(text) > 58 else (text or "New chat")


class ChatStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else DATA_DIR / "chats"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def new() -> dict:
        now = time.time()
        return {"id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
                "title": "New chat", "created": now, "updated": now, "messages": []}

    def _path(self, chat_id: str) -> Path:
        if not re.fullmatch(r"[\w-]+", chat_id):
            raise ValueError("bad chat id")
        return self.root / f"{chat_id}.json"

    def list(self) -> list[dict]:
        chats = []
        for f in self.root.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                chats.append({"id": data["id"], "title": data.get("title") or "Chat",
                              "updated": data.get("updated", 0)})
            except (OSError, ValueError, KeyError):
                continue
        return sorted(chats, key=lambda c: c["updated"], reverse=True)

    def load(self, chat_id: str) -> dict | None:
        try:
            return json.loads(self._path(chat_id).read_text())
        except (OSError, ValueError):
            return None

    def save(self, chat: dict) -> None:
        chat["updated"] = time.time()
        self._write(chat)

    def _write(self, chat: dict) -> None:
        path = self._path(chat["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(chat, ensure_ascii=False, indent=1))
        os.replace(tmp, path)

    def rename(self, chat_id: str, title: str) -> str:
        title = " ".join(title.split())
        if not title:
            raise ValueError("Enter a name for the chat.")
        chat = json.loads(self._path(chat_id).read_text())
        if not isinstance(chat, dict) or chat.get("id") != chat_id:
            raise ValueError("The saved chat could not be read.")
        chat["title"] = title
        self._write(chat)
        return title

    def delete(self, chat_id: str) -> None:
        try:
            self._path(chat_id).unlink()
        except FileNotFoundError:
            pass
