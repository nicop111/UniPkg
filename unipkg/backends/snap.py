"""Snap backend, talking to snapd's REST API on the host."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import urllib.parse
from typing import Any, Optional

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
from ..util import parse_date, parse_deb822
from .base import Backend, BackendError, Command, guard_protected

SOCKET = "/run/snapd.socket"

# interface -> (label, category, sensitive)
INTERFACES: dict[str, tuple[str, C, bool]] = {
    "network": ("Network access", C.NETWORK, True),
    "network-bind": ("Act as a network server", C.NETWORK, True),
    "network-observe": ("Observe network status", C.NETWORK, False),
    "network-manager": ("Configure network (NetworkManager)", C.NETWORK, True),
    "network-control": ("Full network control", C.NETWORK, True),
    "network-status": ("Network status", C.NETWORK, False),
    "home": ("Home folder", C.FILESYSTEM, True),
    "removable-media": ("Removable media", C.FILESYSTEM, True),
    "personal-files": ("Personal files", C.FILESYSTEM, True),
    "system-files": ("System files", C.FILESYSTEM, True),
    "camera": ("Camera", C.CAMERA, True),
    "audio-playback": ("Sound playback", C.AUDIO, False),
    "audio-record": ("Microphone", C.AUDIO, True),
    "pulseaudio": ("PulseAudio (sound and microphone)", C.AUDIO, True),
    "desktop": ("Desktop integration & notifications", C.NOTIFICATIONS, False),
    "desktop-legacy": ("Legacy desktop integration", C.DISPLAY, False),
    "unity7": ("Unity7 / X11 legacy access", C.DISPLAY, True),
    "x11": ("X11 display", C.DISPLAY, True),
    "wayland": ("Wayland display", C.DISPLAY, False),
    "opengl": ("GPU acceleration", C.DEVICES, False),
    "bluez": ("Bluetooth", C.DEVICES, True),
    "bluetooth-control": ("Bluetooth control", C.DEVICES, True),
    "raw-usb": ("Raw USB access", C.DEVICES, True),
    "joystick": ("Game controllers", C.DEVICES, False),
    "u2f-devices": ("Security keys (U2F)", C.DEVICES, False),
    "hardware-observe": ("Read hardware information", C.SYSTEM, False),
    "cups-control": ("Printing", C.DEVICES, False),
    "cups": ("Printing", C.DEVICES, False),
    "location-observe": ("Location", C.LOCATION, True),
    "location-control": ("Location control", C.LOCATION, True),
    "password-manager-service": ("Password manager / secrets", C.BUS, True),
    "system-observe": ("Observe system processes", C.SYSTEM, True),
    "process-control": ("Control processes", C.SYSTEM, True),
    "mount-observe": ("Observe mounts", C.SYSTEM, False),
    "screen-inhibit-control": ("Prevent screen locking", C.OTHER, False),
    "browser-support": ("Browser sandbox support", C.OTHER, False),
    "gsettings": ("Desktop settings", C.OTHER, False),
    "shutdown": ("Shut down the computer", C.SYSTEM, True),
    "ssh-keys": ("SSH keys", C.FILESYSTEM, True),
    "ssh-public-keys": ("SSH public keys", C.FILESYSTEM, False),
    "gpg-keys": ("GPG keys", C.FILESYSTEM, True),
    "login-session-observe": ("Observe login sessions", C.SYSTEM, False),
    "upower-observe": ("Battery status", C.SYSTEM, False),
    "content": ("Shared content", C.OTHER, False),
}


def _api_curl(path: str, method: str = "GET", body: Optional[dict] = None) -> Optional[dict]:
    argv = ["curl", "-sS", "--unix-socket", SOCKET, "-X", method]
    if body is not None:
        argv += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    argv.append(f"http://localhost{path}")
    res = host.run(argv, timeout=120)
    if res.returncode == 127 or (not res.stdout and res.returncode != 0):
        return None
    try:
        return json.loads(res.stdout)
    except ValueError:
        return {"raw": res.stdout}


_PY_CLIENT = r"""
import socket, sys
path = sys.argv[1]
s = socket.socket(socket.AF_UNIX); s.connect("/run/snapd.socket")
s.sendall(("GET %s HTTP/1.0\r\nHost: localhost\r\n\r\n" % path).encode())
data = b""
while True:
    chunk = s.recv(65536)
    if not chunk: break
    data += chunk
