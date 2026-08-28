"""Prepare a pinned existing Codex binary without reading an ambient Codex home."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import selectors
import signal
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from narumi.errors import CancelledError
from narumi.providers._acl import ensure_no_extended_allow_acl
from narumi.providers._io import _open_directory, _open_regular
from narumi.providers.codex._rpc import unavailable
from narumi.providers.secrets import _trusted_directory_tree

SUPPORTED_VERSION = "0.150.1"
RESOURCE_ID = "codex-app-server-0-150-1"
MAX_BINARY_BYTES = 512 * 1024 * 1024
_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\x7fELF"}


def installed_candidates() -> list[tuple[Path, Path]]:
    """Known installations only; never run a PATH entry, shell wrapper or user config."""
    result = []
    for app in (Path("/Applications/Codex.app"), Path("/Applications/Codex Dev.app")):
        result.append((app, app / "Contents/Resources/codex"))
        result.append((app, app / "Contents/Resources/bin/codex"))
    architecture = "arm64" if platform.machine() in {"arm64", "aarch64"} else "x64"
    target = "aarch64-apple-darwin" if architecture == "arm64" else "x86_64-apple-darwin"
    for prefix in (Path("/opt/homebrew"), Path("/usr/local")):
        package = prefix / "lib/node_modules/@openai/codex"
        vendor = (
            package / "node_modules/@openai" / f"codex-darwin-{architecture}" / "vendor" / target
        )
        result.extend(((package, vendor / "bin/codex"), (package, vendor / "codex/codex")))
    return result


def private_environment(home: Path, codex_home: Path, temporary: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "TMPDIR": str(temporary),
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "RUST_LOG": "off",
        "NO_COLOR": "1",
    }


def _open_binary(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_mode & 0o022
            or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID)
            or not metadata.st_mode & 0o111
            or metadata.st_nlink != 1
            or not 4 <= metadata.st_size <= MAX_BINARY_BYTES
        ):
            raise OSError("untrusted executable")
        ensure_no_extended_allow_acl(descriptor)
        if os.read(descriptor, 4) not in _MAGIC:
            raise OSError("not a native executable")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _digest(descriptor: int) -> str:
    before = os.fstat(descriptor)
    digest = hashlib.sha256()
    length = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        length += len(chunk)
        if length > MAX_BINARY_BYTES:
            raise OSError("executable too large")
        digest.update(chunk)
    after = os.fstat(descriptor)
    if (
        length != before.st_size
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise OSError("executable changed")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _read_metadata(path: Path) -> dict[str, Any] | None:
    """Read package/verification metadata without chmod or following a link."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_mode & 0o022
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= 16 * 1024
        ):
            raise OSError("untrusted runtime metadata")
        ensure_no_extended_allow_acl(descriptor)
        data = os.read(descriptor, 16 * 1024 + 1)
        if len(data) > 16 * 1024:
            raise OSError("runtime metadata too large")
        value = json.loads(data)
        return value if isinstance(value, dict) else None
    finally:
        os.close(descriptor)


