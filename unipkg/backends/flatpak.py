"""Flatpak backend (system and per-user installations)."""

from __future__ import annotations

import gzip
import os
import shlex
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from .. import host
from ..models import (
    Package,
    PackageKind,
    Permission,
    PermissionCategory as C,
    Repository,
    SearchResult,
    Trust,
    Verification,
    VerificationCheck,
)
from ..util import parse_keyfile, parse_size, split_list
from .base import Backend, BackendError, Command
from .gpg import describe_keyring

SYSTEM_DIR = "/var/lib/flatpak"
USER_DIR_EXPR = "$HOME/.local/share/flatpak"


def user_dir() -> str:
    return os.path.expanduser("~/.local/share/flatpak")


def installation_dir(installation: str) -> str:
    return SYSTEM_DIR if installation == "system" else user_dir()


def inst_flag(installation: str) -> str:
    return "--system" if installation == "system" else "--user"


# --------------------------------------------------------------------------
# Permission catalogue
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PermSpec:
    group: str  # metadata key: shared, sockets, devices, features, filesystems
    value: str
    label: str
    category: C
    sensitive: bool = False
    description: str = ""


CATALOG: list[PermSpec] = [
    PermSpec("shared", "network", "Network access", C.NETWORK, True,
             "Connect to the internet and the local network"),
    PermSpec("shared", "ipc", "Shared memory with host", C.OTHER, False,
             "Inter-process communication, needed by X11 apps"),
    PermSpec("sockets", "wayland", "Wayland display", C.DISPLAY),
    PermSpec("sockets", "x11", "X11 display", C.DISPLAY, True,
             "Legacy windowing system; allows reading keystrokes of other X11 windows"),
    PermSpec("sockets", "fallback-x11", "X11 fallback", C.DISPLAY, False,
             "X11 only when Wayland is not available"),
    PermSpec("sockets", "pulseaudio", "Sound and microphone", C.AUDIO, True,
             "Play sound and record audio through PulseAudio/PipeWire"),
    PermSpec("sockets", "cups", "Printing", C.DEVICES),
    PermSpec("sockets", "pcsc", "Smart cards", C.DEVICES),
    PermSpec("sockets", "ssh-auth", "SSH agent", C.OTHER, True,
             "Use your SSH keys through the SSH agent"),
    PermSpec("sockets", "gpg-agent", "GPG agent", C.OTHER, True),
    PermSpec("sockets", "session-bus", "Full session bus", C.BUS, True,
             "Talk to every service of your desktop session (sandbox escape)"),
    PermSpec("sockets", "system-bus", "Full system bus", C.BUS, True,
             "Talk to every system service"),
    PermSpec("devices", "dri", "GPU acceleration", C.DEVICES),
    PermSpec("devices", "input", "Input devices", C.DEVICES, False, "Game controllers etc."),
    PermSpec("devices", "usb", "USB devices", C.DEVICES, True),
    PermSpec("devices", "kvm", "Virtualization (KVM)", C.DEVICES),
    PermSpec("devices", "all", "All devices", C.DEVICES, True,
             "Webcams, microphones, USB and every other device"),
    PermSpec("features", "bluetooth", "Bluetooth", C.DEVICES, True),
    PermSpec("features", "devel", "Debugging (ptrace)", C.SYSTEM, True),
    PermSpec("filesystems", "host", "All files", C.FILESYSTEM, True,
             "Read and write every file on the computer"),
    PermSpec("filesystems", "host-os", "System libraries", C.FILESYSTEM, True),
    PermSpec("filesystems", "host-etc", "System configuration", C.FILESYSTEM, True),
    PermSpec("filesystems", "home", "Home folder", C.FILESYSTEM, True,
             "Read and write all files in your home folder"),
    PermSpec("filesystems", "xdg-download", "Downloads folder", C.FILESYSTEM),
    PermSpec("filesystems", "xdg-documents", "Documents folder", C.FILESYSTEM),
    PermSpec("filesystems", "xdg-pictures", "Pictures folder", C.FILESYSTEM),
    PermSpec("filesystems", "xdg-music", "Music folder", C.FILESYSTEM),
    PermSpec("filesystems", "xdg-videos", "Videos folder", C.FILESYSTEM),
    PermSpec("filesystems", "xdg-desktop", "Desktop folder", C.FILESYSTEM),
]

