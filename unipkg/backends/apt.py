"""APT / dpkg backend.

Handles packages from APT repositories as well as manually installed ``.deb``
files (packages whose installed version is not available from any configured
repository – ``apt`` reports them as ``[installed,local]``).
"""

from __future__ import annotations

import os
import re
import shlex
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
from ..util import parse_deb822, parse_size
from .base import Backend, BackendError, Command
from .gpg import KeyInfo, key_data_keys, keyring_keys

APT_ENV = {"DEBIAN_FRONTEND": "noninteractive"}
APT_OPTS = ["-y", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]
DEFAULT_KEYRINGS = ["/etc/apt/trusted.gpg", "/etc/apt/trusted.gpg.d/*.gpg", "/etc/apt/trusted.gpg.d/*.asc"]

DPKG_FORMAT = (
    "${db:Status-Abbrev}\t${Package}\t${Version}\t${Architecture}\t${Installed-Size}\t"
    "${Maintainer}\t${binary:Summary}\t${Section}\t${Homepage}\n"
)


def unconfined_permissions(local: bool) -> list[Permission]:
    perms = [
        Permission("system:unconfined", "Full system access", C.SYSTEM, True, toggleable=False,
                   sensitive=True,
                   description="Traditional packages are not sandboxed: the software can access "
                               "all your files, devices and the network."),
        Permission("system:root-scripts", "Ran installation scripts as root", C.SYSTEM, True,
                   toggleable=False, sensitive=True,
                   description="Maintainer scripts were executed with administrator rights."),
    ]
    if local:
        perms.append(Permission(
            "system:no-updates", "No automatic security updates", C.SYSTEM, True, toggleable=False,
            sensitive=True, description="Installed from a file, not from a repository."))
    return perms


# ---------------------------------------------------------------------------
# sources.list parsing
# ---------------------------------------------------------------------------

@dataclass
class SourceEntry:
    file: str
    index: int  # stanza index (deb822) or line index (one-line format)
    fmt: str  # "deb822" or "list"
    types: list[str]
    uris: list[str]
    suites: list[str]
    components: list[str]
    enabled: bool
    options: dict[str, str] = field(default_factory=dict)

    @property
    def signed_by(self) -> str:
        return self.options.get("Signed-By") or self.options.get("signed-by", "")

    @property
    def trusted_yes(self) -> bool:
        return (self.options.get("Trusted") or self.options.get("trusted", "")).lower() == "yes"


def parse_one_line_sources(path: str, text: str) -> list[SourceEntry]:
    entries = []
    for idx, raw in enumerate(text.splitlines()):
        line = raw.strip()
        enabled = True
        if line.startswith("#"):
            line = line.lstrip("#").strip()
            enabled = False
        m = re.match(r"^(deb|deb-src)\s+(\[[^\]]*\]\s+)?(\S+)\s+(\S+)\s*(.*)$", line)
        if not m:
            continue
        options: dict[str, str] = {}
        if m.group(2):
            for opt in m.group(2).strip()[1:-1].split():
                if "=" in opt:
                    k, v = opt.split("=", 1)
                    options[k] = v
        entries.append(SourceEntry(
            file=path, index=idx, fmt="list", types=[m.group(1)], uris=[m.group(3)],
            suites=[m.group(4)], components=m.group(5).split(), enabled=enabled, options=options,
        ))
    return entries


def parse_deb822_sources(path: str, text: str) -> list[SourceEntry]:
    entries = []
    for idx, stanza in enumerate(_deb822_stanzas_keep_index(text)):
        if "URIs" not in stanza:
            continue
        options = {k: v for k, v in stanza.items() if k not in ("Types", "URIs", "Suites", "Components")}
        entries.append(SourceEntry(
            file=path, index=idx, fmt="deb822", types=stanza.get("Types", "deb").split(),
            uris=stanza["URIs"].split(), suites=stanza.get("Suites", "").split(),
            components=stanza.get("Components", "").split(),
            enabled=stanza.get("Enabled", "yes").lower() != "no", options=options,
        ))
    return entries


def _deb822_stanzas_keep_index(text: str) -> list[dict[str, str]]:
    return [{k: v for k, v in s.items()} for s in parse_deb822(text)]


def _split_raw_stanzas(text: str) -> list[list[str]]:
    """Split a deb822 file into raw stanzas (keeping comments)."""
    stanzas: list[list[str]] = [[]]
    for line in text.splitlines():
        if not line.strip():
            if stanzas[-1]:
                stanzas.append([])
            continue
        stanzas[-1].append(line)
    return [s for s in stanzas if s]


