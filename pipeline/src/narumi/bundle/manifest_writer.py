"""Process-shared meeting manifest writer fence and atomic CAS persistence."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import math
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from narumi.errors import (
    BusyError,
    ConfigurationConflictError,
    ErrorCode,
    InvalidArgumentError,
    NarumiError,
)

MEETING_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
LOCK_DIRECTORY_NAME = ".manifest-locks"
LOCK_TIMEOUT_SECONDS = 5.0
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ManifestSnapshot:
    contents: bytes
    sha256: str


@dataclass
class _Lease:
    key: tuple[str, str]
    pid: int
    thread: int
    task: int | None
    descriptor: int
    active: bool = True


_LEASES: ContextVar[tuple[_Lease, ...]] = ContextVar("manifest_writer_leases", default=())
_STATE_GUARD = threading.RLock()
_MUTEXES: dict[tuple[str, str], threading.Lock] = {}
_ACTIVE_DESCRIPTORS: set[int] = set()


def _before_fork() -> None:
    _STATE_GUARD.acquire()


def _after_fork_parent() -> None:
    _STATE_GUARD.release()


def _after_fork_child() -> None:
    global _ACTIVE_DESCRIPTORS, _MUTEXES, _STATE_GUARD
    for descriptor in tuple(_ACTIVE_DESCRIPTORS):
        try:
            os.close(descriptor)
        except OSError:
            pass
    _ACTIVE_DESCRIPTORS = set()
    _MUTEXES = {}
    _STATE_GUARD = threading.RLock()
    _LEASES.set(())


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_parent,
        after_in_child=_after_fork_child,
    )


def _storage_error() -> NarumiError:
    return NarumiError("Meeting data could not be saved securely", code=ErrorCode.INTERNAL)


def _conflict() -> ConfigurationConflictError:
    return ConfigurationConflictError(
        "Meeting data changed; reload it before saving",
        details={"reason": "manifest_generation_conflict"},
    )


def _outcome_unknown() -> ConfigurationConflictError:
    return ConfigurationConflictError(
        "The meeting save outcome is unknown; reload it before saving again",
        details={"reason": "manifest_save_outcome_unknown", "outcome_unknown": True},
    )


def _validate_meeting_id(meeting_id: str) -> None:
    if not isinstance(meeting_id, str) or MEETING_ID_RE.fullmatch(meeting_id) is None:
        raise InvalidArgumentError("invalid meeting_id")


def _task_identity() -> int | None:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None
    return None if task is None else id(task)


def _canonical_root(meetings_root: Path) -> Path:
    return Path(os.path.abspath(os.fspath(meetings_root)))


def _check_directory(descriptor: int, *, private: bool) -> None:
    metadata = os.fstat(descriptor)
    forbidden = 0o077 if private else 0o022
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & forbidden
    ):
        raise OSError("unsafe meeting directory")


def _open_root(root: Path, *, create: bool) -> int:
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(root, _DIRECTORY_FLAGS)
    try:
        _check_directory(descriptor, private=False)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_lock_directory(root: Path) -> int:
    root_descriptor = _open_root(root, create=True)
    directory: int | None = None
    try:
        created = False
        try:
            os.mkdir(LOCK_DIRECTORY_NAME, mode=0o700, dir_fd=root_descriptor)
            created = True
        except FileExistsError:
            pass
        directory = os.open(LOCK_DIRECTORY_NAME, _DIRECTORY_FLAGS, dir_fd=root_descriptor)
        _check_directory(directory, private=True)
        if created:
            os.fsync(root_descriptor)
    except BaseException:
        if directory is not None:
            try:
                os.close(directory)
            except OSError:
                pass
        try:
            os.close(root_descriptor)
        except OSError:
            pass
        raise
    try:
        os.close(root_descriptor)
    except BaseException:
        try:
            os.close(directory)
        except OSError:
            pass
        raise
    assert directory is not None
    return directory


def _open_regular(
    directory: int,
    name: str,
    flags: int,
    *,
    create: bool = False,
    exclusive: bool = False,
    private: bool = False,
    track_fork: bool = False,
) -> int:
    flags |= _FILE_FLAGS
    if exclusive:
        descriptor = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    elif create:
        try:
            descriptor = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        except FileExistsError:
            descriptor = os.open(name, flags, dir_fd=directory)
    else:
        descriptor = os.open(name, flags, dir_fd=directory)
    if track_fork:
        _ACTIVE_DESCRIPTORS.add(descriptor)
    try:
        metadata = os.fstat(descriptor)
        forbidden = 0o077 if private else 0o022
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & forbidden
        ):
            raise OSError("unsafe meeting file")
        return descriptor
    except BaseException:
        if track_fork:
            _ACTIVE_DESCRIPTORS.discard(descriptor)
        os.close(descriptor)
        raise


def _mutex_for(key: tuple[str, str]) -> threading.Lock:
    with _STATE_GUARD:
        mutex = _MUTEXES.get(key)
        if mutex is None:
            mutex = threading.Lock()
            _MUTEXES[key] = mutex
        return mutex


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def _acquire_mutex(mutex: threading.Lock, deadline: float | None) -> bool:
    remaining = _remaining(deadline)
    return mutex.acquire() if remaining is None else mutex.acquire(timeout=remaining)


def _acquire_flock(descriptor: int, deadline: float | None) -> None:
    if deadline is None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = _remaining(deadline)
            if remaining == 0:
                raise BusyError("Meeting data is being updated; retry the operation") from None
            time.sleep(min(0.01, remaining))


def _close_tracked(descriptor: int) -> bool:
    """Close once while fork is excluded; never risk closing a reused descriptor."""
    failed = False
    with _STATE_GUARD:
        try:
            os.close(descriptor)
        except OSError:
            failed = True
        finally:
            _ACTIVE_DESCRIPTORS.discard(descriptor)
    return failed


@contextmanager
def manifest_writer_lock(
    meetings_root: Path, meeting_id: str, *, timeout: float | None = LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Acquire the process-local mutex before the process-shared meeting lock."""
    _validate_meeting_id(meeting_id)
    if timeout is not None:
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise InvalidArgumentError("invalid manifest lock timeout")
    root = _canonical_root(meetings_root)
    key = (str(root), meeting_id)
    pid, thread, task = os.getpid(), threading.get_ident(), _task_identity()
    for lease in reversed(_LEASES.get()):
        if (
            lease.active
            and lease.pid == pid
            and lease.thread == thread
            and lease.task == task
            and lease.key == key
        ):
            yield
            return

    deadline = None if timeout is None else time.monotonic() + float(timeout)
    mutex = _mutex_for(key)
    if not _acquire_mutex(mutex, deadline):
        raise BusyError("Meeting data is being updated; retry the operation")

    descriptor: int | None = None
    flock_acquired = False
    try:
        try:
            _STATE_GUARD.acquire()
            try:
                directory = _open_lock_directory(root)
                try:
                    descriptor = _open_regular(
                        directory,
                        f"{meeting_id}.lock",
                        os.O_RDWR,
                        create=True,
                        private=True,
                        track_fork=True,
                    )
                finally:
                    os.close(directory)
            finally:
                _STATE_GUARD.release()
            _acquire_flock(descriptor, deadline)
            flock_acquired = True
        except (BusyError, InvalidArgumentError):
            raise
        except OSError:
            raise _storage_error() from None

        lease = _Lease(key, pid, thread, task, descriptor)
        token = _LEASES.set((*_LEASES.get(), lease))
        body_failed = False
        try:
            yield
        except BaseException:
            body_failed = True
            raise
        finally:
            if os.getpid() == pid:
                lease.active = False
                _LEASES.reset(token)
                release_failed = False
                try:
                    if flock_acquired:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    release_failed = True
                flock_acquired = False
                release_failed = _close_tracked(descriptor) or release_failed
                descriptor = None
                if release_failed and not body_failed:
                    raise _storage_error()
    finally:
        if os.getpid() == pid:
            try:
                if descriptor is not None:
                    if flock_acquired:
                        try:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                        except OSError:
                            pass
                    _close_tracked(descriptor)
            finally:
                mutex.release()