CATALOG_INDEX = {(s.group, s.value): s for s in CATALOG}

# Portal permissions stored in the permission store: key -> (table, id, label, category, sensitive)
PORTAL_PERMS = {
    "notifications": ("notifications", "notification", "Show notifications", C.NOTIFICATIONS, False),
    "background": ("background", "background", "Run in background", C.BACKGROUND, False),
    "camera": ("devices", "camera", "Camera (via portal)", C.CAMERA, True),
    "location": ("location", "location", "Location (via portal)", C.LOCATION, True),
}

_OVERRIDE_FLAGS = {
    "shared": ("--share", "--unshare"),
    "sockets": ("--socket", "--nosocket"),
    "devices": ("--device", "--nodevice"),
    "features": ("--allow", "--disallow"),
    "filesystems": ("--filesystem", "--nofilesystem"),
    "session": ("--talk-name", "--no-talk-name"),
    "system": ("--system-talk-name", "--system-no-talk-name"),
}


def _fs_key(entry: str) -> tuple[str, str]:
    """Split ``home:ro`` into ``("home", "ro")``."""
    if ":" in entry and entry.rsplit(":", 1)[1] in ("ro", "rw", "create"):
        path, mode = entry.rsplit(":", 1)
        return path, mode
    return entry, "rw"


@dataclass
class ContextState:
    """Effective sandbox context of an app (metadata + overrides)."""

    values: dict[str, dict[str, str]] = field(default_factory=dict)  # group -> value -> mode
    overridden: set[tuple[str, str]] = field(default_factory=set)

    def apply(self, groups: dict[str, dict[str, str]], is_override: bool) -> None:
        ctx = groups.get("Context", {})
        for group in ("shared", "sockets", "devices", "features", "filesystems"):
            for item in split_list(ctx.get(group, "")):
                negate = item.startswith("!")
                item = item[1:] if negate else item
                mode = "rw"
                if group == "filesystems":
                    item, mode = _fs_key(item)
                bucket = self.values.setdefault(group, {})
                if negate:
                    bucket.pop(item, None)
                else:
                    bucket[item] = mode
                if is_override:
                    self.overridden.add((group, item))
        for section, group in (("Session Bus Policy", "session"), ("System Bus Policy", "system")):
            for name, policy in groups.get(section, {}).items():
                bucket = self.values.setdefault(group, {})
                if policy == "none":
                    bucket.pop(name, None)
                else:
                    bucket[name] = policy
                if is_override:
                    self.overridden.add((group, name))

    def has(self, group: str, value: str) -> bool:
        return value in self.values.get(group, {})


def build_permissions(state: ContextState, portal: dict[str, Optional[str]]) -> list[Permission]:
    perms: list[Permission] = []
    seen: set[tuple[str, str]] = set()
    for spec in CATALOG:
        granted = state.has(spec.group, spec.value)
        label = spec.label
        if granted and spec.group == "filesystems" and state.values["filesystems"][spec.value] == "ro":
            label += " (read-only)"
        perms.append(Permission(
            key=f"{spec.group}:{spec.value}", label=label, category=spec.category,
            granted=granted, sensitive=spec.sensitive, description=spec.description,
            overridden=(spec.group, spec.value) in state.overridden,
        ))
        seen.add((spec.group, spec.value))
    # Additional, app specific filesystem paths and bus names.
    for group in ("filesystems", "session", "system"):
        for value, mode in sorted(state.values.get(group, {}).items()):
            if (group, value) in seen:
                continue
            if group == "filesystems":
                label = value + (" (read-only)" if mode == "ro" else "")
                cat, sensitive, desc = C.FILESYSTEM, value.startswith(("/", "~")), "File system path"
            elif value == "org.freedesktop.Notifications":
                label, cat, sensitive, desc = "Notifications (D-Bus)", C.NOTIFICATIONS, False, value
            else:
                label = f"Talk to {value}"
                cat = C.BUS
                sensitive = group == "system" or value in ("org.freedesktop.secrets", "org.freedesktop.Flatpak")
                desc = "System bus service" if group == "system" else "Session bus service"
            perms.append(Permission(
                key=f"{group}:{value}", label=label, category=cat, granted=True,
                sensitive=sensitive, description=desc,
                overridden=(group, value) in state.overridden,
            ))
    for key, (_table, _id, label, cat, sensitive) in PORTAL_PERMS.items():
        value = portal.get(key)
        if key == "notifications":
            granted = value != "no"  # allowed unless explicitly denied
        elif key == "background":
            granted = value != "no"
        elif key == "location":
            granted = bool(value) and value.upper() not in ("NONE", "NO")
        else:
            granted = value == "yes"
        desc = {
            None: "Not decided yet: the app will ask when it needs it",
        }.get(value, f"Stored decision: {value}")
        perms.append(Permission(
            key=f"portal:{key}", label=label, category=cat, granted=granted,
            sensitive=sensitive, description=desc, overridden=value is not None,
        ))
    return perms


