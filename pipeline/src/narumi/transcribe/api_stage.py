"""Sequential, checkpointed API ASR behind the existing transcription stage."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING

from narumi.bundle import Bundle, StageResult
from narumi.errors import (
    CancelledError,
    ConfigurationConflictError,
    EngineUnavailableError,
    InvalidArgumentError,
)
from narumi.models import Segment
from narumi.preprocess.stage import AUDIO_TRACKS, audio_artifact_key
from narumi.providers.audio_response import parse_saved_result
from narumi.transcribe.api_transcript import project_segments, publish_transcript
from narumi.transcribe.base import EngineProfile
from narumi.transcribe.checkpoints import TranscriptionCheckpoints, transcription_execution_lock
from narumi.transcribe.chunks import build_transcription_plan
from narumi.transcribe.policy import check_send_policy

if TYPE_CHECKING:
    from narumi.providers.transcription import TranscriptionResolver
    from narumi.transcription_selection import TranscriptionRetry

_API_PROFILE = EngineProfile(
    sends_audio_externally=True,
    supports_vocab_hints=False,
    supports_word_timestamps=True,
    data_destination="openai",
    cost_class="api",
)


def _check_cancelled(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise CancelledError("Transcription was cancelled")


def run_api_transcribe(
    bundle: Bundle,
    *,
    force: bool = False,
    transcription_resolver: TranscriptionResolver | None = None,
    transcription_retry: TranscriptionRetry | None = None,
    should_cancel: Callable[[], bool] | None = None,
    progress: Callable[[str, float], None] | None = None,
) -> list[StageResult]:
    """Resolve and verify the whole plan before any audio leaves this meeting."""
    # The manifest fence is outermost: a stale Bundle must fail before resolving a
    # provider, and no concurrent writer may change config or artifacts while the
    # checkpointed upload and transcript publication are in progress. Server jobs
    # may already hold this lease; the lock is scoped-reentrant for that exact task.
    with bundle.writer_lock(timeout=None):
        return _run_api_transcribe_locked(
            bundle,
            force=force,
            transcription_resolver=transcription_resolver,
            transcription_retry=transcription_retry,
            should_cancel=should_cancel,
            progress=progress,
        )


def _run_api_transcribe_locked(
    bundle: Bundle,
    *,
    force: bool,
    transcription_resolver: TranscriptionResolver | None,
    transcription_retry: TranscriptionRetry | None,
    should_cancel: Callable[[], bool] | None,
    progress: Callable[[str, float], None] | None,
) -> list[StageResult]:
    """Run API ASR after the Bundle's current manifest generation is fenced."""
    config = bundle.manifest.config.model_copy(deep=True)
    selection = config.transcription_model
    if selection is None:
        raise InvalidArgumentError("An API transcription model selection is required")
    check_send_policy(config.external_send_policy, _API_PROFILE, subject="API transcription")
    if force:
        raise InvalidArgumentError("API transcription cannot force successful or unknown chunks")
    if transcription_resolver is None:
        raise EngineUnavailableError("API transcription requires the resident provider resolver")

    def check_current() -> None:
        _check_cancelled(should_cancel)
        if bundle.manifest.config != config:
            raise ConfigurationConflictError(
                "The transcription configuration changed during processing"
            )

    check_current()

    with transcription_execution_lock(bundle):
        provider = transcription_resolver.resolve(config, should_cancel=should_cancel)
        params = copy.deepcopy(provider.transcription_params)
        sources = {}
        hashes = {}
        for track in AUDIO_TRACKS:
            key = audio_artifact_key(track)
            record = bundle.artifact(key)
            if record is not None:
                sources[track] = bundle.artifact_path(key)
                hashes[track] = record.sha256
        if not sources:
            raise InvalidArgumentError("No preprocessed audio is available for transcription")
        plan = build_transcription_plan(
            bundle,
            sources=sources,
            params=params,
            expected_hashes=hashes,
            should_cancel=should_cancel,
        )
        _check_cancelled(should_cancel)
        checkpoints = TranscriptionCheckpoints(
            bundle,
            plan,
            cache_epoch=selection.cache_epoch,
            retry=transcription_retry,
            should_cancel=should_cancel,
        )
        checkpoints.preflight()
        track_chunks = {
            track: [chunk for chunk in plan.chunks if chunk.track == track]
            for track in AUDIO_TRACKS
        }
        # Preflight also includes projection constraints. A corrupt later success
        # must stop us before an earlier, previously unprocessed chunk is sent.
        for track, chunks in track_chunks.items():
            for chunk_index, chunk in enumerate(chunks):
                saved = checkpoints.get_success(chunk)
                if saved is not None:
                    result = parse_saved_result(
                        saved, model_id=selection.model_id, chunk_duration=chunk.duration_sec
                    )
                    project_segments(
                        result,
                        chunk,
                        source_id=f"own-{track}",
                        first_index=0,
                        track_chunk_index=chunk_index,
                    )
                _check_cancelled(should_cancel)
        results: list[StageResult] = []
        completed = 0
        for track, chunks in track_chunks.items():
            if not chunks:
                continue
            segments: list[Segment] = []
            for chunk_index, chunk in enumerate(chunks):
                check_current()
                if progress is not None:
                    progress(f"transcribe/{track}/{chunk.index + 1}", completed / len(plan.chunks))
                saved = checkpoints.get_success(chunk)
                if saved is None:
                    # Loading verifies bytes against the planned hash before a
                    # pending receipt can consume an explicit retry confirmation.
                    audio = chunk.read_audio()
                    _check_cancelled(should_cancel)
                    attempt = checkpoints.begin_attempt(chunk)
                    reply_received = False
                    try:
                        response = provider.transcribe_chunk(audio, duration_sec=chunk.duration_sec)
                        reply_received = True
                        payload = json.loads(json.dumps(asdict(response), allow_nan=False))
                        result = parse_saved_result(
                            payload, model_id=selection.model_id, chunk_duration=chunk.duration_sec
                        )
                        projected = project_segments(
                            result,
                            chunk,
                            source_id=f"own-{track}",
                            first_index=len(segments),
                            track_chunk_index=chunk_index,
                        )
                    except BaseException as error:
                        failure = (
                            EngineUnavailableError(
                                "The transcription reply could not be validated or saved",
                                details={"outcome_unknown": True},
                            )
                            if reply_received
                            else error
                        )
                        checkpoints.fail(attempt, failure)
                        raise
                    # Persistence already preserves a pending receipt on failure.
                    # Do not call fail() afterwards: a late fsync error may follow
                    # a committed success, which must never be overwritten.
                    checkpoints.succeed(attempt, payload)
                else:
                    result = parse_saved_result(
                        saved, model_id=selection.model_id, chunk_duration=chunk.duration_sec
                    )
                    projected = project_segments(
                        result,
                        chunk,
                        source_id=f"own-{track}",
                        first_index=len(segments),
                        track_chunk_index=chunk_index,
                    )
                # A received success stays reusable even if cancellation arrived
                # while the provider was returning the final bytes.
                check_current()
                segments.extend(projected)
                completed += 1
                if progress is not None:
                    progress(f"transcribe/{track}/{chunk.index + 1}", completed / len(plan.chunks))
            check_current()
            results.append(
                publish_transcript(
                    bundle,
                    track=track,
                    language=config.language,
                    model_id=selection.model_id,
                    params=params,
                    chunks=chunks,
                    segments=segments,
                )
            )
        check_current()
        return results