def _stanza_has_fields(lines: list[str]) -> bool:
    return any(not l.startswith("#") and ":" in l and not l.startswith((" ", "\t")) for l in lines)


def set_entry_enabled(text: str, entry: SourceEntry, enabled: bool) -> str:
    if entry.fmt == "list":
        lines = text.splitlines()
        line = lines[entry.index]
        if enabled:
            lines[entry.index] = re.sub(r"^\s*#+\s*", "", line)
        elif not line.lstrip().startswith("#"):
            lines[entry.index] = "# " + line
        return "\n".join(lines) + "\n"
    stanzas = [s for s in _split_raw_stanzas(text)]
    real = [s for s in stanzas if _stanza_has_fields(s)]
    target = real[entry.index]
    target[:] = [l for l in target if not l.lower().startswith("enabled:")]
    if not enabled:
        target.append("Enabled: no")
    return "\n\n".join("\n".join(s) for s in stanzas) + "\n"


def remove_entry(text: str, entry: SourceEntry) -> str:
    if entry.fmt == "list":
        lines = text.splitlines()
        del lines[entry.index]
        return "\n".join(lines) + "\n"
    stanzas = _split_raw_stanzas(text)
    real = [s for s in stanzas if _stanza_has_fields(s)]
    stanzas = [s for s in stanzas if s is not real[entry.index]]
    return "\n\n".join("\n".join(s) for s in stanzas) + "\n"


def inline_key(signed_by: str) -> Optional[str]:
    """Return the armored key if Signed-By embeds the key itself."""
    if "BEGIN PGP PUBLIC KEY BLOCK" not in signed_by:
        return None
    lines = []
    for line in signed_by.splitlines():
        line = line.strip()
        lines.append("" if line == "." else line)
    return "\n".join(lines) + "\n"


def lists_prefix(uri: str, suite: str) -> str:
    """Prefix of the files apt stores in /var/lib/apt/lists for a source."""
    uri = re.sub(r"^[a-z+]+://", "", uri).rstrip("/")
    uri = re.sub(r"^[^@/]*@", "", uri)  # strip credentials
    if suite.endswith("/"):  # flat repository
        return (uri + "/" + suite.rstrip("/")).replace("/", "_").rstrip("_")
    return f"{uri}/dists/{suite}".replace("/", "_")


def parse_policy(text: str) -> dict[str, dict]:
    """Parse ``apt-cache policy pkg...`` output."""
    out: dict[str, dict] = {}
    current = None
    in_table = False
    version = None
    for line in text.splitlines():
        if line and not line.startswith(" ") and line.endswith(":"):
            current = {"installed": "", "candidate": "", "versions": {}}
            out[line[:-1]] = current
            in_table = False
            continue
        if current is None:
            continue
        s = line.strip()
        if s.startswith("Installed:"):
            current["installed"] = s.split(":", 1)[1].strip()
        elif s.startswith("Candidate:"):
            current["candidate"] = s.split(":", 1)[1].strip()
        elif s.startswith("Version table:"):
            in_table = True
        elif in_table:
            m = re.match(r"^ (?:\*\*\*|   ) (\S+) (-?\d+)(?: \(.*\))?$", line)
            if m:
                version = m.group(1)
                current["versions"].setdefault(version, [])
                continue
            m2 = re.match(r"^\s{6,}(-?\d+) (\S+)(?: (\S+))?", line)
            if m2 and version:
                current["versions"][version].append((m2.group(2), m2.group(3) or ""))
    return out


