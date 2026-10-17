"""Shared UI helpers: threading, icons and small widgets."""

from __future__ import annotations

import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from .. import host  # noqa: E402
from ..models import Package, PackageKind, Trust  # noqa: E402


def run_async(
    func: Callable[[], Any],
    on_done: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[BaseException], None]] = None,
) -> None:
    """Run *func* in a worker thread and deliver the result on the main loop."""

    def deliver(cb, value):
        cb(value)
        return GLib.SOURCE_REMOVE

    def worker():
        try:
            result = func()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            if on_error:
                GLib.idle_add(deliver, on_error, exc)
            return
        if on_done:
            GLib.idle_add(deliver, on_done, result)

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------

KIND_ICONS = {
    PackageKind.APT: "package-x-generic-symbolic",
    PackageKind.DEB: "folder-download-symbolic",
    PackageKind.FLATPAK: "application-x-addon-symbolic",
    PackageKind.SNAP: "application-x-executable-symbolic",
}


class IconLoader:
    """Loads package icons from the host asynchronously and caches them."""

    def __init__(self) -> None:
        self._cache: dict[str, Optional[Gdk.Paintable]] = {}
        self._pending: dict[str, list[Callable[[Optional[Gdk.Paintable]], None]]] = {}
        self._pool = ThreadPoolExecutor(max_workers=4)
        self._theme_ready = False

    def _setup_theme(self) -> None:
        if self._theme_ready:
            return
        self._theme_ready = True
        display = Gdk.Display.get_default()
        if display is None:
            return
        theme = Gtk.IconTheme.get_for_display(display)
        if host.IN_FLATPAK:
            for path in ("/run/host/usr/share/icons", "/run/host/usr/share/pixmaps",
                         "/run/host/share/icons"):
                if os.path.isdir(path):
                    theme.add_search_path(path)

    def set_image(self, image: Gtk.Image, pkg_or_result: Any, size: int = 32) -> None:
        self._setup_theme()
        kind = pkg_or_result.kind
        fallback = KIND_ICONS.get(kind, "package-x-generic-symbolic")
        image.set_pixel_size(size)
        theme = Gtk.IconTheme.get_for_display(image.get_display())
        icon_name = getattr(pkg_or_result, "icon_name", None)
        key = self._key(pkg_or_result)
        if icon_name and not icon_name.startswith("/") and theme.has_icon(icon_name):
            image.set_from_icon_name(icon_name)
            return
        if key is None:
            image.set_from_icon_name(fallback)
            return
        if key in self._cache:
            paintable = self._cache[key]
            if paintable is not None:
                image.set_from_paintable(paintable)
            else:
                image.set_from_icon_name(fallback)
            return
        image.set_from_icon_name(fallback)
        image._unipkg_icon_key = key  # type: ignore[attr-defined]

        def apply(paintable: Optional[Gdk.Paintable]) -> None:
            if getattr(image, "_unipkg_icon_key", None) == key and paintable is not None:
                image.set_from_paintable(paintable)

        callbacks = self._pending.setdefault(key, [])
        callbacks.append(apply)
        if len(callbacks) == 1:
            self._pool.submit(self._load, key, pkg_or_result)

    @staticmethod
    def _key(obj: Any) -> Optional[str]:
        extra = getattr(obj, "extra", {})
        if getattr(obj, "icon_path", None):
            return obj.icon_path
        if extra.get("icon_path"):
            return extra["icon_path"]
        if obj.kind == PackageKind.SNAP and (extra.get("desktop_files") or extra.get("icon_url")):
            return f"snap:{obj.name}"
        if extra.get("icon_candidates"):
            return f"candidates:{obj.uid}"
        icon_name = getattr(obj, "icon_name", None)
        if icon_name:
            return f"name:{icon_name}"
        return None

    def _load(self, key: str, obj: Any) -> None:
        paintable = None
        try:
            for path in self._paths_for(key, obj):
                data = host.read_host_file(path, max_bytes=4 * 1024 * 1024)
                if data:
                    paintable = _texture_from_bytes(data)
                    if paintable is not None:
                        break
            if paintable is None and obj.kind == PackageKind.SNAP and obj.extra.get("icon_url"):
                paintable = _download_texture(obj.extra["icon_url"])
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        GLib.idle_add(self._finish, key, paintable)

    def _paths_for(self, key: str, obj: Any) -> list[str]:
        if key.startswith("/"):
            paths = [key]
            paths += obj.extra.get("icon_candidates", []) if hasattr(obj, "extra") else []
            return paths
        if key.startswith("candidates:"):
            return list(obj.extra.get("icon_candidates", []))
        if key.startswith("snap:"):
            paths = []
            for desktop in obj.extra.get("desktop_files", []):
                data = host.read_host_file(desktop) or b""
                for line in data.decode("utf-8", "replace").splitlines():
                    if line.startswith("Icon="):
                        paths.append(line[5:].strip())
                        break
            paths.append(f"/snap/{obj.name}/current/meta/gui/icon.png")
            paths.append(f"/snap/{obj.name}/current/meta/gui/icon.svg")
            return paths
        if key.startswith("name:"):
            name = key[5:]
            paths = []
            for size in ("128x128", "256x256", "64x64", "48x48", "scalable"):
                ext = "svg" if size == "scalable" else "png"
                paths.append(f"/usr/share/icons/hicolor/{size}/apps/{name}.{ext}")
            paths += [f"/usr/share/pixmaps/{name}.png", f"/usr/share/pixmaps/{name}.svg",
                      f"/usr/share/pixmaps/{name}.xpm"]
            return paths
        return []

    def _finish(self, key: str, paintable: Optional[Gdk.Paintable]) -> bool:
        self._cache[key] = paintable
        for cb in self._pending.pop(key, []):
            cb(paintable)
        return GLib.SOURCE_REMOVE