def parse_permission_store(text: str) -> dict[str, dict[str, str]]:
    """Parse ``flatpak permissions TABLE`` -> {app: {id: value}}."""
    out: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < 4:
            continue
        _table, ident, app, value = cols[:4]
        out.setdefault(app.strip(), {})[ident.strip()] = value.strip().split(",")[0]
    return out


# --------------------------------------------------------------------------
# AppStream (developer names, verification, icons)
# --------------------------------------------------------------------------

@dataclass
class AppStreamInfo:
    name: str = ""
    summary: str = ""
    developer: str = ""
    verified: bool = False
    verification_method: str = ""
    verification_detail: str = ""
    icon: str = ""  # cached icon file name
    project_license: str = ""


def parse_appstream(data: bytes) -> dict[str, AppStreamInfo]:
    apps: dict[str, AppStreamInfo] = {}
    for _event, elem in ET.iterparse(__import__("io").BytesIO(data), events=("end",)):
        if elem.tag != "component":
            continue
        ident = elem.findtext("id") or ""
        if not ident:
            elem.clear()
            continue
        info = AppStreamInfo()
        for child in elem:
            if child.tag == "name" and not child.attrib and not info.name:
                info.name = (child.text or "").strip()
            elif child.tag == "summary" and not child.attrib and not info.summary:
                info.summary = (child.text or "").strip()
            elif child.tag == "developer_name" and not child.attrib and not info.developer:
                info.developer = (child.text or "").strip()
            elif child.tag == "developer" and not info.developer:
                for name in child.findall("name"):
                    if not name.attrib and name.text:
                        info.developer = name.text.strip()
                        break
            elif child.tag == "project_license":
                info.project_license = (child.text or "").strip()
            elif child.tag == "icon" and child.attrib.get("type") == "cached":
                if child.attrib.get("height") == "128" and "scale" not in child.attrib:
                    info.icon = (child.text or "").strip()
                elif not info.icon:
                    info.icon = (child.text or "").strip()
            elif child.tag == "custom":
                for value in child:
                    key = value.attrib.get("key", "")
                    if key == "flathub::verification::verified":
                        info.verified = (value.text or "").strip() == "true"
                    elif key == "flathub::verification::method":
                        info.verification_method = (value.text or "").strip()
                    elif key in ("flathub::verification::website",
                                 "flathub::verification::login_name"):
                        info.verification_detail = (value.text or "").strip()
        apps[ident.removesuffix(".desktop")] = info
        elem.clear()
    return apps