def _bundle_parts(bundle_path: Path) -> tuple[Path, str]:
    absolute = Path(os.path.abspath(os.fspath(bundle_path)))
    meeting_id = absolute.name
    _validate_meeting_id(meeting_id)
    return absolute.parent, meeting_id


def _open_bundle_directory(root: Path, meeting_id: str) -> int:
    root_descriptor = _open_root(root, create=False)
    directory: int | None = None
    try:
        directory = os.open(meeting_id, _DIRECTORY_FLAGS, dir_fd=root_descriptor)
        _check_directory(directory, private=False)
    except BaseException:
        if directory is not None:
            try:
                os.close(directory)
            except OSError:
                pass
        try:
            os.close(root_descriptor)
        except OSError:
            pass
        raise
    try:
        os.close(root_descriptor)
    except BaseException:
        try:
            os.close(directory)
        except OSError:
            pass
        raise
    assert directory is not None
    return directory


def _read_manifest_at(directory: int) -> ManifestSnapshot | None:
    try:
        descriptor = _open_regular(directory, "manifest.json", os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        contents = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(contents) > MAX_MANIFEST_BYTES:
        raise OSError("manifest is too large")
    return ManifestSnapshot(contents, hashlib.sha256(contents).hexdigest())


def read_manifest_snapshot(bundle_path: Path) -> ManifestSnapshot | None:
    """Read one complete manifest without following substituted files."""
    root, meeting_id = _bundle_parts(bundle_path)
    try:
        directory = _open_bundle_directory(root, meeting_id)
        try:
            return _read_manifest_at(directory)
        finally:
            os.close(directory)
    except FileNotFoundError:
        return None
    except (InvalidArgumentError, NarumiError):
        raise
    except OSError:
        raise _storage_error() from None


def _visible_manifest(bundle_path: Path) -> tuple[tuple[int, int], ManifestSnapshot | None]:
    root, meeting_id = _bundle_parts(bundle_path)
    directory = _open_bundle_directory(root, meeting_id)
    try:
        metadata = os.fstat(directory)
        return (metadata.st_dev, metadata.st_ino), _read_manifest_at(directory)
    finally:
        os.close(directory)


def sync_meetings_root(meetings_root: Path) -> None:
    """Durably publish a newly created or removed meeting directory entry."""
    try:
        descriptor = _open_root(_canonical_root(meetings_root), create=False)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise _storage_error() from None


def assert_manifest_generation(bundle_path: Path, expected_sha256: str | None) -> None:
    root, meeting_id = _bundle_parts(bundle_path)
    with manifest_writer_lock(root, meeting_id):
        current = read_manifest_snapshot(bundle_path)
        if (None if current is None else current.sha256) != expected_sha256:
            raise _conflict()


def replace_manifest_bytes(
    bundle_path: Path, contents: bytes, *, expected_sha256: str | None
) -> str:
    """Replace the manifest iff its exact raw byte generation still matches."""
    if not isinstance(contents, bytes) or len(contents) > MAX_MANIFEST_BYTES:
        raise _storage_error()
    if expected_sha256 is not None and _HASH_RE.fullmatch(expected_sha256) is None:
        raise InvalidArgumentError("invalid manifest generation")
    root, meeting_id = _bundle_parts(bundle_path)
    with manifest_writer_lock(root, meeting_id):
        try:
            directory = _open_bundle_directory(root, meeting_id)
        except OSError:
            raise _storage_error() from None
        try:
            metadata = os.fstat(directory)
            directory_identity = (metadata.st_dev, metadata.st_ino)
            current = _read_manifest_at(directory)
        except OSError:
            try:
                os.close(directory)
            except OSError:
                pass
            raise _storage_error() from None
        if (None if current is None else current.sha256) != expected_sha256:
            try:
                os.close(directory)
            except OSError:
                pass
            raise _conflict()
        temporary = f".manifest.json.{uuid.uuid4().hex}.tmp"
        publishing = False
        temporary_created = False
        try:
            descriptor = _open_regular(
                directory, temporary, os.O_WRONLY, exclusive=True, private=True
            )
            temporary_created = True
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            publishing = True
            os.replace(temporary, "manifest.json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            generation = hashlib.sha256(contents).hexdigest()
            try:
                visible_identity, visible = _visible_manifest(bundle_path)
            except (NarumiError, OSError):
                raise _outcome_unknown() from None
            if (
                visible_identity != directory_identity
                or visible is None
                or visible.sha256 != generation
            ):
                raise _outcome_unknown()
            return generation
        except OSError:
            raise (_outcome_unknown() if publishing else _storage_error()) from None
        finally:
            if temporary_created:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
            os.close(directory)
