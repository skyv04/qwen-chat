"""Settings and per-user paths (XDG)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path


def _xdg(var: str, fallback: str, name: str = "qwen-chat") -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback) / name


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config")
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share")   # chats
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache")
# What Qwen needs to work (saved skills, its Python environment, the GPU helper) is kept
# apart from the chats, so clearing chat history can't remove it.
HARNESS_DIR = _xdg("XDG_DATA_HOME", ".local/share", "qwen-chat-harness")

DEFAULTS = {
    # The only folder the model can see. Changing it here is deliberate: the app
    # offers no way for the model (or a chat) to point it anywhere else.
    "workspace": "~/QwenWorkspace",
    "server_url": "http://127.0.0.1:8080",
    "server_command": ["~/.local/bin/qwen-server"],
    "context_size": 16384,
    "web": True,
    "files": True,
    # Lets the model run programs and install software. Routine commands and small installs
    # run without asking; risky commands and big installs always ask.
    "commands": True,
    # Ask before every command and install, not only the risky and big ones.
    "ask_commands": False,
    "thinking": False,
    # Ask before replacing or deleting a file (they go to the Trash either way).
    "ask_file_changes": False,
    "stop_server_on_exit": True,
    "color_scheme": "system",
    "window_size": [1180, 820],
}


class Settings(dict):
    def __init__(self, path: Path | None = None):
        super().__init__(copy.deepcopy(DEFAULTS))
        self.path = Path(path) if path else CONFIG_DIR / "settings.json"
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            self.update({k: v for k, v in data.items() if k in DEFAULTS})
        if data is None:
            self.save()

    @property
    def workspace_path(self) -> Path:
        return Path(os.path.expanduser(str(self["workspace"])))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self, indent=2) + "\n")
        os.replace(tmp, self.path)
