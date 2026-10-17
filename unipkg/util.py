"""Small helpers shared by backends and UI."""

from __future__ import annotations

import re

_UNITS = {
    "b": 1,
    "bytes": 1,
    "kb": 1000,
    "kib": 1024,
    "k": 1000,
    "mb": 1000**2,
    "mib": 1024**2,
    "m": 1000**2,
    "gb": 1000**3,
    "gib": 1024**3,
    "g": 1000**3,
    "tb": 1000**4,
}


def parse_size(text: str) -> int:
    """Parse sizes such as ``"1.4 MB"``, ``"423,6 kB"`` or ``"12G"`` to bytes."""
    if not text:
        return 0
    m = re.match(r"\s*([\d.,]+)\s*([A-Za-z]*)", text.replace("\xa0", " "))
    if not m:
        return 0
    number = m.group(1).replace(",", ".")
    if number.count(".") > 1:
        number = number.replace(".", "", number.count(".") - 1)
    try:
        value = float(number)
    except ValueError:
        return 0
    unit = m.group(2).lower() or "b"
    return int(value * _UNITS.get(unit, 1))


def format_size(size: int) -> str:
    if not size:
        return "—"
    value = float(size)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1000
    return f"{size} B"


def parse_keyfile(text: str) -> dict[str, dict[str, str]]:
    """Parse a GLib key file (flatpak metadata / overrides)."""
    groups: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = groups.setdefault(line[1:-1], {})
            continue
        if current is not None and "=" in line:
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip()
    return groups


def split_list(value: str) -> list[str]:
    return [v for v in value.split(";") if v]


def parse_deb822(text: str) -> list[dict[str, str]]:
    """Parse deb822 formatted stanzas (apt .sources, dpkg status, apt-cache show)."""
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    last_key = None
    for raw in text.splitlines():
        if not raw.strip():
            if current:
                stanzas.append(current)
            current, last_key = {}, None
            continue
        if raw.startswith("#"):
            continue
        if raw[0] in " \t" and last_key:
            current[last_key] += "\n" + raw.strip()
            continue
        if ":" in raw:
            key, value = raw.split(":", 1)
            last_key = key.strip()
            current[last_key] = value.strip()
    if current:
        stanzas.append(current)
    return stanzas
