"""Development helpers (enabled through environment variables).

UNIPKG_SCREENSHOT=/path/prefix  render the window to PNG files and quit
UNIPKG_SCRIPT="page:explore;search:vlc;wait:8;shot:explore;details:firefox;shot:details"
"""

from __future__ import annotations

import os

import gi

gi.require_version("Graphene", "1.0")
from gi.repository import GLib, Graphene, Gtk  # noqa: E402


def capture(window: Gtk.Window, path: str) -> None:
    width, height = window.get_width(), window.get_height()
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        return
    renderer = window.get_native().get_renderer()
    texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, width, height))
    texture.save_to_png(path)
    print(f"[devtools] saved {path}")


def run_script(app, window) -> None:
    prefix = os.environ.get("UNIPKG_SCREENSHOT")
    script = os.environ.get("UNIPKG_SCRIPT", "wait:10;shot:installed")
    if not prefix:
        return
    steps = [s.split(":", 1) for s in script.split(";") if s]

    def next_step() -> bool:
        if not steps:
            app.quit()
            return GLib.SOURCE_REMOVE
        cmd, arg = steps.pop(0)
        delay = 600
        if cmd == "wait":
            delay = int(float(arg) * 1000)
        elif cmd == "shot":
            capture(window, f"{prefix}-{arg}.png")
        elif cmd == "page":
            window.show_page(arg)
        elif cmd == "search":
            window.explore.entry.set_text(arg)
            window.explore._on_search()
        elif cmd == "filter":
            window.installed.search_entry.set_text(arg)
        elif cmd == "details":
            pkg = next((p for p in window.manager.packages if p.name == arg), None)
            if pkg:
                window.show_details(pkg)
        elif cmd == "compare":
            row = window.explore.results_box.get_row_at_index(int(arg))
            if row:
                window.explore._on_row_activated(None, row)
        elif cmd == "scope":
            window.installed.scope_dd.set_selected(int(arg))
        elif cmd == "sort":
            window.installed.sort_dd.set_selected(int(arg))
        elif cmd == "scroll":
            dialog = window.get_visible_dialog()
            target = dialog if dialog is not None else window
            for sw in _find(target, Gtk.ScrolledWindow):
                adj = sw.get_vadjustment()
                if adj.get_upper() > adj.get_page_size():
                    adj.set_value(float(arg) * (adj.get_upper() - adj.get_page_size()))
                    break
        elif cmd == "close":
            dialog = window.get_visible_dialog()
            if dialog is not None:
                dialog.force_close()
        elif cmd == "size":
            w, h = arg.split("x")
            window.set_default_size(int(w), int(h))
        GLib.timeout_add(delay, next_step)
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(500, next_step)


def _find(widget, cls):
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, cls) and child.get_mapped():
            yield child
        yield from _find(child, cls)
        child = child.get_next_sibling()
