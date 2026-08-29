"""Reviewed OpenAI audio capabilities and fixed transcription request options.

The Models API does not describe timestamp support. Intersect its exact IDs with
this table; do not infer audio support from names or reuse text-model defaults.
The API does not promise a resolved model ID in transcription responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from narumi.providers.metadata.validation import parameter_schema

AUDIO_CAPABILITY_TABLE_VERSION = "openai-audio-2026-08-29-v1"
AUDIO_CAPABILITIES_VERIFIED_AT = "2026-08-29"
AUDIO_SOURCE_URL = (
    "https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/"
    "methods/create"
)
TIMESTAMPS_SOURCE_URL = "https://developers.openai.com/api/docs/guides/speech-to-text"


@dataclass(frozen=True)
class OpenAIAudioModelCapabilities:
    model_id: str
    display_name: str
    response_format: str | None
    timestamp_support: Literal["word", "diarized_segment", "none"]
    availability: Literal["available", "unsupported"]
    reason: str | None = None
    timestamp_granularities: tuple[str, ...] = ()
    chunking_strategy: str | None = None
    stream: bool | None = None
    resolved_revision: str | None = None
    source_url: str = AUDIO_SOURCE_URL
    verified_at: str = AUDIO_CAPABILITIES_VERIFIED_AT

    @property
    def wire_parameters(self) -> dict[str, Any]:
        """Return a fresh payload fragment with no model, language or prompt."""
        if self.availability != "available":
            return {}
        result: dict[str, Any] = {"response_format": self.response_format}
        if self.timestamp_granularities:
            result["timestamp_granularities"] = list(self.timestamp_granularities)
        if self.chunking_strategy is not None:
            result["chunking_strategy"] = self.chunking_strategy
        if self.stream is not None:
            result["stream"] = self.stream
        return result

    def parameter_schema(self) -> dict[str, Any]:
        # Audio has neither a max_tokens setting nor inherited reasoning options.
        return parameter_schema(None, enabled=False)


_CAPABILITIES = MappingProxyType(
    {
        entry.model_id: entry
        for entry in (
            OpenAIAudioModelCapabilities(
                model_id="whisper-1",
                display_name="Whisper 1",
                response_format="verbose_json",
                timestamp_support="word",
                timestamp_granularities=("segment", "word"),
                availability="available",
            ),
            OpenAIAudioModelCapabilities(
                model_id="gpt-4o-transcribe-diarize",
                display_name="GPT-4o Transcribe Diarize",
                response_format="diarized_json",
                timestamp_support="diarized_segment",
                chunking_strategy="auto",
                stream=False,
                availability="available",
            ),
            *(
                OpenAIAudioModelCapabilities(
                    model_id=model_id,
                    display_name=display_name,
                    response_format=None,
                    timestamp_support="none",
                    availability="unsupported",
                    reason="timestamp_support_required",
                )
                for model_id, display_name in (
                    ("gpt-4o-transcribe", "GPT-4o Transcribe"),
                    ("gpt-4o-mini-transcribe", "GPT-4o mini Transcribe"),
                )
            ),
        )
    }
)


def audio_model_capabilities(model_id: str) -> OpenAIAudioModelCapabilities | None:
    """Return exact known capabilities; callers must also check availability."""
    return _CAPABILITIES.get(model_id) if isinstance(model_id, str) else None
