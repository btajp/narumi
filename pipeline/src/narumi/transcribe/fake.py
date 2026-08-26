"""``fake`` transcription engine for tests and offline pipeline runs.

If ``<wav>.fake.json`` exists next to the wav it is used verbatim (a JSON list of
``{"start", "end", "text", "speaker"?, "confidence"?}``); otherwise the wav duration is split
into fixed 5 s chunks labelled ``ダミー発話 <i>``. Output is fully deterministic.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from narumi.errors import InvalidArgumentError
from narumi.models import Segment
from narumi.preprocess.ffmpeg import probe_duration
from narumi.transcribe.base import EngineProfile, segment_id

SIDECAR_SUFFIX = ".fake.json"
CHUNK_SECONDS = 5.0


class FakeSegmentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    speaker: str | None = None
    confidence: float | None = None


_SIDECAR = TypeAdapter(list[FakeSegmentSpec])


def sidecar_path(wav: Path) -> Path:
    """``<wav>.fake.json`` — the optional scripted transcript for ``wav``."""
    return Path(wav).with_name(Path(wav).name + SIDECAR_SUFFIX)


def load_sidecar(path: Path) -> list[FakeSegmentSpec]:
    try:
        return _SIDECAR.validate_json(Path(path).read_bytes())
    except ValidationError as exc:
        raise InvalidArgumentError(
            f"invalid fake transcript sidecar {path}: {exc.error_count()} validation error(s)",
            details={"path": str(path), "errors": exc.errors(include_url=False)},
        ) from exc


class FakeEngine:
    name = "fake"
    version = "1"
    model = "fake"
    profile = EngineProfile(
        sends_audio_externally=False,
        supports_vocab_hints=True,
        supports_word_timestamps=False,
    )

    def __init__(self) -> None:
        self.params: dict[str, Any] = {"chunk_seconds": CHUNK_SECONDS}

    def transcribe(
        self, wav: Path, *, source_id: str, language: str, vocab_hints: list[str]
    ) -> list[Segment]:
        wav = Path(wav)
        sidecar = sidecar_path(wav)
        if sidecar.exists():
            return [
                Segment(
                    id=segment_id(source_id, index),
                    start=spec.start,
                    end=spec.end,
                    text=spec.text,
                    speaker=spec.speaker,
                    confidence=spec.confidence,
                )
                for index, spec in enumerate(load_sidecar(sidecar))
            ]
        duration = probe_duration(wav)
        if duration is None:
            raise InvalidArgumentError(
                f"cannot determine the duration of {wav}", details={"path": str(wav)}
            )
        segments: list[Segment] = []
        for index in range(math.ceil(duration / CHUNK_SECONDS)):
            start = index * CHUNK_SECONDS
            end = min(start + CHUNK_SECONDS, duration)
            segments.append(
                Segment(
                    id=segment_id(source_id, index),
                    start=round(start, 3),
                    end=round(end, 3),
                    text=f"ダミー発話 {index}",
                    confidence=1.0,
                )
            )
        return segments
