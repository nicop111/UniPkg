"""Main application window."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk

from ..models import Package
from .details import PackageDetailsDialog
from .explore import ExplorePage
from .installed import InstalledPage
from .repos import RepositoriesPage
from .updates import UpdatesPage

PAGES = [
    ("installed", "Installed", "view-list-bullet-symbolic"),
    ("explore", "Explore", "system-search-symbolic"),
    ("updates", "Updates", "software-update-available-symbolic"),
    ("repositories", "Repositories", "network-server-symbolic"),
]


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, manager):
        super().__init__(application=app, title="UniPkg", default_width=1320, default_height=840)
        self.set_size_request(380, 500)
        self.manager = manager

        self.toast_overlay = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=210, max_sidebar_width=260)
        self.toast_overlay.set_child(self.split)
        self.set_content(self.toast_overlay)

        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        breakpoint.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint)

        # pages ------------------------------------------------------------
        self.installed = InstalledPage(manager, self.show_details)
        self.explore = ExplorePage(manager)
        self.updates = UpdatesPage(manager, self.show_details)
        self.repositories = RepositoriesPage(manager)
        # Keep only name, type and size columns on narrow (phone) screens.
        for key in ("developer", "source", "permissions", "version"):
            breakpoint.add_setter(self.installed.columns[key], "visible", False)
        widgets = {
            "installed": (self.installed, self._installed_header()),
            "explore": (self.explore, []),
            "updates": (self.updates, self.updates.header_widgets()),
            "repositories": (self.repositories, self.repositories.header_widgets()),
        }
        self.nav_pages: dict[str, Adw.NavigationPage] = {}
        for key, title, _icon in PAGES:
            widget, header_widgets = widgets[key]
            toolbar = Adw.ToolbarView()
            header = Adw.HeaderBar()
            for w in header_widgets:
                header.pack_end(w)
            toolbar.add_top_bar(header)
            if key == "installed":
                self.banner = Adw.Banner(button_label="Details")
                self.banner.connect("button-clicked", self._show_errors)
                toolbar.add_top_bar(self.banner)
            toolbar.set_content(widget)
            self.nav_pages[key] = Adw.NavigationPage(title=title, tag=key, child=toolbar)

        # sidebar ------------------------------------------------------------
        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("Reload Packages", "app.reload")
        menu.append("Install Package File…", "app.install-file")
        menu.append("About UniPkg", "app.about")
        sidebar_header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                               tooltip_text="Main menu", primary=True))
        sidebar_toolbar.add_top_bar(sidebar_header)
        self.sidebar_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar_list.add_css_class("navigation-sidebar")
        self.counters: dict[str, Gtk.Label] = {}
        for key, title, icon in PAGES:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=title, xalign=0, hexpand=True))
            counter = Gtk.Label(visible=False)
            counter.add_css_class("counter")
            box.append(counter)
            self.counters[key] = counter
            row.set_child(box)
            row._key = key  # type: ignore[attr-defined]
            self.sidebar_list.append(row)
        self.sidebar_list.connect("row-selected", self._on_sidebar)
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_box.append(self.sidebar_list)
        sidebar_box.append(Gtk.Box(vexpand=True))
        self.status_label = Gtk.Label(wrap=True, xalign=0, margin_start=18, margin_end=18,
                                      margin_bottom=18)
        self.status_label.add_css_class("caption")
        self.status_label.add_css_class("dim-label")
        sidebar_box.append(self.status_label)
        sidebar_toolbar.set_content(sidebar_box)
        self.split.set_sidebar(Adw.NavigationPage(title="UniPkg", child=sidebar_toolbar))
        self.sidebar_list.select_row(self.sidebar_list.get_row_at_index(0))

        # actions ------------------------------------------------------------
        focus = Gio.SimpleAction.new("search", None)
        focus.connect("activate", self._on_search_action)
        self.add_action(focus)
        for idx, (key, _t, _i) in enumerate(PAGES, 1):
            act = Gio.SimpleAction.new(f"page-{key}", None)
            act.connect("activate", lambda *_a, k=key: self.show_page(k))
            self.add_action(act)
            app.set_accels_for_action(f"win.page-{key}", [f"<Alt>{idx}"])
        app.set_accels_for_action("win.search", ["<Control>f"])

        manager.connect("packages-changed", lambda *_: self._update_status())
        manager.connect("updates-changed", lambda *_: self._update_status())
        manager.connect("loading-changed", lambda *_: self._update_status())

    def _installed_header(self) -> list[Gtk.Widget]:
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload packages (Ctrl+R)")
        refresh.set_action_name("app.reload")
        return [refresh]

    # ------------------------------------------------------------------
    def show_page(self, key: str) -> None:
        for idx, (k, _t, _i) in enumerate(PAGES):
            if k == key:
                self.sidebar_list.select_row(self.sidebar_list.get_row_at_index(idx))

    def _on_sidebar(self, _list, row) -> None:
        if row is None:
            return
        key = row._key
        self.split.set_content(self.nav_pages[key])
        self.split.set_show_content(True)
        if key == "repositories":
            self.repositories.ensure_loaded()
        elif key == "explore":
            GLib.idle_add(lambda: (self.explore.focus_search(), GLib.SOURCE_REMOVE)[1])
        elif key == "updates":
            self.updates.rebuild()

    def _on_search_action(self, *_):
        row = self.sidebar_list.get_selected_row()
        key = row._key if row else "installed"
        if key == "explore":
            self.explore.focus_search()
        else:
            self.show_page("installed")
            self.installed.search_entry.grab_focus()

    def show_details(self, pkg: Package) -> None:
        PackageDetailsDialog(self.manager, pkg).present(self)

    def _update_status(self) -> None:
        m = self.manager
        n_updates = len(m.packages_with_updates)
        self.counters["updates"].set_label(str(n_updates))
        self.counters["updates"].set_visible(n_updates > 0)
        apps = sum(1 for p in m.packages if p.is_app)
        self.counters["installed"].set_label(str(apps))
        self.counters["installed"].set_visible(bool(m.packages))
        parts = []
        if m.loading:
            parts.append("Loading packages…")
        elif m.packages:
            from collections import Counter

            counts = Counter(p.kind.label for p in m.packages)
            parts.append(" · ".join(f"{v} {k}" for k, v in sorted(counts.items())))
        if m.checking_updates:
            parts.append("Checking for updates…")
        self.status_label.set_label("\n".join(parts))
        if m.errors:
            self.banner.set_title(f"Some package sources could not be loaded: {', '.join(m.errors)}")
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def _show_errors(self, *_):
        dialog = Adw.AlertDialog(heading="Errors while loading",
                                 body="\n\n".join(f"{k}: {v}" for k, v in self.manager.errors.items()))
        dialog.add_response("close", "Close")
        dialog.present(self)
