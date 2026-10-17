"""A filter button that opens a list of checkboxes (nothing checked = everything)."""

from __future__ import annotations

from typing import Callable

from gi.repository import Gtk


class CheckFilterButton(Gtk.MenuButton):
    def __init__(self, all_label: str, tooltip: str, on_changed: Callable[[set[str]], None],
                 heading: str = ""):
        super().__init__(label=all_label, always_show_arrow=True, tooltip_text=tooltip)
        self.all_label = all_label
        self._on_changed = on_changed
        self._items: list[tuple[str, str]] = []
        self._checks: dict[str, Gtk.CheckButton] = {}
        self.selected: set[str] = set()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=8,
                      margin_bottom=8, margin_start=8, margin_end=8)
        if heading:
            title = Gtk.Label(label=heading, xalign=0, margin_start=4, margin_top=4)
            title.add_css_class("heading")
            box.append(title)
        hint = Gtk.Label(label="Nothing checked shows everything", xalign=0, margin_start=4)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        box.append(hint)
        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        scroller = Gtk.ScrolledWindow(child=self._list, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True, max_content_height=420,
                                      propagate_natural_width=True)
        box.append(scroller)
        clear = Gtk.Button(label="Clear Filter", margin_top=6)
        clear.add_css_class("flat")
        clear.connect("clicked", lambda *_: self.set_selected(set()))
        box.append(clear)
        self.set_popover(Gtk.Popover(child=box))

    def set_items(self, items: list[tuple[str, str]]) -> None:
        """Replace the options (``(key, label)``); keeps still existing selections."""
        if items == self._items:
            return
        self._items = list(items)
        child = self._list.get_first_child()
        while child is not None:
            self._list.remove(child)
            child = self._list.get_first_child()
        self._checks.clear()
        keys = {k for k, _ in items}
        dropped = self.selected - keys
        self.selected &= keys
        for key, label in items:
            check = Gtk.CheckButton(label=label, active=key in self.selected)
            check.connect("toggled", self._on_toggled, key)
            self._checks[key] = check
            self._list.append(check)
        self._update_label()
        if dropped:
            self._on_changed(set(self.selected))

    def set_selected(self, keys: set[str]) -> None:
        self.selected = {k for k in keys if k in self._checks}
        for key, check in self._checks.items():
            check.handler_block_by_func(self._on_toggled)
            check.set_active(key in self.selected)
            check.handler_unblock_by_func(self._on_toggled)
        self._update_label()
        self._on_changed(set(self.selected))

    def _on_toggled(self, check: Gtk.CheckButton, key: str) -> None:
        (self.selected.add if check.get_active() else self.selected.discard)(key)
        self._update_label()
        self._on_changed(set(self.selected))

    def _update_label(self) -> None:
        labels = [label for key, label in self._items if key in self.selected]
        if not labels:
            text = self.all_label
        elif len(labels) <= 2:
            text = ", ".join(labels)
        else:
            text = f"{labels[0]} +{len(labels) - 1}"
        self.set_label(text)
        if labels:
            self.add_css_class("filter-active")
        else:
            self.remove_css_class("filter-active")