def _texture_from_bytes(data: bytes) -> Optional[Gdk.Paintable]:
    try:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        pixbuf = loader.get_pixbuf()
        if pixbuf is None:
            return None
        if pixbuf.get_width() > 128:
            pixbuf = pixbuf.scale_simple(128, 128 * pixbuf.get_height() // pixbuf.get_width(),
                                         GdkPixbuf.InterpType.BILINEAR)
        return Gdk.Texture.new_for_pixbuf(pixbuf)
    except GLib.Error:
        return None


def _download_texture(url: str) -> Optional[Gdk.Paintable]:
    import urllib.request

    if not url.startswith("https://"):
        return None
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            return _texture_from_bytes(resp.read(4 * 1024 * 1024))
    except OSError:
        return None


ICONS = IconLoader()


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

def kind_badge(kind: PackageKind) -> Gtk.Label:
    label = Gtk.Label(label=kind.label, valign=Gtk.Align.CENTER, halign=Gtk.Align.START)
    label.add_css_class("badge")
    label.add_css_class(kind.css_class)
    return label


def trust_badge(trust: Trust, text: Optional[str] = None) -> Gtk.Box:
    box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER, halign=Gtk.Align.START)
    box.add_css_class("trust")
    box.add_css_class(f"trust-{trust.value}")
    box.append(Gtk.Image.new_from_icon_name(trust.icon))
    box.append(Gtk.Label(label=text or trust.label))
    box.set_tooltip_text(TRUST_TOOLTIPS[trust])
    return box


TRUST_TOOLTIPS = {
    Trust.VERIFIED: "Cryptographically signed source and verified publisher",
    Trust.SIGNED: "Cryptographically signed source; the publisher is not verified",
    Trust.UNVERIFIED: "No signature: the origin of this package cannot be verified",
    Trust.WARNING: "Verification problem detected",
    Trust.UNKNOWN: "Not checked yet",
}


def dim_label(text: str = "", **kwargs) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, **kwargs)
    label.add_css_class("dim-label")
    return label


def toast(widget: Gtk.Widget, message: str, timeout: int = 4) -> None:
    root = widget.get_root()
    overlay = getattr(root, "toast_overlay", None)
    if overlay is not None:
        overlay.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=timeout))
    else:
        print(message)


def error_dialog(parent: Gtk.Widget, heading: str, exc: BaseException | str) -> None:
    dialog = Adw.AlertDialog(heading=heading, body=str(exc))
    dialog.add_response("close", "Close")
    dialog.present(parent)
