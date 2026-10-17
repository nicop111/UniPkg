"""Inspection of OpenPGP signing keys (repository keyrings)."""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field
from typing import Optional

from .. import host


@dataclass
class KeyInfo:
    fingerprint: str
    uids: list[str] = field(default_factory=list)
    algo_bits: str = ""
    created: Optional[int] = None
    expires: Optional[int] = None

    @property
    def expired(self) -> bool:
        return bool(self.expires and self.expires < time.time())

    def describe(self) -> str:
        fpr = " ".join(self.fingerprint[i:i + 4] for i in range(0, len(self.fingerprint), 4))
        who = self.uids[0] if self.uids else "(no user id)"
        parts = [who, fpr]
        if self.expires:
            date = _dt.date.fromtimestamp(self.expires).isoformat()
            parts.append(("EXPIRED " if self.expired else "expires ") + date)
        return "\n".join(parts)


def parse_colons(text: str) -> list[KeyInfo]:
    keys: list[KeyInfo] = []
    current: Optional[KeyInfo] = None
    expect_fpr = False
    for line in text.splitlines():
        cols = line.split(":")
        rec = cols[0]
        if rec == "pub":
            current = KeyInfo(fingerprint="", algo_bits=f"{cols[2]} bit")
            current.created = int(cols[5]) if cols[5].isdigit() else None
            current.expires = int(cols[6]) if len(cols) > 6 and cols[6].isdigit() else None
            keys.append(current)
            expect_fpr = True
        elif rec == "sub":
            expect_fpr = False
        elif rec == "fpr" and current is not None and expect_fpr:
            current.fingerprint = cols[9]
            expect_fpr = False
        elif rec == "uid" and current is not None:
            current.uids.append(cols[9].replace("\\x3a", ":"))
    return keys


def _show(argv: list[str], data: Optional[bytes] = None) -> list[KeyInfo]:
    res = host.run(
        ["gpg", "--batch", "--no-default-keyring", "--keyring", "/dev/null",
         "--with-colons", "--show-keys", *argv],
        input=data, timeout=30,
    )
    return parse_colons(res.stdout)


def keyring_keys(path: str) -> list[KeyInfo]:
    return _show([path])


def key_data_keys(data: str | bytes) -> list[KeyInfo]:
    if isinstance(data, str):
        data = data.encode()
    return _show([], data)


def describe_keyring(path: str) -> list[str]:
    return [k.describe() for k in keyring_keys(path)]
