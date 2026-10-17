"""Search for new software across all package formats and compare them."""

from __future__ import annotations

import os
import time
from collections import OrderedDict

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .. import host
from ..models import PackageKind, SearchResult, Trust, Verification
from ..util import format_date, format_datetime, format_size, version_key

STALE_AFTER = 365 * 86400  # a release older than this suggests the package is unmaintained
from .common import ICONS, dim_label, error_dialog, kind_badge, run_async, trust_badge
from .operation import run_operation

KIND_ORDER = [PackageKind.FLATPAK, PackageKind.SNAP, PackageKind.APT, PackageKind.DEB]


def group_results(results: list[SearchResult]) -> "OrderedDict[str, list[SearchResult]]":
    """Group results that are most likely the same application."""
    groups: "OrderedDict[str, list[SearchResult]]" = OrderedDict()
    aliases: dict[str, str] = {}
    for r in results:
        keys = {r.group_key, *(_norm_alias(a) for a in r.extra.get("common_ids", []))}
        if r.kind == PackageKind.FLATPAK:
            keys.add(_norm_alias(r.name))
        target = next((aliases[k] for k in keys if k in aliases), None) or r.group_key
        for k in keys:
            aliases.setdefault(k, target)
        groups.setdefault(target, []).append(r)
    for items in groups.values():
        items.sort(key=lambda r: KIND_ORDER.index(r.kind))
    return groups


def _norm_alias(app_id: str) -> str:
    from ..models import normalize_name

    return normalize_name(app_id)


def _relevance(query: str, items: list[SearchResult]) -> tuple:
    q = query.lower()
    names = [i.display_name.lower() for i in items] + [i.name.lower() for i in items]
    exact = any(n == q for n in names)
    prefix = any(n.startswith(q) for n in names)
    return (not exact, not prefix, -len(items))


