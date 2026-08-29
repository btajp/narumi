"""Native ASR timing projection and immutable, completed-track publication."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from narumi.bundle import Bundle, StageResult, sha256_bytes
from narumi.errors import EngineUnavailableError
from narumi.models import EngineInfo, Segment, Transcript, Word
from narumi.providers._io import _open_directory
from narumi.transcribe._storage import read_bytes, write_bytes

if TYPE_CHECKING:
    from narumi.providers.audio_response import AudioTranscriptionResult
    from narumi.transcribe.chunks import TranscriptionChunk

ASSEMBLER_VERSION = "1"


def project_segments(
    result: AudioTranscriptionResult,
    chunk: TranscriptionChunk,
    *,
    source_id: str,
    first_index: int,
    track_chunk_index: int,
) -> list[Segment]:
    """Keep native boundaries; move each segment/word onto the track clock once."""
    offset = chunk.start_sample / chunk.sample_rate
    segments = [
        Segment(
            id=f"{source_id}:{first_index + index}",
            start=segment.start + offset,
            end=segment.end + offset,
            text=segment.text,
            speaker=(
                f"asr:{chunk.track}:{track_chunk_index}:{segment.speaker}"
                if segment.speaker is not None
                else None
            ),
            words=[] if result.words is not None else None,
        )
        for index, segment in enumerate(result.segments)
    ]
    for word in result.words or ():
        # Whisper supplies words at response level. Associate each with one native
        # segment using containment, then greatest overlap; never invent boundaries.
        candidates = [
            (
                segment.start <= word.start and word.end <= segment.end,
                max(0.0, min(word.end, segment.end) - max(word.start, segment.start)),
                -index,
            )
            for index, segment in enumerate(result.segments)
        ]
        if not candidates:
            raise _invalid_words()
        contained, shared, negative_index = max(candidates)
        if not contained and shared <= 0:
            raise _invalid_words()
        target = segments[-negative_index]
        assert target.words is not None
        target.words.append(Word(start=word.start + offset, end=word.end + offset, text=word.text))
    return segments


def _invalid_words() -> EngineUnavailableError:
    return EngineUnavailableError(
        "The transcription words do not match native segment timing",
        details={"reason": "transcription_projection_invalid"},
    )


def publish_transcript(
    bundle: Bundle,
    *,
    track: str,
    language: str,
    model_id: str,
    params: dict[str, Any],
    chunks: list[TranscriptionChunk],
    segments: list[Segment],
) -> StageResult:
    """Publish only a complete track, retaining previous immutable source files."""
    source_id = f"own-{track}"
    provenance = {
        "model": model_id,
        "transcription_params": copy.deepcopy(params),
        "chunk_fingerprints": [chunk.fingerprint for chunk in chunks],
        "timing_source": "native",
    }
    transcript = Transcript(
        source_id=source_id,
        kind="own",
        track=track,
        engine=EngineInfo(name="openai-api", version=ASSEMBLER_VERSION, params=provenance),
        language=language,
        time_offset=0.0,
        segments=segments,
    )
    payload = (transcript.model_dump_json(indent=2) + "\n").encode("utf-8")
    digest = sha256_bytes(payload)
    output = f"transcripts/{source_id}/{digest}.json"
    path = bundle.abspath(output)

    try:
        directory = _open_directory(path.parent, trusted_root=bundle.path)
        try:
            # Bundle.run_stage only tests existence on skip. Verify the completed
            # bytes too, without following links or reading an unbounded file.
            existing = read_bytes(directory, path.name, len(payload))
            if existing is not None and existing != payload:
                raise EngineUnavailableError(
                    "The completed transcription artifact could not be verified",
                    details={"reason": "transcription_artifact_unavailable"},
                )

            def write(_out: Path) -> None:
                write_bytes(directory, path.name, payload, immutable=True)

            result = bundle.run_stage(
                f"transcripts/{source_id}",
                inputs={f"preprocess/audio/{track}": chunks[0].source_sha256},
                params={"engine": "openai-api", "version": ASSEMBLER_VERSION, **provenance},
                producer=("openai-api", ASSEMBLER_VERSION),
                output=output,
                fn=write,
            )
        finally:
            os.close(directory)
        if not result.skipped:
            _sync(bundle.manifest_path, bundle.path)
        return result
    except OSError:
        # The chunk receipts remain verified success. A later run can finish
        # publication without resending audio; do not expose filesystem paths.
        raise EngineUnavailableError(
            "The completed transcription could not be published",
            details={"reason": "transcription_artifact_unavailable"},
        ) from None


def _sync(path: Path, root: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    for parent in path.parents:
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if parent == root:
            break
