"""Application state shared by all pages: backends, packages and updates."""

from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from gi.repository import GLib, GObject

from .backends.apt import AptBackend
from .backends.base import Backend, Command
from .backends.flatpak import FlatpakBackend
from .backends.snap import SnapBackend
from .models import Package, PackageKind, SearchResult


class Manager(GObject.Object):
    __gsignals__ = {
        "packages-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "updates-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "loading-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "repositories-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self.all_backends: list[Backend] = [AptBackend(), FlatpakBackend(), SnapBackend()]
        self.backends: list[Backend] = []
        self.packages: list[Package] = []
        self.updates: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self.loading = False
        self.checking_updates = False
        self._lock = threading.Lock()
        self.operation_lock = threading.Lock()  # one package operation at a time
        self._pool = ThreadPoolExecutor(max_workers=6)

    # ------------------------------------------------------------------
    def backend_for(self, kind: PackageKind) -> Optional[Backend]:
        for backend in self.backends:
            if kind in backend.kinds:
                return backend
        return None

    @property
    def apt(self) -> Optional[AptBackend]:
        return self.backend_for(PackageKind.APT)  # type: ignore[return-value]

    @property
    def flatpak(self) -> Optional[FlatpakBackend]:
        return self.backend_for(PackageKind.FLATPAK)  # type: ignore[return-value]

    @property
    def snap(self) -> Optional[SnapBackend]:
        return self.backend_for(PackageKind.SNAP)  # type: ignore[return-value]

    def _emit(self, signal: str) -> bool:
        self.emit(signal)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    def reload(self) -> None:
        """Reload installed packages from all backends (asynchronously)."""
        if self.loading:
            return
        self.loading = True
        self.emit("loading-changed")
        threading.Thread(target=self._reload_worker, daemon=True).start()

    def _reload_worker(self) -> None:
        if not self.backends:
            self.backends = [b for b in self.all_backends if _safe_available(b)]
        results: dict[str, list[Package]] = {}
        errors: dict[str, str] = {}

        def load(backend: Backend) -> None:
            try:
                results[backend.name] = backend.list_installed()
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                errors[backend.name] = str(exc)

        futures = [self._pool.submit(load, b) for b in self.backends]
        for f in futures:
            f.result()
        packages = [p for b in self.backends for p in results.get(b.name, [])]
        for pkg in packages:
            pkg.update_version = self.updates.get(pkg.uid)
        with self._lock:
            self.packages = packages
            self.errors = errors
        self.loading = False
        GLib.idle_add(self._emit, "packages-changed")
        GLib.idle_add(self._emit, "loading-changed")

    def check_updates(self) -> None:
        if self.checking_updates:
            return
        self.checking_updates = True
        self.emit("updates-changed")
        threading.Thread(target=self._updates_worker, daemon=True).start()

    def _updates_worker(self) -> None:
        while not self.backends:
            if not self.loading:
                self.backends = [b for b in self.all_backends if _safe_available(b)]
                break
            threading.Event().wait(0.2)
        updates: dict[str, str] = {}

        def load(backend: Backend) -> None:
            try:
                updates.update(backend.list_updates())
            except Exception:  # noqa: BLE001
                traceback.print_exc()

        for f in [self._pool.submit(load, b) for b in self.backends]:
            f.result()
        self.updates = updates
        for pkg in self.packages:
            pkg.update_version = updates.get(pkg.uid)
        self.checking_updates = False
        GLib.idle_add(self._emit, "updates-changed")

    @property
    def packages_with_updates(self) -> list[Package]:
        return [p for p in self.packages if p.update_version]

    def search(self, query: str) -> list[SearchResult]:
        results: list[SearchResult] = []

        def run(backend: Backend) -> list[SearchResult]:
            try:
                return backend.search(query)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                return []

        for f in [self._pool.submit(run, b) for b in self.backends]:
            results.extend(f.result())
        return results

    def find_installed(self, kind: PackageKind, name: str) -> Optional[Package]:
        for pkg in self.packages:
            if pkg.kind == kind and pkg.name == name:
                return pkg
        return None


def _safe_available(backend: Backend) -> bool:
    try:
        return backend.available()
    except Exception:  # noqa: BLE001
        return False


__all__ = ["Manager", "Command"]
