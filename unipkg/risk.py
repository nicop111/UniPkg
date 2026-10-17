"""Security risk assessment of installed applications.

Each finding adds points; the sum decides the risk level. The weights are a
heuristic meant to rank apps against each other, not an absolute measure.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Optional

from .models import Package, PackageKind, Trust

YEAR = 365 * 86400


class RiskLevel(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"

    @property
    def label(self) -> str:
        return {"high": "High risk", "medium": "Medium risk", "low": "Low risk", "none": "No findings"}[self.value]

    @property
    def short(self) -> str:
        return {"high": "High", "medium": "Medium", "low": "Low", "none": "None"}[self.value]


@dataclass
class RiskFactor:
    title: str
    points: int
    explanation: str
    recommendation: str = ""


@dataclass
class RiskAssessment:
    pkg: Package
    factors: list[RiskFactor] = field(default_factory=list)

    @property
    def score(self) -> int:
        return sum(f.points for f in self.factors)

    @property
    def level(self) -> RiskLevel:
        return level_for(self.score)

    @property
    def summary(self) -> str:
        top = sorted(self.factors, key=lambda f: -f.points)
        return " · ".join(f.title for f in top[:3]) + (f" · +{len(top) - 3} more" if len(top) > 3 else "")


def level_for(score: int) -> RiskLevel:
    if score >= 50:
        return RiskLevel.HIGH
    if score >= 20:
        return RiskLevel.MEDIUM
    if score > 0:
        return RiskLevel.LOW
    return RiskLevel.NONE


# Sandbox permissions that weaken the sandbox: key -> (points, title, explanation)
_PERMISSION_RISKS: dict[str, tuple[int, str, str]] = {
    "sockets:session-bus": (25, "Can escape the sandbox (session bus)",
                            "Full access to the session bus lets the app control other programs and "
                            "start commands outside the sandbox."),
    "sockets:system-bus": (20, "Full system bus access",
                           "The app can talk to every system service."),
    "session:org.freedesktop.Flatpak": (25, "Can escape the sandbox (Flatpak service)",
                                        "Talking to org.freedesktop.Flatpak allows running commands "
                                        "on the host, outside the sandbox."),
    "filesystems:host": (15, "Access to all files",
                         "The app can read and modify every file on the computer."),
    "filesystems:home": (12, "Access to your home folder",
                         "The app can read and modify all your personal files, including "
                         "configuration of other apps."),
    "home": (10, "Access to your home folder",
             "The snap can read and modify the non-hidden files in your home folder."),
    "devices:all": (8, "Access to all devices",
                    "Webcams, microphones, USB devices and other hardware are directly accessible."),
    "sockets:x11": (5, "Uses X11",
                    "X11 apps can read keystrokes and screen content of other X11 windows."),
    "x11": (5, "Uses X11", "X11 apps can read keystrokes and screen content of other X11 windows."),
    "unity7": (5, "Legacy X11 desktop access",
               "Grants X11 access that can observe other windows."),
    "sockets:ssh-auth": (6, "Can use your SSH keys", "The app can authenticate with your SSH agent."),
    "ssh-keys": (8, "Can read your SSH keys", "The snap can read your private SSH keys."),
    "sockets:gpg-agent": (6, "Can use your GPG keys", "The app can use your GPG agent."),
    "gpg-keys": (8, "Can read your GPG keys", "The snap can read your GPG keyring."),
    "features:devel": (5, "Debugging permissions", "The app may trace (ptrace) other processes."),
    "system-files": (8, "Access to system files", "The snap may access selected system files."),
    "process-control": (6, "Can control processes", "The snap can signal and re-prioritise processes."),
    "network-control": (6, "Full network control", "The snap can reconfigure networking."),
}


DISTRIBUTION_ORIGINS = ("Ubuntu", "Debian", "Linux Mint", "elementary", "Pop!_OS", "Kali")


def is_distribution_package(pkg: Package) -> bool:
    """Whether an APT package comes from the distribution's own archive."""
    source = pkg.source
    return source.startswith(DISTRIBUTION_ORIGINS) or source == "Repository (superseded version)"


