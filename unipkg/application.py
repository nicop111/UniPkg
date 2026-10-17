"""Adw.Application subclass."""

from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__  # noqa: E402
from .manager import Manager  # noqa: E402

APP_ID = "io.github.unipkg.UniPkg"


class Application(Adw.Application):
    def __init__(self) -> None:
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if os.environ.get("UNIPKG_SCREENSHOT"):
            # Scripted runs must not hand over to an already running instance.
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        GLib.set_application_name("UniPkg")
        self.manager = Manager()
        self.window = None
        self._pending_files: list[str] = []

        for name, callback, accels in (
            ("reload", self._on_reload, ["<Control>r", "F5"]),
            ("install-file", self._on_install_file, ["<Control>o"]),
            ("about", self._on_about, []),
            ("quit", lambda *_: self.quit(), ["<Control>q"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css_path = os.path.join(os.path.dirname(__file__), "style.css")
        css.load_from_path(css_path)
        display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._dark_css = Gtk.CssProvider()
        self._dark_css.load_from_path(os.path.join(os.path.dirname(__file__), "style-dark.css"))
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", self._on_dark_changed)
        self._on_dark_changed(style)

    def _on_dark_changed(self, style, *_args) -> None:
        display = Gdk.Display.get_default()
        if style.get_dark():
            Gtk.StyleContext.add_provider_for_display(
                display, self._dark_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        else:
            Gtk.StyleContext.remove_provider_for_display(display, self._dark_css)

    def do_activate(self) -> None:
        if self.window is None:
            from .ui.window import MainWindow

            self.window = MainWindow(self, self.manager)
            self.manager.reload()
            self.manager.check_updates()
            if os.environ.get("UNIPKG_SCREENSHOT"):
                from .devtools import run_script

                run_script(self, self.window)
        self.window.present()
        for path in self._pending_files:
            self._open_path(path)
        self._pending_files.clear()

    def do_open(self, files, _n_files, _hint) -> None:
        self._pending_files.extend(f.get_path() for f in files if f.get_path())
        self.do_activate()

    def _open_path(self, path: str) -> None:
        from .ui.explore import install_local_file

        self.window.show_page("explore")
        install_local_file(self.window, self.manager, path)

    def _on_reload(self, *_args) -> None:
        self.manager.reload()
        self.manager.check_updates()

    def _on_install_file(self, *_args) -> None:
        if self.window:
            self.window.show_page("explore")
            self.window.explore._on_install_file()

    def _on_about(self, *_args) -> None:
        about = Adw.AboutDialog(
            application_name="UniPkg",
            application_icon=APP_ID,
            developer_name="UniPkg contributors",
            version=__version__,
            comments="One place to manage APT, Debian, Flatpak and Snap packages: install, update, "
                     "remove, control permissions and verify signatures.",
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/unipkg/unipkg",
        )
        about.present(self.window)


def main() -> int:
    app = Application()
    return app.run(sys.argv)
