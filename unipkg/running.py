"""Detect, close and relaunch running applications (used around updates).

Flatpak and Snap apps run in their own systemd scopes, so they are found by
their cgroup (``app-flatpak-<id>-….scope`` / ``snap.<name>.….scope``).
Traditional (APT / .deb) apps are found by matching the executables of the
user's processes against the files of the package.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from . import host
from .models import Package, PackageKind

APP_ID = "io.github.unipkg.UniPkg"

_PROC_SCRIPT = r"""
for p in /proc/[0-9]*; do
  e=$(readlink "$p/exe" 2>/dev/null) || continue
  c=$(tail -n 1 "$p/cgroup" 2>/dev/null)
  a=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null | cut -c1-300)
  printf '%s\t%s\t%s\t%s\n' "${p#/proc/}" "${c##*/}" "$e" "$a"
done
"""


@dataclass
class Proc:
    pid: int
    scope: str
    exe: str
    cmdline: str = ""

    @property
    def is_background_service(self) -> bool:
        """Services and session helpers restart on demand and do not block updates.

        That covers D-Bus activated / systemd services, GApplication background
        services (search providers) and helpers in libexec directories.
        """
        return (self.scope.endswith(".service") or "--gapplication-service" in self.cmdline
                or "/libexec/" in self.exe)


@dataclass
class RunningApp:
    pkg: Package
    pids: list[int] = field(default_factory=list)

    @property
    def can_update_while_running(self) -> bool:
        # snapd refuses to refresh a snap while it has running apps.
        return self.pkg.kind != PackageKind.SNAP


def parse_process_table(text: str) -> list[Proc]:
    procs = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < 3 or not cols[0].isdigit():
            continue
        exe = cols[2].removesuffix(" (deleted)")
        procs.append(Proc(int(cols[0]), cols[1], exe, cols[3].strip() if len(cols) > 3 else ""))
    return procs


def process_table() -> list[Proc]:
    """The user's processes (root processes are unreadable and skipped)."""
    return parse_process_table(host.run_shell(_PROC_SCRIPT, timeout=60).stdout)


def match_running(pkgs: list[Package], procs: list[Proc],
                  package_files: dict[str, set[str]]) -> list[RunningApp]:
    running = []
    procs = [p for p in procs if not p.is_background_service]
    for pkg in pkgs:
        if pkg.kind == PackageKind.FLATPAK:
            if pkg.name == APP_ID:
                continue  # updating ourselves is fine, the new version is used next start
            prefix = f"app-flatpak-{pkg.name}-"
            pids = [p.pid for p in procs if p.scope.startswith(prefix)]
        elif pkg.kind == PackageKind.SNAP:
            prefix = f"snap.{pkg.name}."
            pids = [p.pid for p in procs if p.scope.startswith(prefix)]
        else:
            files = package_files.get(pkg.extra.get("package", pkg.name), set())
            pids = [p.pid for p in procs if p.exe in files]
        if pids:
            running.append(RunningApp(pkg, sorted(pids)))
    return running


def find_running(manager, pkgs: list[Package]) -> list[RunningApp]:
    """Return the applications among *pkgs* that are currently running."""
    apps = [p for p in pkgs if p.is_app]
    if not apps:
        return []
    procs = process_table()
    files: dict[str, set[str]] = {}
    debs = [p.extra.get("package", p.name) for p in apps if p.kind in (PackageKind.APT, PackageKind.DEB)]
    if debs and manager.apt:
        files = manager.apt.package_files(debs)
    return match_running(apps, procs, files)


def terminate(pids: list[int], timeout: float = 10) -> list[int]:
    """Ask processes to quit (SIGTERM) and wait; returns PIDs still alive.

    Processes are never killed forcefully so that apps can save their state.
    """
    if not pids:
        return []
    ids = " ".join(str(p) for p in pids)
    script = (
        f"kill -TERM {ids} 2>/dev/null; i=0; "
        f"while [ $i -lt {int(timeout * 2)} ]; do alive=''; "
        f"for p in {ids}; do s=$(ps -o stat= -p $p 2>/dev/null) && "
        f"[ \"${{s#Z}}\" = \"$s\" ] && alive=\"$alive $p\"; done; "
        f"[ -z \"$alive\" ] && exit 0; sleep 0.5; i=$((i+1)); done; echo $alive"
    )
    res = host.run_shell(script, timeout=timeout + 15)
    return [int(p) for p in res.stdout.split() if p.isdigit()]


def launch(pkg: Package) -> bool:
    """Start an application again (detached from UniPkg)."""
    if pkg.kind == PackageKind.FLATPAK:
        cmd = f"flatpak run {shlex.quote(pkg.name)}"
    elif pkg.kind == PackageKind.SNAP:
        desktop = (pkg.extra.get("desktop_files") or [""])[0]
        cmd = (f"gio launch {shlex.quote(desktop)}" if desktop
               else f"snap run {shlex.quote(pkg.name)}")
    else:
        desktop = pkg.extra.get("desktop_file", "")
        if not desktop:
            return False
        cmd = (f"gio launch {shlex.quote(desktop)} || "
               f"gtk-launch {shlex.quote(desktop.rsplit('/', 1)[-1])}")
    res = host.run(["sh", "-c", f"setsid -f sh -c {shlex.quote(cmd)} >/dev/null 2>&1 </dev/null"],
                   timeout=20)
    return res.ok
