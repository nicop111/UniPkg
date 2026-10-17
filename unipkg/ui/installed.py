"""The "Installed" page: a sortable, filterable list of all packages."""

from __future__ import annotations

from typing import Callable, Optional

from gi.repository import Adw, Gio, GObject, Gtk, Pango

from ..models import Package, PackageKind, PermissionCategory, Trust
from ..util import format_date, format_datetime, format_size
from .common import ICONS, dim_label

SCOPES = [
    ("apps", "Applications"),
    ("manual", "Manually installed"),
    ("all", "All packages"),
]

PERMISSION_FILTERS: list[tuple[str, str, Callable[[Package], bool]]] = [
    ("any", "Any permissions", lambda p: True),
    ("unrestricted", "Unrestricted (no sandbox)", lambda p: p.permission_summary == "Unrestricted"),
    ("sandboxed", "Sandboxed", lambda p: p.permission_summary != "Unrestricted"),
    ("network", "Network access", lambda p: _has(p, PermissionCategory.NETWORK)),
    ("files", "Home / all files", lambda p: any(
        x.granted and x.sensitive and x.category == PermissionCategory.FILESYSTEM for x in p.permissions)),
    ("camera", "Camera", lambda p: _has(p, PermissionCategory.CAMERA) or any(
        x.granted and x.key in ("devices:all",) for x in p.permissions)),
    ("audio", "Sound / microphone", lambda p: _has(p, PermissionCategory.AUDIO)),
    ("notifications", "Notifications", lambda p: _has(p, PermissionCategory.NOTIFICATIONS)),
    ("devices", "Devices", lambda p: any(
        x.granted and x.sensitive and x.category == PermissionCategory.DEVICES for x in p.permissions)),
    ("background", "Runs in background", lambda p: _has(p, PermissionCategory.BACKGROUND)),
    ("location", "Location", lambda p: _has(p, PermissionCategory.LOCATION)),
    ("bus", "System services", lambda p: any(
        x.granted and x.sensitive and x.category == PermissionCategory.BUS for x in p.permissions)),
]

SORT_KEYS = [
    ("name", "Name"),
    ("developer", "Developer"),
    ("kind", "Package type"),
    ("source", "Source"),
    ("permissions", "Permissions"),
    ("size", "Size"),
    ("updated", "Last updated"),
]


def _has(pkg: Package, cat: PermissionCategory) -> bool:
    if pkg.permission_summary == "Unrestricted":
        return True  # unconfined software can do everything
    return cat in pkg.permission_categories


class PackageObject(GObject.Object):
    def __init__(self, pkg: Package):
        super().__init__()
        self.pkg = pkg


def _sort_value(pkg: Package, key: str):
    if key == "name":
        return pkg.display_name.lower()
    if key == "developer":
        return (pkg.developer or "~").lower()
    if key == "kind":
        return pkg.kind.label
    if key == "source":
        return pkg.source.lower()
    if key == "permissions":
        return pkg.risk_score
    if key == "size":
        return pkg.size
    if key == "updated":
        return pkg.updated or 0.0
    if key == "version":
        return pkg.version
    if key == "trust":
        return list(Trust).index(pkg.trust)
    return pkg.display_name.lower()


def _make_sorter(key: str) -> Gtk.CustomSorter:
    def compare(a, b, _data=None):
        va, vb = _sort_value(a.pkg, key), _sort_value(b.pkg, key)
        if va == vb:
            na, nb = a.pkg.display_name.lower(), b.pkg.display_name.lower()
            return (na > nb) - (na < nb)
        return -1 if va < vb else 1

    return Gtk.CustomSorter.new(compare, None)


