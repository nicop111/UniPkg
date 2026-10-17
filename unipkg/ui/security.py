"""The "Security" page: installed apps ranked by risk."""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, GLib, Gtk

from ..models import Package
from ..risk import RiskAssessment, RiskLevel, assess_all
from .common import ICONS, dim_label, kind_badge
from .details import confirm_bulk_uninstall, confirm_uninstall, protected_badge
from .selection import Selection, SelectionBar


def risk_badge(level: RiskLevel, score: int | None = None) -> Gtk.Label:
    text = level.short if score is None else f"{level.short} · {score}"
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    label.add_css_class("badge")
    label.add_css_class(f"risk-{level.value}")
    return label


class SecurityPage(Gtk.Box):
    def __init__(self, manager, show_details: Callable[[Package], None],
                 find_alternatives: Callable[[Package], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.manager = manager
        self._show_details = show_details
        self._find_alternatives = find_alternatives
        self.assessments: list[RiskAssessment] = []

        self.stack = Gtk.Stack(vexpand=True)
        loading = Adw.StatusPage(title="Analysing installed apps…")
        loading.set_child(Adw.Spinner(width_request=48, height_request=48))
        self.stack.add_named(loading, "loading")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18,
                          margin_bottom=24, margin_start=12, margin_end=12)
        intro = dim_label("Installed apps (or all packages) ranked by risk: missing sandbox, "
                          "unverified origin, obsolete packages without security updates, pending "
                          "security updates and permissions that weaken the sandbox. Click an entry "
                          "to see why it was ranked.", wrap=True)
        content.append(intro)

        controls = Gtk.Box(spacing=8)
        self.scope_dd = Gtk.DropDown.new_from_strings(["Applications", "All packages"])
        self.scope_dd.set_tooltip_text("Assess only applications or every installed package, "
                                       "including libraries and system components")
        self.scope_dd.connect("notify::selected", lambda *_: self.rebuild())
        controls.append(self.scope_dd)
        self.search = Gtk.SearchEntry(placeholder_text="Filter the report…", hexpand=True)
        self.search.connect("search-changed", lambda *_: self._apply_search())
        controls.append(self.search)
        content.append(controls)

        self.tiles = Gtk.Box(spacing=12, homogeneous=True)
        self._tile_labels: dict[RiskLevel, Gtk.Label] = {}
        for level in (RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.LOW, RiskLevel.NONE):
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            tile.add_css_class("card")
            tile.add_css_class("risk-tile")
            tile.add_css_class(f"risk-tile-{level.value}")
            number = Gtk.Label(label="0")
            number.add_css_class("title-1")
            tile.append(number)
            caption = dim_label(level.label)
            caption.set_xalign(0.5)
            tile.append(caption)
            self._tile_labels[level] = number
            self.tiles.append(tile)
        content.append(self.tiles)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", self._on_row_activated)
        content.append(self.list)
        self.clean_label = dim_label("", wrap=True)
        content.append(self.clean_label)

        clamp = Adw.Clamp(maximum_size=1000, child=content)
        self.stack.add_named(Gtk.ScrolledWindow(child=clamp), "list")
        self.append(self.stack)

        self.selection = Selection()
        self._checks: dict[str, Gtk.CheckButton] = {}
        self.selection.connect("changed", lambda *_: self._sync_checks())
        self.selection_bar = SelectionBar(self.selection, self.select_all_listed,
                                          self.uninstall_selected)
        self.append(self.selection_bar)

        manager.connect("packages-changed", lambda *_: self.rebuild())
        manager.connect("updates-changed", lambda *_: self.rebuild())
        manager.connect("loading-changed", lambda *_: self.rebuild())

    # ------------------------------------------------------------------
    @property
    def high_count(self) -> int:
        return sum(1 for a in self.assessments if a.level == RiskLevel.HIGH)

    def rebuild(self) -> None:
        if self.manager.loading and not self.manager.packages:
            self.stack.set_visible_child_name("loading")
            return
        security = getattr(self.manager.apt, "security_updates", set()) if self.manager.apt else set()
        self.assessments = assess_all(self.manager.packages, security,
                                      include_all=self.scope_dd.get_selected() == 1)
        counts = {level: 0 for level in RiskLevel}
        for a in self.assessments:
            counts[a.level] += 1
        for level, label in self._tile_labels.items():
            label.set_label(str(counts[level]))

        present = {a.pkg.uid for a in self.assessments if a.level != RiskLevel.NONE}
        if self.selection.uids - present:
            self.selection.replace(self.selection.uids & present)
        self.list.remove_all()
        self._checks.clear()
        for a in self.assessments:
            if a.level != RiskLevel.NONE:
                self.list.append(self._row(a))
        clean = counts[RiskLevel.NONE]
        noun = "package" if self.scope_dd.get_selected() == 1 else "app"
        self.clean_label.set_label(f"{clean} {noun}{'s' if clean != 1 else ''} with no findings are not listed."
                                   if clean else "")
        self._apply_search()
        self.stack.set_visible_child_name("list")
        root = self.get_root()
        if hasattr(root, "_update_status"):
            root._update_status()  # sidebar counter follows the chosen scope

    def _apply_search(self) -> None:
        query = self.search.get_text().strip().lower()
        row = self.list.get_first_child()
        while row is not None:
            a = getattr(row, "_assessment", None)
            if a is not None:
                hay = f"{a.pkg.display_name} {a.pkg.name} {a.summary}".lower()
                row.set_visible(all(w in hay for w in query.split()))
            row = row.get_next_sibling()

    def _row(self, a: RiskAssessment) -> Gtk.ListBoxRow:
        pkg = a.pkg
        title = pkg.display_name if pkg.display_name == pkg.name or pkg.is_app else \
            f"{pkg.display_name} ({pkg.name})"
        row = Adw.ActionRow(title=title, subtitle=a.summary, activatable=True,
                            use_markup=False, subtitle_lines=2)
        image = Gtk.Image(pixel_size=32)
        ICONS.set_image(image, pkg, 32)
        row.add_prefix(image)
        reason = pkg.extra.get("protected")
        if reason:
            row.add_prefix(protected_badge(reason))  # later prefixes are placed further left
        else:
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=pkg.uid in self.selection,
                                    tooltip_text="Select for bulk uninstall")
            check.connect("toggled", lambda c, uid=pkg.uid: self.selection.set(uid, c.get_active()))
            self._checks[pkg.uid] = check
            row.add_prefix(check)
        row.add_suffix(kind_badge(pkg.kind))
        row.add_suffix(risk_badge(a.level, a.score))
        remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=f"Uninstall {pkg.display_name}…")
        remove.add_css_class("flat")
        remove.add_css_class("error")
        remove.connect("clicked", lambda *_: self.uninstall(pkg))
        remove.set_visible(not reason)
        row.add_suffix(remove)
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row._assessment = a  # type: ignore[attr-defined]
        return row

    # selection --------------------------------------------------------------
    def _on_row_activated(self, _list, row) -> None:
        self.show_assessment(row._assessment)

    def _sync_checks(self) -> None:
        for uid, check in self._checks.items():
            active = uid in self.selection
            if check.get_active() != active:
                check.set_active(active)  # set() ignores no-op changes, so no loop

    def select_all_listed(self) -> None:
        """Select every row that is currently shown (respects the search)."""
        uids = set()
        row = self.list.get_first_child()
        while row is not None:
            a = getattr(row, "_assessment", None)
            if a is not None and row.get_visible() and not a.pkg.extra.get("protected"):
                uids.add(a.pkg.uid)
            row = row.get_next_sibling()
        self.selection.replace(self.selection.uids | uids)

    def uninstall_selected(self) -> None:
        pkgs = [a.pkg for a in self.assessments if a.pkg.uid in self.selection]
        if pkgs:
            confirm_bulk_uninstall(self, self.manager, pkgs, lambda ok: self.manager.reload())

    def uninstall(self, pkg: Package, parent: Gtk.Widget | None = None) -> None:
        confirm_uninstall(parent or self, self.manager, pkg, False,
                          lambda ok: ok and self.manager.reload())

    def show_assessment(self, a: RiskAssessment) -> None:
        RiskDialog(self, a).present(self)


