"""``faster-whisper`` engine (CTranslate2, local). Heavy imports happen inside ``transcribe``.

Verified against faster-whisper 1.2.1: ``WhisperModel(model_size_or_path, device, compute_type)``
and ``WhisperModel.transcribe(audio, language=..., beam_size=..., temperature=...,
initial_prompt=..., word_timestamps=..., vad_filter=..., condition_on_previous_text=...)``
returning ``(Iterable[Segment], TranscriptionInfo)`` where ``Segment`` has ``start / end / text /
avg_logprob / words`` and ``Word`` has ``start / end / word / probability``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from narumi.errors import EngineUnavailableError
from narumi.models import Segment, Word
from narumi.transcribe.base import (
    EngineProfile,
    build_initial_prompt,
    confidence_from_logprob,
    package_version,
    segment_id,
)

ENV_MODEL = "NARUMI_WHISPER_MODEL"
DEFAULT_MODEL = "large-v3-turbo"
DISTRIBUTION = "faster-whisper"
INSTALL_HINT = "install with `uv sync --extra whisper-faster`"


class FasterWhisperEngine:
    name = "faster-whisper"
    profile = EngineProfile(
        sends_audio_externally=False,
        supports_vocab_hints=True,
        supports_word_timestamps=True,
    )

    def __init__(
        self,
        model: str | None = None,
        *,
        device: str = "auto",
        compute_type: str = "default",
    ) -> None:
        self.model = model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
        self.version = package_version(DISTRIBUTION, install_hint=INSTALL_HINT)
        self.device = device
        self.compute_type = compute_type
        self.params: dict[str, Any] = {
            "temperature": 0.0,
            "beam_size": 5,
            "word_timestamps": True,
            "vad_filter": True,
            "condition_on_previous_text": True,
        }
        self._model: Any | None = None

    def _load_model(self) -> Any:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise EngineUnavailableError(
                    f"faster-whisper could not be imported ({exc}); {INSTALL_HINT}",
                    details={"engine": self.name, "install": INSTALL_HINT},
                ) from exc
            self._model = WhisperModel(
                self.model, device=self.device, compute_type=self.compute_type
            )
        return self._model

    def transcribe(
        self, wav: Path, *, source_id: str, language: str, vocab_hints: list[str]
    ) -> list[Segment]:
        model = self._load_model()
        raw_segments, _info = model.transcribe(
            str(wav),
            language=language,
            beam_size=self.params["beam_size"],
            temperature=self.params["temperature"],
            initial_prompt=build_initial_prompt(vocab_hints, language=language),
            word_timestamps=self.params["word_timestamps"],
            vad_filter=self.params["vad_filter"],
            condition_on_previous_text=self.params["condition_on_previous_text"],
        )
        segments: list[Segment] = []
        for item in raw_segments:
            text = str(item.text).strip()
            if not text:
                continue
            words = [
                Word(
                    start=round(float(word.start), 3),
                    end=round(float(word.end), 3),
                    text=str(word.word).strip(),
                    confidence=round(float(word.probability), 4),
                )
                for word in item.words or []
            ]
            start = round(float(item.start), 3)
            end = max(round(float(item.end), 3), start)
            segments.append(
                Segment(
                    id=segment_id(source_id, len(segments)),
                    start=start,
                    end=end,
                    text=text,
                    confidence=confidence_from_logprob(item.avg_logprob),
                    words=words or None,
                )
            )
        return segments