def parse_apt_list(text: str) -> dict[tuple[str, str], dict]:
    """Parse ``apt list --installed`` / ``--upgradable``."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([^/\s]+)/(\S+)\s+(\S+)\s+(\S+)\s+\[([^\]]*)\]", line)
        if not m:
            continue
        name, suites, version, arch, flags = m.groups()
        out[(name, arch)] = {
            "suites": [s for s in suites.split(",") if s != "now"],
            "version": version,
            "flags": [f.strip() for f in flags.split(",")],
            "raw_flags": flags,
        }
    return out


def parse_policy_origins(text: str) -> dict[str, str]:
    """Map ``uri suite`` and archive names to the origin label from ``apt-cache policy``."""
    origins: dict[str, str] = {}
    archive_origins: dict[str, set[str]] = {}
    last_key = None
    for line in text.splitlines():
        m = re.match(r"^\s*-?\d+\s+(\S+)\s+(\S+)", line)
        if m and "://" in m.group(1):
            suite = m.group(2).split("/")[0]
            last_key = f"{m.group(1).rstrip('/')} {suite}"
            continue
        m = re.match(r"^\s+release\s+(.*)$", line)
        if m and last_key:
            fields = dict(p.split("=", 1) for p in m.group(1).split(",") if "=" in p)
            origin = fields.get("o") or fields.get("l") or ""
            if origin:
                origins[last_key] = origin
                if fields.get("a"):
                    archive_origins.setdefault(fields["a"], set()).add(origin)
    for archive, names in archive_origins.items():
        if len(names) == 1:
            origins[f"archive:{archive}"] = next(iter(names))
    return origins


def clean_maintainer(maint: str) -> str:
    return re.sub(r"\s*<[^>]*>", "", maint).strip()


class AptBackend(Backend):
    name = "APT"
    kinds = (PackageKind.APT, PackageKind.DEB)

    def available(self) -> bool:
        return host.which("apt-get") and host.which("dpkg-query")

    # ------------------------------------------------------------------
    def list_installed(self) -> list[Package]:
        dpkg = host.run(["dpkg-query", "-W", "-f", DPKG_FORMAT], timeout=120)
        if not dpkg.ok:
            raise BackendError(dpkg.stderr.strip())
        apt_list = parse_apt_list(host.run(["apt", "list", "--installed"], timeout=120).stdout)
        origins = parse_policy_origins(host.run(["apt-cache", "policy"], timeout=60).stdout)
        desktop = self._desktop_entries()

        packages: list[Package] = []
        for line in dpkg.stdout.splitlines():
            cols = line.split("\t")
            if len(cols) < 9 or not cols[0].startswith("ii"):
                continue
            _status, name, version, arch, isize, maint, summary, section, homepage = cols[:9]
            info = apt_list.get((name, arch)) or apt_list.get((name, "all")) or {}
            flags = info.get("flags", [])
            local = "local" in flags
            suites = info.get("suites", [])
            if local:
                source = "Local file"
            elif suites:
                origin = origins.get(f"archive:{suites[0]}", "")
                source = f"{origin} ({suites[0]})" if origin else suites[0]
            elif "upgradable to" in info.get("raw_flags", ""):
                source = "Repository (superseded version)"
            else:
                source = "Unknown"
            entry = desktop.get(name)
            pkg = Package(
                kind=PackageKind.DEB if local else PackageKind.APT,
                name=name if arch in ("all", self.native_arch) else f"{name}:{arch}",
                display_name=(entry or {}).get("Name") or name,
                version=version, summary=summary, developer=clean_maintainer(maint),
                source=source, size=int(float(isize or 0) * 1024), is_app=bool(entry),
                auto_installed="automatic" in flags,
                icon_name=(entry or {}).get("Icon"),
                permissions=unconfined_permissions(local),
                confinement="Not sandboxed",
                trust=Trust.UNVERIFIED if local else Trust.SIGNED,
                extra={"installation": "", "arch": arch, "section": section, "homepage": homepage,
                       "suites": suites, "package": name},
            )
            packages.append(pkg)
        return packages

    @property
    def native_arch(self) -> str:
        if not hasattr(self, "_arch"):
            self._arch = host.run(["dpkg", "--print-architecture"], timeout=10).stdout.strip()
        return self._arch

    def _desktop_entries(self) -> dict[str, dict[str, str]]:
        """Map package name -> visible desktop entry (Name, Icon)."""
        script = r"""
