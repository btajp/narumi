"""Strict normalized WAV reads without path traversal or a second hashing read."""

from __future__ import annotations

import hashlib
import os
import stat
import struct
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from narumi.errors import ConfigurationConflictError, InvalidArgumentError
from narumi.providers._acl import ensure_no_extended_allow_acl
from narumi.transcribe._storage import check_cancelled

SAMPLE_RATE = 16_000
SAMPLE_BYTES = 2
HASH_READ_BYTES = 1024 * 1024
# Normalized ffmpeg WAVs need only a small fmt/LIST header. Bound unneeded metadata too.
MAX_METADATA_BYTES = 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def invalid_audio() -> InvalidArgumentError:
    return InvalidArgumentError(
        "Audio transcription requires a complete, non-empty mono 16 kHz PCM16 WAV",
        details={"stage": "transcribe", "reason": "transcription_audio_invalid"},
    )


def changed_audio() -> ConfigurationConflictError:
    return ConfigurationConflictError(
        "The preprocessed audio changed before transcription; no audio was sent",
        details={"stage": "transcribe", "reason": "transcription_input_changed"},
    )


def _signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_nlink,
        value.st_uid,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _check_owner(descriptor: int, *, regular: bool = False) -> None:
    value = os.fstat(descriptor)
    if value.st_uid != os.geteuid() or value.st_mode & 0o022:
        raise invalid_audio()
    if regular and (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1):
        raise invalid_audio()
    ensure_no_extended_allow_acl(descriptor)


def _open_source(root: Path, path: Path) -> int:
    # The bundle itself is the trusted anchor; do not resolve any source descendants.
    lexical_root = Path(os.path.abspath(root))
    canonical_root = lexical_root.resolve(strict=True)
    candidate = Path(os.path.abspath(path if path.is_absolute() else lexical_root / path))
    try:
        relative = candidate.relative_to(lexical_root)
    except ValueError:
        relative = candidate.relative_to(canonical_root)
    if not relative.parts:
        raise invalid_audio()
    absolute = canonical_root / relative
    directory = os.open(absolute.anchor, _DIR_FLAGS)
    try:
        for index, component in enumerate(absolute.parts[1:-1], start=1):
            child = os.open(component, _DIR_FLAGS, dir_fd=directory)
            os.close(directory)
            directory = child
            if index >= len(canonical_root.parts) - 1:
                _check_owner(directory)
        descriptor = os.open(
            absolute.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
        try:
            _check_owner(descriptor, regular=True)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(directory)


@dataclass(frozen=True)
class WaveSource:
    stream: BinaryIO
    sample_count: int
    data_offset: int
    file_size: int
    signature: tuple[int, ...]

    def check_unchanged(self) -> None:
        if _signature(os.fstat(self.stream.fileno())) != self.signature:
            raise changed_audio()


def _inspect(stream: BinaryIO, should_cancel: Callable[[], bool] | None) -> WaveSource:
    metadata = os.fstat(stream.fileno())
    signature = _signature(metadata)
    header = stream.read(12)
    if len(header) != 12:
        raise invalid_audio()
    kind, riff_size, wave = struct.unpack("<4sI4s", header)
    if kind != b"RIFF" or wave != b"WAVE" or riff_size + 8 != metadata.st_size:
        raise invalid_audio()
    position, data_offset, data_size = 12, None, None
    seen_format = False
    while position < metadata.st_size:
        check_cancelled(should_cancel)
        if metadata.st_size - position < 8:
            raise invalid_audio()
        stream.seek(position)
        chunk_header = stream.read(8)
        if len(chunk_header) != 8:
            raise invalid_audio()
        chunk_kind, size = struct.unpack("<4sI", chunk_header)
        end = position + 8 + size + (size % 2)
        if end > metadata.st_size:
            raise invalid_audio()
        if chunk_kind == b"fmt ":
            if seen_format or size not in (16, 18) or data_offset is not None:
                raise invalid_audio()
            contents = stream.read(size)
            if len(contents) != size:
                raise invalid_audio()
            if struct.unpack("<HHIIHH", contents[:16]) != (1, 1, SAMPLE_RATE, 32_000, 2, 16):
                raise invalid_audio()
            if size == 18 and contents[16:] != b"\0\0":
                raise invalid_audio()
            seen_format = True
        elif chunk_kind == b"data":
            if not seen_format or data_offset is not None or size == 0 or size % SAMPLE_BYTES:
                raise invalid_audio()
            data_offset, data_size = position + 8, size
        position = end
        # This also bounds the number of zero-length RIFF chunks scanned.
        if position - (data_size or 0) > MAX_METADATA_BYTES:
            raise invalid_audio()
    if data_offset is None or data_size is None or position != metadata.st_size:
        raise invalid_audio()
    source = WaveSource(stream, data_size // SAMPLE_BYTES, data_offset, metadata.st_size, signature)
    source.check_unchanged()
    return source


@contextmanager
def open_wave_source(
    root: Path, path: Path, should_cancel: Callable[[], bool] | None = None
) -> Iterator[WaveSource]:
    check_cancelled(should_cancel)
    try:
        descriptor = _open_source(Path(root), Path(path))
    except (OSError, ValueError):
        raise invalid_audio() from None
    try:
        with os.fdopen(descriptor, "rb", buffering=0) as stream:
            yield _inspect(stream, should_cancel)
    except OSError:
        raise invalid_audio() from None


def canonical_wave_header(sample_bytes: int) -> bytes:
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + sample_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        SAMPLE_RATE,
        32_000,
        2,
        16,
        b"data",
        sample_bytes,
    )


def canonical_wave(samples: bytes) -> bytes:
    return canonical_wave_header(len(samples)) + samples


def stream_wave(
    source: WaveSource,
    *,
    chunk_samples: int,
    on_chunk: Callable[[int, int, bytes], None],
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    """Hash every source byte exactly where its sample bytes are copied."""
    source.check_unchanged()
    source.stream.seek(0)
    digest = hashlib.sha256()

    def read_hashed(size: int, *, retain: bool = False) -> bytes:
        contents = bytearray()
        while size:
            check_cancelled(should_cancel)
            part = source.stream.read(min(size, HASH_READ_BYTES))
            if not part:
                raise changed_audio()
            digest.update(part)
            if retain:
                contents.extend(part)
            size -= len(part)
        return bytes(contents)

    read_hashed(source.data_offset)
    for start in range(0, source.sample_count, chunk_samples):
        check_cancelled(should_cancel)
        end = min(source.sample_count, start + chunk_samples)
        samples = read_hashed((end - start) * SAMPLE_BYTES, retain=True)
        on_chunk(start, end, canonical_wave(samples))
        check_cancelled(should_cancel)
    tail = source.file_size - source.data_offset - source.sample_count * SAMPLE_BYTES
    read_hashed(tail)
    if source.stream.read(1):
        raise changed_audio()
    source.check_unchanged()
    check_cancelled(should_cancel)
    return digest.hexdigest()