sys.stdout.write(data.split(b"\r\n\r\n", 1)[1].decode())
"""


def api(path: str) -> Any:
    data = _api_curl(path)
    if data is None:
        res = host.run(["python3", "-c", _PY_CLIENT, path], timeout=120)
        if not res.ok:
            raise BackendError("Cannot reach snapd (curl or python3 is required on the host)")
        try:
            data = json.loads(res.stdout)
        except ValueError:
            data = {"raw": res.stdout}
    return data


def api_raw(path: str) -> str:
    res = host.run(["curl", "-sS", "--unix-socket", SOCKET, f"http://localhost{path}"], timeout=60)
    return res.stdout


def parse_assertions(text: str) -> list[dict[str, str]]:
    """Parse the headers of a stream of snap assertions."""
    out = []
    for block in text.split("\n\n"):
        if block.startswith("type:"):
            out.append(parse_deb822(block)[0])
    return out


def _validation_trust(validation: str) -> Trust:
    return Trust.VERIFIED if validation in ("verified", "starred") else Trust.SIGNED


# The snap CLI only lets polkit ask for a password when stdin is a terminal;
# UniPkg runs commands without one, so every state-changing snap command is
# wrapped in pkexec (see host.privileged).


class SnapBackend(Backend):
    name = "Snap"
    kinds = (PackageKind.SNAP,)

    def available(self) -> bool:
        return host.which("snap") and host.run(["test", "-S", SOCKET], timeout=10).ok

    def list_installed(self) -> list[Package]:
        data = api("/v2/snaps")
        result = data.get("result")
        if not isinstance(result, list):
            raise BackendError(str(result))
        packages = []
        for snap in result:
            publisher = snap.get("publisher") or {}
            classic = snap.get("confinement") == "classic"
            devmode = snap.get("devmode", False)
            apps = snap.get("apps") or []
            desktop_files = [a["desktop-file"] for a in apps if a.get("desktop-file")]
            is_app = snap.get("type") == "app" and bool(desktop_files)
            validation = publisher.get("validation", "")
            local = snap.get("revision", "").startswith("x")
            pkg = Package(
                kind=PackageKind.SNAP, name=snap["name"],
                display_name=snap.get("title") or snap["name"],
                version=snap.get("version", ""), summary=snap.get("summary", ""),
                developer=publisher.get("display-name") or snap.get("developer", ""),
                source="Local file" if local else f"Snap Store ({snap.get('tracking-channel') or snap.get('channel', '')})",
                size=int(snap.get("installed-size") or 0), is_app=is_app,
                confinement="Unconfined (classic)" if classic else ("Devmode (not enforced)" if devmode else "Strict confinement"),
                trust=Trust.UNVERIFIED if local else _validation_trust(validation),
                extra={"installation": "", "id": snap.get("id", ""), "revision": snap.get("revision", ""),
                       "classic": classic or devmode, "devmode": devmode and not classic,
                       "type": snap.get("type", ""),
                       "validation": validation, "channel": snap.get("tracking-channel", ""),
                       "desktop_files": desktop_files, "mounted_from": snap.get("mounted-from", ""),
                       "publisher_username": publisher.get("username", ""),
                       "license": snap.get("license", "")},
            )
            pkg.updated = parse_date(snap.get("install-date", ""))
            if snap.get("type") in ("base", "os", "core", "kernel", "gadget", "snapd"):
                pkg.extra["protected"] = {
                    "snapd": "The snap system itself",
                    "kernel": "Kernel snap",
                    "gadget": "Device gadget snap",
                }.get(snap.get("type"), "Base snap other snaps run on")
            if snap.get("icon", "").startswith("/v2/icons"):
                pkg.extra["icon_api"] = snap["icon"]
            packages.append(pkg)
        self._load_connections(packages)
        return packages

    def _load_connections(self, packages: list[Package]) -> None:
        data = api("/v2/connections?select=all")
        result = data.get("result") or {}
        by_snap: dict[str, list[dict]] = {}
        for plug in result.get("plugs", []):
            by_snap.setdefault(plug["snap"], []).append(plug)
        for pkg in packages:
            pkg.permissions = self._permissions_from_plugs(by_snap.get(pkg.name, []), pkg)

    def _permissions_from_plugs(self, plugs: list[dict], pkg: Package) -> list[Permission]:
        perms = unconfined_snap_permissions(pkg)
        for plug in sorted(plugs, key=lambda p: p["plug"]):
            iface = plug.get("interface", "")
            label, cat, sensitive = INTERFACES.get(iface, (iface.replace("-", " ").capitalize(), C.OTHER, False))
            if iface == "content":
                label = f"Shared content: {plug.get('attrs', {}).get('content', plug['plug'])}"
            elif plug["plug"] != iface:
                label = f"{label} ({plug['plug']})"
            connections = plug.get("connections") or []
            perms.append(Permission(
                key=plug["plug"], label=label, category=cat, granted=bool(connections),
                sensitive=sensitive, toggleable=not pkg.extra.get("classic"),
                description=f"Interface “{iface}”" + (
                    f" connected to {connections[0]['snap']}:{connections[0]['slot']}" if connections else " (disconnected)"),
            ))
        return perms

    def reload_permissions(self, pkg: Package) -> None:
        data = api(f"/v2/connections?snap={urllib.parse.quote(pkg.name)}&select=all")
        plugs = [p for p in (data.get("result") or {}).get("plugs", []) if p["snap"] == pkg.name]
        pkg.permissions = self._permissions_from_plugs(plugs, pkg)

    def permissions_manageable(self, pkg: Package) -> bool:
        return not pkg.extra.get("classic")

    def set_permission(self, pkg: Package, perm: Permission, granted: bool) -> list[Command]:
        verb = "connect" if granted else "disconnect"
        return [Command(f"{verb.capitalize()} {perm.label}",
                        host.privileged(["snap", verb, f"{pkg.name}:{perm.key}"]))]

    # Search / install ------------------------------------------------------------
    def search(self, query: str) -> list[SearchResult]:
        data = api(f"/v2/find?q={urllib.parse.quote(query)}")
        result = data.get("result")
        if not isinstance(result, list):
            return []
        installed = {s["name"] for s in (api("/v2/snaps").get("result") or [])}
        out = []
        for snap in result[:40]:
            publisher = snap.get("publisher") or {}
            classic = snap.get("confinement") == "classic"
            out.append(SearchResult(
                kind=PackageKind.SNAP, name=snap["name"], display_name=snap.get("title") or snap["name"],
                version=snap.get("version", ""), summary=snap.get("summary", ""),
                developer=publisher.get("display-name", ""), source=f"Snap Store ({snap.get('channel', 'stable')})",
                download_size=int(snap.get("download-size") or 0),
                # Snaps stay compressed (squashfs) on disk: installed size == download size.
                installed_size=int(snap.get("download-size") or 0),
                trust=_validation_trust(publisher.get("validation", "")),
                confinement="Unconfined (classic)" if classic else "Strict confinement",
                installed=snap["name"] in installed,
                extra={"classic": classic, "validation": publisher.get("validation", ""),
                       "permissions": ["Full system access (classic)"] if classic else
                       ["Interfaces granted by the store policy; reviewable after install"],
                       "icon_url": snap.get("icon", ""), "license": snap.get("license", ""),
                       "common_ids": snap.get("common-ids", [])},
            ))
        return out

    def enrich(self, result: SearchResult) -> None:
        """Look up when the version on the tracked channel was released."""
        data = api(f"/v2/find?name={urllib.parse.quote(result.name)}")
        found = data.get("result")
        if not isinstance(found, list) or not found:
            return
        channels = found[0].get("channels") or {}
        channel = found[0].get("channel") or "stable"
        info = channels.get(channel if "/" in channel else f"latest/{channel}") or {}
        result.released = parse_date(info.get("released-at", ""))

    def install(self, result: SearchResult) -> list[Command]:
        argv = ["snap", "install", result.name]
        if result.extra.get("classic"):
            argv.append("--classic")
        return [Command(f"Install {result.display_name} (Snap)", host.privileged(argv))]

    def install_file(self, host_path: str) -> list[Command]:
        return [Command("Install local snap (unsigned)",
                        host.privileged(["snap", "install", "--dangerous", host_path]))]

    def uninstall_many(self, pkgs: list[Package], delete_data: bool) -> list[Command]:
        guard_protected(pkgs)
        argv = ["snap", "remove", *(["--purge"] if delete_data else []), *[p.name for p in pkgs]]
        cmds = [Command(f"Remove {', '.join(p.display_name for p in pkgs)}", host.privileged(argv))]
        if delete_data:
            for pkg in pkgs:
                cmds.append(Command(f"Delete user data of {pkg.display_name} in ~/snap",
                                    ["sh", "-c", f'rm -rf -- "$HOME/snap/{pkg.name}"'],
                                    allow_failure=True))
        return cmds

    def data_locations(self, pkg: Package) -> list[str]:
        return [f"~/snap/{pkg.name}", f"/var/snap/{pkg.name}"]

    # Updates ------------------------------------------------------------------------
    def list_updates(self) -> dict[str, str]:
        data = api("/v2/find?select=refresh")
        result = data.get("result")
        if not isinstance(result, list):
            return {}
        return {f"snap::{s['name']}": s.get("version", "") for s in result}

    def update(self, pkg: Package) -> list[Command]:
        return [Command(f"Refresh {pkg.display_name}", host.privileged(["snap", "refresh", pkg.name]),
                        failure_hint=running_apps_hint)]

    def update_all(self) -> list[Command]:
        return [Command("Refresh all snaps", host.privileged(["snap", "refresh"]),
                        failure_hint=running_apps_hint)]

    def update_many(self, pkgs: list[Package]) -> list[Command]:
        names = [p.name for p in pkgs]
        return [Command(f"Refresh {', '.join(names)}", host.privileged(["snap", "refresh", *names]),
                        failure_hint=running_apps_hint)]

    # Verification -------------------------------------------------------------------
    def verify(self, pkg: Package, deep: bool = False) -> Verification:
        checks: list[VerificationCheck] = []
        rev = pkg.extra.get("revision", "")
        if rev.startswith("x"):
            checks.append(VerificationCheck(
                "Installed from the Snap Store", False,
                "Installed with --dangerous from a local file: no store signature"))
            pkg.trust = Trust.UNVERIFIED
            return Verification(Trust.UNVERIFIED, "Local snap without store assertions", checks)
        snap_id = pkg.extra.get("id", "")
        text = api_raw(f"/v2/assertions/snap-revision?snap-id={snap_id}&snap-revision={rev}")
        revs = [a for a in parse_assertions(text) if a.get("snap-revision") == rev]
        decl_text = api_raw(f"/v2/assertions/snap-declaration?snap-id={snap_id}")
        decls = parse_assertions(decl_text)
        if revs:
            a = revs[0]
            checks.append(VerificationCheck(
                "Signed snap-revision assertion", True,
                f"Signed by {a.get('authority-id')} (key {a.get('sign-key-sha3-384', '')[:16]}…)\n"
                f"Expected SHA3-384: {a.get('snap-sha3-384')}"))
        else:
            checks.append(VerificationCheck("Signed snap-revision assertion", False, "No assertion found"))
        if decls:
            d = decls[0]
            checks.append(VerificationCheck(
                "Snap declaration", True,
                f"Name “{d.get('snap-name')}” registered to publisher id {d.get('publisher-id')}"))
        validation = pkg.extra.get("validation", "")
        checks.append(VerificationCheck(
            "Publisher verified by the Snap Store", True if validation in ("verified", "starred") else None,
            f"{pkg.developer} — validation: {validation or 'unknown'}"))
        tampered = False
        if deep and revs and pkg.extra.get("mounted_from"):
            digest = _sha3_384_host_file(pkg.extra["mounted_from"])
            if not digest:
                # Snap files are only readable by root.
                digest = _sha3_384_host_file(pkg.extra["mounted_from"], privileged=True)
            expected = revs[0].get("snap-sha3-384", "")
            ok: Optional[bool] = (digest == expected) if digest else None
            tampered = ok is False
            checks.append(VerificationCheck(
                "Snap file matches signed checksum", ok,
                f"{pkg.extra['mounted_from']}\nComputed SHA3-384: {digest or 'unreadable'}"))
        if pkg.extra.get("classic"):
            checks.append(VerificationCheck(
                "Confinement", None, "Classic confinement: the snap has full access to your system"))
        if tampered:
            trust, summary = Trust.WARNING, "Installed snap does not match its signed checksum!"
        elif not revs:
            trust, summary = Trust.UNVERIFIED, "No store signature found"
        elif validation in ("verified", "starred"):
            trust, summary = Trust.VERIFIED, "Signed by the Snap Store, verified publisher"
        else:
            trust, summary = Trust.SIGNED, "Signed by the Snap Store, publisher not verified"
        pkg.trust = trust
        return Verification(trust, summary, checks)

    def verify_search_result(self, result: SearchResult) -> Verification:
        validation = result.extra.get("validation", "")
        checks = [
            VerificationCheck("Distributed through the Snap Store", True,
                              "Every revision is signed by Canonical's store key"),
            VerificationCheck("Publisher verified", True if validation in ("verified", "starred") else None,
                              f"{result.developer}: {validation or 'unknown'}"),
        ]
        return Verification(result.trust, result.trust.label, checks)

    # Repositories ---------------------------------------------------------------------
    def list_repositories(self) -> list[Repository]:
        store = "Snap Store"
        return [Repository(
            kind=PackageKind.SNAP, id="snapstore", name=store, url="https://snapcraft.io",
            details="Snaps are always installed from the store configured in snapd; "
                    "channels are chosen per snap.",
            signed_by="Canonical store root key (built into snapd)", trust=Trust.SIGNED,
            removable=False, toggleable=False,
        )]


def running_apps_hint(output: str) -> Optional[str]:
    """Explain snapd's refusal to refresh snaps that are running."""
    names = re.findall(r'snap "([^"]+)" has running apps', output)
    if not names and "has running apps" not in output:
        return None
    which = ", ".join(names) if names else "The app"
    return (f"{which} is still running. Snaps cannot be updated while they run — close the app "
            "and try again (the update will also be applied automatically after it is closed).")


