"""Backend independent data model."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class PackageKind(str, enum.Enum):
    APT = "apt"
    DEB = "deb"  # manually installed .deb file not available in any repository
    FLATPAK = "flatpak"
    SNAP = "snap"

    @property
    def label(self) -> str:
        return {
            PackageKind.APT: "APT",
            PackageKind.DEB: "Local .deb",
            PackageKind.FLATPAK: "Flatpak",
            PackageKind.SNAP: "Snap",
        }[self]

    @property
    def css_class(self) -> str:
        return f"kind-{self.value}"


class PermissionCategory(str, enum.Enum):
    NETWORK = "network"
    NOTIFICATIONS = "notifications"
    FILESYSTEM = "filesystem"
    DEVICES = "devices"
    AUDIO = "audio"
    CAMERA = "camera"
    DISPLAY = "display"
    BUS = "bus"
    BACKGROUND = "background"
    LOCATION = "location"
    SYSTEM = "system"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            "network": "Network",
            "notifications": "Notifications",
            "filesystem": "Files",
            "devices": "Devices",
            "audio": "Audio",
            "camera": "Camera",
            "display": "Display",
            "bus": "System services",
            "background": "Background",
            "location": "Location",
            "system": "System",
            "other": "Other",
        }[self.value]

    @property
    def icon(self) -> str:
        return {
            "network": "network-wireless-symbolic",
            "notifications": "preferences-system-notifications-symbolic",
            "filesystem": "folder-symbolic",
            "devices": "drive-harddisk-usb-symbolic",
            "audio": "audio-speakers-symbolic",
            "camera": "camera-web-symbolic",
            "display": "video-display-symbolic",
            "bus": "system-run-symbolic",
            "background": "preferences-system-time-symbolic",
            "location": "find-location-symbolic",
            "system": "dialog-warning-symbolic",
            "other": "applications-system-symbolic",
        }[self.value]


@dataclass
class Permission:
    key: str  # backend specific identifier, e.g. "shared:network"
    label: str
    category: PermissionCategory
    granted: bool
    toggleable: bool = True
    description: str = ""
    sensitive: bool = False  # considered privacy/security relevant
    overridden: bool = False  # changed from the package default by the user


class Trust(str, enum.Enum):
    VERIFIED = "verified"  # signed source + verified publisher
    SIGNED = "signed"  # cryptographically signed source, publisher not verified
    UNVERIFIED = "unverified"  # no signature could be established
    WARNING = "warning"  # something is actively wrong (e.g. trusted=yes, tampering)
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            "verified": "Verified",
            "signed": "Signed",
            "unverified": "Unverified",
            "warning": "Warning",
            "unknown": "Unknown",
        }[self.value]

    @property
    def icon(self) -> str:
        return {
            "verified": "security-high-symbolic",
            "signed": "security-medium-symbolic",
            "unverified": "security-low-symbolic",
            "warning": "dialog-warning-symbolic",
            "unknown": "dialog-question-symbolic",
        }[self.value]


@dataclass
class VerificationCheck:
    title: str
    passed: Optional[bool]  # None = informational
    detail: str = ""


@dataclass
class Verification:
    trust: Trust
    summary: str
    checks: list[VerificationCheck] = field(default_factory=list)


@dataclass
class Package:
    kind: PackageKind
    name: str  # technical identifier (package name / app id / snap name)
    display_name: str
    version: str = ""
    summary: str = ""
    developer: str = ""
    source: str = ""
    size: int = 0  # installed size in bytes
    download_size: int = 0
    is_app: bool = False
    auto_installed: bool = False
    installed: bool = True
    icon_name: Optional[str] = None
    icon_path: Optional[str] = None
    update_version: Optional[str] = None
    permissions: list[Permission] = field(default_factory=list)
    confinement: str = ""  # human readable sandbox status
    trust: Trust = Trust.UNKNOWN
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        scope = self.extra.get("installation", "")
        return f"{self.kind.value}:{scope}:{self.name}"

    @property
    def granted_permissions(self) -> list[Permission]:
        return [p for p in self.permissions if p.granted]

    @property
    def permission_categories(self) -> set[PermissionCategory]:
        return {p.category for p in self.granted_permissions}

    @property
    def risk_score(self) -> int:
        """Rough measure of how much access the package has (used for sorting)."""
        if self.kind in (PackageKind.APT, PackageKind.DEB):
            return 1000
        if self.extra.get("classic"):
            return 1000
        score = 0
        for perm in self.granted_permissions:
            score += 10 if perm.sensitive else 1
        return score

    @property
    def permission_summary(self) -> str:
        if self.kind in (PackageKind.APT, PackageKind.DEB) or self.extra.get("classic"):
            return "Unrestricted"
        sensitive = [p for p in self.granted_permissions if p.sensitive]
        if not self.granted_permissions:
            return "Sandboxed, no extra access"
        cats = sorted({p.category.label for p in sensitive})
        if not cats:
            return "Sandboxed"
        return ", ".join(cats)


@dataclass
class Repository:
    kind: PackageKind
    id: str
    name: str
    url: str
    enabled: bool = True
    scope: str = ""  # e.g. "system"/"user" for flatpak, file path for apt
    details: str = ""
    signed_by: str = ""
    key_info: list[str] = field(default_factory=list)
    trust: Trust = Trust.UNKNOWN
    removable: bool = True
    toggleable: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    kind: PackageKind
    name: str
    display_name: str
    version: str = ""
    summary: str = ""
    developer: str = ""
    source: str = ""
    download_size: int = 0
    installed_size: int = 0
    trust: Trust = Trust.UNKNOWN
    confinement: str = ""
    installed: bool = False
    icon_name: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def group_key(self) -> str:
        return normalize_name(self.display_name or self.name)


def normalize_name(name: str) -> str:
    import re

    name = name.lower().strip()
    if name.count(".") >= 2 and " " not in name:
        name = name.rsplit(".", 1)[-1]  # reverse DNS app id
    name = re.sub(r"[-_ ](desktop|app)$", "", name)
    return re.sub(r"[^a-z0-9]", "", name)
