"""Transcription: engine abstraction (fake / mlx-whisper / faster-whisper) and the stage runner."""

from narumi.transcribe.base import (
    EngineProfile,
    TranscriptionEngine,
    build_initial_prompt,
    package_version,
    segment_id,
)
from narumi.transcribe.fake import FakeEngine, sidecar_path
from narumi.transcribe.faster_whisper_engine import FasterWhisperEngine
from narumi.transcribe.mlx_whisper_engine import MlxWhisperEngine
from narumi.transcribe.policy import check_send_policy, is_local
from narumi.transcribe.registry import (
    AUTO,
    AUTO_ORDER,
    ENGINE_FACTORIES,
    ENGINE_MODULES,
    available_engines,
    engine_profile,
    get_engine,
    is_engine_available,
    resolve_engine_name,
)
from narumi.transcribe.stage import (
    own_source_id,
    run_transcribe,
    transcribe_track,
    transcript_artifact_key,
    transcript_output_path,
)

__all__ = [
    "AUTO",
    "AUTO_ORDER",
    "ENGINE_FACTORIES",
    "ENGINE_MODULES",
    "EngineProfile",
    "FakeEngine",
    "FasterWhisperEngine",
    "MlxWhisperEngine",
    "TranscriptionEngine",
    "available_engines",
    "build_initial_prompt",
    "check_send_policy",
    "engine_profile",
    "get_engine",
    "is_engine_available",
    "is_local",
    "own_source_id",
    "package_version",
    "resolve_engine_name",
    "run_transcribe",
    "segment_id",
    "sidecar_path",
    "transcribe_track",
    "transcript_artifact_key",
    "transcript_output_path",
]