class ExplorePage(Gtk.Box):
    def __init__(self, manager):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.manager = manager
        self._generation = 0

        clamp = Adw.Clamp(maximum_size=900, margin_top=18, margin_start=12, margin_end=12)
        top = Gtk.Box(spacing=8)
        self.entry = Gtk.SearchEntry(placeholder_text="Search apps in APT, Flatpak and Snap…",
                                     hexpand=True)
        self.entry.add_css_class("big-search")
        self.entry.connect("activate", self._on_search)
        top.append(self.entry)
        search_btn = Gtk.Button(label="Search")
        search_btn.add_css_class("suggested-action")
        search_btn.connect("clicked", self._on_search)
        top.append(search_btn)
        file_btn = Gtk.Button(icon_name="document-open-symbolic")
        file_btn.set_tooltip_text("Install a package file (.deb, .snap, .flatpakref, .flatpak)")
        file_btn.connect("clicked", self._on_install_file)
        top.append(file_btn)
        clamp.set_child(top)
        self.append(clamp)

        self.stack = Gtk.Stack(vexpand=True)
        start = Adw.StatusPage(
            icon_name="system-search-symbolic", title="Find new software",
            description="Search all package formats at once. When an app is available in several "
                        "formats you can compare size, sandboxing and verification before installing.")
        self.stack.add_named(start, "start")
        busy = Adw.StatusPage(title="Searching…")
        busy.set_child(Adw.Spinner(width_request=48, height_request=48))
        self.stack.add_named(busy, "busy")
        self.empty = Adw.StatusPage(icon_name="edit-find-symbolic", title="No results")
        self.stack.add_named(self.empty, "empty")

        self.results_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.results_box.add_css_class("boxed-list")
        self.results_box.connect("row-activated", self._on_row_activated)
        res_clamp = Adw.Clamp(maximum_size=900, child=self.results_box, margin_top=12,
                              margin_bottom=24, margin_start=12, margin_end=12)
        self.stack.add_named(Gtk.ScrolledWindow(child=res_clamp), "results")
        self.append(self.stack)

    # ------------------------------------------------------------------
    def focus_search(self) -> None:
        self.entry.grab_focus()

    def _on_search(self, *_):
        query = self.entry.get_text().strip()
        if len(query) < 2:
            return
        self._generation += 1
        gen = self._generation
        self.stack.set_visible_child_name("busy")
        run_async(lambda: self.manager.search(query), lambda res: self._show(gen, query, res),
                  lambda exc: error_dialog(self, "Search failed", exc))

    def _show(self, gen: int, query: str, results: list[SearchResult]) -> None:
        if gen != self._generation:
            return
        self.results_box.remove_all()
        if not results:
            self.empty.set_description(f"Nothing found for “{query}”")
            self.stack.set_visible_child_name("empty")
            return
        groups = group_results(results)
        ordered = sorted(groups.values(), key=lambda items: _relevance(query, items))
        for items in ordered[:60]:
            self.results_box.append(self._make_row(items))
        self.stack.set_visible_child_name("results")

    def _make_row(self, items: list[SearchResult]) -> Gtk.ListBoxRow:
        main = items[0]
        row = Adw.ActionRow(title=GLib.markup_escape_text(main.display_name),
                            subtitle=GLib.markup_escape_text(main.summary or main.name),
                            activatable=True, subtitle_lines=2)
        image = Gtk.Image(pixel_size=48)
        ICONS.set_image(image, _IconProxy(items), 48)
        row.add_prefix(image)
        badges = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        for item in items:
            badge = kind_badge(item.kind)
            badge.set_tooltip_text(f"{item.name} {item.version} — {item.source}")
            badges.append(badge)
        row.add_suffix(badges)
        if any(i.installed for i in items):
            inst = Gtk.Label(label="Installed")
            inst.add_css_class("badge")
            inst.add_css_class("installed-badge")
            row.add_suffix(inst)
        if len(items) > 1:
            cmp_label = dim_label(f"Compare {len(items)}")
            cmp_label.add_css_class("caption")
            row.add_suffix(cmp_label)
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row._items = items  # type: ignore[attr-defined]
        return row

    def _on_row_activated(self, _box, row):
        CompareDialog(self.manager, row._items).present(self)

    # install from file ------------------------------------------------------
    def _on_install_file(self, *_):
        dialog = Gtk.FileDialog(title="Install a package file")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        f = Gtk.FileFilter(name="Packages (.deb, .snap, .flatpakref, .flatpak)")
        for pattern in ("*.deb", "*.snap", "*.flatpakref", "*.flatpak"):
            f.add_pattern(pattern)
        filters.append(f)
        dialog.set_filters(filters)
        dialog.open(self.get_root(), None, self._on_file_chosen)

    def _on_file_chosen(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        path = gfile.get_path()
        if not path:
            return
        install_local_file(self, self.manager, path)


def _lower_relative(text: str) -> str:
    """"Today" -> "today", but keep dates like "24 Sep 2026" unchanged."""
    return text if text[:1].isdigit() else text[:1].lower() + text[1:]


class _IconProxy:
    """Lets the icon loader pick the best icon of a group of results."""

    def __init__(self, items: list[SearchResult]):
        self.items = items
        best = next((i for i in items if i.extra.get("icon_path")), None) \
            or next((i for i in items if i.extra.get("icon_url")), items[0])
        self.kind = best.kind
        self.name = best.name
        self.extra = best.extra
        self.icon_name = best.name if best.kind == PackageKind.APT else None
        self.icon_path = best.extra.get("icon_path")
        self.uid = f"search:{best.kind.value}:{best.name}"


def is_sandboxed(result: SearchResult) -> bool:
    if result.kind == PackageKind.FLATPAK:
        return True
    if result.kind == PackageKind.SNAP:
        return not result.extra.get("classic")
    return False  # APT / .deb packages run without a sandbox


class CompareDialog(Adw.Dialog):
    """Side by side comparison of the formats an app is available in."""

    ROWS = [
        ("Version", lambda r: r.version or "—"),
        ("Latest release", lambda r: format_date(r.released) if r.released else
         ("Loading…" if not r.extra.get("enriched") else "Unknown")),
        ("On this computer", lambda r: r.extra.get("installed_note", "Not installed")),
        ("Download size", lambda r: format_size(r.download_size)),
        ("Installed size", lambda r: format_size(r.installed_size)),
        ("Developer", lambda r: r.developer or "—"),
        ("Source", lambda r: r.source or "—"),
        ("Sandboxing", lambda r: "Sandboxed" if is_sandboxed(r) else "Not sandboxed"),
        ("Permissions", lambda r: ", ".join(r.extra.get("permissions", [])[:8]) or
         ("Full system access" if r.kind == PackageKind.APT else "—")),
        ("License", lambda r: r.extra.get("license") or "—"),
        ("Updates", lambda r: {PackageKind.APT: "With system updates (apt)",
                               PackageKind.FLATPAK: "Flatpak remote",
                               PackageKind.SNAP: "Automatic (snapd)"}.get(r.kind, "—")),
    ]

    def __init__(self, manager, items: list[SearchResult]):
        main = items[0]
        super().__init__(title=main.display_name,
                         content_width=min(1100, 360 + 280 * len(items)), content_height=760)
        self.manager = manager
        self.items = items
        self._update_installed_state()
        self._labels: dict[tuple[int, str], Gtk.Label] = {}
        self._stale_icons: dict[int, Gtk.Image] = {}
        self._action_boxes: dict[int, Gtk.Box] = {}
        self._trust_boxes: dict[int, Gtk.Box] = {}

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                          margin_top=12, margin_bottom=24, margin_start=18, margin_end=18)

        head = Gtk.Box(spacing=16)
        image = Gtk.Image(pixel_size=72)
        ICONS.set_image(image, _IconProxy(items), 72)
        head.append(image)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=4)
        title = Gtk.Label(label=main.display_name, xalign=0, wrap=True)
        title.add_css_class("title-1")
        texts.append(title)
        texts.append(dim_label(main.summary, wrap=True))
        if len(items) > 1:
            hint = Gtk.Label(label=f"Available in {len(items)} formats — compare before installing",
                             xalign=0)
            hint.add_css_class("accent")
            texts.append(hint)
        head.append(texts)
        content.append(head)

        grid = Gtk.Grid(column_spacing=12, row_spacing=0, column_homogeneous=False)
        grid.add_css_class("compare-grid")
        # header row
        grid.attach(Gtk.Box(), 0, 0, 1, 1)
        for col, item in enumerate(items, 1):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True,
                          margin_bottom=8)
            box.append(kind_badge(item.kind))
            name = Gtk.Label(label=item.name, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE,
                             selectable=True)
            name.add_css_class("caption")
            name.add_css_class("dim-label")
            box.append(name)
            tbox = Gtk.Box(spacing=6)
            tbox.append(trust_badge(item.trust))
            self._trust_boxes[col] = tbox
            box.append(tbox)
            if len(items) > 1 and col == self._recommended():
                rec = Gtk.Label(label="★ Recommended", xalign=0)
                rec.add_css_class("caption-heading")
                rec.add_css_class("accent")
                rec.set_tooltip_text("Best combination of sandboxing and verification")
                box.append(rec)
            grid.attach(box, col, 0, 1, 1)

        for row_idx, (title_text, getter) in enumerate(self.ROWS, 1):
            key = Gtk.Label(label=title_text, xalign=0, valign=Gtk.Align.START)
            key.add_css_class("heading")
            key.add_css_class("compare-key")
            grid.attach(key, 0, row_idx, 1, 1)
            for col, item in enumerate(items, 1):
                label = Gtk.Label(label=getter(item), xalign=0, wrap=True, selectable=True,
                                  valign=Gtk.Align.START, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                  max_width_chars=34)
                label.add_css_class("compare-cell")
                self._labels[(col, title_text)] = label
                if title_text == "Latest release":
                    # Date plus a warning icon when the last release is over a year old.
                    cell = Gtk.Box(spacing=6, valign=Gtk.Align.START)
                    cell.add_css_class("compare-cell-box")
                    cell.append(label)
                    stale = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                    stale.add_css_class("stale-release")
                    stale.set_visible(False)
                    cell.append(stale)
                    self._stale_icons[col] = stale
                    grid.attach(cell, col, row_idx, 1, 1)
                else:
                    grid.attach(label, col, row_idx, 1, 1)
        self._highlight_best()

        btn_row = len(self.ROWS) + 1
        for col in range(1, len(items) + 1):
            self._action_boxes[col] = Gtk.Box(margin_top=14, halign=Gtk.Align.START)
            grid.attach(self._action_boxes[col], col, btn_row, 1, 1)
        self._refresh_actions()

        scroller = Gtk.ScrolledWindow(child=grid, hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                      vscrollbar_policy=Gtk.PolicyType.NEVER)
        content.append(scroller)

        self.verify_group = Adw.PreferencesGroup(title="Signature verification")
        content.append(self.verify_group)
        toolbar.set_content(Gtk.ScrolledWindow(child=content))
        self.set_child(toolbar)

        for col, item in enumerate(items, 1):
            run_async(lambda item=item: self._load_details(item),
                      lambda res, col=col, item=item: self._update_column(col, item, res))

        # Keep install/uninstall buttons in sync while the dialog is open.
        handler = manager.connect("packages-changed", lambda *_: self._on_packages_changed())
        self.connect("closed", lambda *_: manager.disconnect(handler))

    # installed state / actions ---------------------------------------------
    def _installed_pkg(self, item: SearchResult):
        return (self.manager.find_installed(item.kind, item.name)
                or (item.kind == PackageKind.APT and self.manager.find_installed(PackageKind.DEB, item.name))
                or None)

    def _update_installed_state(self) -> None:
        for item in self.items:
            pkg = self._installed_pkg(item)
            item.installed = pkg is not None
            if pkg is None:
                item.extra.pop("installed_note", None)
            else:
                item.extra["installed_note"] = (
                    f"Installed {pkg.version}, updated {_lower_relative(format_date(pkg.updated))}"
                    if pkg.updated else f"Installed {pkg.version}")

    def _refresh_actions(self) -> None:
        for col, item in enumerate(self.items, 1):
            box = self._action_boxes[col]
            child = box.get_first_child()
            if child is not None:
                box.remove(child)
            pkg = self._installed_pkg(item)
            if pkg is not None and pkg.extra.get("protected"):
                btn = Gtk.Button(label="Protected", sensitive=False,
                                 tooltip_text=f"Protected system component: {pkg.extra['protected']}")
            elif pkg is not None:
                btn = Gtk.Button(label=f"Uninstall {item.kind.label}")
                btn.add_css_class("destructive-action")
                btn.connect("clicked", lambda _b, pkg=pkg: self._on_uninstall(pkg))
            else:
                btn = Gtk.Button(label=f"Install {item.kind.label}")
                btn.add_css_class("suggested-action")
                btn.connect("clicked", self._on_install, item)
            btn.add_css_class("pill")
            box.append(btn)

    def _on_packages_changed(self) -> None:
        self._update_installed_state()
        for col, item in enumerate(self.items, 1):
            self._labels[(col, "On this computer")].set_label(
                item.extra.get("installed_note", "Not installed"))
        self._refresh_actions()

    def _on_uninstall(self, pkg) -> None:
        from .details import confirm_uninstall

        confirm_uninstall(self, self.manager, pkg, False,
                          lambda ok: ok and self.manager.reload())

    def _recommended(self) -> int:
        """Prefer a sandboxed, verified format."""
        def score(item):
            return (item.trust == Trust.VERIFIED, item.kind == PackageKind.FLATPAK,
                    item.kind == PackageKind.SNAP and not item.extra.get("classic"))
        best = max(range(len(self.items)), key=lambda i: score(self.items[i]))
        return best + 1

    def _load_details(self, item: SearchResult):
        backend = self.manager.backend_for(item.kind)
        if backend is None:
            return None
        try:
            backend.enrich(item)
        finally:
            item.extra["enriched"] = True
        try:
            return backend.verify_search_result(item)
        except Exception:  # noqa: BLE001
            return None

    def _update_column(self, col: int, item: SearchResult, verification: Verification | None) -> None:
        for title_text, getter in self.ROWS:
            self._labels[(col, title_text)].set_label(getter(item))
        self._highlight_best()
        if verification:
            box = self._trust_boxes[col]
            child = box.get_first_child()
            if child:
                box.remove(child)
            box.append(trust_badge(verification.trust))
            exp = Adw.ExpanderRow(title=f"{item.kind.label}: {verification.summary}",
                                  subtitle=item.source)
            exp.add_prefix(Gtk.Image.new_from_icon_name(verification.trust.icon))
            for check in verification.checks:
                icon = {True: "object-select-symbolic", False: "dialog-error-symbolic",
                        None: "dialog-information-symbolic"}[check.passed]
                r = Adw.ActionRow(title=check.title, subtitle=GLib.markup_escape_text(check.detail),
                                  subtitle_selectable=True)
                img = Gtk.Image.new_from_icon_name(icon)
                img.add_css_class({True: "success", False: "error", None: "dim-label"}[check.passed])
                r.add_prefix(img)
                exp.add_row(r)
            self.verify_group.add(exp)

    def _highlight_best(self) -> None:
        for col, item in enumerate(self.items, 1):
            label = self._labels[(col, "Sandboxing")]
            sandboxed = is_sandboxed(item)
            label.remove_css_class("sandboxed" if not sandboxed else "not-sandboxed")
            label.add_css_class("sandboxed" if sandboxed else "not-sandboxed")
        # Newest version (normalised across formats); nothing if all are equal.
        keys = {col: version_key(i.version, debian=i.kind in (PackageKind.APT, PackageKind.DEB))
                for col, i in enumerate(self.items, 1)}
        known = {col: k for col, k in keys.items() if k is not None}
        newest = max(known.values()) if known else None
        for col in range(1, len(self.items) + 1):
            label = self._labels[(col, "Version")]
            label.remove_css_class("best-value")
            if newest is not None and len(set(known.values())) > 1 and keys[col] == newest:
                label.add_css_class("best-value")
                label.set_tooltip_text("Newest version of the compared formats")
            else:
                label.set_tooltip_text(None)

        released = [(i.released, col) for col, i in enumerate(self.items, 1) if i.released]
        now = time.time()
        for col in range(1, len(self.items) + 1):
            label = self._labels[(col, "Latest release")]
            label.remove_css_class("best-value")
            item = self.items[col - 1]
            stale = bool(item.released and now - item.released > STALE_AFTER)
            icon = self._stale_icons[col]
            icon.set_visible(stale)
            if stale:
                years = (now - item.released) / (365 * 86400)
                tip = (f"Last release {format_datetime(item.released)}, {years:.1f} years ago: "
                       "this package is likely not maintained anymore and may miss security fixes.")
                icon.set_tooltip_text(tip)
                label.set_tooltip_text(tip)
            else:
                label.set_tooltip_text(format_datetime(item.released) if item.released else None)
        if len(released) > 1:
            self._labels[(max(released)[1], "Latest release")].add_css_class("best-value")
        for title_text, attr in (("Download size", "download_size"), ("Installed size", "installed_size")):
            values = [(getattr(i, attr), col) for col, i in enumerate(self.items, 1) if getattr(i, attr)]
            for col in range(1, len(self.items) + 1):
                self._labels[(col, title_text)].remove_css_class("best-value")
            if len(values) > 1:
                _v, best_col = min(values)
                self._labels[(best_col, title_text)].add_css_class("best-value")

    def _on_install(self, _btn, item: SearchResult):
        backend = self.manager.backend_for(item.kind)
        if backend is None:
            return
        body = f"{item.display_name} {item.version} will be installed from {item.source}."
        if item.kind == PackageKind.APT:
            body += "\n\nAPT packages are not sandboxed and can access your entire system."
        if item.extra.get("classic"):
            body += "\n\nThis snap uses classic confinement: it is not sandboxed."
        if item.trust not in (Trust.VERIFIED,):
            body += "\n\nThe publisher of this package is not verified."
        dialog = Adw.AlertDialog(heading=f"Install {item.display_name} ({item.kind.label})?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("install", "Install")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response == "install":
                run_operation(self, self.manager, f"Installing {item.display_name}",
                              backend.install(item), self._after_install)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _after_install(self, ok: bool) -> None:
        # Stay open so another format can be uninstalled right away.
        if ok:
            self.manager.reload()


def install_local_file(parent: Gtk.Widget, manager, path: str) -> None:
    """Inspect and install a local package file."""
    name = os.path.basename(path)
    lower = name.lower()
    if lower.endswith(".deb"):
        apt = manager.apt
        if apt is None:
            error_dialog(parent, "APT is not available", "This system cannot install .deb packages")
            return

        def inspect():
            host_path = host.copy_to_host_tmp(path, name)
            return host_path, apt.inspect_deb(host_path)

        run_async(inspect, lambda res: _confirm_deb(parent, manager, *res),
                  lambda exc: error_dialog(parent, "Cannot open package", exc))
    elif lower.endswith(".snap"):
        snap = manager.snap
        if snap is None:
            error_dialog(parent, "Snap is not available", "snapd is not installed")
            return
        dialog = Adw.AlertDialog(
            heading=f"Install {name}?",
            body="Local snap files are not signed by the Snap Store. They are installed with "
                 "--dangerous and their origin cannot be verified. Only continue if you trust the file.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("install", "Install anyway")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_snap(_d, response):
            if response == "install":
                run_async(lambda: host.copy_to_host_tmp(path, name),
                          lambda host_path: run_operation(parent, manager, f"Installing {name}",
                                                          snap.install_file(host_path),
                                                          lambda ok: ok and manager.reload()))

        dialog.connect("response", on_snap)
        dialog.present(parent)
    elif lower.endswith((".flatpakref", ".flatpak")):
        fp = manager.flatpak
        if fp is None:
            error_dialog(parent, "Flatpak is not available", "flatpak is not installed")
            return
        run_async(lambda: host.copy_to_host_tmp(path, name),
                  lambda host_path: run_operation(parent, manager, f"Installing {name}",
                                                  fp.install_bundle_or_ref(host_path),
                                                  lambda ok: ok and manager.reload()))
    else:
        error_dialog(parent, "Unsupported file", "Choose a .deb, .snap, .flatpakref or .flatpak file")


def _confirm_deb(parent, manager, host_path: str, info: dict) -> None:
    verification: Verification = info["verification"]
    dialog = Adw.AlertDialog(heading=f"Install {info.get('Package', 'package')}?")
    body = [f"Version {info.get('Version', '?')} by {info.get('Maintainer', 'unknown')}"]
    if info.get("installed_version"):
        body.append(f"Currently installed: {info['installed_version']}")
    body.append("")
    body.append(f"Verification: {verification.summary}")
    for check in verification.checks:
        mark = {True: "✔", False: "✘", None: "•"}[check.passed]
        body.append(f"{mark} {check.title}" + (f": {check.detail}" if check.detail else ""))
    body.append("")
    body.append("Debian packages run installation scripts as root and are not sandboxed.")
    dialog.set_body("\n".join(body))
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("install", "Install")
    dialog.set_response_appearance(
        "install", Adw.ResponseAppearance.SUGGESTED if verification.trust == Trust.SIGNED
        else Adw.ResponseAppearance.DESTRUCTIVE)

    def on_response(_d, response):
        if response == "install":
            run_operation(parent, manager, f"Installing {info.get('Package')}",
                          manager.apt.install_file(host_path, info.get("Package", "")),
                          lambda ok: ok and manager.reload())

    dialog.connect("response", on_response)
    dialog.present(parent)
