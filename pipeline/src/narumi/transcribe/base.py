"""Transcription engine abstraction shared by the registry, the stage and every engine."""

from __future__ import annotations

import importlib.metadata
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from narumi.errors import EngineUnavailableError
from narumi.models import Segment

DATA_DESTINATION_LOCAL = "local"
COST_CLASS_LOCAL = "local"
COST_CLASS_SUBSCRIPTION = "subscription"
COST_CLASS_API = "api"


@dataclass(frozen=True)
class EngineProfile:
    """Capability profile of a transcription engine (what it can do, where audio goes)."""

    sends_audio_externally: bool
    supports_vocab_hints: bool
    supports_word_timestamps: bool
    data_destination: str = DATA_DESTINATION_LOCAL
    """``"local"`` or the name of the external destination (vendor / service)."""
    cost_class: str = COST_CLASS_LOCAL
    """``local`` | ``subscription`` | ``api`` — matched against ``external_send_policy``."""


class TranscriptionEngine(Protocol):
    """Deterministic speech-to-text over one 16 kHz mono wav.

    Implementations must produce segment ids ``f"{source_id}:{index}"`` in order and must not
    load models or import heavy packages before ``transcribe`` is called.
    """

    name: str
    version: str
    model: str
    profile: EngineProfile
    params: dict[str, Any]
    """Fixed decode parameters (recorded in the manifest; part of the idempotency key)."""

    def transcribe(
        self, wav: Path, *, source_id: str, language: str, vocab_hints: list[str]
    ) -> list[Segment]: ...


def segment_id(source_id: str, index: int) -> str:
    return f"{source_id}:{index}"


def build_initial_prompt(vocab_hints: Sequence[str], *, language: str) -> str | None:
    """Whisper ``initial_prompt`` from vocabulary hints (``None`` when there are none)."""
    hints = [hint.strip() for hint in vocab_hints if hint and hint.strip()]
    if not hints:
        return None
    separator = "、" if language.lower().startswith("ja") else ", "
    return separator.join(hints)


def confidence_from_logprob(avg_logprob: float | None) -> float | None:
    """Map Whisper's average log-probability to a 0..1 confidence."""
    if avg_logprob is None:
        return None
    return round(min(1.0, max(0.0, math.exp(avg_logprob))), 4)


def package_version(distribution: str, *, install_hint: str) -> str:
    """Installed version of ``distribution`` or ``EngineUnavailableError`` with ``install_hint``."""
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as exc:
        raise EngineUnavailableError(
            f"{distribution} is not installed; {install_hint}",
            details={"package": distribution, "install": install_hint},
        ) from exc
