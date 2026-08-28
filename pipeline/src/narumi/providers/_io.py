"""Owner-only provider registry I/O through directory-relative file descriptors."""

from __future__ import annotations

import fcntl
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from narumi.providers._acl import ensure_no_extended_allow_acl

MAX_REGISTRY_BYTES = 16 * 1024 * 1024
REGISTRY_NAME = "registry.json"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_directory(path: Path, *, trusted_root: Path | None = None) -> int:
    """Check mode and ACL within the root; never chmod pre-existing ancestors."""
    absolute = Path(os.path.abspath(path))
    trusted = Path(os.path.abspath(trusted_root)) if trusted_root is not None else absolute
    if not absolute.is_relative_to(trusted):
        raise OSError("provider directory is outside its trusted namespace")
    descriptor = os.open(absolute.anchor, _DIRECTORY_FLAGS)
    try:
        if trusted == Path(absolute.anchor):
            metadata = os.fstat(descriptor)
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                raise OSError("provider directory must be writable only by its owner")
            ensure_no_extended_allow_acl(descriptor)
        for index, component in enumerate(absolute.parts[1:], start=1):
            created = False
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    created = True
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            try:
                if created or index >= len(trusted.parts) - 1:
                    metadata = os.fstat(child)
                    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                        raise OSError("provider directory must be writable only by its owner")
                    ensure_no_extended_allow_acl(child)
                if created or index == len(absolute.parts) - 1:
                    os.fchmod(child, 0o700)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_regular(directory: int, name: str, flags: int) -> int:
    flags |= os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if flags & os.O_CREAT and not flags & os.O_EXCL:
        # A shared lock's first creation must be exclusive. Concurrent openat with
        # O_CREAT alone can return ENOENT on macOS even though another opener won.
        try:
            descriptor = os.open(name, flags | os.O_EXCL, 0o600, dir_fd=directory)
        except FileExistsError:
            descriptor = os.open(name, flags & ~os.O_CREAT, dir_fd=directory)
    else:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o022
        ):
            raise OSError("provider file must be an owner-only regular file")
        ensure_no_extended_allow_acl(descriptor)
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def locked_directory(root: Path) -> Iterator[int]:
    """Lock reads and complete mutations across processes and store instances."""
    directory = _open_directory(root / "providers", trusted_root=root)
    try:
        lock = _open_regular(directory, REGISTRY_NAME + ".lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield directory
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            os.close(lock)
    finally:
        os.close(directory)


def read_private(directory: int) -> str | None:
    """Read a size-bounded registry without following a substituted file."""
    try:
        descriptor = _open_regular(directory, REGISTRY_NAME, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        contents = stream.read(MAX_REGISTRY_BYTES + 1)
    if len(contents) > MAX_REGISTRY_BYTES:
        raise ValueError("provider registry is too large")
    return contents.decode("utf-8")


def replace_private(directory: int, contents: str) -> None:
    """Fsync a unique private temporary file, replace, then fsync its parent."""
    encoded = contents.encode("utf-8")
    if len(encoded) > MAX_REGISTRY_BYTES:
        raise ValueError("provider registry is too large")
    # A malformed existing target must not be silently replaced or chmod-ed.
    try:
        previous = _open_regular(directory, REGISTRY_NAME, os.O_RDONLY)
    except FileNotFoundError:
        pass
    else:
        os.close(previous)
    temporary = f".{REGISTRY_NAME}.{uuid.uuid4().hex}.tmp"
    descriptor = _open_regular(directory, temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, REGISTRY_NAME, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