class RiskDialog(Adw.Dialog):
    def __init__(self, page: SecurityPage, a: RiskAssessment):
        super().__init__(title=a.pkg.display_name, content_width=640, content_height=700)
        self.page = page
        pkg = a.pkg
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        prefs = Adw.PreferencesPage()

        head_group = Adw.PreferencesGroup()
        head = Gtk.Box(spacing=16)
        image = Gtk.Image(pixel_size=64)
        ICONS.set_image(image, pkg, 64)
        head.append(image)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        title = Gtk.Label(label=pkg.display_name, xalign=0, wrap=True)
        title.add_css_class("title-2")
        texts.append(title)
        badges = Gtk.Box(spacing=6)
        badges.append(kind_badge(pkg.kind))
        badges.append(risk_badge(a.level))
        texts.append(badges)
        score = dim_label(f"Risk score {a.score} — High from 50, Medium from 20 points")
        score.add_css_class("caption")
        texts.append(score)
        head.append(texts)
        head_group.add(head)
        prefs.add(head_group)

        why = Adw.PreferencesGroup(title="Why this app was ranked",
                                   description="Each finding adds points to the risk score.")
        for factor in sorted(a.factors, key=lambda f: -f.points):
            row = Adw.ExpanderRow(title=factor.title, subtitle=factor.explanation, use_markup=False,
                                  subtitle_lines=0)
            points = Gtk.Label(label=f"+{factor.points}", valign=Gtk.Align.CENTER)
            points.add_css_class("badge")
            points.add_css_class(f"risk-{a.level.value if factor.points >= 20 else 'low'}")
            row.add_suffix(points)
            if factor.recommendation:
                tip = Adw.ActionRow(title="What you can do", subtitle=factor.recommendation,
                                    use_markup=False, subtitle_lines=0)
                tip.add_prefix(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
                row.add_row(tip)
                row.set_enable_expansion(True)
            else:
                row.set_enable_expansion(False)
            why.add(row)
        if not a.factors:
            why.add(Adw.ActionRow(title="No findings"))
        prefs.add(why)

        actions = Adw.PreferencesGroup(title="Actions")
        alt = Adw.ButtonRow(title="Find Safer Alternatives", start_icon_name="system-search-symbolic")
        alt.connect("activated", self._on_alternatives)
        actions.add(alt)
        details = Adw.ButtonRow(title="Open App Details and Permissions",
                                start_icon_name="emblem-system-symbolic")
        details.connect("activated", self._on_details)
        actions.add(details)
        if pkg.extra.get("protected"):
            locked = Adw.ActionRow(title="Protected system component",
                                   subtitle=f"{pkg.extra['protected']}. It cannot be removed here.",
                                   use_markup=False)
            locked.add_prefix(Gtk.Image.new_from_icon_name("system-lock-screen-symbolic"))
            actions.add(locked)
        else:
            remove = Adw.ButtonRow(title="Uninstall…", start_icon_name="user-trash-symbolic")
            remove.add_css_class("destructive-action")
            remove.connect("activated", lambda *_: page.uninstall(pkg, self))
            actions.add(remove)
        prefs.add(actions)

        toolbar.set_content(prefs)
        self.set_child(toolbar)
        self._pkg = pkg

    def _on_alternatives(self, *_):
        self.close()
        GLib.idle_add(lambda: (self.page._find_alternatives(self._pkg), GLib.SOURCE_REMOVE)[1])

    def _on_details(self, *_):
        self.close()
        GLib.idle_add(lambda: (self.page._show_details(self._pkg), GLib.SOURCE_REMOVE)[1])
