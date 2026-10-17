"""The "Repositories" page: APT sources, Flatpak remotes and the Snap store."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from ..models import Repository, Trust
from .common import dim_label, error_dialog, run_async, trust_badge
from .operation import run_operation


class RepositoriesPage(Gtk.Box):
    def __init__(self, manager):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.manager = manager
        self.stack = Gtk.Stack(vexpand=True)
        loading = Adw.StatusPage(title="Loading repositories…")
        loading.set_child(Adw.Spinner(width_request=48, height_request=48))
        self.stack.add_named(loading, "loading")
        self.page = Adw.PreferencesPage()
        self.stack.add_named(self.page, "list")
        self.append(self.stack)
        self._groups: list[Adw.PreferencesGroup] = []
        self._loaded = False
        manager.connect("repositories-changed", lambda *_: self.load())

    def header_widgets(self) -> list[Gtk.Widget]:
        add = Gtk.Button(icon_name="list-add-symbolic")
        add.set_tooltip_text("Add a repository")
        add.connect("clicked", lambda *_: AddRepositoryDialog(self.manager).present(self))
        return [add]

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def load(self) -> None:
        self._loaded = True
        self.stack.set_visible_child_name("loading")

        def work():
            out = []
            for backend in self.manager.backends:
                try:
                    out.append((backend, backend.list_repositories()))
                except Exception as exc:  # noqa: BLE001
                    out.append((backend, exc))
            return out

        run_async(work, self._show)

    def _show(self, data) -> None:
        for g in self._groups:
            self.page.remove(g)
        self._groups.clear()
        descriptions = {
            "APT": "Software sources for Debian packages. Each source is verified with its OpenPGP signing key.",
            "Flatpak": "Flatpak remotes. Remotes should always require GPG signed content.",
            "Snap": "Snaps come from the Snap Store; every snap revision is signed by the store.",
        }
        for backend, repos in data:
            group = Adw.PreferencesGroup(title=f"{backend.name} repositories",
                                         description=descriptions.get(backend.name, ""))
            if isinstance(repos, Exception):
                group.add(Adw.ActionRow(title="Could not load repositories", subtitle=str(repos)))
            else:
                enabled = [r for r in repos if r.enabled]
                disabled = [r for r in repos if not r.enabled]
                for repo in enabled + disabled:
                    group.add(self._row(backend, repo))
                if not repos:
                    group.add(Adw.ActionRow(title="No repositories configured"))
            self.page.add(group)
            self._groups.append(group)
        self.stack.set_visible_child_name("list")

    def _row(self, backend, repo: Repository) -> Gtk.Widget:
        row = Adw.ExpanderRow(title=GLib.markup_escape_text(repo.name),
                              subtitle=GLib.markup_escape_text(repo.url))
        icon = Gtk.Image.new_from_icon_name(repo.trust.icon)
        icon.add_css_class(f"trust-icon-{repo.trust.value}")
        icon.set_tooltip_text(repo.signed_by)
        row.add_prefix(icon)
        if not repo.enabled:
            row.add_css_class("disabled-repo")
        if repo.toggleable:
            switch = Gtk.Switch(active=repo.enabled, valign=Gtk.Align.CENTER)
            switch.set_tooltip_text("Enable or disable this repository")
            switch.connect("state-set", self._on_toggle, backend, repo)
            row.add_suffix(switch)

        for title, value in (("Details", repo.details), ("Configured in", repo.scope),
                             ("Signature", repo.signed_by)):
            if value:
                r = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(value),
                                  subtitle_selectable=True)
                r.add_css_class("property")
                row.add_row(r)
        if repo.trust == Trust.WARNING:
            warn = Adw.ActionRow(title="Signatures are not verified",
                                 subtitle="Anyone able to tamper with the connection could inject packages")
            warn.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
            warn.add_css_class("error")
            row.add_row(warn)

        if hasattr(backend, "repository_keys"):
            keys_row = Adw.ActionRow(title="Signing keys", subtitle="Loading…", subtitle_selectable=True)
            keys_row.add_prefix(Gtk.Image.new_from_icon_name("dialog-password-symbolic"))
            row.add_row(keys_row)
            loaded = {"done": False}

            def load_keys(*_):
                if loaded["done"] or not row.get_expanded():
                    return
                loaded["done"] = True
                run_async(lambda: backend.repository_keys(repo),
                          lambda keys: keys_row.set_subtitle(
                              GLib.markup_escape_text("\n\n".join(keys)) if keys else
                              "No dedicated key (global keyring)"),
                          lambda exc: keys_row.set_subtitle(str(exc)))

            row.connect("notify::expanded", load_keys)

        if repo.removable:
            remove = Adw.ButtonRow(title="Remove repository")
            remove.add_css_class("destructive-action")
            remove.connect("activated", lambda *_: self._confirm_remove(backend, repo))
            row.add_row(remove)
        return row

    def _on_toggle(self, switch, state, backend, repo):
        try:
            commands = backend.set_repository_enabled(repo, state)
        except Exception as exc:  # noqa: BLE001
            error_dialog(self, "Cannot change repository", exc)
            return True
        run_operation(self, self.manager, f"{'Enabling' if state else 'Disabling'} {repo.name}",
                      commands, lambda ok: self._after(ok, switch, not state))
        return False

    def _after(self, ok, switch=None, revert_to=None):
        if not ok and switch is not None:
            switch.set_active(revert_to)
        self.load()

    def _confirm_remove(self, backend, repo):
        dialog = Adw.AlertDialog(heading=f"Remove {repo.name}?",
                                 body=f"{repo.url}\n\nPackages from this repository stay installed but "
                                      "will no longer receive updates.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, resp):
            if resp == "remove":
                run_operation(self, self.manager, f"Removing {repo.name}",
                              backend.remove_repository(repo), lambda ok: self._after(ok))

        dialog.connect("response", on_response)
        dialog.present(self)


class AddRepositoryDialog(Adw.Dialog):
    def __init__(self, manager):
        super().__init__(title="Add Repository", content_width=560, content_height=640)
        self.manager = manager
        self._key_host_path = ""

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.add_btn = Gtk.Button(label="Add")
        self.add_btn.add_css_class("suggested-action")
        self.add_btn.connect("clicked", self._on_add)
        header.pack_end(self.add_btn)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        type_group = Adw.PreferencesGroup()
        kinds = []
        if manager.apt:
            kinds.append(("apt", "APT (Debian packages)"))
        if manager.flatpak:
            kinds.append(("flatpak", "Flatpak remote"))
        self._kinds = kinds
        self.type_row = Adw.ComboRow(title="Type", model=Gtk.StringList.new([k[1] for k in kinds]))
        self.type_row.connect("notify::selected", lambda *_: self._sync())
        type_group.add(self.type_row)
        page.add(type_group)

        # APT
        self.apt_group = Adw.PreferencesGroup(
            title="APT source",
            description="Enter a PPA (ppa:user/name) or a repository URL. Always add the repository's "
                        "signing key so packages can be verified.")
        self.apt_name = Adw.EntryRow(title="Name")
        self.apt_url = Adw.EntryRow(title="URL or ppa:user/name")
        self.apt_suites = Adw.EntryRow(title="Suites (e.g. stable)")
        self.apt_components = Adw.EntryRow(title="Components (e.g. main)")
        self.apt_key = Adw.EntryRow(title="Signing key URL (https://…)")
        fetch = Gtk.Button(label="Fetch", valign=Gtk.Align.CENTER)
        fetch.add_css_class("flat")
        fetch.connect("clicked", self._on_fetch_key)
        self.apt_key.add_suffix(fetch)
        self.key_info = Adw.ActionRow(title="Key fingerprint",
                                      subtitle="Fetch the key and compare the fingerprint with the one "
                                               "published by the vendor", subtitle_selectable=True)
        self.key_info.add_prefix(Gtk.Image.new_from_icon_name("dialog-password-symbolic"))
        for w in (self.apt_name, self.apt_url, self.apt_suites, self.apt_components, self.apt_key,
                  self.key_info):
            self.apt_group.add(w)
        page.add(self.apt_group)

        # Flatpak
        self.fp_group = Adw.PreferencesGroup(
            title="Flatpak remote",
            description="Use a .flatpakrepo URL — it contains the signing key of the remote.")
        self.fp_name = Adw.EntryRow(title="Name (e.g. flathub-beta)")
        self.fp_url = Adw.EntryRow(title=".flatpakrepo URL")
        self.fp_inst = Adw.ComboRow(title="Installation", model=Gtk.StringList.new(["System", "User"]))
        for w in (self.fp_name, self.fp_url, self.fp_inst):
            self.fp_group.add(w)
        page.add(self.fp_group)
        examples = Adw.PreferencesGroup()
        ex = dim_label("Example: flathub-beta — https://flathub.org/beta-repo/flathub-beta.flatpakrepo",
                       wrap=True)
        ex.add_css_class("caption")
        examples.add(ex)
        page.add(examples)
        self._examples = examples

        toolbar.set_content(page)
        self.set_child(toolbar)
        self._sync()

    def _kind(self) -> str:
        idx = self.type_row.get_selected()
        return self._kinds[idx][0] if 0 <= idx < len(self._kinds) else ""

    def _sync(self):
        kind = self._kind()
        self.apt_group.set_visible(kind == "apt")
        self.fp_group.set_visible(kind == "flatpak")
        self._examples.set_visible(kind == "flatpak")

    def _on_fetch_key(self, *_):
        url = self.apt_key.get_text().strip()
        if not url.startswith("https://"):
            error_dialog(self, "Invalid key URL", "The key must be downloaded over HTTPS")
            return
        self.key_info.set_subtitle("Downloading…")

        def done(res):
            path, keys = res
            self._key_host_path = path
            text = "\n\n".join(k.describe() for k in keys)
            self.key_info.set_subtitle(GLib.markup_escape_text(text))

        run_async(lambda: self.manager.apt.fetch_key(url), done,
                  lambda exc: self.key_info.set_subtitle(GLib.markup_escape_text(str(exc))))

    def _on_add(self, *_):
        kind = self._kind()
        try:
            if kind == "apt":
                url = self.apt_url.get_text().strip()
                if not url:
                    raise ValueError("Enter a repository URL or PPA")
                if not url.startswith("ppa:") and not self._key_host_path:
                    if self.apt_key.get_text().strip():
                        raise ValueError("Press “Fetch” to download and check the signing key first")
                fallback = url.split("/")[2] if "://" in url else url
                name = self.apt_name.get_text().strip() or fallback
                commands = self.manager.apt.add_repository(
                    name=name, url=url, suites=self.apt_suites.get_text().strip(),
                    components=self.apt_components.get_text().strip(), key_path=self._key_host_path)
            elif kind == "flatpak":
                name = self.fp_name.get_text().strip()
                url = self.fp_url.get_text().strip()
                if not name or not url:
                    raise ValueError("Enter a name and URL")
                inst = "system" if self.fp_inst.get_selected() == 0 else "user"
                commands = self.manager.flatpak.add_repository(name=name, url=url, installation=inst)
            else:
                return
        except Exception as exc:  # noqa: BLE001
            error_dialog(self, "Cannot add repository", exc)
            return
        if kind == "apt" and not url.startswith("ppa:") and not self._key_host_path:
            warn = Adw.AlertDialog(
                heading="Add repository without a signing key?",
                body="Without a dedicated signing key the repository is checked against every key in "
                     "the global APT keyring. If the repository is unsigned, apt will refuse it.")
            warn.add_response("cancel", "Cancel")
            warn.add_response("add", "Add anyway")
            warn.set_response_appearance("add", Adw.ResponseAppearance.DESTRUCTIVE)
            warn.connect("response", lambda _d, r: r == "add" and self._run(commands))
            warn.present(self)
            return
        self._run(commands)

    def _run(self, commands):
        def done(ok):
            if ok:
                self.manager.emit("repositories-changed")
                self.close()

        run_operation(self, self.manager, "Adding repository", commands, done)

