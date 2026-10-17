"""Dialog that runs a list of commands and shows their live output."""

from __future__ import annotations

import threading
import traceback
from typing import Callable, Optional

from gi.repository import Adw, GLib, Gtk

from .. import host
from ..backends.base import Command


class OperationDialog(Adw.Dialog):
    def __init__(
        self,
        title: str,
        commands: list[Command],
        lock: threading.Lock,
        on_done: Optional[Callable[[bool], None]] = None,
    ):
        super().__init__(title=title, content_width=620, content_height=460)
        self._commands = commands
        self._lock = lock
        self._on_done = on_done
        self._process: Optional[host.StreamingProcess] = None
        self._cancelled = False
        self._finished = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        self._close_btn = Gtk.Button(label="Cancel")
        self._close_btn.connect("clicked", self._on_close_clicked)
        header.pack_end(self._close_btn)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        self._status_icon = Adw.Spinner(width_request=32, height_request=32)
        self._status_stack = Gtk.Stack(halign=Gtk.Align.CENTER)
        self._status_stack.add_named(self._status_icon, "spinner")
        self._ok_icon = Gtk.Image.new_from_icon_name("object-select-symbolic")
        self._ok_icon.set_pixel_size(32)
        self._ok_icon.add_css_class("success")
        self._fail_icon = Gtk.Image.new_from_icon_name("dialog-error-symbolic")
        self._fail_icon.set_pixel_size(32)
        self._fail_icon.add_css_class("error")
        self._warn_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        self._warn_icon.set_pixel_size(32)
        self._warn_icon.add_css_class("warning")
        self._status_stack.add_named(self._ok_icon, "ok")
        self._status_stack.add_named(self._fail_icon, "fail")
        self._status_stack.add_named(self._warn_icon, "warn")
        box.append(self._status_stack)

        self._title = Gtk.Label(label=title, wrap=True, justify=Gtk.Justification.CENTER)
        self._title.add_css_class("title-3")
        box.append(self._title)
        self._step = Gtk.Label(label="Waiting…", wrap=True, justify=Gtk.Justification.CENTER)
        self._step.add_css_class("dim-label")
        box.append(self._step)
        self._progress = Gtk.ProgressBar(show_text=False)
        box.append(self._progress)
        self._warning = Gtk.Label(wrap=True, xalign=0, visible=False, selectable=True)
        self._warning.add_css_class("warning")
        box.append(self._warning)

        self._buffer = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self._buffer, editable=False, monospace=True,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR, cursor_visible=False,
                            top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        view.add_css_class("op-log")
        self._view = view
        scroller = Gtk.ScrolledWindow(child=view, vexpand=True, min_content_height=180)
        scroller.add_css_class("card")
        expander = Gtk.Expander(label="Details", child=scroller, vexpand=True)
        expander.set_expanded(False)
        box.append(expander)

        toolbar.set_content(box)
        self.set_child(toolbar)
        self.set_can_close(False)
        self.connect("close-attempt", lambda *_: self._on_close_clicked())

        self._pulse_id = GLib.timeout_add(120, self._pulse)
        threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------------
    def _pulse(self) -> bool:
        if self._finished:
            return GLib.SOURCE_REMOVE
        self._progress.pulse()
        return GLib.SOURCE_CONTINUE

    def _append(self, text: str) -> bool:
        end = self._buffer.get_end_iter()
        self._buffer.insert(end, text + "\n")
        mark = self._buffer.create_mark(None, self._buffer.get_end_iter(), False)
        self._view.scroll_mark_onscreen(mark)
        self._buffer.delete_mark(mark)
        return GLib.SOURCE_REMOVE

    def _set_step(self, text: str) -> bool:
        self._step.set_label(text)
        return GLib.SOURCE_REMOVE

    def _worker(self) -> None:
        ok = True
        warnings: list[str] = []
        with self._lock:
            total = len(self._commands)
            for index, cmd in enumerate(self._commands, 1):
                if self._cancelled:
                    ok = False
                    break
                GLib.idle_add(self._set_step, f"Step {index} of {total}: {cmd.description}")
                GLib.idle_add(self._append, f"$ {cmd.description}")
                output: list[str] = []

                def on_line(line: str, output=output) -> None:
                    output.append(line)
                    GLib.idle_add(self._append, line)

                try:
                    if cmd.func is not None:
                        message = cmd.func()
                        if message:
                            GLib.idle_add(self._append, message)
                        code = 0
                    else:
                        assert cmd.argv is not None
                        GLib.idle_add(self._append, f"  {host.format_argv(cmd.argv)}")
                        self._process = host.StreamingProcess(cmd.argv, on_line, cmd.env)
                        code = self._process.wait()
                        self._process = None
                except Exception as exc:  # noqa: BLE001
                    traceback.print_exc()
                    GLib.idle_add(self._append, f"Error: {exc}")
                    code = 1
                if code != 0:
                    if code in (126, 127) and cmd.argv and cmd.argv[0] == "pkexec":
                        GLib.idle_add(self._append, "Authentication was cancelled or failed.")
                    else:
                        GLib.idle_add(self._append, f"Exit status {code}")
                        warning = None
                        if cmd.on_failure is not None and not self._cancelled:
                            try:
                                warning = cmd.on_failure(code, "\n".join(output))
                            except Exception:  # noqa: BLE001
                                traceback.print_exc()
                        if warning:
                            GLib.idle_add(self._append, f"Warning: {warning}")
                            warnings.append(warning)
                            continue
                    if not cmd.allow_failure:
                        if cmd.failure_hint is not None:
                            try:
                                hint = cmd.failure_hint("\n".join(output))
                            except Exception:  # noqa: BLE001
                                hint = None
                            if hint:
                                warnings.append(hint)
                        ok = False
                        break
        GLib.idle_add(self._finish, ok, warnings)

    def _finish(self, ok: bool, warnings: list[str]) -> bool:
        self._finished = True
        self._progress.set_fraction(1.0 if ok else 0.0)
        self._status_stack.set_visible_child_name(("warn" if warnings else "ok") if ok else "fail")
        if ok and warnings:
            self._step.set_label("Completed with warnings")
            self._warning.set_label("\n\n".join(warnings))
            self._warning.set_visible(True)
        elif ok:
            self._step.set_label("Completed successfully")
        elif self._cancelled:
            self._step.set_label("Cancelled")
        else:
            self._step.set_label("The operation failed — see details")
            if warnings:
                self._warning.set_label("\n\n".join(warnings))
                self._warning.set_visible(True)
        self._close_btn.set_label("Close")
        self._close_btn.add_css_class("suggested-action")
        self.set_can_close(True)
        if self._on_done:
            self._on_done(ok)
        if ok and not warnings:
            GLib.timeout_add(900, self._auto_close)
        return GLib.SOURCE_REMOVE

    def _auto_close(self) -> bool:
        self.close()
        return GLib.SOURCE_REMOVE

    def _on_close_clicked(self, *_args) -> None:
        if self._finished:
            self.set_can_close(True)
            self.close()
            return
        self._cancelled = True
        if self._process:
            self._process.cancel()
        self._step.set_label("Cancelling…")


def run_operation(parent: Gtk.Widget, manager, title: str, commands: list[Command],
                  on_done: Optional[Callable[[bool], None]] = None) -> None:
    """Show an :class:`OperationDialog` for *commands* on top of *parent*."""
    if not commands:
        if on_done:
            on_done(True)
        return
    dialog = OperationDialog(title, commands, manager.operation_lock, on_done)
    dialog.present(parent)

