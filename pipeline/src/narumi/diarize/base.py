"""Diarization engine abstraction (layer 2) shared by the registry, the stage and engines."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from narumi.models import Turn
from narumi.transcribe.base import COST_CLASS_LOCAL, DATA_DESTINATION_LOCAL

LAYER_TRACKS = 1
LAYER_ENGINE = 2
LAYER1_ENGINE_NAME = "tracks"
LAYER1_ENGINE_VERSION = "1"


@dataclass(frozen=True)
class DiarizationProfile:
    """Where the audio goes when this engine runs (matched against ``external_send_policy``)."""

    sends_audio_externally: bool
    data_destination: str = DATA_DESTINATION_LOCAL
    cost_class: str = COST_CLASS_LOCAL


class DiarizationEngine(Protocol):
    """Deterministic speaker diarization over one 16 kHz mono wav (layer 2).

    Turns carry ``layer=2`` and anonymous labels ``SPEAKER_00``, ``SPEAKER_01``, … Engines must
    not import heavy packages or load models before ``diarize`` is called.
    """

    name: str
    version: str
    profile: DiarizationProfile
    params: dict[str, Any]

    def diarize(self, wav: Path, *, num_speakers: int | None = None) -> list[Turn]: ...


def speaker_label(index: int) -> str:
    """Anonymous layer-2 speaker label (``SPEAKER_00`` style)."""
    return f"SPEAKER_{index:02d}"
