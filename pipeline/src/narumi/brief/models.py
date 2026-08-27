"""Persisted meeting-brief data and the public Gaia evidence behind it."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Participant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    aliases: list[str] = Field(default_factory=list)
    note: str | None = None
    person_id: int | None = None


class BriefSource(BaseModel):
    """A reference a connector or human can follow; not injected into prompts."""

    model_config = ConfigDict(extra="forbid")

    system: str
    uri: str
    note: str | None = None
    title: str | None = None
    snapshot: str | None = None
    ref_id: int | None = None
    scope: str | None = None


class Brief(BaseModel):
    """会議ブリーフ: prioritized context plus lossless source-response snapshots."""

    model_config = ConfigDict(extra="forbid")

    vocab_hints: list[str] = Field(default_factory=list)
    """Config hints first, then Gaia vocabulary and terms; order-preserving deduplication."""
    participants: list[Participant] = Field(default_factory=list)
    previous_points: list[str] = Field(default_factory=list)
    background: list[str] = Field(default_factory=list)
    sources: list[BriefSource] = Field(default_factory=list)
    gaia_context: dict[str, Any] = Field(default_factory=dict)
    """Actual read-tool responses only; credentials and server-info are never stored here."""