class CodexRuntime:
    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(root))
        self.directory = self.root / "providers/runtime/codex-app-server" / SUPPORTED_VERSION
        self.executable = self.directory / "codex"

    def _candidate(self) -> tuple[Path, str] | None:
        candidates = [(self.directory, self.executable), *installed_candidates()]
        for root, path in candidates:
            if not _trusted_directory_tree(root, path.parent):
                continue
            try:
                if path != self.executable:
                    package = _read_metadata(root / "package.json")
                    if package is None or (
                        package.get("name") != "@openai/codex"
                        or package.get("version") != SUPPORTED_VERSION
                    ):
                        continue
                descriptor = _open_binary(path)
                try:
                    digest = _digest(descriptor)
                    if path == self.executable and _read_metadata(
                        self.directory / "verification.json"
                    ) != {"version": SUPPORTED_VERSION, "sha256": digest}:
                        continue
                    return path, digest
                finally:
                    os.close(descriptor)
            except (OSError, ValueError):
                continue
        return None

    def resource(self) -> dict[str, Any]:
        candidate = self._candidate()
        return {
            "resource_id": RESOURCE_ID,
            "display_name": "Codex App Server 0.150.1 installed binary verification",
            "kind": "runtime",
            "version": SUPPORTED_VERSION if candidate else None,
            "source": "installed",
            "download_host": None,
            "sha256": candidate[1] if candidate else None,
            "license": "Apache-2.0",
        }

    def prepare(self, resource: dict[str, Any], progress: Any) -> None:
        progress("inspect_installed_codex", 0.2)
        if resource != self.resource() or resource["sha256"] is None:
            raise unavailable("codex_installed_runtime_unavailable")
        candidate = self._candidate()
        if candidate is None:
            raise unavailable("codex_installed_runtime_unavailable")
        try:
            directory = _open_directory(self.directory, trusted_root=self.root)
        except OSError:
            raise unavailable("codex_runtime_not_secure") from None
        temporary = f".codex.{uuid.uuid4().hex}.tmp"
        try:
            for name in ("verification-home", "verification-state", "verification-tmp"):
                child = _open_directory(self.directory / name, trusted_root=self.root)
                os.close(child)
            env = private_environment(
                self.directory / "verification-home",
                self.directory / "verification-state",
                self.directory / "verification-tmp",
            )
            source = _open_binary(candidate[0])
            target = _open_regular(directory, temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                if _digest(source) != resource["sha256"]:
                    raise unavailable("codex_runtime_changed")
                with os.fdopen(target, "wb") as output:
                    while chunk := os.read(source, 1024 * 1024):
                        if getattr(progress, "cancelled", False):
                            raise CancelledError("Codex preparation was cancelled")
                        output.write(chunk)
                    output.flush()
                    os.fchmod(output.fileno(), 0o700)
                    os.fsync(output.fileno())
            finally:
                os.close(source)
            progress("verify_codex_version", 0.65)
            prepared = self.directory / temporary
            descriptor = _open_binary(prepared)
            try:
                if _digest(descriptor) != resource["sha256"]:
                    raise unavailable("codex_runtime_changed")
            finally:
                os.close(descriptor)
            verify_version(prepared, env, self.directory)
            if getattr(progress, "cancelled", False):
                raise CancelledError("Codex preparation was cancelled")
            descriptor = _open_binary(prepared)
            try:
                if _digest(descriptor) != resource["sha256"]:
                    raise unavailable("codex_runtime_changed")
            finally:
                os.close(descriptor)
            try:
                previous = _open_binary(self.executable)
            except FileNotFoundError:
                pass
            else:
                os.close(previous)
            os.replace(temporary, "codex", src_dir_fd=directory, dst_dir_fd=directory)
            write_private_json(
                self.directory,
                self.root,
                "verification.json",
                {"version": SUPPORTED_VERSION, "sha256": resource["sha256"]},
            )
            os.fsync(directory)
            progress("codex_runtime_ready", 0.9)
        except (OSError, ValueError):
            raise unavailable("codex_runtime_not_secure") from None
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

    def require_prepared(self) -> Path:
        try:
            if not _trusted_directory_tree(self.root, self.directory):
                raise OSError("runtime directory unavailable")
            directory = _open_directory(self.directory, trusted_root=self.root)
            try:
                record = _open_regular(directory, "verification.json", os.O_RDONLY)
                with os.fdopen(record, "rb") as stream:
                    data = stream.read(4097)
                if len(data) > 4096:
                    raise ValueError("invalid verification record")
                saved = json.loads(data)
            finally:
                os.close(directory)
            descriptor = _open_binary(self.executable)
            try:
                if saved != {"version": SUPPORTED_VERSION, "sha256": _digest(descriptor)}:
                    raise ValueError("runtime changed")
            finally:
                os.close(descriptor)
            return self.executable
        except (OSError, ValueError, TypeError):
            raise unavailable("codex_runtime_preparation_required") from None


def write_private_json(directory: Path, root: Path, name: str, value: dict[str, Any]) -> None:
    descriptor = _open_directory(directory, trusted_root=root)
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        try:
            previous = _open_regular(descriptor, name, os.O_RDONLY)
        except FileNotFoundError:
            pass
        else:
            os.close(previous)
        target = _open_regular(descriptor, temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(target, "wb") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


def verify_version(executable: Path, env: dict[str, str], cwd: Path) -> None:
    try:
        process = subprocess.Popen(
            [str(executable), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
            start_new_session=True,
            close_fds=True,
            umask=0o077,
        )
    except (OSError, ValueError):
        raise unavailable("codex_version_unverified") from None
    output = bytearray()
    deadline = time.monotonic() + 10
    try:
        if process.stdout is None:
            raise unavailable("codex_version_unverified")
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as pending:
            pending.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not pending.select(remaining):
                    raise unavailable("codex_version_unverified")
                chunk = os.read(process.stdout.fileno(), 4097)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 4096:
                    raise unavailable("codex_version_unverified")
        if bytes(output).strip() != f"codex-cli {SUPPORTED_VERSION}".encode():
            raise unavailable("codex_runtime_version_unsupported")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise unavailable("codex_version_unverified") from None
    finally:
        if process.stdout is not None:
            process.stdout.close()
        # Retain the unreaped leader until group cleanup, so its PID cannot be
        # reused by an unrelated process between wait() and killpg().
        threading.Event().wait(0.05)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait(timeout=2)
    if process.returncode != 0:
        raise unavailable("codex_version_unverified")
