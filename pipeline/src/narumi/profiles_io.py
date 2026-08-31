"""Atomic profile writes with a bounded lock shared by processes and store instances."""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from narumi.errors import BusyError, ConfigurationConflictError, ErrorCode, NarumiError

LOCK_TIMEOUT_SECONDS = 2.0


@contextmanager
def write_lock(path: Path) -> Iterator[None]:
    """Keep load, comparison and replacement in one process-shared critical section."""
    descriptor: int | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            path.with_name(path.name + ".lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
        ):
            raise OSError("invalid profile lock")
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BusyError(
                        "Profile settings are being updated; retry the operation"
                    ) from None
                time.sleep(0.01)
    except BaseException as exc:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(exc, OSError):
            raise NarumiError(
                "Profile settings could not be locked", code=ErrorCode.INTERNAL
            ) from None
        raise
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def replace_file(path: Path, contents: str) -> None:
    """Publish complete settings only after syncing a unique temporary file."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    publishing = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        publishing = True
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        if publishing:
            raise ConfigurationConflictError(
                "Profile settings may already have been saved; reload before retrying",
                details={"reason": "profile_save_outcome_unknown", "outcome_unknown": True},
            ) from None
        raise
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            # Cleanup cannot turn a published/unknown save into a known failure,
            # nor hide the original write error. Any remaining temp file is private.
            pass
