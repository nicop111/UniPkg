"""Small helpers shared by backends and UI."""

from __future__ import annotations

import datetime as _dt
import re
import time
from email.utils import parsedate_to_datetime
from typing import Optional

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


def parse_date(text: str) -> Optional[float]:
    """Parse ISO 8601 (snapd), ``2026-10-09 17:39:24 +0000`` (flatpak) and
    RFC 2822 (Debian changelog) dates to epoch seconds."""
    text = (text or "").strip()
    if not text:
        return None
    iso = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$", text)
    if iso:
        frac = (iso.group(3) or "")[:7]  # Python accepts at most microseconds
        tz = iso.group(4) or "+00:00"
        tz = "+00:00" if tz == "Z" else (tz if ":" in tz else f"{tz[:3]}:{tz[3:]}")
        try:
            return _dt.datetime.fromisoformat(f"{iso.group(1)}T{iso.group(2)}{frac}{tz}").timestamp()
        except ValueError:
            return None
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


def format_date(epoch: Optional[float], now: Optional[float] = None) -> str:
    """Short, human friendly date: "Today", "Yesterday", "3 days ago" or "12 Oct 2026"."""
    if not epoch:
        return "—"
    now = now or time.time()
    day = _dt.date.fromtimestamp(epoch)
    today = _dt.date.fromtimestamp(now)
    delta = (today - day).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if 1 < delta < 7:
        return f"{delta} days ago"
    return f"{day.day} {day.strftime('%b %Y')}"


def format_datetime(epoch: Optional[float]) -> str:
    if not epoch:
        return "Unknown"
    return _dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