def assess(pkg: Package, *, security_update: bool = False, now: Optional[float] = None) -> RiskAssessment:
    now = now or time.time()
    result = RiskAssessment(pkg)
    add = result.factors.append
    alternative = "Look for a sandboxed Flatpak or Snap version of this app."

    # --- sandboxing ------------------------------------------------------
    if pkg.kind == PackageKind.APT and is_distribution_package(pkg):
        add(RiskFactor("Not sandboxed", 15,
                       "This package comes from your distribution's signed archive and receives its "
                       "security updates, but it runs with all of your permissions: it can read "
                       "every file and use all devices and the network.", alternative))
    elif pkg.kind in (PackageKind.APT, PackageKind.DEB):
        add(RiskFactor("Not sandboxed, third-party package", 30,
                       "This package does not come from your distribution. It runs with all of your "
                       "permissions (every file, all devices, the network) and its installation "
                       "scripts ran as root, so you fully depend on trusting its vendor.",
                       alternative))
    elif pkg.extra.get("devmode"):
        add(RiskFactor("Sandbox not enforced (devmode)", 30,
                       "This snap was installed in developer mode: its confinement is not enforced.",
                       "Reinstall it without --devmode, or use another version."))
    elif pkg.extra.get("classic"):
        add(RiskFactor("Not sandboxed (classic snap)", 30,
                       "Classic snaps run without confinement and have full access to your system.",
                       alternative))

    # --- origin / signatures ----------------------------------------------
    if pkg.kind == PackageKind.DEB:
        add(RiskFactor("Unverified origin", 25,
                       "This package was installed from a downloaded .deb file that no configured "
                       "repository provides, so there is no signature proving where it came from.",
                       "Prefer software from a signed repository, Flathub or the Snap Store."))
        add(RiskFactor("No automatic updates", 15,
                       "Packages installed from files are not updated by the system, so security "
                       "fixes are missed unless you install new versions by hand.",
                       "Add the vendor's signed repository or switch to a Flatpak/Snap."))
    elif pkg.kind == PackageKind.SNAP and pkg.extra.get("revision", "").startswith("x"):
        add(RiskFactor("Unverified origin", 25,
                       "This snap was installed from a local file with --dangerous; it is not signed "
                       "by the Snap Store and does not receive updates.",
                       "Install it from the Snap Store instead."))
    elif pkg.trust == Trust.WARNING:
        add(RiskFactor("Verification problem", 30,
                       "A previous verification of this package found a problem (e.g. modified "
                       "files or a repository without signature checks).",
                       "Open the details and run a deep check."))
    elif pkg.kind in (PackageKind.FLATPAK, PackageKind.SNAP) and pkg.trust == Trust.SIGNED:
        add(RiskFactor("Publisher not verified", 5,
                       "The package is signed by its store, but the store has not verified that "
                       "the publisher is the original developer of the app.",
                       "Check that the publisher is who you expect."))

    # --- updates -------------------------------------------------------------
    if pkg.update_version:
        if security_update:
            add(RiskFactor("Security update pending", 20,
                           f"Version {pkg.update_version} fixes security issues and is waiting to be "
                           "installed.", "Update this app on the Updates page."))
        else:
            add(RiskFactor("Update pending", 5,
                           f"Version {pkg.update_version} is available; updates often include "
                           "security fixes.", "Update this app."))
    if (pkg.kind in (PackageKind.DEB, PackageKind.FLATPAK, PackageKind.SNAP)
            and pkg.updated and now - pkg.updated > YEAR):
        years = (now - pkg.updated) / YEAR
        add(RiskFactor("Not updated for over a year", 10,
                       f"The installed version is {years:.1f} years old; the app may be abandoned "
                       "and miss security fixes.", "Check whether the app is still maintained."))

    # --- weakened sandbox ------------------------------------------------------
    if pkg.kind in (PackageKind.FLATPAK, PackageKind.SNAP) and not pkg.extra.get("classic"):
        seen_titles = set()
        for perm in pkg.permissions:
            if not perm.granted:
                continue
            risk = _PERMISSION_RISKS.get(perm.key)
            if risk and risk[1] not in seen_titles:
                seen_titles.add(risk[1])
                add(RiskFactor(risk[1], risk[0], risk[2],
                               "Revoke this permission in the app's details if it is not needed."))
    return result


def assess_all(packages: list[Package], security_updates: set[str]) -> list[RiskAssessment]:
    now = time.time()
    results = [assess(p, security_update=p.uid in security_updates, now=now)
               for p in packages if p.is_app]
    results.sort(key=lambda r: (-r.score, r.pkg.display_name.lower()))
    return results