class InstalledPage(Gtk.Box):
    def __init__(self, manager, on_activate: Callable[[Package], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.manager = manager
        self._on_activate = on_activate
        self.kinds: set[PackageKind] = set(PackageKind)
        self.scope = "apps"
        self.source = ""
        self.permission = "any"
        self.query = ""

        # --- filter bar -------------------------------------------------
        bar = Adw.WrapBox(child_spacing=8, line_spacing=8, margin_start=12, margin_end=12,
                          margin_top=8, margin_bottom=8)
        bar.add_css_class("filter-bar")
        self.search_entry = Gtk.SearchEntry(placeholder_text="Filter installed packages…", hexpand=True,
                                            width_chars=24)
        self.search_entry.connect("search-changed", self._on_search)
        bar.append(self.search_entry)

        kinds_box = Gtk.Box()
        kinds_box.add_css_class("linked")
        self._kind_buttons: dict[PackageKind, Gtk.ToggleButton] = {}
        for kind in PackageKind:
            btn = Gtk.ToggleButton(label=kind.label, active=True)
            btn.set_tooltip_text(f"Show {kind.label} packages")
            btn.connect("toggled", self._on_kind_toggled, kind)
            kinds_box.append(btn)
            self._kind_buttons[kind] = btn
        bar.append(kinds_box)
        self.append(bar)

        bar2 = Adw.WrapBox(child_spacing=8, line_spacing=8, margin_start=12, margin_end=12,
                           margin_bottom=8)
        self.scope_dd = self._dropdown([label for _k, label in SCOPES], self._on_scope)
        self.scope_dd.set_tooltip_text("Which packages to show")
        bar2.append(self.scope_dd)

        self._sources: list[str] = [""]
        self.source_model = Gtk.StringList.new(["All sources"])
        self.source_dd = Gtk.DropDown(model=self.source_model)
        self.source_dd.set_tooltip_text("Filter by source / repository")
        self.source_dd.connect("notify::selected", self._on_source)
        bar2.append(self.source_dd)

        self.perm_dd = self._dropdown([label for _k, label, _f in PERMISSION_FILTERS], self._on_perm)
        self.perm_dd.set_tooltip_text("Filter by permission")
        bar2.append(self.perm_dd)

        sort_box = Gtk.Box(spacing=6)
        sort_box.append(Gtk.Label(label="Sort by"))
        self.sort_dd = self._dropdown([label for _k, label in SORT_KEYS], self._on_sort_choice)
        sort_box.append(self.sort_dd)
        self.sort_dir_btn = Gtk.ToggleButton(icon_name="view-sort-ascending-symbolic")
        self.sort_dir_btn.set_tooltip_text("Reverse sort order")
        self.sort_dir_btn.connect("toggled", self._on_sort_choice)
        sort_box.append(self.sort_dir_btn)
        bar2.append(sort_box)
        self.summary_label = dim_label("", valign=Gtk.Align.CENTER)
        bar2.append(self.summary_label)
        self.append(bar2)

        # --- list ---------------------------------------------------------
        self.store = Gio.ListStore(item_type=PackageObject)
        self.filter = Gtk.CustomFilter.new(self._filter_func, None)
        self.filter_model = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.sort_model = Gtk.SortListModel(model=self.filter_model)
        self.selection = Gtk.SingleSelection(model=self.sort_model, autoselect=False,
                                             can_unselect=True)
        self.view = Gtk.ColumnView(model=self.selection, show_row_separators=True,
                                   single_click_activate=True, reorderable=False)
        self.view.add_css_class("package-list")
        self.view.connect("activate", self._on_row_activate)
        self.sort_model.set_sorter(self.view.get_sorter())
        self.columns: dict[str, Gtk.ColumnViewColumn] = {}
        self._add_column("name", "Name", self._setup_name, self._bind_name, expand=True, fixed=240)
        self._add_column("developer", "Developer", self._setup_label, self._bind_text(lambda p: p.developer),
                         fixed=150)
        self._add_column("kind", "Type", self._setup_badge, self._bind_badge, fixed=100)
        self._add_column("source", "Source", self._setup_label, self._bind_text(lambda p: p.source), fixed=160)
        self._add_column("permissions", "Permissions", self._setup_perm, self._bind_perm, fixed=180)
        self._add_column("size", "Size", self._setup_label_right,
                         self._bind_text(lambda p: format_size(p.size)), fixed=90)
        self._add_column("updated", "Updated", self._setup_label, self._bind_updated, fixed=100)
        self._add_column("version", "Version", self._setup_label,
                         self._bind_text(lambda p: p.version), fixed=130)

        scroller = Gtk.ScrolledWindow(child=self.view, vexpand=True, hexpand=True)

        self.stack = Gtk.Stack()
        loading = Adw.StatusPage(title="Loading packages…",
                                 description="Collecting APT, Flatpak and Snap packages")
        loading.set_child(Adw.Spinner(width_request=48, height_request=48))
        self.stack.add_named(loading, "loading")
        self.stack.add_named(scroller, "list")
        self.empty = Adw.StatusPage(icon_name="edit-find-symbolic", title="No packages match",
                                    description="Try changing the filters")
        self.stack.add_named(self.empty, "empty")
        self.append(self.stack)

        self.view.sort_by_column(self.columns["name"], Gtk.SortType.ASCENDING)
        manager.connect("packages-changed", lambda *_: self.reload())
        manager.connect("updates-changed", lambda *_: self.filter.changed(Gtk.FilterChange.DIFFERENT))
        manager.connect("loading-changed", lambda *_: self._update_stack())
        self.filter_model.connect("items-changed", lambda *_: self._update_summary())

    # ------------------------------------------------------------------
    def _dropdown(self, labels: list[str], callback) -> Gtk.DropDown:
        dd = Gtk.DropDown.new_from_strings(labels)
        dd.connect("notify::selected", callback)
        return dd

    def _add_column(self, key, title, setup, bind, expand=False, fixed=None):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", setup)
        factory.connect("bind", bind)
        col = Gtk.ColumnViewColumn(title=title, factory=factory, expand=expand, resizable=True)
        if fixed:
            col.set_fixed_width(fixed)
        col.set_sorter(_make_sorter(key))
        self.view.append_column(col)
        self.columns[key] = col

    # factories ------------------------------------------------------------
    def _setup_name(self, _f, item):
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6)
        image = Gtk.Image(pixel_size=32)
        box.append(image)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        title_box = Gtk.Box(spacing=6)
        title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        title.add_css_class("heading")
        title_box.append(title)
        update = Gtk.Label(label="Update")
        update.add_css_class("badge")
        update.add_css_class("update-badge")
        title_box.append(update)
        texts.append(title_box)
        subtitle = dim_label(ellipsize=Pango.EllipsizeMode.END)
        subtitle.add_css_class("caption")
        texts.append(subtitle)
        box.append(texts)
        item.set_child(box)
        box._parts = (image, title, subtitle, update)  # type: ignore[attr-defined]

    def _bind_name(self, _f, item):
        pkg: Package = item.get_item().pkg
        image, title, subtitle, update = item.get_child()._parts
        title.set_label(pkg.display_name)
        subtitle.set_label(pkg.summary or pkg.name)
        update.set_visible(bool(pkg.update_version))
        item.get_child().set_tooltip_text(f"{pkg.name}\n{pkg.summary}")
        ICONS.set_image(image, pkg, 32)

    def _setup_label(self, _f, item):
        item.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END))

    def _setup_label_right(self, _f, item):
        label = Gtk.Label(xalign=1, ellipsize=Pango.EllipsizeMode.END)
        label.add_css_class("numeric")
        item.set_child(label)

    def _bind_text(self, getter):
        def bind(_f, item):
            pkg = item.get_item().pkg
            text = getter(pkg) or "—"
            item.get_child().set_label(text)
            item.get_child().set_tooltip_text(text)

        return bind

    def _bind_updated(self, _f, item):
        pkg: Package = item.get_item().pkg
        label = item.get_child()
        label.set_label(format_date(pkg.updated))
        label.set_tooltip_text(f"Installed or last updated on this computer: {format_datetime(pkg.updated)}"
                               if pkg.updated else "Unknown")

    def _setup_badge(self, _f, item):
        label = Gtk.Label(halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        label.add_css_class("badge")
        item.set_child(label)

    def _bind_badge(self, _f, item):
        pkg: Package = item.get_item().pkg
        label = item.get_child()
        for kind in PackageKind:
            label.remove_css_class(kind.css_class)
        label.add_css_class(pkg.kind.css_class)
        label.set_label(pkg.kind.label)

    def _setup_perm(self, _f, item):
        box = Gtk.Box(spacing=6)
        icon = Gtk.Image()
        label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        box.append(icon)
        box.append(label)
        box._parts = (icon, label)  # type: ignore[attr-defined]
        item.set_child(box)

    def _bind_perm(self, _f, item):
        pkg: Package = item.get_item().pkg
        icon, label = item.get_child()._parts
        summary = pkg.permission_summary
        box = item.get_child()
        for cls in ("perm-high", "perm-medium", "perm-low"):
            box.remove_css_class(cls)
        if summary == "Unrestricted":
            icon.set_from_icon_name("dialog-warning-symbolic")
            box.add_css_class("perm-high")
        elif any(p.granted and p.sensitive for p in pkg.permissions):
            icon.set_from_icon_name("security-medium-symbolic")
            box.add_css_class("perm-medium")
        else:
            icon.set_from_icon_name("security-high-symbolic")
            box.add_css_class("perm-low")
        label.set_label(summary)
        granted = [p.label for p in pkg.granted_permissions]
        box.set_tooltip_text("\n".join(granted[:25]) if granted else "No special permissions")

    # filters ------------------------------------------------------------
    def _filter_func(self, obj, _data=None) -> bool:
        pkg: Package = obj.pkg
        if pkg.kind not in self.kinds:
            return False
        if self.scope == "apps" and not pkg.is_app:
            return False
        if self.scope == "manual":
            if pkg.auto_installed:
                return False
            if pkg.kind == PackageKind.SNAP and pkg.extra.get("type") not in ("app", None):
                return False
        if self.source and pkg.source != self.source:
            return False
        if self.permission != "any":
            for key, _label, func in PERMISSION_FILTERS:
                if key == self.permission and not func(pkg):
                    return False
        if self.query:
            haystack = f"{pkg.display_name} {pkg.name} {pkg.developer} {pkg.summary}".lower()
            if not all(word in haystack for word in self.query.split()):
                return False
        return True

    def _refilter(self) -> None:
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_stack()
        self._scroll_to_top()

    def _scroll_to_top(self) -> None:
        if self.sort_model.get_n_items() > 0:
            self.view.scroll_to(0, None, Gtk.ListScrollFlags.NONE, None)

    def _on_search(self, entry):
        self.query = entry.get_text().strip().lower()
        self._refilter()

    def _on_kind_toggled(self, btn, kind):
        if btn.get_active():
            self.kinds.add(kind)
        else:
            self.kinds.discard(kind)
        self._refilter()

    def set_kind_filter(self, kinds: Optional[set[PackageKind]]) -> None:
        kinds = kinds or set(PackageKind)
        for kind, btn in self._kind_buttons.items():
            btn.set_active(kind in kinds)

    def _on_scope(self, dd, _pspec):
        self.scope = SCOPES[dd.get_selected()][0]
        self._refilter()

    def _on_source(self, dd, _pspec):
        idx = dd.get_selected()
        self.source = self._sources[idx] if 0 <= idx < len(self._sources) else ""
        self._refilter()

    def _on_perm(self, dd, _pspec):
        self.permission = PERMISSION_FILTERS[dd.get_selected()][0]
        self._refilter()

    def _on_sort_choice(self, widget=None, *_args):
        key = SORT_KEYS[self.sort_dd.get_selected()][0]
        if widget is self.sort_dd and key == "updated" and not self.sort_dir_btn.get_active():
            # Most recently updated first is what one usually wants.
            self.sort_dir_btn.set_active(True)  # re-enters this handler
            return
        descending = self.sort_dir_btn.get_active()
        self.sort_dir_btn.set_icon_name("view-sort-descending-symbolic" if descending
                                        else "view-sort-ascending-symbolic")
        self.view.sort_by_column(self.columns[key],
                                 Gtk.SortType.DESCENDING if descending else Gtk.SortType.ASCENDING)
        self._scroll_to_top()

    def _on_row_activate(self, _view, position):
        obj = self.sort_model.get_item(position)
        if obj is not None:
            self._on_activate(obj.pkg)

    # data ------------------------------------------------------------------
    def reload(self) -> None:
        objects = [PackageObject(p) for p in self.manager.packages]
        self.store.splice(0, self.store.get_n_items(), objects)
        sources = sorted({p.source for p in self.manager.packages if p.source})
        current = self.source
        self._sources = [""] + sources
        self.source_model.splice(0, self.source_model.get_n_items(), ["All sources"] + sources)
        self.source_dd.set_selected(self._sources.index(current) if current in self._sources else 0)
        self._update_stack()
        self._update_summary()

    def _update_stack(self) -> None:
        if self.manager.loading and self.store.get_n_items() == 0:
            self.stack.set_visible_child_name("loading")
        elif self.filter_model.get_n_items() == 0:
            self.stack.set_visible_child_name("empty")
        else:
            self.stack.set_visible_child_name("list")

    def _update_summary(self) -> None:
        n = self.filter_model.get_n_items()
        total = 0
        for i in range(n):
            total += self.filter_model.get_item(i).pkg.size
        self.summary_label.set_label(f"{n} packages · {format_size(total)}")
        self._update_stack()
