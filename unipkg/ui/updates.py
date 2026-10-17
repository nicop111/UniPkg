"""The "Updates" page: update single packages or everything at once."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from ..models import Package, PackageKind
from .common import ICONS, kind_badge
from .operation import run_operation
from .running_check import update_with_running_check


class UpdatesPage(Gtk.Box):
    def __init__(self, manager, on_activate):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.manager = manager
        self._on_activate = on_activate

        self.stack = Gtk.Stack(vexpand=True)
        checking = Adw.StatusPage(title="Checking for updates…")
        checking.set_child(Adw.Spinner(width_request=48, height_request=48))
        self.stack.add_named(checking, "checking")

        uptodate = Adw.StatusPage(icon_name="object-select-symbolic", title="Everything is up to date",
                                  description="APT, Flatpak and Snap packages are current")
        refresh_btn = Gtk.Button(label="Refresh package lists", halign=Gtk.Align.CENTER)
        refresh_btn.add_css_class("pill")
        refresh_btn.set_tooltip_text("Download the latest package lists (requires authentication for APT)")
        refresh_btn.connect("clicked", self.refresh_lists)
        uptodate.set_child(refresh_btn)
        self.stack.add_named(uptodate, "uptodate")

        self.page = Adw.PreferencesPage()
        self.stack.add_named(self.page, "list")
        self._groups: list[Adw.PreferencesGroup] = []
        self.append(self.stack)

        manager.connect("updates-changed", lambda *_: self.rebuild())
        manager.connect("packages-changed", lambda *_: self.rebuild())

    def header_widgets(self) -> list[Gtk.Widget]:
        self.update_all_btn = Gtk.Button(label="Update All")
        self.update_all_btn.add_css_class("suggested-action")
        self.update_all_btn.connect("clicked", self.update_all)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("Refresh package lists and check for updates")
        refresh.connect("clicked", self.refresh_lists)
        return [self.update_all_btn, refresh]

    # ------------------------------------------------------------------
    def rebuild(self) -> None:
        for g in self._groups:
            self.page.remove(g)
        self._groups.clear()
        if self.manager.checking_updates:
            self.stack.set_visible_child_name("checking")
            return
        pkgs = self.manager.packages_with_updates
        if hasattr(self, "update_all_btn"):
            self.update_all_btn.set_sensitive(bool(pkgs))
        if not pkgs:
            self.stack.set_visible_child_name("uptodate")
            return
        apps = [p for p in pkgs if p.is_app]
        others = [p for p in pkgs if not p.is_app]
        if apps:
            self._add_group("Applications", apps)
        for kind in PackageKind:
            items = [p for p in others if p.kind == kind]
            if items:
                self._add_group(f"{kind.label} packages", items, collapsed=len(items) > 8)
        self.stack.set_visible_child_name("list")

    def _add_group(self, title: str, pkgs: list[Package], collapsed: bool = False) -> None:
        group = Adw.PreferencesGroup(title=title, description=f"{len(pkgs)} updates available")
        btn = Gtk.Button(label="Update these", valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        btn.connect("clicked", lambda *_: self.update_packages(pkgs, title))
        group.set_header_suffix(btn)
        container = group
        if collapsed:
            exp = Adw.ExpanderRow(title=f"{len(pkgs)} packages",
                                  subtitle=", ".join(p.name for p in pkgs[:6]) + ("…" if len(pkgs) > 6 else ""))
            group.add(exp)
            container = exp
        for pkg in sorted(pkgs, key=lambda p: p.display_name.lower()):
            row = Adw.ActionRow(title=GLib.markup_escape_text(pkg.display_name),
                                subtitle=GLib.markup_escape_text(f"{pkg.version}  →  {pkg.update_version}"),
                                activatable=True)
            image = Gtk.Image(pixel_size=32)
            ICONS.set_image(image, pkg, 32)
            row.add_prefix(image)
            row.add_suffix(kind_badge(pkg.kind))
            upd = Gtk.Button(label="Update", valign=Gtk.Align.CENTER)
            upd.connect("clicked", lambda _b, p=pkg: self.update_packages([p], p.display_name))
            row.add_suffix(upd)
            row.connect("activated", lambda _r, p=pkg: self._on_activate(p))
            if isinstance(container, Adw.ExpanderRow):
                container.add_row(row)
            else:
                container.add(row)
        self.page.add(group)
        self._groups.append(group)

    # actions --------------------------------------------------------------
    def update_packages(self, pkgs: list[Package], title: str) -> None:
        def build(skip: set[str]) -> list:
            selected = [p for p in pkgs if p.uid not in skip]
            commands = []
            apt_pkgs = [p for p in selected if p.kind in (PackageKind.APT, PackageKind.DEB)]
            if apt_pkgs and self.manager.apt:
                commands += self.manager.apt.update_many(apt_pkgs)
            snaps = [p for p in selected if p.kind == PackageKind.SNAP]
            if snaps and self.manager.snap:
                commands += self.manager.snap.update_many(snaps)
            for pkg in selected:
                if pkg.kind == PackageKind.FLATPAK and self.manager.flatpak:
                    commands += self.manager.flatpak.update(pkg)
            return commands

        update_with_running_check(self, self.manager, pkgs, f"Updating {title}", build, self._after)

    def update_all(self, *_):
        pending = self.manager.packages_with_updates
        kinds = {p.kind for p in pending}

        def build(skip: set[str]) -> list:
            commands = []
            for backend in self.manager.backends:
                if not any(k in kinds for k in backend.kinds):
                    continue
                skipped = [p for p in pending if p.uid in skip and p.kind in backend.kinds]
                if skipped and backend is self.manager.snap:
                    # Refresh only the snaps that are not running.
                    rest = [p for p in pending if p.kind == PackageKind.SNAP and p.uid not in skip]
                    if rest:
                        commands += backend.update_many(rest)
                else:
                    commands += backend.update_all()
            return commands

        update_with_running_check(self, self.manager, pending, "Updating everything", build,
                                  self._after)

    def refresh_lists(self, *_):
        commands = []
        for backend in self.manager.backends:
            commands += backend.refresh()
        run_operation(self, self.manager, "Refreshing package lists", commands,
                      lambda ok: self.manager.check_updates())

    def _after(self, _ok: bool) -> None:
        self.manager.reload()
        self.manager.check_updates()
