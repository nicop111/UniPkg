"""Running commands on the host system.

When UniPkg runs inside a Flatpak sandbox every package-manager command has
to be executed on the host through ``flatpak-spawn --host`` (which requires the
``--talk-name=org.freedesktop.Flatpak`` permission). Outside the sandbox the
commands are executed directly, which makes development and testing easy.

Privileged commands are wrapped in ``pkexec`` so that polkit asks the user for
authentication with the regular desktop dialog.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

IN_FLATPAK = os.path.exists("/.flatpak-info")


@dataclass
class Result:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandError(RuntimeError):
    def __init__(self, result: Result):
        self.result = result
        msg = (result.stderr or result.stdout).strip().splitlines()
        super().__init__(msg[-1] if msg else f"Command failed: {' '.join(result.argv)}")


def host_argv(argv: Sequence[str], env: Optional[dict[str, str]] = None) -> list[str]:
    """Translate *argv* so that it is executed on the host."""
    argv = list(argv)
    if IN_FLATPAK:
        prefix = ["flatpak-spawn", "--host", "--watch-bus"]
        for key, value in (env or {}).items():
            prefix.append(f"--env={key}={value}")
        return prefix + argv
    return argv


def _local_env(env: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
    if IN_FLATPAK or not env:
        return None
    merged = dict(os.environ)
    merged.update(env)
    return merged


def _base_env() -> dict[str, str]:
    # Stable, parseable output from all tools.
    return {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}


def run(
    argv: Sequence[str],
    *,
    input: Optional[bytes | str] = None,
    timeout: Optional[float] = 300,
    env: Optional[dict[str, str]] = None,
    check: bool = False,
) -> Result:
    """Run *argv* on the host and capture its output."""
    full_env = _base_env()
    full_env.update(env or {})
    cmd = host_argv(argv, full_env)
    if isinstance(input, str):
        input = input.encode()
    try:
        proc = subprocess.run(
            cmd,
            input=input,
            capture_output=True,
            timeout=timeout,
            env=_local_env(full_env),
        )
    except FileNotFoundError as exc:
        result = Result(list(argv), 127, "", str(exc))
    except subprocess.TimeoutExpired:
        result = Result(list(argv), 124, "", "Command timed out")
    else:
        result = Result(
            list(argv),
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )
    if check and not result.ok:
        raise CommandError(result)
    return result


def run_bytes(argv: Sequence[str], timeout: Optional[float] = 120) -> Optional[bytes]:
    """Run *argv* on the host and return raw stdout (``None`` on failure)."""
    try:
        proc = subprocess.run(
            host_argv(argv), capture_output=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def run_shell(script: str, **kwargs) -> Result:
    """Run a POSIX shell snippet on the host."""
    return run(["sh", "-c", script], **kwargs)


def privileged(argv: Sequence[str], env: Optional[dict[str, str]] = None) -> list[str]:
    """Wrap *argv* so that it runs as root after polkit authentication."""
    argv = list(argv)
    if os.geteuid() == 0 and not IN_FLATPAK:
        return argv
    if env:
        return ["pkexec", "env", *[f"{k}={v}" for k, v in env.items()], *argv]
    return ["pkexec", *argv]


def which(program: str) -> bool:
    return run(["sh", "-c", f"command -v {shlex.quote(program)}"], timeout=10).ok


_host_files_lock = threading.Lock()


def read_host_file(path: str, max_bytes: int = 64 * 1024 * 1024) -> Optional[bytes]:
    """Read a file from the host file system.

    Inside the sandbox paths below ``/usr`` and ``/etc`` are reachable through
    ``/run/host`` (granted with ``host-os``/``host-etc``); everything else is
    read with ``cat`` on the host.
    """
    candidates = [path]
    if IN_FLATPAK:
        candidates = []
        for prefix in ("/usr/", "/etc/"):
            if path.startswith(prefix):
                candidates.append("/run/host" + path)
        if path.startswith(os.path.expanduser("~/")) and not path.startswith(
            os.path.expanduser("~/.var/")
        ):
            candidates.append(path)
    for candidate in candidates:
        try:
            with open(candidate, "rb") as fh:
                return fh.read(max_bytes)
        except OSError:
            continue
    return run_bytes(["cat", path])


def write_host_file_privileged(path: str, content: str) -> Result:
    """Write *content* to a root-owned host file via ``pkexec tee``."""
    return run(privileged(["tee", path]), input=content.encode(), timeout=600)


def copy_to_host_tmp(local_path: str, name: str) -> str:
    """Copy a file that is visible inside the sandbox to the host's /tmp.

    Files chosen through the file chooser portal live in the document portal
    FUSE mount, which root (pkexec) cannot read, so we stream them to a host
    temporary file first.
    """
    target = f"/tmp/unipkg-{os.getpid()}-{os.path.basename(name)}"
    if not IN_FLATPAK:
        import shutil

        shutil.copyfile(local_path, target)
        return target
    with open(local_path, "rb") as fh:
        data = fh.read()
    res = run(["sh", "-c", f"cat > {shlex.quote(target)}"], input=data)
    if not res.ok:
        raise CommandError(res)
    return target


class StreamingProcess:
    """A long running host process whose output is delivered line by line."""

    def __init__(
        self,
        argv: Sequence[str],
        on_output: Callable[[str], None],
        env: Optional[dict[str, str]] = None,
    ):
        full_env = _base_env()
        full_env.update(env or {})
        self.argv = list(argv)
        self._on_output = on_output
        self._proc = subprocess.Popen(
            host_argv(argv, full_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=_local_env(full_env),
        )

    def wait(self) -> int:
        assert self._proc.stdout is not None
        buf = b""
        while True:
            chunk = self._proc.stdout.read1(4096)  # type: ignore[attr-defined]
            if not chunk:
                break
            buf += chunk
            # Progress bars use carriage returns; treat them as line breaks.
            parts = buf.replace(b"\r", b"\n").split(b"\n")
            buf = parts.pop()
            for part in parts:
                if part.strip():
                    self._on_output(part.decode("utf-8", "replace"))
        if buf.strip():
            self._on_output(buf.decode("utf-8", "replace"))
        return self._proc.wait()

    def cancel(self) -> None:
        try:
            self._proc.terminate()
        except OSError:
            pass


def format_argv(argv: Iterable[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)
