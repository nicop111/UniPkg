"""Package details: information, verification, permissions and actions."""

from __future__ import annotations

from collections import OrderedDict

from gi.repository import Adw, GLib, Gtk, Pango

from .. import host
from ..models import Package, PackageKind, Permission, PermissionCategory, Verification
from ..util import format_date, format_datetime, format_size
from .common import ICONS, dim_label, error_dialog, kind_badge, run_async, trust_badge
from .operation import run_operation
from .running_check import update_with_running_check


class PackageDetailsDialog(Adw.Dialog):
    def __init__(self, manager, pkg: Package):
        super().__init__(title=pkg.display_name, content_width=720, content_height=820)
        self.manager = manager
        self.pkg = pkg
        self.backend = manager.backend_for(pkg.kind)

        self.toast_overlay = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.page = Adw.PreferencesPage()
        toolbar.set_content(self.page)
        self.toast_overlay.set_child(toolbar)
        self.set_child(self.toast_overlay)

        self._build_header()
        self._build_info()
        self._build_verification()
        self.perm_group = Adw.PreferencesGroup(title="Permissions")
        self.page.add(self.perm_group)
        self._perm_rows: list[Gtk.Widget] = []
        self._build_permissions()
        self._build_data()

    # header -------------------------------------------------------------
    def _build_header(self) -> None:
        pkg = self.pkg
        group = Adw.PreferencesGroup()
        box = Gtk.Box(spacing=18, margin_bottom=6)
        image = Gtk.Image(pixel_size=96)
        image.add_css_class("icon-dropshadow")
        ICONS.set_image(image, pkg, 96)
        box.append(image)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER,
                        hexpand=True)
        title = Gtk.Label(label=pkg.display_name, xalign=0, wrap=True)
        title.add_css_class("title-1")
        texts.append(title)
        texts.append(dim_label(pkg.developer or "Unknown developer", wrap=True))
        badges = Gtk.Box(spacing=6, margin_top=4)
        badges.append(kind_badge(pkg.kind))
        self.header_trust = Gtk.Box()
        self.header_trust.append(trust_badge(pkg.trust))
        badges.append(self.header_trust)
        texts.append(badges)
        if pkg.summary:
            summary = Gtk.Label(label=pkg.summary, xalign=0, wrap=True, margin_top=4)
            texts.append(summary)
        box.append(texts)
        group.add(box)

        actions = Gtk.Box(spacing=8, margin_top=12, halign=Gtk.Align.START)
        if pkg.update_version:
            update_btn = Gtk.Button(label=f"Update to {pkg.update_version}")
            update_btn.add_css_class("suggested-action")
            update_btn.add_css_class("pill")
            update_btn.connect("clicked", self._on_update)
            actions.append(update_btn)
        if pkg.extra.get("protected"):
            actions.append(protected_badge(pkg.extra["protected"], with_text=True))
        else:
            remove_btn = Gtk.Button(label="Uninstall…")
            remove_btn.add_css_class("destructive-action")
            remove_btn.add_css_class("pill")
            remove_btn.connect("clicked", lambda *_: self._confirm_uninstall(False))
            actions.append(remove_btn)
        group.add(actions)
        self.page.add(group)

    # information -------------------------------------------------------
    def _build_info(self) -> None:
        pkg = self.pkg
        group = Adw.PreferencesGroup(title="Details")
        rows = [
            ("Package", pkg.name),
            ("Version", pkg.version + (f"  →  {pkg.update_version}" if pkg.update_version else "")),
            ("Type", pkg.kind.label),
            ("Source", pkg.source),
            ("Installed size", format_size(pkg.size)),
            ("Last updated", f"{format_datetime(pkg.updated)} ({format_date(pkg.updated)})"
             if pkg.updated else ""),
            ("Sandbox", pkg.confinement),
        ]
        if pkg.kind == PackageKind.FLATPAK:
            rows.append(("Installation", pkg.extra.get("installation", "").capitalize()))
            rows.append(("Runtime", pkg.extra.get("runtime", "")))
        if pkg.kind == PackageKind.SNAP:
            rows.append(("Channel", pkg.extra.get("channel", "")))
            rows.append(("Revision", pkg.extra.get("revision", "")))
        if pkg.extra.get("license"):
            rows.append(("License", pkg.extra["license"]))
        if pkg.extra.get("section"):
            rows.append(("Section", pkg.extra["section"]))
        if pkg.auto_installed:
            rows.append(("Installed as", "Dependency (automatically installed)"))
        for title, value in rows:
            if not value:
                continue
            row = Adw.ActionRow(title=title, subtitle=value, subtitle_selectable=True, use_markup=False)
            row.add_css_class("property")
            group.add(row)
        homepage = pkg.extra.get("homepage")
        if homepage:
            row = Adw.ActionRow(title="Homepage", subtitle=homepage, activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
            row.connect("activated", lambda *_: Gtk.UriLauncher(uri=homepage).launch(
                self.get_root(), None, None, None))
            group.add(row)
        self.page.add(group)

    # verification -------------------------------------------------------------
    def _build_verification(self) -> None:
        self.verify_group = Adw.PreferencesGroup(
            title="Verification",
            description="Checks that this package comes from a cryptographically signed source")
        self.deep_btn = Gtk.Button(label="Deep check", valign=Gtk.Align.CENTER)
        self.deep_btn.set_tooltip_text(
            "Also verify installed files / the downloaded image against the signed checksums")
        self.deep_btn.add_css_class("flat")
        self.deep_btn.connect("clicked", lambda *_: self._run_verification(True))
        self.verify_group.set_header_suffix(self.deep_btn)
        self.verify_status = Adw.ActionRow(title="Checking…")
        spinner = Adw.Spinner()
        self.verify_status.add_prefix(spinner)
        self.verify_group.add(self.verify_status)
        self._verify_rows: list[Gtk.Widget] = []
        self.page.add(self.verify_group)
        self._run_verification(False)

    def _run_verification(self, deep: bool) -> None:
        if not self.backend:
            return
        self.deep_btn.set_sensitive(False)
        self.verify_status.set_title("Running deep check…" if deep else "Checking…")
        run_async(lambda: self.backend.verify(self.pkg, deep=deep), self._show_verification,
                  self._verification_failed)

    def _verification_failed(self, exc) -> None:
        self.deep_btn.set_sensitive(True)
        self.verify_status.set_title("Verification failed")
        self.verify_status.set_subtitle(str(exc))

    def _show_verification(self, result: Verification) -> None:
        self.deep_btn.set_sensitive(True)
        for row in self._verify_rows:
            self.verify_group.remove(row)
        self._verify_rows.clear()
        self.verify_group.remove(self.verify_status)
        self.verify_status = Adw.ActionRow(title=result.summary, use_markup=False)
        icon = Gtk.Image.new_from_icon_name(result.trust.icon)
        icon.set_pixel_size(24)
        icon.add_css_class(f"trust-icon-{result.trust.value}")
        self.verify_status.add_prefix(icon)
        self.verify_status.add_suffix(trust_badge(result.trust))
        self.verify_group.add(self.verify_status)
        child = self.header_trust.get_first_child()
        if child:
            self.header_trust.remove(child)
        self.header_trust.append(trust_badge(result.trust))
        for check in result.checks:
            row = Adw.ExpanderRow(title=check.title, use_markup=False) \
                if check.detail and len(check.detail) > 80 \
                else Adw.ActionRow(title=check.title, subtitle=check.detail, subtitle_selectable=True,
                                   use_markup=False)
            if isinstance(row, Adw.ExpanderRow):
                detail = Gtk.Label(label=check.detail, xalign=0, wrap=True, selectable=True,
                                   wrap_mode=Pango.WrapMode.WORD_CHAR,
                                   margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
                detail.add_css_class("monospace")
                detail.add_css_class("caption")
                row.add_row(detail)
            name, css = {True: ("object-select-symbolic", "success"),
                         False: ("dialog-error-symbolic", "error"),
                         None: ("dialog-information-symbolic", "dim-label")}[check.passed]
            img = Gtk.Image.new_from_icon_name(name)
            img.add_css_class(css)
            row.add_prefix(img)
            self.verify_group.add(row)
            self._verify_rows.append(row)

    # permissions ---------------------------------------------------------------
    def _build_permissions(self) -> None:
        for row in self._perm_rows:
            self.perm_group.remove(row)
        self._perm_rows.clear()
        pkg = self.pkg
        manageable = bool(self.backend and self.backend.permissions_manageable(pkg))
        if pkg.kind in (PackageKind.APT, PackageKind.DEB):
            self.perm_group.set_description(
                "Traditional packages are not sandboxed. Their access cannot be restricted — "
                "consider the Flatpak or Snap version if one is available.")
        elif pkg.extra.get("devmode"):
            self.perm_group.set_description(
                "This snap is installed in developer mode: snapd does not enforce its sandbox, so "
                "its permissions cannot be restricted.")
        elif pkg.extra.get("classic"):
            self.perm_group.set_description(
                "This snap uses classic confinement: it runs without a sandbox and has full access "
                "to your system. Snapd offers no permissions to change for classic snaps — if you "
                "need restrictions, look for a strictly confined snap or a Flatpak of this app.")
        else:
            self.perm_group.set_description(
                "Toggle what this app may access. Changes apply the next time the app starts.")
        if manageable and pkg.kind == PackageKind.FLATPAK:
            reset = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
            reset.add_css_class("flat")
            reset.set_tooltip_text("Undo your changes to the sandbox permissions")
            reset.connect("clicked", self._on_reset_permissions)
            self.perm_group.set_header_suffix(reset)

        by_cat: "OrderedDict[PermissionCategory, list[Permission]]" = OrderedDict()
        order = list(PermissionCategory)
        for perm in sorted(pkg.permissions, key=lambda p: (order.index(p.category), not p.granted, p.label)):
            by_cat.setdefault(perm.category, []).append(perm)
        for cat, perms in by_cat.items():
            granted = sum(1 for p in perms if p.granted)
            exp = Adw.ExpanderRow(title=cat.label, subtitle=f"{granted} of {len(perms)} granted")
            exp.add_prefix(Gtk.Image.new_from_icon_name(cat.icon))
            if any(p.granted and p.sensitive for p in perms):
                warn = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                warn.add_css_class("warning")
                warn.set_tooltip_text("Grants privacy-sensitive access")
                exp.add_suffix(warn)
            if cat in (PermissionCategory.NETWORK, PermissionCategory.FILESYSTEM,
                       PermissionCategory.NOTIFICATIONS, PermissionCategory.SYSTEM):
                exp.set_expanded(True)
            for perm in perms:
                exp.add_row(self._permission_row(perm, manageable))
            self.perm_group.add(exp)
            self._perm_rows.append(exp)
        if not pkg.permissions:
            row = Adw.ActionRow(title="No permissions", subtitle="This package requests no special access")
            self.perm_group.add(row)
            self._perm_rows.append(row)

    def _permission_row(self, perm: Permission, manageable: bool) -> Gtk.Widget:
        subtitle = perm.description
        if perm.overridden and self.pkg.kind == PackageKind.FLATPAK:
            subtitle = (subtitle + " · " if subtitle else "") + "changed by you"
        if manageable and perm.toggleable:
            row = Adw.SwitchRow(title=perm.label, subtitle=subtitle or "", active=perm.granted,
                                use_markup=False)
            row.connect("notify::active", self._on_permission_toggled, perm)
        else:
            row = Adw.ActionRow(title=perm.label, subtitle=subtitle or "", use_markup=False)
            state = Gtk.Label(label="Granted" if perm.granted else "Not granted")
            state.add_css_class("dim-label")
            row.add_suffix(state)
        if perm.sensitive and perm.granted:
            row.add_css_class("sensitive-perm")
        return row

    def _on_permission_toggled(self, row: Adw.SwitchRow, _pspec, perm: Permission) -> None:
        granted = row.get_active()
        if granted == perm.granted:
            return
        try:
            commands = self.backend.set_permission(self.pkg, perm, granted)
        except Exception as exc:  # noqa: BLE001
            error_dialog(self, "Cannot change permission", exc)
            return
        row.set_sensitive(False)

        def work():
            for cmd in commands:
                res = host.run(cmd.argv, env=cmd.env, timeout=300)
                if not res.ok and not cmd.allow_failure:
                    raise RuntimeError((res.stderr or res.stdout).strip() or "Command failed")
            self.backend.reload_permissions(self.pkg)

        def done(_):
            self.toast_overlay.add_toast(Adw.Toast(
                title=f"{perm.label}: {'granted' if granted else 'revoked'}", timeout=2))
            GLib.idle_add(self._rebuild_permissions)

        def failed(exc):
            row.set_sensitive(True)
            perm_granted = perm.granted
            row.handler_block_by_func(self._on_permission_toggled)
            row.set_active(perm_granted)
            row.handler_unblock_by_func(self._on_permission_toggled)
            error_dialog(self, "Could not change permission", exc)

        run_async(work, done, failed)

    def _rebuild_permissions(self) -> bool:
        self._build_permissions()
        self.manager.emit("packages-changed")
        return GLib.SOURCE_REMOVE

    def _on_reset_permissions(self, *_):
        commands = self.backend.reset_permissions(self.pkg)

        def work():
            for cmd in commands:
                host.run(cmd.argv, timeout=60)
            self.backend.reload_permissions(self.pkg)

        run_async(work, lambda _: (self._rebuild_permissions(),
                                   self.toast_overlay.add_toast(Adw.Toast(title="Permissions reset"))))

    # data -------------------------------------------------------------------------
    def _build_data(self) -> None:
        group = Adw.PreferencesGroup(title="Data and storage")
        self.data_row = Adw.ActionRow(title="User data", subtitle="Looking for data folders…")
        self.data_row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
        group.add(self.data_row)
        if not self.pkg.extra.get("protected"):
            purge = Adw.ButtonRow(title="Uninstall and delete all data…")
            purge.add_css_class("destructive-action")
            purge.connect("activated", lambda *_: self._confirm_uninstall(True))
            group.add(purge)
        self.page.add(group)
        run_async(lambda: self.backend.data_locations(self.pkg) if self.backend else [],
                  self._show_data, lambda e: self.data_row.set_subtitle(str(e)))

    def _show_data(self, locations: list[str]) -> None:
        self._data_locations = locations
        self.data_row.set_subtitle("\n".join(locations) if locations else "No user data folders found")

    # actions -------------------------------------------------------------------------
    def _on_update(self, *_):
        update_with_running_check(
            self, self.manager, [self.pkg], f"Updating {self.pkg.display_name}",
            lambda skip: [] if self.pkg.uid in skip else self.backend.update(self.pkg),
            self._after_change)

    def _after_change(self, ok: bool) -> None:
        if ok:
            self.manager.reload()
            self.manager.check_updates()
            GLib.timeout_add(1200, lambda: (self.close(), GLib.SOURCE_REMOVE)[1])

    def _confirm_uninstall(self, delete_data: bool) -> None:
        confirm_uninstall(self, self.manager, self.pkg, delete_data, self._after_uninstall,
                          on_closed=self._uninstall_dialog_closed)

    def _after_uninstall(self, ok: bool) -> None:
        # Keep this view and the progress dialog open; the user closes them.
        self._uninstalled = ok
        if ok:
            self.manager.reload()
            self.manager.check_updates()

    def _uninstall_dialog_closed(self) -> None:
        if getattr(self, "_uninstalled", False):
            self.close()  # the package no longer exists


def confirm_uninstall(parent: Gtk.Widget, manager, pkg: Package, delete_data: bool, on_done,
                      on_closed=None) -> None:
    backend = manager.backend_for(pkg.kind)
    if pkg.extra.get("protected"):
        show_protected(parent, [pkg])
        return
    dialog = Adw.AlertDialog(
        heading=f"Uninstall {pkg.display_name}?",
        body=("The application will be removed from this computer." if pkg.is_app
              else f"The package “{pkg.name}” will be removed."),
    )
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    check = Gtk.CheckButton(label="Also delete app data and settings", active=delete_data)
    box.append(check)
    data_label = dim_label("", wrap=True)
    data_label.add_css_class("caption")
    box.append(data_label)
    extra_label = Gtk.Label(xalign=0, wrap=True)
    extra_label.add_css_class("caption")
    box.append(extra_label)
    dialog.set_extra_child(box)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("remove", "Uninstall")
    dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")

    def show_locations(locations):
        if locations:
            data_label.set_label("Data folders: " + ", ".join(locations))

    run_async(lambda: backend.data_locations(pkg), show_locations)

    if pkg.kind in (PackageKind.APT, PackageKind.DEB):
        # Only allow the removal once apt confirmed it does not take critical packages along.
        dialog.set_response_enabled("remove", False)
        extra_label.set_label("Checking which packages would be removed…")

        def show_removals(names):
            if _block_if_critical(dialog, extra_label, backend, names):
                return
            others = [n for n in names if n != pkg.name.split(":")[0]]
            extra_label.set_label(
                f"⚠ This will also remove {len(others)} other package(s): " + ", ".join(others[:30])
                if others else "")
            if others:
                extra_label.add_css_class("warning")
            dialog.set_response_enabled("remove", True)

        run_async(lambda: backend.simulate_removal(pkg, delete_data), show_removals,
                  lambda exc: _check_failed(dialog, extra_label, exc))

        broken_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        broken_label.add_css_class("caption")
        broken_label.add_css_class("warning")
        box.append(broken_label)

        def show_broken(names):
            if names:
                broken_label.set_label(
                    f"⚠ These packages are not fully installed: {', '.join(names)}. apt will try to "
                    "configure them again and may report errors that are unrelated to this removal.")
                broken_label.set_visible(True)
        run_async(backend.broken_packages, show_broken)

    def on_response(_d, response):
        if response != "remove":
            return
        try:
            commands = backend.uninstall(pkg, check.get_active())
        except Exception as exc:  # noqa: BLE001 - e.g. refused protected packages
            error_dialog(parent, "Cannot uninstall", exc)
            return
        run_operation(parent, manager, f"Uninstalling {pkg.display_name}", commands, on_done,
                      auto_close=False, on_closed=on_closed)

    dialog.connect("response", on_response)
    dialog.present(parent)



def confirm_bulk_uninstall(parent: Gtk.Widget, manager, pkgs: list[Package], on_done=None) -> None:
    """Confirm and uninstall several packages, batched per package format."""
    protected = [p for p in pkgs if p.extra.get("protected")]
    pkgs = [p for p in pkgs if not p.extra.get("protected")]
    if not pkgs:
        show_protected(parent, protected)
        return
    if len(pkgs) == 1 and not protected:
        confirm_uninstall(parent, manager, pkgs[0], False, on_done)
        return
    dialog = Adw.AlertDialog(heading=f"Uninstall {len(pkgs)} apps?",
                             body="These apps will be removed from this computer:")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    names = Gtk.Label(label="\n".join(f"• {p.display_name} ({p.kind.label})" for p in pkgs),
                      xalign=0, wrap=True)
    scroller = Gtk.ScrolledWindow(child=names, min_content_height=min(220, 22 * len(pkgs)),
                                  hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
    box.append(scroller)
    check = Gtk.CheckButton(label="Also delete app data and settings")
    box.append(check)
    warn = Gtk.Label(xalign=0, wrap=True, visible=False)
    warn.add_css_class("caption")
    warn.add_css_class("warning")
    box.append(warn)
    dialog.set_extra_child(box)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("remove", f"Uninstall {len(pkgs)} Apps")
    dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")

    if protected:
        skipped = Gtk.Label(xalign=0, wrap=True, label="🔒 Skipped protected system components: "
                            + ", ".join(p.display_name for p in protected))
        skipped.add_css_class("caption")
        skipped.add_css_class("dim-label")
        box.append(skipped)

    debs = [p for p in pkgs if p.kind in (PackageKind.APT, PackageKind.DEB)]
    if debs and manager.apt:
        selected = {p.name.split(":")[0] for p in debs}
        dialog.set_response_enabled("remove", False)
        warn.set_label("Checking which packages would be removed…")
        warn.set_visible(True)

        def show_removals(removed):
            if _block_if_critical(dialog, warn, manager.apt, removed):
                return
            others = [n for n in removed if n not in selected]
            warn.set_label(f"⚠ APT will also remove {len(others)} other package(s): "
                           + ", ".join(others[:30]) if others else "")
            warn.set_visible(bool(others))
            dialog.set_response_enabled("remove", True)

        run_async(lambda: manager.apt.simulate_removal(debs, False), show_removals,
                  lambda exc: _check_failed(dialog, warn, exc))

    def on_response(_d, response):
        if response != "remove":
            return
        commands = []
        try:
            for backend in manager.backends:
                group = [p for p in pkgs if p.kind in backend.kinds]
                if group:
                    commands += backend.uninstall_many(group, check.get_active())
        except Exception as exc:  # noqa: BLE001
            error_dialog(parent, "Cannot uninstall", exc)
            return
        run_operation(parent, manager, f"Uninstalling {len(pkgs)} apps", commands, on_done,
                      auto_close=False)

    dialog.connect("response", on_response)
    dialog.present(parent)


# ---------------------------------------------------------------------------
# Protection of critical system components
# ---------------------------------------------------------------------------

def protected_badge(reason: str, with_text: bool = False) -> Gtk.Widget:
    box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER,
                  tooltip_text=f"Protected system component: {reason}. It cannot be removed here.")
    box.add_css_class("protected")
    box.append(Gtk.Image.new_from_icon_name("system-lock-screen-symbolic"))
    if with_text:
        label = Gtk.Label(label=f"Protected system component — {reason}", wrap=True, xalign=0)
        box.append(label)
    return box


def show_protected(parent: Gtk.Widget, pkgs: list[Package]) -> None:
    lines = "\n".join(f"• {p.display_name}: {p.extra.get('protected')}" for p in pkgs)
    error_dialog(parent, "Protected system components",
                 "These packages are critical for your operating system and cannot be removed "
                 f"with UniPkg:\n\n{lines}")


def _block_if_critical(dialog: Adw.AlertDialog, label: Gtk.Label, apt_backend, removed: list[str]) -> bool:
    """Keep the removal disabled if apt would also remove protected packages."""
    critical = [(n, apt_backend.protected[n]) for n in removed if n in apt_backend.protected]
    if not critical:
        return False
    label.set_label("⛔ Not possible: this would also remove critical system components:\n"
                    + "\n".join(f"• {name} — {reason}" for name, reason in critical[:12])
                    + ("\n…" if len(critical) > 12 else ""))
    label.remove_css_class("warning")
    label.add_css_class("error")
    label.set_visible(True)
    dialog.set_response_enabled("remove", False)
    return True


def _check_failed(dialog: Adw.AlertDialog, label: Gtk.Label, exc) -> None:
    label.set_label(f"Could not check the dependencies ({exc}); apt itself still refuses to remove "
                    "essential packages.")
    label.set_visible(True)
    dialog.set_response_enabled("remove", True)
