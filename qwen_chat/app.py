"""Gtk application: styling, app-level actions and the model server's lifetime."""
from __future__ import annotations

import os
import signal
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, __version__  # noqa: E402
from .config import Settings  # noqa: E402
from .llm import ServerManager  # noqa: E402

HERE = Path(__file__).resolve().parent


class QwenChatApp(Adw.Application):
    def __init__(self):
        flags = Gio.ApplicationFlags.FLAGS_NONE
        if os.environ.get("QWEN_CHAT_NON_UNIQUE") == "1":
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        # Sets the X11 WM_CLASS so panels match the window to the desktop entry and icon.
        GLib.set_prgname(APP_ID)
        GLib.set_application_name(APP_NAME)
        self.window = None
        self.settings: Settings | None = None
        self.server: ServerManager | None = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        from .window import apply_color_scheme
        self.settings = Settings()
        apply_color_scheme(self.settings["color_scheme"])
        display = Gdk.Display.get_default()
        # libadwaita's UI uses Adwaita's symbolic icons; desktop themes such as Tango (XFCE
        # here) lack many of them, which left blank header buttons.
        Gtk.Settings.get_for_display(display).props.gtk_icon_theme_name = "Adwaita"
        Gtk.IconTheme.get_for_display(display).add_search_path(str(HERE / "data" / "icons"))
        css = Gtk.CssProvider()
        css.load_from_path(str(HERE / "data" / "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        for name, cb in (("quit", lambda *_: self.quit()), ("about", lambda *_: self._about())):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
        for action, accels in (("app.quit", ["<Ctrl>q"]), ("win.new-chat", ["<Ctrl>n"]),
                               ("win.stop", ["Escape"]), ("win.toggle-sidebar", ["F9"]),
                               ("win.focus-composer", ["<Ctrl>l"])):
            self.set_accels_for_action(action, accels)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)

    def _on_signal(self):
        self.quit()
        return GLib.SOURCE_REMOVE

    def do_activate(self):
        if self.window is None:
            from .window import ChatWindow
            self.server = ServerManager(self.settings)
            self.window = ChatWindow(self, self.settings, self.server)
        self.window.present()

    def do_shutdown(self):
        if self.window is not None and self.window.running:
            self.window.cancel.set()
            self.window.agent.abort()
        if self.server is not None and self.server.owned and self.settings["stop_server_on_exit"]:
            self.server.stop()
        Adw.Application.do_shutdown(self)

    def _about(self):
        about = Adw.AboutWindow(
            transient_for=self.window, application_name=APP_NAME, application_icon=APP_ID,
            version=__version__, developer_name="skyv04",
            comments=("A native chat app for Qwen running on this device's GPU with llama.cpp.\n"
                      "It can use the internet and a sandboxed workspace folder."),
            license_type=Gtk.License.MIT_X11)
        model = self.server.model_name if self.server else "Qwen"
        about.set_debug_info(
            f"Model: {model}\nServer: {self.settings['server_url']}\n"
            f"Context: {self.server.n_ctx if self.server else '?'} tokens\n"
            f"Workspace: {self.settings.workspace_path}\n"
            f"Settings: {self.settings.path}\n"
            f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}, "
            f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}")
        about.present()


def run_app(argv: list[str]) -> int:
    return QwenChatApp().run(argv)
