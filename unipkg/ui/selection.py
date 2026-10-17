"""Multi-selection support shared by the Installed and Security pages."""

from __future__ import annotations

from typing import Callable

from gi.repository import GObject, Gtk


class Selection(GObject.Object):
    """A set of selected package uids that survives filtering and sorting."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self) -> None:
        super().__init__()
        self.uids: set[str] = set()

    def __contains__(self, uid: str) -> bool:
        return uid in self.uids

    def __len__(self) -> int:
        return len(self.uids)

    def set(self, uid: str, selected: bool) -> None:
        if selected == (uid in self.uids):
            return
        (self.uids.add if selected else self.uids.discard)(uid)
        self.emit("changed")

    def toggle(self, uid: str) -> None:
        self.set(uid, uid not in self.uids)

    def replace(self, uids: set[str]) -> None:
        self.uids = set(uids)
        self.emit("changed")

    def clear(self) -> None:
        self.replace(set())


class SelectionBar(Gtk.Revealer):
    """Bottom bar shown while apps are selected: count, select all/none, uninstall."""

    def __init__(self, selection: Selection, on_select_all: Callable[[], None],
                 on_uninstall: Callable[[], None]):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.selection = selection
        bar = Gtk.ActionBar()
        self.label = Gtk.Label(label="Select apps")
        self.label.add_css_class("heading")
        bar.pack_start(self.label)
        all_btn = Gtk.Button(label="Select All", tooltip_text="Select every app shown by the current filters")
        all_btn.connect("clicked", lambda *_: on_select_all())
        bar.pack_start(all_btn)
        none_btn = Gtk.Button(label="Select None")
        none_btn.connect("clicked", lambda *_: selection.clear())
        bar.pack_start(none_btn)
        self.uninstall_btn = Gtk.Button(label="Uninstall…", sensitive=False)
        self.uninstall_btn.add_css_class("destructive-action")
        self.uninstall_btn.connect("clicked", lambda *_: on_uninstall())
        bar.pack_end(self.uninstall_btn)
        self.set_child(bar)
        selection.connect("changed", lambda *_: self._sync())

    def _sync(self) -> None:
        n = len(self.selection)
        self.set_reveal_child(n > 0)
        self.label.set_label(f"{n} selected" if n else "Select apps")
        self.uninstall_btn.set_label(f"Uninstall {n}…" if n else "Uninstall…")
        self.uninstall_btn.set_sensitive(n > 0)

