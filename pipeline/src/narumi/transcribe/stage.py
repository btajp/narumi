"""``run_transcribe``: ``transcripts/own-<track>.json`` from every preprocessed audio track.

External (``ext-<context_id>``) transcripts are *not* produced here; they will come from the
context parsers of a later step and share the same ``Transcript`` model.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from narumi.bundle import Bundle, StageResult
from narumi.errors import ErrorCode, InvalidArgumentError, NarumiError
from narumi.models import EngineInfo, Segment, Transcript
from narumi.preprocess.stage import AUDIO_TRACKS, audio_artifact_key
from narumi.transcribe.base import TranscriptionEngine, segment_id
from narumi.transcribe.policy import check_send_policy
from narumi.transcribe.registry import get_engine

if TYPE_CHECKING:
    from narumi.providers.transcription import TranscriptionResolver
    from narumi.transcription_selection import TranscriptionRetry


def own_source_id(track: str) -> str:
    """Transcript ``source_id`` of a track (``own-<track>``)."""
    return f"own-{track}"


def transcript_artifact_key(track: str) -> str:
    """Manifest artifact key (``transcripts/own-<track>``)."""
    return f"transcripts/{own_source_id(track)}"


def transcript_output_path(track: str) -> str:
    """Bundle-relative path (``transcripts/own-<track>.json``)."""
    return f"transcripts/{own_source_id(track)}.json"


def validate_segment_ids(segments: list[Segment], *, source_id: str, engine: str) -> None:
    """Enforce the ``<source_id>:<index>`` id contract on engine output."""
    for index, segment in enumerate(segments):
        expected = segment_id(source_id, index)
        if segment.id != expected:
            raise NarumiError(
                f"engine {engine!r} returned segment id {segment.id!r}, expected {expected!r}",
                code=ErrorCode.INTERNAL,
                details={"engine": engine, "source_id": source_id, "index": index},
            )


def transcribe_track(
    engine: TranscriptionEngine,
    wav: Path,
    *,
    track: str,
    language: str,
    vocab_hints: list[str],
) -> Transcript:
    """Run ``engine`` over one track wav and wrap the result as an own-source ``Transcript``."""
    source_id = own_source_id(track)
    segments = engine.transcribe(
        wav, source_id=source_id, language=language, vocab_hints=list(vocab_hints)
    )
    validate_segment_ids(segments, source_id=source_id, engine=engine.name)
    return Transcript(
        source_id=source_id,
        kind="own",
        track=track,  # type: ignore[arg-type]  # AUDIO_TRACKS ⊂ TrackName
        engine=EngineInfo(
            name=engine.name,
            version=engine.version,
            params={"model": engine.model, **engine.params},
        ),
        language=language,
        time_offset=0.0,
        segments=segments,
    )


def run_transcribe(
    bundle: Bundle,
    *,
    force: bool = False,
    vocab_hints: list[str] | None = None,
    transcription_resolver: TranscriptionResolver | None = None,
    transcription_retry: TranscriptionRetry | None = None,
    should_cancel: Callable[[], bool] | None = None,
    progress: Callable[[str, float], None] | None = None,
) -> list[StageResult]:
    """Transcribe every ``preprocess/audio/<track>`` artifact with the configured engine.

    The engine is resolved from ``config.transcription_engine`` and checked against
    ``config.external_send_policy`` *before* any stage runs. Stage inputs are the preprocessed
    wav hashes; params are engine / version / model / language / vocab_hints / decode settings.

    ``vocab_hints`` overrides ``config.vocab_hints`` (the pipeline passes the meeting brief's
    merged hints — config first, then gaia glossary terms); ``None`` keeps the config's own
    hints. The effective hints are part of the stage params, so changed hints re-transcribe.
    """
    config = bundle.manifest.config
    if config.transcription_model is not None:
        from narumi.transcribe.api_stage import run_api_transcribe

        return run_api_transcribe(
            bundle,
            force=force,
            transcription_resolver=transcription_resolver,
            transcription_retry=transcription_retry,
            should_cancel=should_cancel,
            progress=progress,
        )
    if transcription_retry is not None:
        raise InvalidArgumentError(
            "Transcription retry requires a selected API transcription model"
        )
    hints = list(config.vocab_hints) if vocab_hints is None else list(vocab_hints)
    engine = get_engine(config.transcription_engine)
    check_send_policy(
        config.external_send_policy,
        engine.profile,
        subject=f"transcription engine {engine.name!r}",
    )
    results: list[StageResult] = []
    for track in AUDIO_TRACKS:
        input_key = audio_artifact_key(track)
        input_record = bundle.artifact(input_key)
        if input_record is None:
            continue
        wav = bundle.artifact_path(input_key)
        output = transcript_output_path(track)

        def produce(
            out: Path, *, track: str = track, wav: Path = wav, output: str = output
        ) -> None:
            transcript = transcribe_track(
                engine,
                wav,
                track=track,
                language=config.language,
                vocab_hints=list(hints),
            )
            bundle.write_json(output, transcript)

        results.append(
            bundle.run_stage(
                transcript_artifact_key(track),
                inputs={input_key: input_record.sha256},
                params={
                    "engine": engine.name,
                    "version": engine.version,
                    "model": engine.model,
                    "language": config.language,
                    "vocab_hints": list(hints),
                    "decode": dict(engine.params),
                },
                producer=(engine.name, engine.version),
                output=output,
                fn=produce,
                force=force,
            )
        )
    if not results:
        raise InvalidArgumentError(
            "no preprocessed audio artifact found; run preprocess first",
            details={"expected": [audio_artifact_key(t) for t in AUDIO_TRACKS]},
        )
    return results
