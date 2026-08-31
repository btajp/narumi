"""Durable, bounded ASR storage and a process-shared execution lease."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from narumi.bundle import Bundle
from narumi.errors import BusyError, CancelledError, EngineUnavailableError
from narumi.providers._io import _open_directory, _open_regular

MAX_JSON_BYTES = 16 * 1024 * 1024
_NAMES = frozenset({"chunks", "plans", "results"})
_FILE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
_TEMPORARY_NAME = re.compile(
    r"^\.(?:ledger\.json|[0-9a-f]{64}(?:-[0-9a-f]{32})?\.json|"
    r"[0-9a-f]{64}\.wav|\.pending-[0-9a-f]{32}\.wav)\.[0-9a-f]{32}\.tmp$"
)
_STAGING_NAME = re.compile(r"^\.pending-[0-9a-f]{32}\.wav$")


def storage_error() -> EngineUnavailableError:
    return EngineUnavailableError(
        "The transcription checkpoint could not be verified; no automatic resend is allowed",
        details={"stage": "transcribe", "reason": "transcription_checkpoint_unavailable"},
    )


def check_cancelled(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise CancelledError("Audio transcription was cancelled")


@contextmanager
def storage_directory(bundle: Bundle | Path, name: str | None = None) -> Iterator[int]:
    """Open a checked directory without following links below the bundle root."""
    if name is not None and name not in _NAMES:
        raise storage_error()
    root = (bundle.path if isinstance(bundle, Bundle) else Path(bundle)).resolve()
    path = root / "preprocess" / "transcription"
    if name is not None:
        path /= name
    try:
        descriptor = _open_directory(path, trusted_root=root)
    except (OSError, ValueError):
        raise storage_error() from None
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _name(name: str) -> str:
    if not isinstance(name, str) or not _FILE_NAME.fullmatch(name) or name in {".", ".."}:
        raise storage_error()
    return name


def read_bytes(directory: int, name: str, max_bytes: int = MAX_JSON_BYTES) -> bytes | None:
    try:
        descriptor = _open_regular(directory, _name(name), os.O_RDONLY)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise storage_error() from None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            contents = stream.read(max_bytes + 1)
        if len(contents) > max_bytes:
            raise storage_error()
        return contents
    except OSError:
        raise storage_error() from None


def write_bytes(directory: int, name: str, data: bytes, *, immutable: bool = False) -> None:
    """Fsync a unique temporary file, install it atomically, then fsync its directory."""
    name = _name(name)
    existing = read_bytes(directory, name, max(len(data), MAX_JSON_BYTES))
    if immutable and existing is not None:
        if existing != data:
            raise storage_error()
        return
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = _open_regular(directory, temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # The execution lease excludes other writers. A rename keeps the installed file
        # single-linked even if the process exits before temporary-file cleanup.
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
            os.fsync(directory)
        except FileNotFoundError:
            pass


def cleanup_temporaries(directory: int, *, chunks: bool = False) -> None:
    """Reclaim only our checked, uncommitted temporary files under the execution lease."""
    removed = False
    try:
        for name in os.listdir(directory):
            if _TEMPORARY_NAME.fullmatch(name) is None and not (
                chunks and _STAGING_NAME.fullmatch(name) is not None
            ):
                continue
            descriptor = _open_regular(directory, name, os.O_RDONLY)
            os.close(descriptor)
            os.unlink(name, dir_fd=directory)
            removed = True
        if removed:
            os.fsync(directory)
    except OSError:
        raise storage_error() from None


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise storage_error() from None


def strict_json(contents: bytes) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(_value: str) -> Any:
        raise ValueError

    def finite_float(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError
        return result

    try:
        return json.loads(
            contents.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=invalid_constant,
            parse_float=finite_float,
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise storage_error() from None


@dataclass
class _ExecutionLease:
    root: Path
    pid: int
    thread: int
    active: bool = True


_LEASE: ContextVar[_ExecutionLease | None] = ContextVar(
    "transcription_execution_lease", default=None
)


def require_execution_lease(bundle: Bundle) -> None:
    lease = _LEASE.get()
    if (
        lease is None
        or not lease.active
        or lease.pid != os.getpid()
        or lease.thread != threading.get_ident()
        or lease.root != bundle.path.resolve()
    ):
        raise BusyError("Audio transcription requires its exclusive execution lease")


@contextmanager
def transcription_execution_lock(bundle: Bundle) -> Iterator[None]:
    """Hold through plan creation, HTTP calls and completed transcript publication."""
    with storage_directory(bundle) as directory:
        try:
            descriptor = _open_regular(directory, "execution.lock", os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                raise BusyError("Audio transcription is already running for this meeting") from None
            except BaseException:
                os.close(descriptor)
                raise
        except OSError:
            raise storage_error() from None
        lease = _ExecutionLease(bundle.path.resolve(), os.getpid(), threading.get_ident())
        token = _LEASE.set(lease)
        try:
            cleanup_temporaries(directory)
            with storage_directory(bundle, "chunks") as chunks:
                cleanup_temporaries(chunks, chunks=True)
            yield
        finally:
            lease.active = False
            _LEASE.reset(token)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


@contextmanager
def locked_ledger(bundle: Bundle) -> Iterator[int]:
    require_execution_lease(bundle)
    with storage_directory(bundle) as directory:
        try:
            descriptor = _open_regular(directory, "ledger.lock", os.O_CREAT | os.O_RDWR)
        except OSError:
            raise storage_error() from None
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError:
                raise storage_error() from None
            try:
                yield directory
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
