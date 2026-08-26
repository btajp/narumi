"""``manifest.json`` schema (pydantic). See docs/superpowers/specs for the layout."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from narumi.models import MeetingConfig

MANIFEST_VERSION = 1

MeetingStatus = Literal["recording", "recorded", "processing", "ready", "failed"]


class TrackRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str | None = None
    bytes: int | None = None
    duration_sec: float | None = None
    discarded: bool = False


class RecordingInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    started_at: str | None = None
    stopped_at: str | None = None
    duration_sec: float | None = None
    tracks: dict[str, TrackRecord] = Field(default_factory=dict)
    recorder: dict[str, Any] = Field(default_factory=dict)
    """Raw summary emitted by narumi-recorder (kept for provenance)."""


class Producer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str


class ArtifactRecord(BaseModel):
    """Provenance of one generated artifact; the key to idempotent regeneration."""

    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str
    inputs: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    params_hash: str
    producer: Producer
    created_at: str


class ContextRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_id: str
    source_type: str
    registered_at: str
    path: str
    status: Literal["stored", "parsed", "failed"] = "stored"
    label: str | None = None
    request_id: str | None = None


class MinutesVersionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    path: str
    generated_at: str
    provider: str


class ExportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str
    ref: str
    minutes_version: int
    at: str
    request_id: str | None = None


class RegenerationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str | None = None
    at: str
    reason: str
    minutes_version: int | None = None


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_version: int = MANIFEST_VERSION
    meeting_id: str
    meeting_name: str
    engagement: str | None = None
    scope: str | None = None
    profile: str = "default"
    status: MeetingStatus = "recording"
    created_at: str
    updated_at: str
    recording: RecordingInfo = Field(default_factory=RecordingInfo)
    config: MeetingConfig = Field(default_factory=MeetingConfig)
    artifacts: dict[str, ArtifactRecord] = Field(default_factory=dict)
    contexts: list[ContextRecord] = Field(default_factory=list)
    minutes_versions: list[MinutesVersionRecord] = Field(default_factory=list)
    exports: list[ExportRecord] = Field(default_factory=list)
    regenerations: list[RegenerationRecord] = Field(default_factory=list)

    @property
    def latest_minutes_version(self) -> int | None:
        if not self.minutes_versions:
            return None
        return max(m.version for m in self.minutes_versions)
