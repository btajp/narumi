"""Shared data models for transcripts, diarization, alignment and meeting configuration.

These models are the *internal* schema of a session bundle. Tool contracts (``contracts/``)
are the external schema; the server maps between the two.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from narumi.model_selection import ModelSelection

SPEAKER_ME = "me"
SPEAKER_OTHER = "other"


class ExternalSendPolicy(StrEnum):
    LOCAL_ONLY = "local_only"
    SUBSCRIPTION_OK = "subscription_ok"
    API_OK = "api_ok"


class MeetingConfig(BaseModel):
    """Per-meeting processing configuration (mirrors ``manifest.config``)."""

    model_config = ConfigDict(extra="forbid")

    transcription_engine: str = "auto"
    diarization_engine: str = "none"
    llm_provider: str = "none"
    minutes_model: ModelSelection | None = None
    external_send_policy: ExternalSendPolicy = ExternalSendPolicy.LOCAL_ONLY
    language: str = "ja"
    self_name: str | None = None
    vocab_hints: list[str] = Field(default_factory=list)


class Word(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: float
    end: float
    text: str
    confidence: float | None = None


class Segment(BaseModel):
    """A timestamped transcript segment. ``id`` is ``<source_id>:<index>``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    speaker: str | None = None
    confidence: float | None = None
    words: list[Word] | None = None


class EngineInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    params: dict[str, Any] = Field(default_factory=dict)


TranscriptKind = Literal["own", "external"]
TrackName = Literal["mic", "system"]


class Transcript(BaseModel):
    """One transcript *source* (系統). Own sources are per track; external ones per context."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    kind: TranscriptKind
    track: TrackName | None = None
    engine: EngineInfo
    language: str = "ja"
    time_offset: float = 0.0
    """Seconds to add to segment times to align with the recording clock."""
    segments: list[Segment] = Field(default_factory=list)


class Turn(BaseModel):
    """A diarization turn from one layer."""

    model_config = ConfigDict(extra="forbid")

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker: str
    confidence: float = 1.0
    layer: int = Field(ge=1, le=4)
    source_id: str | None = None


class Diarization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer: int = Field(ge=1, le=4)
    engine: EngineInfo
    turns: list[Turn] = Field(default_factory=list)


class SpeakerEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer: int = Field(ge=1, le=4)
    detail: str


class SpeakerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    confidence: float = 0.0
    evidence: list[SpeakerEvidence] = Field(default_factory=list)


class SpeakerMap(BaseModel):
    """Anonymous label (me / other / SPEAKER_00) → resolved identity."""

    model_config = ConfigDict(extra="forbid")

    speakers: dict[str, SpeakerEntry] = Field(default_factory=dict)

    def name_for(self, label: str | None) -> str | None:
        if label is None:
            return None
        entry = self.speakers.get(label)
        return entry.name if entry else None


class Anchor(BaseModel):
    """A unique n-gram matched across two sources, used to estimate clock offsets."""

    model_config = ConfigDict(extra="forbid")

    ngram: str
    source_a: str
    segment_a: str
    source_b: str
    segment_b: str
    offset: float


class Interval(BaseModel):
    """A time interval of the merged timeline with the segments each source contributes."""

    model_config = ConfigDict(extra="forbid")

    id: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    columns: dict[str, list[str]] = Field(default_factory=dict)


class Alignment(BaseModel):
    """Stage-1 output: deterministic correspondence table across transcript sources."""

    model_config = ConfigDict(extra="forbid")

    intervals: list[Interval] = Field(default_factory=list)
    offsets: dict[str, float] = Field(default_factory=dict)
    anchors: list[Anchor] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class MergedSegment(BaseModel):
    """Stage-2 output: one row of the integrated transcript."""

    model_config = ConfigDict(extra="forbid")

    id: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    speaker_label: str | None = None
    speaker_name: str | None = None
    sources: list[str] = Field(default_factory=list)


class MergedTranscript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[MergedSegment] = Field(default_factory=list)
    speaker_map: SpeakerMap = Field(default_factory=SpeakerMap)
    provider: str = "none"
    params: dict[str, Any] = Field(default_factory=dict)


class MinutesMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    generated_at: str
    provider: str
    prompt_version: str | None = None
    inputs: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    unresolved_speakers: list[str] = Field(default_factory=list)