def unconfined_snap_permissions(pkg: Package) -> list[Permission]:
    """Read-only entries describing a snap that runs without a sandbox.

    Classic snaps declare no interfaces at all (snapd enforces nothing for
    them); devmode snaps declare interfaces but violations are only logged.
    """
    if pkg.extra.get("devmode"):
        return [Permission(
            "confinement:devmode", "Sandbox not enforced (devmode)", C.SYSTEM, True,
            toggleable=False, sensitive=True,
            description="Developer mode: the interfaces below are not enforced, the app can "
                        "access the whole system. Reinstall it without --devmode to sandbox it.")]
    if pkg.extra.get("classic"):
        return [
            Permission("confinement:classic", "Full system access (classic confinement)", C.SYSTEM,
                       True, toggleable=False, sensitive=True,
                       description="Classic snaps run without a sandbox, like traditional packages. "
                                   "They can access all your files, devices and the network."),
            Permission("confinement:files", "All your files", C.FILESYSTEM, True, toggleable=False,
                       sensitive=True, description="Not restricted by snapd"),
            Permission("confinement:network", "Network access", C.NETWORK, True, toggleable=False,
                       sensitive=True, description="Not restricted by snapd"),
            Permission("confinement:devices", "All devices (camera, microphone, USB…)", C.DEVICES,
                       True, toggleable=False, sensitive=True, description="Not restricted by snapd"),
        ]
    return []


def _sha3_384_host_file(path: str, privileged: bool = False) -> str:
    """Stream *path* from the host and compute the URL-safe base64 SHA3-384."""
    h = hashlib.sha3_384()
    argv = ["cat", path]
    if privileged:
        argv = host.privileged(argv)
    try:
        proc = subprocess.Popen(host.host_argv(argv), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(1 << 20)
        if not chunk:
            break
        h.update(chunk)
    if proc.wait() != 0:
        return ""
    return base64.urlsafe_b64encode(h.digest()).decode().rstrip("=")