dpkg -S /usr/share/applications/*.desktop 2>/dev/null | while IFS= read -r line; do
  pkgs=${line%%: *}; f=${line#*: }
  echo "@@F $pkgs $f"
  sed -n '/^\[Desktop Entry\]/,/^\[/p' "$f" | grep -E '^(Name|Icon|NoDisplay|Type)='
done
"""
        res = host.run_shell(script, timeout=120)
        entries: dict[str, dict[str, str]] = {}
        current_pkgs: list[str] = []
        current: dict[str, str] = {}

        def flush():
            if current_pkgs and current.get("NoDisplay", "false") != "true" and current.get("Type", "Application") == "Application":
                for p in current_pkgs:
                    entries.setdefault(p.split(":")[0], dict(current))

        for line in res.stdout.splitlines():
            if line.startswith("@@F "):
                flush()
                parts = line[4:].split(" ", 1)
                current_pkgs = [p.strip() for p in parts[0].split(",")]
                current = {}
            elif "=" in line:
                k, v = line.split("=", 1)
                current.setdefault(k, v)
        flush()
        return entries

    # Search / install ------------------------------------------------------------
    def search(self, query: str) -> list[SearchResult]:
        res = host.run(["apt-cache", "search", "--names-only", query], timeout=60)
        names = []
        q = query.lower()
        for line in res.stdout.splitlines():
            name = line.split(" - ", 1)[0].strip()
            if name:
                names.append(name)
        # Prefer close matches and skip noisy library/debug packages.
        names.sort(key=lambda n: (n != q, not n.startswith(q),
                                  n.startswith("lib") or n.endswith(("-dev", "-dbg", "-doc")), len(n)))
        names = names[:25]
        if not names:
            return []
        show = host.run(["apt-cache", "show", "--no-all-versions", *names], timeout=60)
        policy = parse_policy(host.run(["apt-cache", "policy", *names], timeout=60).stdout)
        origins = parse_policy_origins(host.run(["apt-cache", "policy"], timeout=60).stdout)
        results = []
        seen = set()
        for stanza in parse_deb822(show.stdout):
            name = stanza.get("Package", "")
            if not name or name in seen:
                continue
            seen.add(name)
            pol = policy.get(name, {})
            version = stanza.get("Version", "")
            sources = pol.get("versions", {}).get(version, [])
            source = ""
            for uri, suite in sources:
                if "://" in uri:
                    suite_name = suite.split("/")[0]
                    origin = origins.get(f"{uri.rstrip('/')} {suite_name}", "")
                    source = f"{origin} ({suite_name})" if origin else f"{uri} {suite_name}"
                    break
            installed = pol.get("installed", "(none)") not in ("(none)", "")
            results.append(SearchResult(
                kind=PackageKind.APT, name=name, display_name=name, version=version,
                summary=(stanza.get("Description") or stanza.get("Description-en", "")).split("\n")[0],
                developer=clean_maintainer(stanza.get("Original-Maintainer") or stanza.get("Maintainer", "")),
                source=source or "APT repository",
                download_size=int(stanza.get("Size", "0") or 0),
                installed_size=int(float(stanza.get("Installed-Size", "0") or 0) * 1024),
                trust=Trust.SIGNED, confinement="Not sandboxed (full system access)",
                installed=installed,
                extra={"sha256": stanza.get("SHA256", ""), "homepage": stanza.get("Homepage", ""),
                       "section": stanza.get("Section", ""), "depends": stanza.get("Depends", ""),
                       "sources": sources},
            ))
        return results

    def enrich(self, result: SearchResult) -> None:
        """Compute download/installed size including missing dependencies."""
        res = host.run(["apt-get", "install", "--print-uris", "-y", result.name], timeout=60)
        for line in res.stdout.splitlines():
            m = re.match(r"^Need to get ([\d.,]+ \w+)", line)
            if m:
                result.download_size = parse_size(m.group(1))
            m = re.match(r"^After this operation, ([\d.,]+ \w+) of additional", line)
            if m:
                result.installed_size = parse_size(m.group(1))
            m = re.match(r"^\d+ upgraded, (\d+) newly installed", line)
            if m:
                result.extra["new_packages"] = int(m.group(1))
        if result.extra.get("new_packages", 0) > 1:
            result.extra["permissions"] = [
                "Full system access",
                f"Installs {result.extra['new_packages']} packages incl. dependencies"]

    def install(self, result: SearchResult) -> list[Command]:
        return [Command(f"Install {result.name} (APT)",
                        host.privileged(["apt-get", "install", *APT_OPTS, result.name], APT_ENV))]

    def inspect_deb(self, host_path: str) -> dict:
        """Inspect a local .deb file and check it against the repositories."""
        fields = ["Package", "Version", "Architecture", "Maintainer", "Installed-Size", "Description",
                  "Depends", "Homepage"]
        res = host.run(["dpkg-deb", "-f", host_path, *fields], timeout=60)
        if not res.ok:
            raise BackendError(res.stderr.strip() or "Not a valid .deb file")
        info = parse_deb822(res.stdout)[0] if res.stdout.strip() else {}
        sha = host.run(["sha256sum", host_path], timeout=120).stdout.split(" ")[0]
        info["SHA256"] = sha
        checks: list[VerificationCheck] = [VerificationCheck("SHA-256 of the file", None, sha)]
        trust = Trust.UNVERIFIED
        repo = host.run(["apt-cache", "show", f"{info.get('Package')}={info.get('Version')}"], timeout=60)
        repo_stanzas = parse_deb822(repo.stdout) if repo.ok else []
        if repo_stanzas:
            match = any(s.get("SHA256") == sha for s in repo_stanzas)
            checks.append(VerificationCheck(
                "Identical to the package in a signed repository", match,
                "Checksum matches the signed repository index" if match else
                "A package with this name and version exists in a repository but the file differs!"))
            trust = Trust.SIGNED if match else Trust.WARNING
        else:
            checks.append(VerificationCheck(
                "Available from a configured repository", None,
                "This package is not in any configured repository; its origin cannot be verified "
                "and it will not receive automatic updates."))
        sig = host.run(["sh", "-c", f"command -v debsig-verify >/dev/null && debsig-verify {shlex.quote(host_path)}"],
                       timeout=60)
        if sig.returncode == 0:
            checks.append(VerificationCheck("Embedded package signature (debsig)", True, sig.stdout.strip()))
            trust = Trust.SIGNED if trust == Trust.UNVERIFIED else trust
        else:
            checks.append(VerificationCheck(
                "Embedded package signature (debsig)", None,
                "The file carries no verifiable embedded signature"))
        installed = host.run(["dpkg-query", "-W", "-f", "${Version}", info.get("Package", "")], timeout=20)
        info["installed_version"] = installed.stdout.strip() if installed.ok else ""
        info["verification"] = Verification(
            trust, {Trust.SIGNED: "Matches a signed repository package",
                    Trust.WARNING: "Differs from the signed repository package",
                    Trust.UNVERIFIED: "Unverified local package"}[trust], checks)
        return info

    def install_file(self, host_path: str) -> list[Command]:
        return [Command(f"Install {os.path.basename(host_path)}",
                        host.privileged(["apt-get", "install", *APT_OPTS, host_path], APT_ENV))]

    def simulate_removal(self, pkg: Package, purge: bool) -> list[str]:
        res = host.run(["apt-get", "-s", "purge" if purge else "remove", pkg.name], timeout=60)
        return [l.split()[1] for l in res.stdout.splitlines() if l.startswith(("Remv ", "Purg "))]

    def uninstall(self, pkg: Package, delete_data: bool) -> list[Command]:
        verb = "purge" if delete_data else "remove"
        script = f"apt-get {verb} {' '.join(shlex.quote(o) for o in APT_OPTS)} {shlex.quote(pkg.name)}"
        cmds = [Command(f"{verb.capitalize()} {pkg.name}",
                        host.privileged(["sh", "-c", script], APT_ENV))]
        if delete_data:
            for loc in self.data_locations(pkg):
                path = loc.replace("~", "$HOME", 1)
                cmds.append(Command(f"Delete {loc}", ["sh", "-c", f'rm -rf -- "{path}"'],
                                    allow_failure=True))
        return cmds

    def data_locations(self, pkg: Package) -> list[str]:
        base = pkg.extra.get("package", pkg.name)
        candidates = {base, pkg.display_name.lower().replace(" ", "-")}
        paths = []
        for name in sorted(candidates):
            for root in ("~/.config", "~/.local/share", "~/.cache"):
                paths.append(f"{root}/{name}")
            paths.append(f"~/.{name}")
        checks = " ".join(shlex.quote(p.replace("~/", "", 1)) for p in paths)
        res = host.run_shell(f'cd "$HOME" && for p in {checks}; do [ -e "$p" ] && echo "$p"; done',
                             timeout=20)
        return ["~/" + l for l in res.stdout.splitlines() if l]

    # Updates -------------------------------------------------------------------
    def refresh(self) -> list[Command]:
        return [Command("Refresh APT package lists", host.privileged(["apt-get", "update"], APT_ENV))]

    def list_updates(self) -> dict[str, str]:
        res = host.run(["apt", "list", "--upgradable"], timeout=120)
        updates = {}
        for (name, arch), info in parse_apt_list(res.stdout).items():
            key = name if arch in ("all", self.native_arch) else f"{name}:{arch}"
            updates[f"apt::{key}"] = info["version"]
        return updates

    def update(self, pkg: Package) -> list[Command]:
        return [Command(f"Upgrade {pkg.name}",
                        host.privileged(["apt-get", "install", "--only-upgrade", *APT_OPTS, pkg.name],
                                        APT_ENV))]

    def update_many(self, pkgs: list[Package]) -> list[Command]:
        return [Command(f"Upgrade {len(pkgs)} APT packages",
                        host.privileged(["apt-get", "install", "--only-upgrade", *APT_OPTS,
                                         *[p.name for p in pkgs]], APT_ENV))]

    def update_all(self) -> list[Command]:
        script = (f"apt-get update && apt-get upgrade --with-new-pkgs "
                  f"{' '.join(shlex.quote(o) for o in APT_OPTS)}")
        return [Command("Upgrade all APT packages", host.privileged(["sh", "-c", script], APT_ENV))]

    # Verification -----------------------------------------------------------------
    def verify(self, pkg: Package, deep: bool = False) -> Verification:
        checks: list[VerificationCheck] = []
        trust = Trust.SIGNED
        name = pkg.name
        parsed = parse_policy(host.run(["apt-cache", "policy", name], timeout=60).stdout)
        policy = parsed.get(name) or parsed.get(name.split(":")[0]) or {}
        sources = [(u, s) for u, s in policy.get("versions", {}).get(pkg.version, []) if "://" in u]
        candidate = policy.get("candidate", "")
        if not sources and candidate and candidate != pkg.version and pkg.kind == PackageKind.APT:
            checks.append(VerificationCheck(
                "Installed version available from a repository", None,
                f"The installed version is no longer published (superseded by {candidate}). "
                "Update the package to get a version verified by the repository signature."))
            cand_sources = [(u, s) for u, s in policy["versions"].get(candidate, []) if "://" in u]
            entries = self._source_entries()
            for uri, suite in cand_sources[:1]:
                checks.extend(self._verify_release(uri, suite.split("/")[0], _find_entry(entries, uri, suite.split("/")[0])))
        elif not sources:
            checks.append(VerificationCheck(
                "Installed version available from a repository", False,
                "Installed from a local .deb file — the origin cannot be verified "
                "and there is no repository signature."))
            trust = Trust.UNVERIFIED
        else:
            entries = self._source_entries()
            for uri, suite in sources[:2]:
                suite_name = suite.split("/")[0]
                entry = _find_entry(entries, uri, suite_name)
                check = self._verify_release(uri, suite_name, entry)
                checks.extend(check)
                if any(c.passed is False for c in check):
                    trust = Trust.WARNING
        # Integrity of installed files.
        if deep:
            res = host.run(["dpkg", "--verify", pkg.name], timeout=300)
            modified = []
            unreadable = 0
            for line in res.stdout.splitlines() + res.stderr.splitlines():
                m = re.match(r"^(\S{9})\s+(c?)\s*(/.*)$", line)
                if m:
                    if m.group(2) == "c":
                        continue  # modified configuration files are expected
                    modified.append(m.group(3))
                elif "Permission denied" in line or "cannot open" in line:
                    unreadable += 1
            detail = "All installed files match the package checksums (md5sums)"
            if modified:
                detail = "Modified files:\n" + "\n".join(modified[:20])
            elif unreadable:
                detail += f" ({unreadable} files could not be read without root)"
            checks.append(VerificationCheck("Installed files unchanged (dpkg --verify)", not modified, detail))
            if modified:
                trust = Trust.WARNING
        summary = {
            Trust.SIGNED: "Installed from a signed repository",
            Trust.UNVERIFIED: "Unverified: installed from a local file",
            Trust.WARNING: "Verification problems found",
        }.get(trust, trust.label)
        pkg.trust = trust
        return Verification(trust, summary, checks)

    def _verify_release(self, uri: str, suite: str, entry: Optional[SourceEntry]) -> list[VerificationCheck]:
        checks = []
        label = f"{uri} {suite}"
        if entry and entry.trusted_yes:
            checks.append(VerificationCheck(
                f"Repository signature ({label})", False,
                "The repository is marked trusted=yes — signatures are NOT checked!"))
            return checks
        prefix = lists_prefix(uri, suite)
        keyrings: list[str] = []
        key_desc: list[str] = []
        tmp_keyring = None
        if entry and entry.signed_by:
            armored = inline_key(entry.signed_by)
            if armored:
                tmp_keyring = f"/tmp/unipkg-key-{os.getpid()}.gpg"
                host.run(["sh", "-c", f"gpg --dearmor > {tmp_keyring}"], input=armored, timeout=30)
                keyrings = [tmp_keyring]
                key_desc = [k.describe() for k in key_data_keys(armored)]
            else:
                keyrings = [p.strip() for p in entry.signed_by.replace(",", " ").split()]
                for kr in keyrings:
                    key_desc += [k.describe() for k in keyring_keys(kr)]
        script = (
            'f=/var/lib/apt/lists/{p}_InRelease; [ -f "$f" ] || f=/var/lib/apt/lists/{p}_Release; '
            '[ -f "$f" ] || {{ echo NOFILE; exit 3; }}; '
            'args=""; for k in {k}; do [ -f "$k" ] && args="$args --keyring $k"; done; '
            'if [ "${{f%_InRelease}}" != "$f" ]; then gpgv $args "$f" 2>&1; '
            'else gpgv $args "$f.gpg" "$f" 2>&1; fi'
        ).format(p=shlex.quote(prefix), k=" ".join(shlex.quote(k) for k in (keyrings or DEFAULT_KEYRINGS)))
        res = host.run_shell(script, timeout=60)
        if tmp_keyring:
            host.run(["rm", "-f", tmp_keyring], timeout=10)
        if "NOFILE" in res.stdout:
            checks.append(VerificationCheck(f"Repository signature ({label})", None,
                                            "Release file not downloaded yet — refresh package lists"))
        else:
            good = res.ok and "Good signature" in res.stdout
            signer = re.findall(r'Good signature from "([^"]+)"', res.stdout)
            checks.append(VerificationCheck(
                f"Repository signature ({label})", good,
                ("Release file signature verified with gpgv" + (f": {signer[0]}" if signer else ""))
                if good else res.stdout.strip()[-400:]))
        if key_desc:
            checks.append(VerificationCheck("Repository signing key", None, "\n\n".join(key_desc)))
        elif entry and not entry.signed_by:
            checks.append(VerificationCheck(
                "Repository key scope", None,
                "No Signed-By: any key in the global APT keyring is accepted for this repository"))
        checks.append(VerificationCheck(
            "Package checksum", True,
            "APT verifies the package's SHA-256 against the signed repository index on download"))
        return checks

    # Repositories -------------------------------------------------------------------
    def _read_sources(self) -> dict[str, str]:
        script = ('for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list '
                  '/etc/apt/sources.list.d/*.sources; do [ -f "$f" ] && { echo "@@FILE $f"; cat "$f"; echo; }; done')
        res = host.run_shell(script, timeout=30)
        files: dict[str, list[str]] = {}
        current = None
        for line in res.stdout.splitlines():
            if line.startswith("@@FILE "):
                current = files.setdefault(line[7:].strip(), [])
            elif current is not None:
                current.append(line)
        return {k: "\n".join(v) for k, v in files.items()}

    def _source_entries(self) -> list[SourceEntry]:
        entries = []
        for path, text in self._read_sources().items():
            if path.endswith(".sources"):
                entries += parse_deb822_sources(path, text)
            else:
                entries += parse_one_line_sources(path, text)
        return entries

    def list_repositories(self) -> list[Repository]:
        repos = []
        for e in self._source_entries():
            if e.types == ["deb-src"]:
                continue
            signed = e.signed_by
            armored = inline_key(signed) if signed else None
            if e.trusted_yes:
                trust, signed_desc = Trust.WARNING, "trusted=yes — signatures are not checked"
            elif signed:
                trust = Trust.SIGNED
                signed_desc = "Embedded key" if armored else f"Signed by {signed}"
            else:
                trust, signed_desc = Trust.SIGNED, "Any key in the global APT keyring"
            name = _repo_name(e)
            repos.append(Repository(
                kind=PackageKind.APT, id=f"{e.file}#{e.index}", name=name,
                url=" ".join(e.uris), enabled=e.enabled, scope=e.file,
                details=f"{' '.join(e.suites)} · {' '.join(e.components)}".strip(" ·"),
                signed_by=signed_desc, trust=trust, extra={"entry": e},
            ))
        return repos

    def repository_keys(self, repo: Repository) -> list[str]:
        entry: SourceEntry = repo.extra["entry"]
        if not entry.signed_by:
            return []
        armored = inline_key(entry.signed_by)
        if armored:
            return [k.describe() for k in key_data_keys(armored)]
        out = []
        for kr in entry.signed_by.replace(",", " ").split():
            out += [k.describe() for k in keyring_keys(kr)]
        return out

    def set_repository_enabled(self, repo: Repository, enabled: bool) -> list[Command]:
        entry: SourceEntry = repo.extra["entry"]

        def apply() -> str:
            text = self._read_sources().get(entry.file, "")
            res = host.write_host_file_privileged(entry.file, set_entry_enabled(text, entry, enabled))
            if not res.ok:
                raise BackendError(res.stderr.strip() or "Could not write sources file")
            return f"Updated {entry.file}"

        return [Command(f"{'Enable' if enabled else 'Disable'} {repo.name}", func=apply)]

    def remove_repository(self, repo: Repository) -> list[Command]:
        entry: SourceEntry = repo.extra["entry"]
        others = [e for e in self._source_entries() if e.file == entry.file and e.index != entry.index]

        if not others and entry.file != "/etc/apt/sources.list":
            return [Command(f"Remove {entry.file}", host.privileged(["rm", "-f", entry.file]))]

        def apply() -> str:
            text = self._read_sources().get(entry.file, "")
            res = host.write_host_file_privileged(entry.file, remove_entry(text, entry))
            if not res.ok:
                raise BackendError(res.stderr.strip() or "Could not write sources file")
            return f"Removed entry from {entry.file}"

        return [Command(f"Remove {repo.name}", func=apply)]

    def fetch_key(self, key_url: str) -> tuple[str, list[KeyInfo]]:
        """Download a signing key on the host; returns (host temp path, keys)."""
        tmp = f"/tmp/unipkg-newkey-{os.getpid()}"
        res = host.run(["curl", "-fsSL", "-o", tmp, key_url], timeout=60)
        if not res.ok:
            raise BackendError(f"Could not download key: {res.stderr.strip()}")
        keys = keyring_keys(tmp)
        if not keys:
            raise BackendError("The downloaded file does not contain an OpenPGP key")
        return tmp, keys

    def add_repository(self, *, name: str, url: str, suites: str = "", components: str = "",
                       key_path: str = "", **_) -> list[Command]:
        if url.startswith("ppa:"):
            return [
                Command(f"Add {url}", host.privileged(["add-apt-repository", "-y", url], APT_ENV)),
            ]
        slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "custom"
        keyring = f"/etc/apt/keyrings/{slug}.gpg"
        lines = ["Types: deb", f"URIs: {url}", f"Suites: {suites or 'stable'}"]
        if components:
            lines.append(f"Components: {components}")
        if key_path:
            lines.append(f"Signed-By: {keyring}")
        content = "\n".join(lines) + "\n"
        target = f"/etc/apt/sources.list.d/{slug}.sources"
        script = "set -e; mkdir -p /etc/apt/keyrings; "
        if key_path:
            script += (f"if grep -q 'BEGIN PGP' {shlex.quote(key_path)}; then "
                       f"gpg --dearmor < {shlex.quote(key_path)} > {keyring}; "
                       f"else cp {shlex.quote(key_path)} {keyring}; fi; chmod 644 {keyring}; ")
        script += f"printf '%s' {shlex.quote(content)} > {target}; apt-get update"
        return [Command(f"Add repository {name}", host.privileged(["sh", "-c", script], APT_ENV))]


def _find_entry(entries: list[SourceEntry], uri: str, suite: str) -> Optional[SourceEntry]:
    uri = uri.rstrip("/")
    for e in entries:
        if not e.enabled:
            continue
        if any(u.rstrip("/") == uri for u in e.uris) and (suite in e.suites or not e.suites):
            return e
    for e in entries:
        if any(u.rstrip("/") == uri for u in e.uris):
            return e
    return None


def _repo_name(e: SourceEntry) -> str:
    uri = e.uris[0] if e.uris else ""
    m = re.match(r"^[a-z+]+://([^/]+)(/.*)?$", uri)
    host_part = m.group(1) if m else uri
    if "ppa.launchpadcontent.net" in host_part or "ppa.launchpad.net" in host_part:
        parts = (m.group(2) or "").strip("/").split("/")
        return f"PPA {parts[0]}/{parts[1]}" if len(parts) >= 2 else "PPA"
    base = os.path.basename(e.file).rsplit(".", 1)[0]
    if base in ("sources", "ubuntu", "debian") or base == "sources.list":
        return f"{host_part} ({' '.join(e.suites)})"
    return f"{base} — {host_part}"
