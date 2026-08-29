"""Deterministic, track-separated audio plans backed by verified immutable WAVs."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from narumi.bundle import Bundle
from narumi.errors import InvalidArgumentError
from narumi.transcribe._storage import (
    canonical_bytes,
    check_cancelled,
    read_bytes,
    storage_directory,
    storage_error,
    write_bytes,
)
from narumi.transcribe._wav import (
    SAMPLE_RATE,
    canonical_wave_header,
    changed_audio,
    open_wave_source,
    stream_wave,
)

CHUNK_SAMPLES = 9_600_000
MAX_CHUNKS = 144
MAX_TOTAL_SAMPLES = 1_382_400_000
MAX_AUDIO_BYTES = 24_000_000
CHUNKER_VERSION = "pcm16-mono16k-600s-v1"
PLAN_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PARAMETER_NAMES = frozenset(
    {
        "provider",
        "connection_id",
        "connection_revision",
        "model_id",
        "language",
        "effective_parameters",
        "adapter_version",
        "capability_table_version",
        "runtime_version",
        "runtime_sha256",
        "runtime_catalog_revision",
        "model_capabilities_sha256",
        "endpoint",
    }
)


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _invalid_plan() -> InvalidArgumentError:
    return InvalidArgumentError(
        "The audio transcription plan is invalid or exceeds the supported audio limits",
        details={"stage": "transcribe", "reason": "transcription_plan_invalid"},
    )


def _checked_params(params: dict[str, Any]) -> dict[str, Any]:
    if type(params) is not dict or set(params) != _PARAMETER_NAMES:
        raise _invalid_plan()
    if type(params["connection_revision"]) is not int or params["connection_revision"] < 1:
        raise _invalid_plan()
    if type(params["effective_parameters"]) is not dict:
        raise _invalid_plan()
    for name in _PARAMETER_NAMES - {"connection_revision", "effective_parameters"}:
        if type(params[name]) is not str or not 1 <= len(params[name]) <= 1024:
            raise _invalid_plan()
    for name in ("runtime_sha256", "model_capabilities_sha256"):
        if not _SHA256.fullmatch(params[name]):
            raise _invalid_plan()
    if params["provider"] != "openai-api" or params["endpoint"] != "https://api.openai.com":
        raise _invalid_plan()
    if not re.fullmatch(r"conn-[0-9a-f]{12,32}", params["connection_id"]):
        raise _invalid_plan()
    if not re.fullmatch(r"[a-z]{2}|auto", params["language"]):
        raise _invalid_plan()
    forbidden = {"epoch", "cache_epoch", "api_key", "authorization", "token", "secret", "timestamp"}

    def check(value: Any) -> None:
        if isinstance(value, dict):
            if any(type(key) is not str or key.lower() in forbidden for key in value):
                raise _invalid_plan()
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    try:
        check(params["effective_parameters"])
        canonical_bytes(params)
        return copy.deepcopy(params)
    except (RecursionError, TypeError, ValueError):
        raise _invalid_plan() from None


@dataclass(frozen=True)
class TranscriptionChunk:
    track: str
    index: int
    start_sample: int
    end_sample: int
    sample_rate: int
    source_sha256: str
    audio_sha256: str
    fingerprint: str
    path: Path
    _bundle_root: Path = field(repr=False, compare=False)

    @property
    def duration_sec(self) -> float:
        return (self.end_sample - self.start_sample) / self.sample_rate

    def as_payload(self) -> dict[str, Any]:
        return {
            "track": self.track,
            "index": self.index,
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "sample_rate": self.sample_rate,
            "source_sha256": self.source_sha256,
            "audio_sha256": self.audio_sha256,
            "fingerprint": self.fingerprint,
        }

    def read_audio(self) -> bytes:
        expected = self._bundle_root / "preprocess" / "transcription" / "chunks"
        if (
            not _SHA256.fullmatch(self.fingerprint)
            or self.path != expected / f"{self.fingerprint}.wav"
        ):
            raise storage_error()
        with storage_directory(self._bundle_root, "chunks") as directory:
            data = read_bytes(directory, self.path.name, MAX_AUDIO_BYTES)
        expected_length = 44 + (self.end_sample - self.start_sample) * 2
        if (
            data is None
            or len(data) != expected_length
            or len(data) > MAX_AUDIO_BYTES
            or self.sample_rate != SAMPLE_RATE
            or self.start_sample < 0
            or self.end_sample <= self.start_sample
            or hashlib.sha256(data).hexdigest() != self.audio_sha256
            or data[:44] != canonical_wave_header(len(data) - 44)
        ):
            raise storage_error()
        return data


@dataclass(frozen=True, init=False)
class TranscriptionPlan:
    input_fingerprint: str
    chunks: tuple[TranscriptionChunk, ...]
    total_samples: int
    _params: dict[str, Any] = field(repr=False)

    def __init__(
        self,
        input_fingerprint: str,
        chunks: tuple[TranscriptionChunk, ...],
        params: dict[str, Any],
        total_samples: int,
    ) -> None:
        object.__setattr__(self, "input_fingerprint", input_fingerprint)
        object.__setattr__(self, "chunks", tuple(chunks))
        object.__setattr__(self, "total_samples", total_samples)
        object.__setattr__(self, "_params", copy.deepcopy(params))
        self.validate()

    @property
    def params(self) -> dict[str, Any]:
        return copy.deepcopy(self._params)

    def validate(self) -> None:
        """Reject forged or changed plans before they can create a durable attempt."""
        params = _checked_params(self._params)
        if (
            type(self.total_samples) is not int
            or not 1 <= self.total_samples <= MAX_TOTAL_SAMPLES
            or type(self.chunks) is not tuple
            or not 1 <= len(self.chunks) <= MAX_CHUNKS
            or type(self.input_fingerprint) is not str
            or not _SHA256.fullmatch(self.input_fingerprint)
        ):
            raise _invalid_plan()
        previous: TranscriptionChunk | None = None
        total = 0
        root: Path | None = None
        for index, chunk in enumerate(self.chunks):
            if (
                not isinstance(chunk, TranscriptionChunk)
                or type(chunk.track) is not str
                or chunk.track not in {"mic", "system"}
            ):
                raise _invalid_plan()
            if any(
                type(value) is not int
                for value in (
                    chunk.index,
                    chunk.start_sample,
                    chunk.end_sample,
                    chunk.sample_rate,
                )
            ):
                raise _invalid_plan()
            length = chunk.end_sample - chunk.start_sample
            if (
                chunk.index != index
                or chunk.sample_rate != SAMPLE_RATE
                or chunk.start_sample < 0
                or not 1 <= length <= CHUNK_SAMPLES
                or 44 + length * 2 > MAX_AUDIO_BYTES
                or any(
                    type(value) is not str or not _SHA256.fullmatch(value)
                    for value in (
                        chunk.source_sha256,
                        chunk.audio_sha256,
                        chunk.fingerprint,
                    )
                )
            ):
                raise _invalid_plan()
            if not isinstance(chunk.path, Path) or not isinstance(chunk._bundle_root, Path):
                raise _invalid_plan()
            if not chunk._bundle_root.is_absolute() or chunk._bundle_root != Path(
                os.path.abspath(chunk._bundle_root)
            ):
                raise _invalid_plan()
            if root is None:
                root = chunk._bundle_root
            if (
                chunk._bundle_root != root
                or chunk.path
                != root / "preprocess" / "transcription" / "chunks" / f"{chunk.fingerprint}.wav"
            ):
                raise _invalid_plan()
            if previous is None or previous.track != chunk.track:
                if chunk.start_sample != 0 or (previous is not None and chunk.track != "system"):
                    raise _invalid_plan()
            elif (
                chunk.start_sample != previous.end_sample
                or chunk.source_sha256 != previous.source_sha256
                or previous.end_sample - previous.start_sample != CHUNK_SAMPLES
            ):
                raise _invalid_plan()
            if chunk.fingerprint != _chunk_fingerprint(
                chunk.track,
                chunk.start_sample,
                chunk.end_sample,
                chunk.source_sha256,
                chunk.audio_sha256,
                params,
            ):
                raise _invalid_plan()
            total += length
            previous = chunk
        if total != self.total_samples or self.input_fingerprint != _plan_fingerprint(self.chunks):
            raise _invalid_plan()

    def as_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "version": PLAN_VERSION,
            "chunker_version": CHUNKER_VERSION,
            "input_fingerprint": self.input_fingerprint,
            "params": self.params,
            "total_samples": self.total_samples,
            "chunks": [chunk.as_payload() for chunk in self.chunks],
        }


def _chunk_fingerprint(
    track: str,
    start: int,
    end: int,
    source_sha256: str,
    audio_sha256: str,
    params: dict[str, Any],
) -> str:
    return _hash(
        {
            "version": PLAN_VERSION,
            "chunker_version": CHUNKER_VERSION,
            "track": track,
            "start_sample": start,
            "end_sample": end,
            "sample_rate": SAMPLE_RATE,
            "source_sha256": source_sha256,
            "audio_sha256": audio_sha256,
            "params": params,
        }
    )


def _plan_fingerprint(chunks: tuple[TranscriptionChunk, ...] | list[TranscriptionChunk]) -> str:
    return _hash(
        {
            "version": PLAN_VERSION,
            "chunker_version": CHUNKER_VERSION,
            "chunks": [chunk.fingerprint for chunk in chunks],
        }
    )


def _finish_track(
    directory: int,
    root: Path,
    track: str,
    first_index: int,
    source_sha256: str,
    pending: list[tuple[str, int, int, str]],
    params: dict[str, Any],
    should_cancel: Callable[[], bool] | None,
) -> list[TranscriptionChunk]:
    chunks = []
    for name, start, end, audio_sha256 in pending:
        check_cancelled(should_cancel)
        audio = read_bytes(directory, name, MAX_AUDIO_BYTES)
        if audio is None or hashlib.sha256(audio).hexdigest() != audio_sha256:
            raise storage_error()
        fingerprint = _chunk_fingerprint(track, start, end, source_sha256, audio_sha256, params)
        filename = f"{fingerprint}.wav"
        write_bytes(directory, filename, audio, immutable=True)
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)
        chunks.append(
            TranscriptionChunk(
                track,
                first_index + len(chunks),
                start,
                end,
                SAMPLE_RATE,
                source_sha256,
                audio_sha256,
                fingerprint,
                root / "preprocess" / "transcription" / "chunks" / filename,
                root,
            )
        )
    return chunks


def build_transcription_plan(
    bundle: Bundle,
    *,
    sources: dict[str, Path],
    params: dict[str, Any],
    expected_hashes: dict[str, str] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> TranscriptionPlan:
    """Validate all inputs before returning a plan that may be used for network calls."""
    check_cancelled(should_cancel)
    params = _checked_params(params)
    if (
        type(sources) is not dict
        or not sources
        or set(sources) - {"mic", "system"}
        or any(not isinstance(path, Path) for path in sources.values())
    ):
        raise _invalid_plan()
    if expected_hashes is not None and (
        type(expected_hashes) is not dict
        or set(expected_hashes) != set(sources)
        or any(
            type(value) is not str or not _SHA256.fullmatch(value)
            for value in expected_hashes.values()
        )
    ):
        raise _invalid_plan()
    sources = dict(sources)
    expected_hashes = dict(expected_hashes) if expected_hashes is not None else None
    root = bundle.path.resolve()
    chunks: list[TranscriptionChunk] = []
    with ExitStack() as stack:
        ordered = [
            (
                track,
                stack.enter_context(
                    open_wave_source(
                        bundle.path,
                        sources[track],
                        should_cancel,
                    )
                ),
            )
            for track in ("mic", "system")
            if track in sources
        ]
        total_samples = sum(source.sample_count for _, source in ordered)
        count = sum(
            (source.sample_count + CHUNK_SAMPLES - 1) // CHUNK_SAMPLES for _, source in ordered
        )
        if total_samples > MAX_TOTAL_SAMPLES or count > MAX_CHUNKS:
            raise _invalid_plan()
        if any(
            44 + min(source.sample_count, CHUNK_SAMPLES) * 2 > MAX_AUDIO_BYTES
            for _, source in ordered
        ):
            raise _invalid_plan()
        with storage_directory(bundle, "chunks") as directory:
            temporary_names: list[str] = []
            try:
                for track, source in ordered:
                    check_cancelled(should_cancel)
                    pending: list[tuple[str, int, int, str]] = []

                    def save(
                        start: int,
                        end: int,
                        audio: bytes,
                        *,
                        pending: list[tuple[str, int, int, str]] = pending,
                    ) -> None:
                        check_cancelled(should_cancel)
                        if len(audio) > MAX_AUDIO_BYTES:
                            raise _invalid_plan()
                        name = f".pending-{uuid.uuid4().hex}.wav"
                        temporary_names.append(name)
                        write_bytes(directory, name, audio, immutable=True)
                        pending.append((name, start, end, hashlib.sha256(audio).hexdigest()))

                    source_hash = stream_wave(
                        source,
                        chunk_samples=CHUNK_SAMPLES,
                        on_chunk=save,
                        should_cancel=should_cancel,
                    )
                    if expected_hashes is not None and source_hash != expected_hashes[track]:
                        raise changed_audio()
                    chunks.extend(
                        _finish_track(
                            directory,
                            root,
                            track,
                            len(chunks),
                            source_hash,
                            pending,
                            params,
                            should_cancel,
                        )
                    )
                    check_cancelled(should_cancel)
                for _, source in ordered:
                    source.check_unchanged()
            except OSError:
                raise storage_error() from None
            finally:
                try:
                    for name in temporary_names:
                        try:
                            os.unlink(name, dir_fd=directory)
                        except FileNotFoundError:
                            pass
                    os.fsync(directory)
                except OSError:
                    raise storage_error() from None
    check_cancelled(should_cancel)
    fingerprint = _plan_fingerprint(chunks)
    return TranscriptionPlan(fingerprint, tuple(chunks), params, total_samples)
