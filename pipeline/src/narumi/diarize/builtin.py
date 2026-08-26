"""Built-in diarization engines that need no ML package: ``none`` and ``fake``."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from narumi.diarize.base import LAYER_ENGINE, DiarizationProfile, speaker_label
from narumi.errors import InvalidArgumentError
from narumi.models import Turn
from narumi.preprocess.ffmpeg import probe_duration

SIDECAR_SUFFIX = ".fake-diar.json"
FAKE_TURN_SECONDS = 10.0
FAKE_SPEAKERS = 2

_LOCAL = DiarizationProfile(sends_audio_externally=False)


class FakeTurnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker: str
    confidence: float = 1.0


_SIDECAR = TypeAdapter(list[FakeTurnSpec])


def sidecar_path(wav: Path) -> Path:
    """``<wav>.fake-diar.json`` — the optional scripted diarization for ``wav``."""
    return Path(wav).with_name(Path(wav).name + SIDECAR_SUFFIX)


def load_sidecar(path: Path) -> list[FakeTurnSpec]:
    try:
        return _SIDECAR.validate_json(Path(path).read_bytes())
    except ValidationError as exc:
        raise InvalidArgumentError(
            f"invalid fake diarization sidecar {path}: {exc.error_count()} validation error(s)",
            details={"path": str(path), "errors": exc.errors(include_url=False)},
        ) from exc


def _duration(wav: Path) -> float:
    duration = probe_duration(wav)
    if duration is None:
        raise InvalidArgumentError(
            f"cannot determine the duration of {wav}", details={"path": str(wav)}
        )
    return duration


class NoneEngine:
    """Single anonymous speaker over the whole wav (the layer-2 identity element)."""

    name = "none"
    version = "1"
    profile = _LOCAL

    def __init__(self) -> None:
        self.params: dict[str, Any] = {}

    def diarize(self, wav: Path, *, num_speakers: int | None = None) -> list[Turn]:
        duration = _duration(Path(wav))
        return [
            Turn(
                start=0.0,
                end=round(duration, 3),
                speaker=speaker_label(0),
                confidence=1.0,
                layer=LAYER_ENGINE,
            )
        ]


class FakeDiarizationEngine:
    """Scripted (``<wav>.fake-diar.json``) or alternating 10 s turns between two speakers."""

    name = "fake"
    version = "1"
    profile = _LOCAL

    def __init__(self) -> None:
        self.params: dict[str, Any] = {
            "turn_seconds": FAKE_TURN_SECONDS,
            "speakers": FAKE_SPEAKERS,
        }

    def diarize(self, wav: Path, *, num_speakers: int | None = None) -> list[Turn]:
        wav = Path(wav)
        sidecar = sidecar_path(wav)
        if sidecar.exists():
            return [
                Turn(
                    start=spec.start,
                    end=spec.end,
                    speaker=spec.speaker,
                    confidence=spec.confidence,
                    layer=LAYER_ENGINE,
                )
                for spec in load_sidecar(sidecar)
            ]
        speakers = num_speakers if num_speakers and num_speakers > 0 else FAKE_SPEAKERS
        duration = _duration(wav)
        turns: list[Turn] = []
        for index in range(math.ceil(duration / FAKE_TURN_SECONDS)):
            start = index * FAKE_TURN_SECONDS
            end = min(start + FAKE_TURN_SECONDS, duration)
            turns.append(
                Turn(
                    start=round(start, 3),
                    end=round(end, 3),
                    speaker=speaker_label(index % speakers),
                    confidence=1.0,
                    layer=LAYER_ENGINE,
                )
            )
        return turns
