"""Backend interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..models import (
    Package,
    PackageKind,
    Permission,
    Repository,
    SearchResult,
    Verification,
)


@dataclass
class Command:
    """One step of an operation.

    Either *argv* is executed on the host (output is streamed to the UI) or
    *func* is called in the worker thread; it may return a string to log.

    If the command exits with an error, *on_failure(exit_code, output)* (when
    set) decides whether the requested change happened anyway: it returns a
    warning message to continue with, or ``None`` for a real failure.
    """

    description: str
    argv: Optional[list[str]] = None
    env: dict[str, str] = field(default_factory=dict)
    func: Optional[Callable[[], Optional[str]]] = None
    allow_failure: bool = False
    on_failure: Optional[Callable[[int, str], Optional[str]]] = None
    # Turns the output of a failed run into a human readable explanation.
    failure_hint: Optional[Callable[[str], Optional[str]]] = None


class BackendError(RuntimeError):
    pass


def guard_protected(pkgs: list[Package]) -> None:
    """Refuse to build removal commands for critical system components."""
    protected = [p for p in pkgs if p.extra.get("protected")]
    if protected:
        raise BackendError("Refusing to remove protected system components: " + ", ".join(
            f"{p.display_name} ({p.extra['protected']})" for p in protected))


class Backend:
    name = "backend"
    kinds: tuple[PackageKind, ...] = ()

    def available(self) -> bool:
        raise NotImplementedError

    # Installed packages -------------------------------------------------
    def list_installed(self) -> list[Package]:
        raise NotImplementedError

    # Permissions ----------------------------------------------------------
    def permissions_manageable(self, pkg: Package) -> bool:
        return False

    def set_permission(self, pkg: Package, perm: Permission, granted: bool) -> list[Command]:
        raise BackendError("Permissions of this package type cannot be changed")

    def reset_permissions(self, pkg: Package) -> list[Command]:
        return []

    def reload_permissions(self, pkg: Package) -> None:
        """Refresh ``pkg.permissions`` in place."""

    # Search / install -------------------------------------------------------
    def search(self, query: str) -> list[SearchResult]:
        return []

    def enrich(self, result: SearchResult) -> None:
        """Fill in details that are expensive to fetch (sizes, …)."""

    def install(self, result: SearchResult) -> list[Command]:
        raise NotImplementedError

    def uninstall(self, pkg: Package, delete_data: bool) -> list[Command]:
        return self.uninstall_many([pkg], delete_data)

    def uninstall_many(self, pkgs: list[Package], delete_data: bool) -> list[Command]:
        """Remove several packages, batching them into as few commands as possible."""
        raise NotImplementedError

    def data_locations(self, pkg: Package) -> list[str]:
        """User data directories that are removed with "delete data"."""
        return []

    # Updates ------------------------------------------------------------------
    def refresh(self) -> list[Command]:
        """Commands that refresh the package metadata before checking updates."""
        return []

    def list_updates(self) -> dict[str, str]:
        """Return a mapping ``Package.uid -> new version``."""
        return {}

    def update(self, pkg: Package) -> list[Command]:
        raise NotImplementedError

    def update_all(self) -> list[Command]:
        raise NotImplementedError

    # Verification ---------------------------------------------------------------
    def verify(self, pkg: Package, deep: bool = False) -> Verification:
        raise NotImplementedError

    def verify_search_result(self, result: SearchResult) -> Verification | None:
        return None

    # Repositories ----------------------------------------------------------------
    def list_repositories(self) -> list[Repository]:
        return []

    def set_repository_enabled(self, repo: Repository, enabled: bool) -> list[Command]:
        raise BackendError("Not supported")

    def remove_repository(self, repo: Repository) -> list[Command]:
        raise BackendError("Not supported")

    def add_repository(self, **kwargs) -> list[Command]:
        raise BackendError("Not supported")
