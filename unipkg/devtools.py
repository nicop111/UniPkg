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
    _capture_widget(window, window, path)
    # Dialogs stacked on other dialogs are not part of the window paintable.
    dialog = window.get_visible_dialog() if hasattr(window, "get_visible_dialog") else None
    if dialog is not None:
        _capture_widget(window, dialog, path.removesuffix(".png") + "-dialog.png")
    # Open popovers are separate surfaces as well.
    installed = getattr(window, "installed", None)
    for name in ("perm_button", "kind_button", "source_button"):
        popover = getattr(installed, name).get_popover() if installed else None
        if popover is not None and popover.get_visible():
            _capture_widget(window, popover, path.removesuffix(".png") + "-popover.png")


def _capture_widget(window: Gtk.Window, widget: Gtk.Widget, path: str) -> None:
    width, height = widget.get_width(), widget.get_height()
    if width <= 0 or height <= 0:
        return
    paintable = Gtk.WidgetPaintable.new(widget)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        return
    renderer = widget.get_native().get_renderer()
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
            # sort:size or sort:updated:desc
            key, _, order = arg.partition(":")
            window.installed.sort_by(key, order == "desc")
        elif cmd == "scroll":
            dialog = window.get_visible_dialog()
            target = dialog if dialog is not None else window
            for sw in _find(target, Gtk.ScrolledWindow):
                adj = sw.get_vadjustment()
                if adj.get_upper() > adj.get_page_size():
                    adj.set_value(float(arg) * (adj.get_upper() - adj.get_page_size()))
                    break
        elif cmd == "dialogcall":
            dialog = window.get_visible_dialog()
            if dialog is not None:
                getattr(dialog, arg)()
        elif cmd == "risk":
            window.security.show_assessment(window.security.assessments[int(arg)])
        elif cmd == "select":
            # select:installed:3  -> enable selection mode and select the first N rows
            page_name, _, count = arg.partition(":")
            page = getattr(window, page_name)
            if page_name == "installed":
                uids = {page.sort_model.get_item(i).pkg.uid for i in range(int(count or 0))}
            else:
                uids = {a.pkg.uid for a in page.assessments[:int(count or 0)]}
            page.selection.replace(uids)
        elif cmd == "bulk":
            getattr(window, arg).uninstall_selected()
        elif cmd == "types":
            window.installed.kind_button.set_selected(set(arg.split(",")) if arg else set())
        elif cmd == "sources":
            window.installed.source_button.set_selected(set(arg.split("|")) if arg else set())
        elif cmd == "popup":
            getattr(window.installed, arg).popup()
        elif cmd == "secscope":
            window.security.scope_dd.set_selected(int(arg))
        elif cmd == "secsearch":
            window.security.search.set_text(arg)
        elif cmd == "uninstall":
            from .ui.details import confirm_uninstall

            pkg = next((p for p in window.manager.packages if p.name == arg), None)
            if pkg:
                confirm_uninstall(window, window.manager, pkg, False, None)
        elif cmd == "close":
            dialog = window.get_visible_dialog()
            if dialog is not None:
                dialog.force_close()
        elif cmd == "perm":
            # perm:network,camera[;all]  or perm:sandboxed:network
            sandbox, _, keys = arg.rpartition(":") if ":" in arg else ("any", "", arg)
            window.installed.set_permission_filter({k for k in keys.split(",") if k},
                                                   sandbox or "any")
        elif cmd == "permpopup":
            window.installed.perm_button.popup()
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
