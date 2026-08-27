"""Private, atomic connection-file I/O and a process-shared POSIX write lock."""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MAX_SETTINGS_BYTES = 16 * 1024


def _private_parent(path: Path) -> None:
    """Create missing directories privately without chmod-ing existing ancestors."""
    try:
        path.mkdir(mode=0o700)
    except FileNotFoundError:
        _private_parent(path.parent)
        _private_parent(path)
    except FileExistsError:
        if not path.is_dir():
            raise OSError("connection parent is not a directory") from None


def _private_open(path: Path, flags: int) -> int:
    flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise OSError("connection file must be a regular file owned by the current user")
        os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def read_private(path: Path) -> str | None:
    """Read a private regular file, or return ``None`` when it does not exist."""
    try:
        descriptor = _private_open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        contents = stream.read(MAX_SETTINGS_BYTES + 1)
    if len(contents) > MAX_SETTINGS_BYTES:
        raise ValueError("connection file is too large")
    return contents.decode("utf-8")


@contextmanager
def write_lock(path: Path) -> Iterator[None]:
    """Serialize complete read-modify-write transactions across processes and instances."""
    _private_parent(path.parent)
    descriptor = _private_open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def replace_private(path: Path, contents: str) -> None:
    """Write through a unique owner-only temporary file; failed writes leave the old file."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
