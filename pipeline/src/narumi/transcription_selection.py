"""Explicit, non-secret selection of a timestamped API transcription model."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ISO 639-1 codes from the third column of the Library of Congress code list,
# retrieved 2026-08-29: https://www.loc.gov/standards/iso639-2/ISO-639-2_utf-8.txt
# This validates language identifiers, not recognition quality or model support.
ISO_639_1_LANGUAGES: frozenset[str] = frozenset(
    "aa ab ae af ak am an ar as av ay az ba be bg bi bm bn bo br bs ca ce ch co cr "
    "cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn "
    "gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki "
    "kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml "
    "mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps pt "
    "qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te "
    "tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu".split()
)


def normalize_transcription_language(value: str) -> str | None:
    """Return the API language, omitting auto without guessing or rewriting codes."""
    if type(value) is not str:
        raise ValueError("API transcription language must be auto or an ISO 639-1 code")
    if value == "auto":
        return None
    if value not in ISO_639_1_LANGUAGES:
        raise ValueError("API transcription language must be auto or an ISO 639-1 code")
    return value


class TranscriptionModelSelection(BaseModel):
    """Pinned audio connection and model; no custom wire parameters are accepted."""

    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Literal["openai-api"]
    connection_id: str = Field(pattern=r"^conn-[0-9a-f]{12,32}$")
    connection_revision: int = Field(ge=1)
    model_id: Literal["whisper-1", "gpt-4o-transcribe-diarize"]
    parameters: dict[str, Any] = Field(default_factory=dict, max_length=0)
    cache_epoch: int = Field(default=0, ge=0)


class TranscriptionRetry(BaseModel):
    """One explicit confirmation to resend a specific outcome-unknown chunk."""

    model_config = ConfigDict(extra="forbid", strict=True)

    input_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    chunk_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    blocked_epoch: int = Field(ge=0)