class FlatpakBackend(Backend):
    name = "Flatpak"
    kinds = (PackageKind.FLATPAK,)

    def __init__(self) -> None:
        self._appstream: dict[tuple[str, str], dict[str, AppStreamInfo]] = {}
        self._appstream_lock = threading.Lock()
        self._arch: Optional[str] = None

    def available(self) -> bool:
        return host.which("flatpak")

    # ------------------------------------------------------------------
    @property
    def arch(self) -> str:
        if self._arch is None:
            res = host.run(["flatpak", "--default-arch"], timeout=20)
            self._arch = res.stdout.strip() or "x86_64"
        return self._arch

    def appstream(self, installation: str, remote: str) -> dict[str, AppStreamInfo]:
        key = (installation, remote)
        with self._appstream_lock:
            if key in self._appstream:
                return self._appstream[key]
            base = f"{installation_dir(installation)}/appstream/{remote}/{self.arch}/active"
            data = host.read_host_file(f"{base}/appstream.xml.gz", max_bytes=256 * 1024 * 1024)
            apps: dict[str, AppStreamInfo] = {}
            if data:
                try:
                    apps = parse_appstream(gzip.decompress(data))
                except (OSError, ET.ParseError):
                    apps = {}
            for info in apps.values():
                if info.icon:
                    info.icon = f"{base}/icons/128x128/{info.icon}"
            self._appstream[key] = apps
            return apps

    def appstream_lookup(self, app_id: str, remote: str, installation: str = "") -> Optional[AppStreamInfo]:
        installs = [installation] if installation else ["system", "user"]
        for inst in installs:
            info = self.appstream(inst, remote).get(app_id)
            if info:
                return info
        return None

    # ------------------------------------------------------------------
    def list_installed(self) -> list[Package]:
        res = host.run([
            "flatpak", "list", "--app",
            "--columns=application,name,version,branch,origin,installation,size,arch,description",
        ])
        if not res.ok:
            raise BackendError(res.stderr.strip())
        packages: list[Package] = []
        for line in res.stdout.splitlines():
            cols = line.split("\t")
            if len(cols) < 8:
                continue
            app_id, name, version, branch, origin, installation, size, arch = cols[:8]
            summary = cols[8] if len(cols) > 8 else ""
            pkg = Package(
                kind=PackageKind.FLATPAK, name=app_id, display_name=name or app_id,
                version=version or branch, summary=summary, source=origin,
                size=parse_size(size), is_app=True, confinement="Flatpak sandbox",
                extra={"installation": installation, "branch": branch, "arch": arch,
                       "ref": f"app/{app_id}/{arch}/{branch}", "origin": origin},
            )
            packages.append(pkg)
        self._load_details(packages)
        return packages

    def _load_details(self, packages: list[Package]) -> None:
        if not packages:
            return
        # One host round-trip for all metadata and override files.
        script_parts = []
        for pkg in packages:
            inst = pkg.extra["installation"]
            base = SYSTEM_DIR if inst == "system" else USER_DIR_EXPR
            app = shlex.quote(pkg.name)
            script_parts.append(
                f'echo "@@META {inst} {pkg.name}"; cat "{base}/app/"{app}"/current/active/metadata" 2>/dev/null;'
            )
            script_parts.append(
                f'echo "@@OVR system {pkg.name}"; cat "{SYSTEM_DIR}/overrides/"{app} 2>/dev/null;'
            )
            script_parts.append(
                f'echo "@@OVR user {pkg.name}"; cat "{USER_DIR_EXPR}/overrides/"{app} 2>/dev/null;'
            )
        script_parts.append(f'echo "@@GLOBAL system"; cat "{SYSTEM_DIR}/overrides/global" 2>/dev/null;')
        script_parts.append(f'echo "@@GLOBAL user"; cat "{USER_DIR_EXPR}/overrides/global" 2>/dev/null;')
        for table in ("notifications", "background", "devices", "location"):
            script_parts.append(f'echo "@@STORE {table}"; flatpak permissions {table} 2>/dev/null;')
        res = host.run_shell("\n".join(script_parts), timeout=120)
        sections = _split_sections(res.stdout)

        store: dict[str, dict[str, dict[str, str]]] = {}
        for (tag, args), body in sections.items():
            if tag == "STORE":
                store[args[0]] = parse_permission_store(body)
        global_sys = parse_keyfile(sections.get(("GLOBAL", ("system",)), ""))
        global_user = parse_keyfile(sections.get(("GLOBAL", ("user",)), ""))

        for pkg in packages:
            inst = pkg.extra["installation"]
            meta = parse_keyfile(sections.get(("META", (inst, pkg.name)), ""))
            state = ContextState()
            state.apply(meta, is_override=False)
            state.apply(global_sys, is_override=False)
            state.apply(global_user, is_override=False)
            state.apply(parse_keyfile(sections.get(("OVR", ("system", pkg.name)), "")), True)
            state.apply(parse_keyfile(sections.get(("OVR", ("user", pkg.name)), "")), True)
            portal = {}
            for key, (table, ident, *_rest) in PORTAL_PERMS.items():
                portal[key] = store.get(table, {}).get(pkg.name, {}).get(ident)
            pkg.permissions = build_permissions(state, portal)
            pkg.extra["runtime"] = meta.get("Application", {}).get("runtime", "")

            info = self.appstream_lookup(pkg.name, pkg.source, inst)
            if info:
                pkg.developer = info.developer
                pkg.icon_path = info.icon or None
                pkg.extra["verified"] = info.verified
                pkg.extra["verification_method"] = info.verification_method
                pkg.extra["verification_detail"] = info.verification_detail
                pkg.extra["license"] = info.project_license
                pkg.trust = Trust.VERIFIED if info.verified else Trust.SIGNED
            else:
                pkg.trust = Trust.SIGNED
            exports = installation_dir(inst) + "/exports/share/icons/hicolor"
            pkg.extra["icon_candidates"] = [
                f"{exports}/128x128/apps/{pkg.name}.png",
                f"{exports}/scalable/apps/{pkg.name}.svg",
                f"{exports}/256x256/apps/{pkg.name}.png",
                f"{exports}/64x64/apps/{pkg.name}.png",
            ]
            if not pkg.developer:
                pkg.developer = _developer_from_id(pkg.name)

    def reload_permissions(self, pkg: Package) -> None:
        self._load_details([pkg])

    # Permissions -------------------------------------------------------
    def permissions_manageable(self, pkg: Package) -> bool:
        return True

    def set_permission(self, pkg: Package, perm: Permission, granted: bool) -> list[Command]:
        group, value = perm.key.split(":", 1)
        if group == "portal":
            table, ident, *_ = PORTAL_PERMS[value]
            if value == "location":
                decision = "EXACT" if granted else "NONE"
                argv = ["flatpak", "permission-set", table, ident, pkg.name, decision]
            else:
                argv = ["flatpak", "permission-set", table, ident, pkg.name,
                        "yes" if granted else "no"]
            return [Command(f"{'Allow' if granted else 'Deny'} {perm.label}", argv)]
        if value.endswith(" (read-only)"):
            value = value[: -len(" (read-only)")]
        grant_flag, revoke_flag = _OVERRIDE_FLAGS[group]
        flag = grant_flag if granted else revoke_flag
        return [Command(
            f"{'Grant' if granted else 'Revoke'} {perm.label}",
            ["flatpak", "override", "--user", f"{flag}={value}", pkg.name],
        )]

    def reset_permissions(self, pkg: Package) -> list[Command]:
        # Only the sandbox overrides are reset; decisions stored in the portal
        # permission store (notifications, background, …) are left untouched.
        return [Command("Reset sandbox overrides",
                        ["flatpak", "override", "--user", "--reset", pkg.name])]

    # Search / install ---------------------------------------------------------
    def search(self, query: str) -> list[SearchResult]:
        res = host.run([
            "flatpak", "search", "--columns=application,name,description,version,branch,remotes", query,
        ], timeout=60)
        if not res.ok:
            return []
        installed = self._installed_ids()
        results: dict[str, SearchResult] = {}
        for line in res.stdout.splitlines():
            cols = line.split("\t")
            if len(cols) < 6 or cols[0] in results:
                continue
            app_id, name, desc, version, branch, remotes = cols[:6]
            remote = remotes.split(",")[0].strip()
            installation = self._installation_for_remote(remote)
            info = self.appstream_lookup(app_id, remote)
            results[app_id] = SearchResult(
                kind=PackageKind.FLATPAK, name=app_id, display_name=name, version=version,
                summary=desc, developer=(info.developer if info else "") or _developer_from_id(app_id),
                source=remote, confinement="Sandboxed (Flatpak)",
                trust=Trust.VERIFIED if info and info.verified else Trust.SIGNED,
                installed=app_id in installed,
                extra={"branch": branch, "remote": remote, "installation": installation,
                       "icon_path": info.icon if info else None,
                       "verified": bool(info and info.verified),
                       "verification_method": info.verification_method if info else "",
                       "verification_detail": info.verification_detail if info else ""},
            )
        return list(results.values())[:40]

    def _installed_ids(self) -> set[str]:
        res = host.run(["flatpak", "list", "--app", "--columns=application"], timeout=30)
        return set(res.stdout.split())

    def _installation_for_remote(self, remote: str) -> str:
        for repo in self.list_repositories():
            if repo.id == remote and repo.scope == "system":
                return "system"
        return "user"

    def enrich(self, result: SearchResult) -> None:
        inst = result.extra.get("installation", "system")
        res = host.run(["flatpak", "remote-info", inst_flag(inst), result.extra["remote"],
                        f"{result.name}//{result.extra.get('branch', '')}".rstrip("/")], timeout=60)
        for line in res.stdout.splitlines():
            if ":" not in line:
                continue
            key, value = (p.strip() for p in line.split(":", 1))
            if key == "Download":
                result.download_size = parse_size(value)
            elif key == "Installed":
                result.installed_size = parse_size(value)
            elif key == "Runtime":
                result.extra["runtime"] = value
            elif key == "Commit":
                result.extra["commit"] = value
            elif key == "License":
                result.extra["license"] = value
        meta = host.run(["flatpak", "remote-info", "--show-metadata", inst_flag(inst),
                         result.extra["remote"], result.name], timeout=60)
        if meta.ok:
            state = ContextState()
            state.apply(parse_keyfile(meta.stdout), is_override=False)
            perms = build_permissions(state, {})
            sensitive = sorted({p.category.label for p in perms
                                if p.granted and p.sensitive and not p.key.startswith("portal:")})
            result.extra["permissions"] = [p.label for p in perms
                                           if p.granted and not p.key.startswith("portal:")]
            result.confinement = "Sandboxed" + (f" — access to {', '.join(sensitive)}" if sensitive else "")

    def install(self, result: SearchResult) -> list[Command]:
        inst = result.extra.get("installation", "system")
        ref = result.name
        if result.extra.get("branch"):
            ref = f"{result.name}//{result.extra['branch']}"
        return [Command(f"Install {result.display_name} (Flatpak)",
                        ["flatpak", "install", inst_flag(inst), "-y", "--noninteractive",
                         result.extra["remote"], ref])]

    def install_bundle_or_ref(self, path: str) -> list[Command]:
        """Install a .flatpakref or .flatpak bundle file."""
        flag = "--from" if path.endswith(".flatpakref") else "--bundle"
        return [Command(f"Install {os.path.basename(path)}",
                        ["flatpak", "install", "--user", "-y", "--noninteractive", flag, path])]

    def uninstall(self, pkg: Package, delete_data: bool) -> list[Command]:
        inst = pkg.extra.get("installation", "system")
        argv = ["flatpak", "uninstall", inst_flag(inst), "-y", "--noninteractive"]
        if delete_data:
            argv.append("--delete-data")
        argv.append(pkg.extra.get("ref", pkg.name))
        cmds = [Command(f"Uninstall {pkg.display_name}", argv)]
        if delete_data:
            cmds.append(Command("Remove permission overrides",
                                ["flatpak", "override", "--user", "--reset", pkg.name],
                                allow_failure=True))
        return cmds

    def data_locations(self, pkg: Package) -> list[str]:
        return [f"~/.var/app/{pkg.name}"]

    # Updates -------------------------------------------------------------------
    def list_updates(self) -> dict[str, str]:
        updates: dict[str, str] = {}
        for inst in ("system", "user"):
            res = host.run(["flatpak", "remote-ls", inst_flag(inst), "--updates", "--app",
                            "--columns=application,version,branch"], timeout=180)
            if not res.ok:
                continue
            for line in res.stdout.splitlines():
                cols = line.split("\t")
                if cols and cols[0]:
                    version = cols[1] if len(cols) > 1 and cols[1] else "new version"
                    updates[f"flatpak:{inst}:{cols[0]}"] = version
        return updates

    def update(self, pkg: Package) -> list[Command]:
        inst = pkg.extra.get("installation", "system")
        return [Command(f"Update {pkg.display_name}",
                        ["flatpak", "update", inst_flag(inst), "-y", "--noninteractive",
                         pkg.extra.get("ref", pkg.name)])]

    def update_all(self) -> list[Command]:
        return [
            Command("Update system Flatpaks", ["flatpak", "update", "--system", "-y", "--noninteractive"]),
            Command("Update user Flatpaks", ["flatpak", "update", "--user", "-y", "--noninteractive"],
                    allow_failure=True),
        ]

    # Verification -------------------------------------------------------------
    def _remote_config(self, installation: str, remote: str) -> dict[str, str]:
        data = host.read_host_file(f"{installation_dir(installation)}/repo/config") or b""
        groups = parse_keyfile(data.decode("utf-8", "replace"))
        return groups.get(f'remote "{remote}"', {})

    def _remote_checks(self, installation: str, remote: str) -> tuple[list[VerificationCheck], bool]:
        cfg = self._remote_config(installation, remote)
        checks: list[VerificationCheck] = []
        gpg_verify = cfg.get("gpg-verify", "true") == "true"
        summary_verify = cfg.get("gpg-verify-summary", "true") == "true"
        checks.append(VerificationCheck(
            "Remote requires GPG signed commits", gpg_verify,
            f"Remote “{remote}” ({cfg.get('url', 'unknown URL')})"
            + ("" if gpg_verify else " has gpg-verify=false — content is NOT verified"),
        ))
        checks.append(VerificationCheck("Remote summary is signed", summary_verify))
        keyring = f"{installation_dir(installation)}/repo/{remote}.trustedkeys.gpg"
        keys = describe_keyring(keyring)
        if keys:
            checks.append(VerificationCheck("Trusted signing key", True, "\n".join(keys)))
        elif gpg_verify:
            checks.append(VerificationCheck(
                "Trusted signing key", None,
                "Key stored in the system keyring / collection metadata"))
        return checks, gpg_verify and summary_verify

    def verify(self, pkg: Package, deep: bool = False) -> Verification:
        inst = pkg.extra.get("installation", "system")
        remote = pkg.source
        checks, signed = self._remote_checks(inst, remote)
        info = host.run(["flatpak", "info", inst_flag(inst), pkg.extra.get("ref", pkg.name)])
        local_commit = ""
        for line in info.stdout.splitlines():
            if line.strip().startswith("Commit:"):
                local_commit = line.split(":", 1)[1].strip()
        if local_commit:
            checks.append(VerificationCheck("Installed commit", None, local_commit))
        if deep and local_commit:
            remote_info = host.run(["flatpak", "remote-info", inst_flag(inst), remote,
                                    pkg.extra.get("ref", pkg.name)], timeout=60)
            remote_commit = ""
            for line in remote_info.stdout.splitlines():
                if line.strip().startswith("Commit:"):
                    remote_commit = line.split(":", 1)[1].strip()
            if remote_commit:
                same = remote_commit == local_commit
                checks.append(VerificationCheck(
                    "Matches the signed commit published by the remote", same if same else None,
                    "Identical" if same else
                    f"Remote has a newer commit {remote_commit[:16]}… (update available)"))
            if inst == "user":
                fsck = host.run(["flatpak", "repair", "--user", "--dry-run"], timeout=600)
                checks.append(VerificationCheck(
                    "Repository object integrity (flatpak repair --dry-run)", fsck.ok,
                    fsck.stdout.strip().splitlines()[-1] if fsck.stdout.strip() else ""))
        verified = pkg.extra.get("verified", False)
        method = pkg.extra.get("verification_method", "")
        detail = pkg.extra.get("verification_detail", "")
        checks.append(VerificationCheck(
            "Developer verified by Flathub", True if verified else None,
            f"Verified via {method}: {detail}" if verified else
            "The publisher of this app has not been verified by the remote"))
        if not signed:
            trust, summary = Trust.WARNING, "The remote does not verify signatures"
        elif verified:
            trust, summary = Trust.VERIFIED, f"Signed by {remote} and developer verified"
        else:
            trust, summary = Trust.SIGNED, f"Signed by {remote}; developer not verified"
        pkg.trust = trust
        return Verification(trust, summary, checks)

    def verify_search_result(self, result: SearchResult) -> Verification:
        checks, signed = self._remote_checks(result.extra.get("installation", "system"), result.source)
        verified = result.extra.get("verified")
        checks.append(VerificationCheck(
            "Developer verified by Flathub", True if verified else None,
            result.extra.get("verification_detail", "") if verified else "Not verified"))
        trust = Trust.WARNING if not signed else (Trust.VERIFIED if verified else Trust.SIGNED)
        return Verification(trust, trust.label, checks)

    # Repositories ----------------------------------------------------------------
    def list_repositories(self) -> list[Repository]:
        repos: list[Repository] = []
        for inst in ("system", "user"):
            res = host.run(["flatpak", "remotes", inst_flag(inst), "--show-disabled",
                            "--columns=name,title,url,options,priority"], timeout=30)
            if not res.ok:
                continue
            cfg_data = host.read_host_file(f"{installation_dir(inst)}/repo/config") or b""
            cfg = parse_keyfile(cfg_data.decode("utf-8", "replace"))
            for line in res.stdout.splitlines():
                cols = line.split("\t")
                if len(cols) < 3:
                    continue
                name, title, url = cols[:3]
                options = cols[3] if len(cols) > 3 else ""
                remote_cfg = cfg.get(f'remote "{name}"', {})
                gpg = remote_cfg.get("gpg-verify", "true") == "true"
                repos.append(Repository(
                    kind=PackageKind.FLATPAK, id=name, name=title or name, url=url,
                    enabled="disabled" not in options, scope=inst,
                    details=f"{inst.capitalize()} installation" + (f" · {options}" if options else ""),
                    signed_by="GPG verification enabled" if gpg else "GPG verification DISABLED",
                    trust=Trust.SIGNED if gpg else Trust.WARNING,
                ))
        return repos

    def repository_keys(self, repo: Repository) -> list[str]:
        return describe_keyring(f"{installation_dir(repo.scope)}/repo/{repo.id}.trustedkeys.gpg")

    def set_repository_enabled(self, repo: Repository, enabled: bool) -> list[Command]:
        return [Command(f"{'Enable' if enabled else 'Disable'} remote {repo.id}",
                        ["flatpak", "remote-modify", inst_flag(repo.scope),
                         "--enable" if enabled else "--disable", repo.id])]

    def remove_repository(self, repo: Repository) -> list[Command]:
        return [Command(f"Remove remote {repo.id}",
                        ["flatpak", "remote-delete", inst_flag(repo.scope), repo.id])]

    def add_repository(self, *, name: str, url: str, installation: str = "system", **_) -> list[Command]:
        argv = ["flatpak", "remote-add", inst_flag(installation), "--if-not-exists", name, url]
        if not url.endswith(".flatpakrepo"):
            # Plain repository URL: signatures are only checked if a key is given.
            pass
        return [Command(f"Add Flatpak remote {name}", argv)]


def _split_sections(text: str) -> dict[tuple[str, tuple[str, ...]], str]:
    sections: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    current: Optional[list[str]] = None
    for line in text.splitlines():
        if line.startswith("@@"):
            parts = line[2:].split()
            current = sections.setdefault((parts[0], tuple(parts[1:])), [])
            continue
        if current is not None:
            current.append(line)
    return {k: "\n".join(v) for k, v in sections.items()}


def _developer_from_id(app_id: str) -> str:
    parts = app_id.split(".")
    if len(parts) >= 3:
        if parts[0] in ("io", "com", "org", "net", "de", "dev", "app", "page", "me") and parts[1] in (
            "github", "gitlab", "codeberg", "sourceforge"
        ):
            return parts[2]
        return parts[1].capitalize()
    return ""
