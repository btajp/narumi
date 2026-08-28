"""Owner-only local bootstrap storage; never follow a supplied symlink.

Only the public certificate, endpoint and opaque Keychain account are persisted here.
The open directory descriptors keep validation and file operations on the same directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from narumi_server.transport_acl import ensure_no_extended_allow_acl
from narumi_server.transport_errors import BootstrapNotFoundError, TransportSecurityError

BOOTSTRAP_FILE = "bootstrap.json"
BOOTSTRAP_VERSION = 1
MAX_BOOTSTRAP_BYTES = 32 * 1024


def bootstrap_path(root: Path) -> Path:
    return root.expanduser() / "runtime" / "server" / BOOTSTRAP_FILE


def _check_directory(fd: int, *, private: bool) -> None:
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or (mode != 0o700 if private else mode & 0o022 != 0)
    ):
        raise TransportSecurityError()
    ensure_no_extended_allow_acl(fd)


def _check_file(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise TransportSecurityError()
    ensure_no_extended_allow_acl(fd)


@contextlib.contextmanager
def private_server_directory(root: Path, *, create: bool = False) -> Iterator[int]:
    """Check the root, then traverse runtime/server with openat + O_NOFOLLOW.

    Existing data roots may be 0755. They must belong to this user and must not be
    writable by other users. Both newly introduced runtime directories are 0700.
    """
    root = root.expanduser()
    fds: list[int] = []
    try:
        if create:
            root.mkdir(parents=True, mode=0o700, exist_ok=True)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(root, flags)
        fds.append(fd)
        _check_directory(fd, private=False)
        for component in ("runtime", "server"):
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            fd = os.open(component, flags, dir_fd=fd)
            fds.append(fd)
            if create:
                # Older app RuntimeLease versions created runtime/ as 0755. Tighten only
                # an owner-verified directory that cannot be modified by another user.
                _check_directory(fd, private=False)
                os.fchmod(fd, 0o700)
            _check_directory(fd, private=True)
        yield fd
    except FileNotFoundError:
        raise BootstrapNotFoundError() from None
    except OSError:
        raise TransportSecurityError() from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def open_private_file(directory: int, name: str, *, create: bool = False) -> int:
    flags = os.O_RDWR if create else os.O_RDONLY
    flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(name, flags, mode=0o600, dir_fd=directory)
        try:
            _check_file(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd
    except FileNotFoundError:
        raise BootstrapNotFoundError() from None
    except OSError:
        raise TransportSecurityError() from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError
        result[name] = value
    return result


def read_bootstrap(directory: int) -> dict[str, Any]:
    fd = open_private_file(directory, BOOTSTRAP_FILE)
    try:
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(MAX_BOOTSTRAP_BYTES + 1)
        if len(raw) > MAX_BOOTSTRAP_BYTES:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, OSError):
        raise TransportSecurityError() from None


def read_client_bootstrap(root: Path) -> dict[str, Any]:
    with private_server_directory(root) as directory:
        return read_bootstrap(directory)


def atomic_private_write(directory: int, name: str, content: bytes) -> None:
    """Create an owner-only temporary file, fsync it and atomically replace a safe target."""
    try:
        existing = open_private_file(directory, name)
    except BootstrapNotFoundError:
        pass
    else:
        os.close(existing)
    temporary = f".{name}.{uuid4()}.tmp"
    fd: int | None = None
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode=0o600,
            dir_fd=directory,
        )
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except OSError:
        raise TransportSecurityError() from None
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def write_bootstrap(directory: int, document: dict[str, Any]) -> None:
    atomic_private_write(
        directory,
        BOOTSTRAP_FILE,
        (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
    )
