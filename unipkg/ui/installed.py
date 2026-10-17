"""The "Installed" page: a sortable, filterable list of all packages."""

from __future__ import annotations

from typing import Callable, Optional

from gi.repository import Adw, Gio, GObject, Gtk, Pango

from ..models import Package, PackageKind, PermissionCategory, Trust
from ..util import format_date, format_datetime, format_size
from .common import ICONS, dim_label
from .details import confirm_bulk_uninstall
from .selection import Selection, SelectionBar

SCOPES = [
    ("apps", "Applications"),
    ("manual", "Manually installed"),
    ("all", "All packages"),
]

SANDBOX_FILTERS: list[tuple[str, str, Callable[[Package], bool]]] = [
    ("any", "Any", lambda p: True),
    ("sandboxed", "Sandboxed only", lambda p: p.permission_summary != "Unrestricted"),
    ("unrestricted", "Unrestricted only (no sandbox)", lambda p: p.permission_summary == "Unrestricted"),
]

PERMISSION_FILTERS: list[tuple[str, str, Callable[[Package], bool]]] = [
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


def matches_permission_filter(pkg: Package, sandbox: str, keys: set[str], match_all: bool) -> bool:
    """Sandbox status must match; of the checked permissions the package needs
    at least one (``match_all=False``) or every one (``match_all=True``)."""
    for key, _label, func in SANDBOX_FILTERS:
        if key == sandbox and not func(pkg):
            return False
    if not keys:
        return True
    results = [func(pkg) for key, _label, func in PERMISSION_FILTERS if key in keys]
    return all(results) if match_all else any(results)


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
        self.sandbox_filter = "any"
        self.permission_keys: set[str] = set()
        self.permission_match_all = False
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

        self.perm_button = Gtk.MenuButton(label="Any permissions", always_show_arrow=True,
                                          tooltip_text="Filter by permission",
                                          popover=self._build_permission_popover())
        bar2.append(self.perm_button)

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

        # --- multi selection -----------------------------------------------
        self.selection = Selection()
        self._bound_checks: set[Gtk.CheckButton] = set()
        select_factory = Gtk.SignalListItemFactory()
        select_factory.connect("setup", self._setup_check)
        select_factory.connect("bind", self._bind_check)
        select_factory.connect("unbind", self._unbind_check)
        self.select_column = Gtk.ColumnViewColumn(title="", factory=select_factory)
        self.select_column.set_fixed_width(44)
        self.view.insert_column(0, self.select_column)
        self.selection.connect("changed", lambda *_: self._sync_checks())
        self.selection_bar = SelectionBar(self.selection, self.select_all_visible,
                                          self.uninstall_selected)
        self.append(self.selection_bar)

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
        if not matches_permission_filter(pkg, self.sandbox_filter, self.permission_keys,
                                         self.permission_match_all):
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

    # permission filter popover -----------------------------------------------
    def _build_permission_popover(self) -> Gtk.Popover:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=8,
                      margin_bottom=8, margin_start=8, margin_end=8)

        def heading(text: str) -> Gtk.Label:
            label = Gtk.Label(label=text, xalign=0, margin_start=4, margin_top=4)
            label.add_css_class("heading")
            return label

        box.append(heading("Sandbox"))
        self._sandbox_checks: dict[str, Gtk.CheckButton] = {}
        group = None
        for key, label, _func in SANDBOX_FILTERS:
            check = Gtk.CheckButton(label=label, active=key == "any", group=group)
            group = group or check
            check.connect("toggled", self._on_sandbox_toggled, key)
            self._sandbox_checks[key] = check
            box.append(check)

        box.append(Gtk.Separator(margin_top=6, margin_bottom=2))
        match_row = Gtk.Box(spacing=8, margin_start=4)
        match_row.append(heading("Has access to"))
        match_row.append(Gtk.Box(hexpand=True))
        linked = Gtk.Box(valign=Gtk.Align.CENTER)
        linked.add_css_class("linked")
        self._match_any = Gtk.ToggleButton(label="Any", active=True,
                                           tooltip_text="Show apps with at least one checked permission")
        self._match_all = Gtk.ToggleButton(label="All", group=self._match_any,
                                           tooltip_text="Show only apps with every checked permission")
        for btn in (self._match_any, self._match_all):
            btn.add_css_class("small-toggle")
            linked.append(btn)
        self._match_all.connect("toggled", self._on_match_toggled)
        match_row.append(linked)
        box.append(match_row)

        self._permission_checks: dict[str, Gtk.CheckButton] = {}
        for key, label, _func in PERMISSION_FILTERS:
            check = Gtk.CheckButton(label=label)
            check.connect("toggled", self._on_permission_check, key)
            self._permission_checks[key] = check
            box.append(check)

        clear = Gtk.Button(label="Clear Filters", margin_top=6)
        clear.add_css_class("flat")
        clear.connect("clicked", lambda *_: self.clear_permission_filter())
        box.append(clear)
        return Gtk.Popover(child=box)

    def _on_sandbox_toggled(self, check: Gtk.CheckButton, key: str) -> None:
        if check.get_active():
            self.sandbox_filter = key
            self._permission_filter_changed()

    def _on_match_toggled(self, btn: Gtk.ToggleButton) -> None:
        self.permission_match_all = btn.get_active()
        self._permission_filter_changed()

    def _on_permission_check(self, check: Gtk.CheckButton, key: str) -> None:
        if check.get_active():
            self.permission_keys.add(key)
        else:
            self.permission_keys.discard(key)
        self._permission_filter_changed()

    def set_permission_filter(self, keys: set[str], sandbox: str = "any", match_all: bool = False) -> None:
        self._sandbox_checks[sandbox].set_active(True)
        (self._match_all if match_all else self._match_any).set_active(True)
        for key, check in self._permission_checks.items():
            check.set_active(key in keys)

    def clear_permission_filter(self) -> None:
        self.set_permission_filter(set())

    def _permission_filter_changed(self) -> None:
        labels = {k: l for k, l, _f in PERMISSION_FILTERS}
        parts = []
        if self.sandbox_filter == "sandboxed":
            parts.append("Sandboxed")
        elif self.sandbox_filter == "unrestricted":
            parts.append("Unrestricted")
        chosen = [labels[k] for k, _l, _f in PERMISSION_FILTERS if k in self.permission_keys]
        if chosen:
            text = chosen[0] if len(chosen) == 1 else f"{chosen[0]} {'&' if self.permission_match_all else 'or'} {len(chosen) - 1} more"
            parts.append(text)
        self.perm_button.set_label(" · ".join(parts) if parts else "Any permissions")
        if parts:
            self.perm_button.add_css_class("filter-active")
        else:
            self.perm_button.remove_css_class("filter-active")
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

    # selection (only the checkbox selects; clicking the row opens details) ---

    def _setup_check(self, _f, item):
        check = Gtk.CheckButton(halign=Gtk.Align.CENTER, tooltip_text="Select for bulk uninstall")
        check._uid = None  # type: ignore[attr-defined]
        check.connect("toggled", self._on_check_toggled)
        item.set_child(check)

    def _bind_check(self, _f, item):
        check = item.get_child()
        check._uid = item.get_item().pkg.uid
        check.handler_block_by_func(self._on_check_toggled)
        check.set_active(check._uid in self.selection)
        check.handler_unblock_by_func(self._on_check_toggled)
        self._bound_checks.add(check)

    def _unbind_check(self, _f, item):
        self._bound_checks.discard(item.get_child())

    def _on_check_toggled(self, check):
        if check._uid:
            self.selection.set(check._uid, check.get_active())

    def _sync_checks(self) -> None:
        for check in self._bound_checks:
            active = check._uid in self.selection
            if check.get_active() != active:
                check.handler_block_by_func(self._on_check_toggled)
                check.set_active(active)
                check.handler_unblock_by_func(self._on_check_toggled)

    def select_all_visible(self) -> None:
        uids = {self.filter_model.get_item(i).pkg.uid for i in range(self.filter_model.get_n_items())}
        self.selection.replace(self.selection.uids | uids)

    def uninstall_selected(self) -> None:
        pkgs = [p for p in self.manager.packages if p.uid in self.selection]
        if not pkgs:
            return

        def done(ok: bool) -> None:
            self.manager.reload()

        confirm_bulk_uninstall(self, self.manager, pkgs, done)

    # data ------------------------------------------------------------------
    def reload(self) -> None:
        if hasattr(self, "selection"):
            present = {p.uid for p in self.manager.packages}
            if self.selection.uids - present:
                self.selection.replace(self.selection.uids & present)
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
