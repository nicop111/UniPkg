"""Updating apps that are currently running.

Before an update the target apps are checked. If some are running the user can
close them (gracefully, SIGTERM) and update, or update anyway: Flatpak and
APT apps keep running the old version until restarted, while snapd refuses to
refresh running snaps, so those are skipped. Afterwards a toast offers to
reopen closed apps or restart apps that are still running the old version.
"""

from __future__ import annotations

from typing import Callable, Optional

from gi.repository import Adw, GLib, Gtk

from .. import running
from ..backends.base import Command
from ..models import Package
from .common import error_dialog, run_async
from .operation import run_operation

# build(skip_uids) -> commands for all packages except the skipped ones
CommandBuilder = Callable[[set[str]], list[Command]]


def update_with_running_check(parent: Gtk.Widget, manager, pkgs: list[Package], title: str,
                              build: CommandBuilder,
                              on_done: Optional[Callable[[bool], None]] = None) -> None:
    window = parent.get_root()

    def checked(apps: list[running.RunningApp]) -> None:
        if not apps:
            run_operation(parent, manager, title, build(set()), on_done)
            return
        _ask(parent, window, manager, pkgs, apps, title, build, on_done)

    def failed(_exc) -> None:  # detection is best effort; never block updates on it
        run_operation(parent, manager, title, build(set()), on_done)

    run_async(lambda: running.find_running(manager, pkgs), checked, failed)


def _ask(parent, window, manager, pkgs, apps, title, build, on_done) -> None:
    names = ", ".join(a.pkg.display_name for a in apps)
    snaps = [a for a in apps if not a.can_update_while_running]
    others = [a for a in apps if a.can_update_while_running]
    lines = []
    if snaps:
        lines.append("Snaps cannot be updated while they run: "
                     + ", ".join(a.pkg.display_name for a in snaps) + ".")
    if others:
        lines.append("These apps can be updated while running but keep using the old version until "
                     "restarted: " + ", ".join(a.pkg.display_name for a in others) + ".")
    lines.append("Closing an app asks it to quit normally; save your work first.")
    plural = len(apps) > 1
    dialog = Adw.AlertDialog(heading=f"{names} {'are' if plural else 'is'} running",
                             body="\n\n".join(lines))
    dialog.add_response("cancel", "Cancel")
    skip_snap_uids = {a.pkg.uid for a in snaps}
    only_running_snaps = {p.uid for p in pkgs} <= skip_snap_uids
    if not only_running_snaps:
        dialog.add_response("anyway", "Skip Running Snaps" if snaps else "Update Anyway")
    dialog.add_response("close", f"Close {'Apps' if plural else 'App'} and Update")
    dialog.set_response_appearance("close", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("close")
    dialog.set_close_response("cancel")

    def on_response(_d, response):
        if response == "anyway":
            def done(ok: bool) -> None:
                if on_done:
                    on_done(ok)
                if ok and others:
                    _toast_restart(window, [a.pkg for a in others], manager)
            run_operation(parent, manager, title, build(skip_snap_uids), done)
        elif response == "close":
            _close_then_update(parent, window, manager, apps, title, build, on_done)

    dialog.connect("response", on_response)
    dialog.present(parent)


def _close_then_update(parent, window, manager, apps, title, build, on_done) -> None:
    pids = [pid for a in apps for pid in a.pids]

    def still_running(remaining: list[int]) -> None:
        if remaining:
            stuck = [a.pkg.display_name for a in apps if set(a.pids) & set(remaining)]
            error_dialog(parent, "Could not close all apps",
                         f"{', '.join(stuck)} did not quit within 10 seconds (it may be showing an "
                         "unsaved-changes prompt). Close it yourself, then start the update again.")
            return

        def done(ok: bool) -> None:
            if on_done:
                on_done(ok)
            _toast_reopen(window, [a.pkg for a in apps], ok)

        run_operation(parent, manager, title, build(set()), done)

    run_async(lambda: running.terminate(pids), still_running,
              lambda exc: error_dialog(parent, "Could not close apps", exc))


def _toast(window, title: str, button: str, action: Callable[[], None]) -> None:
    overlay = getattr(window, "toast_overlay", None)
    if overlay is None:
        return
    toast = Adw.Toast(title=GLib.markup_escape_text(title), button_label=button, timeout=0)
    toast.connect("button-clicked", lambda *_: action())
    overlay.add_toast(toast)


def _toast_reopen(window, pkgs: list[Package], ok: bool) -> None:
    names = ", ".join(p.display_name for p in pkgs)
    title = f"{names} {'updated' if ok else 'not updated'} — the app was closed"
    _toast(window, title, "Reopen", lambda: run_async(lambda: [running.launch(p) for p in pkgs]))


def _toast_restart(window, pkgs: list[Package], manager) -> None:
    names = ", ".join(p.display_name for p in pkgs)

    def restart():
        def work():
            apps = running.find_running(manager, pkgs)
            remaining = running.terminate([pid for a in apps for pid in a.pids])
            if remaining:
                raise RuntimeError("The app did not quit; close it yourself and start it again.")
            for p in pkgs:
                running.launch(p)

        run_async(work, None, lambda exc: error_dialog(window, "Restart failed", exc))

    _toast(window, f"Restart {names} to use the new version", "Restart", restart)

