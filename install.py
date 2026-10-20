#!/usr/bin/python3
"""Install per-user launchers and the desktop entry without changing chat data."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
APP_ID = "io.github.skyv04.QwenChat"


def check_dependencies() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("Qwen Chat needs Python 3.11 or newer. Use Debian's /usr/bin/python3.")
    try:
        import gi
        import yaml

        for name, version in (("Gtk", "4.0"), ("Adw", "1"), ("GtkSource", "5")):
            gi.require_version(name, version)
        from gi.repository import Adw, Gtk, GtkSource
    except (ImportError, ValueError) as exc:
        raise SystemExit(
            f"Missing desktop dependency: {exc}\n"
            "Install python3-gi python3-yaml gir1.2-gtk-4.0 gir1.2-adw-1 "
            "gir1.2-gtksource-5, then run /usr/bin/python3 install.py."
        ) from exc


def install_file(path: Path, content: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            temporary.chmod(mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"Installed {path}")


def desktop_exec(path: Path) -> str:
    text = str(path)
    for character in ("\\", '"', "`", "$"):
        text = text.replace(character, "\\" + character)
    return '"' + text.replace("%", "%%") + '"'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop", action="store_true", help="also add an icon to the desktop")
    args = parser.parse_args()
    check_dependencies()
    bin_dir = Path.home() / ".local/bin"
    data_dir = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    launcher = (
        "#!/bin/sh\n"
        "unset LD_PRELOAD\n"
        'export GSK_RENDERER="${GSK_RENDERER:-cairo}"\n'
        f'export PYTHONPATH={shlex.quote(str(ROOT))}"${{PYTHONPATH:+:$PYTHONPATH}}"\n'
        f'exec {shlex.quote(sys.executable)} -m qwen_chat "$@"\n'
    )
    install_file(bin_dir / "qwen-chat", launcher.encode(), 0o755)
    model_launcher = (ROOT / "scripts/qwen").read_bytes()
    for name in ("qwen", "qwen-server"):
        install_file(bin_dir / name, model_launcher, 0o755)
    icon = ROOT / f"qwen_chat/data/icons/hicolor/scalable/apps/{APP_ID}.svg"
    install_file(data_dir / f"icons/hicolor/scalable/apps/{APP_ID}.svg", icon.read_bytes())
    entry = (
        "[Desktop Entry]\n"
        "Version=1.0\nType=Application\nName=Qwen Chat\nGenericName=AI Chat\n"
        "Comment=Local Qwen chat with web tools and a file workspace\n"
        f"Exec={desktop_exec(bin_dir / 'qwen-chat')}\n"
        f"Icon={APP_ID}\n"
        "Terminal=false\nCategories=Utility;Chat;\n"
        "Keywords=AI;LLM;Qwen;Assistant;Chat;\n"
        f"StartupWMClass={APP_ID}\nStartupNotify=true\n"
    ).encode()
    install_file(data_dir / f"applications/{APP_ID}.desktop", entry)
    if args.desktop:
        directory = Path.home() / "Desktop"
        if shutil.which("xdg-user-dir"):
            result = subprocess.run(["xdg-user-dir", "DESKTOP"], check=True,
                                    capture_output=True, text=True, timeout=5)
            directory = Path(result.stdout.strip())
            if not directory.is_absolute():
                raise SystemExit("xdg-user-dir did not return an absolute desktop path.")
        install_file(directory / "Qwen Chat.desktop", entry, 0o755)
        print("If XFCE asks, choose Allow Launching for the desktop icon.")
    print("\nOpen Qwen Chat from the application menu, or run ~/.local/bin/qwen-chat.")
    print("Keep this checkout: the launcher loads the app from it.")
    print("Model setup: docs/termux-setup.md. No model was downloaded or started.")


if __name__ == "__main__":
    main()
