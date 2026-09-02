"""``mlx-whisper`` engine (Apple Silicon, local). Heavy imports happen inside ``transcribe``.

Verified against mlx-whisper 0.4.3: ``mlx_whisper.transcribe(audio, *, path_or_hf_repo,
temperature, initial_prompt, word_timestamps, condition_on_previous_text, verbose,
**decode_options)`` returns ``{"text", "segments": [{"start", "end", "text", "avg_logprob",
"words": [{"word", "start", "end", "probability"}], ...}], "language"}``.
"""

from __future__ import annotations

import mmap
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.models import Segment, Word
from narumi.transcribe._wav import open_wave_source
from narumi.transcribe.base import (
    EngineProfile,
    build_initial_prompt,
    confidence_from_logprob,
    package_version,
    segment_id,
)

ENV_MODEL = "NARUMI_WHISPER_MODEL"
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
DISTRIBUTION = "mlx-whisper"
INSTALL_HINT = "install with `uv sync --extra whisper-mlx` (Apple Silicon only)"
SEED = 0


def _load_normalized_waveform(wav: Path, mx: Any) -> Any:
    """Load the pipeline's normalized WAV without invoking mlx-whisper's ffmpeg loader."""
    try:
        with open_wave_source(wav.parent, wav) as source:
            mapped = mmap.mmap(source.stream.fileno(), length=0, access=mmap.ACCESS_READ)
            try:
                pcm = memoryview(mapped)[
                    source.data_offset : source.data_offset + source.sample_count * 2
                ].cast("h")
                try:
                    waveform = mx.array(pcm).astype(mx.float32) / 32768.0
                    # mlx operations are lazy. Materialize while the validated mmap is open.
                    mx.eval(waveform)
                finally:
                    pcm.release()
                source.check_unchanged()
                return waveform
            finally:
                mapped.close()
    except InvalidArgumentError:
        raise
    except (
        BufferError,
        MemoryError,
        OSError,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise EngineUnavailableError(
            "mlx-whisper could not load the normalized audio",
            details={"engine": "mlx-whisper", "reason": "mlx_audio_load_failed"},
        ) from exc


class MlxWhisperEngine:
    name = "mlx-whisper"
    profile = EngineProfile(
        sends_audio_externally=False,
        supports_vocab_hints=True,
        supports_word_timestamps=True,
    )

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
        self.version = package_version(DISTRIBUTION, install_hint=INSTALL_HINT)
        self.params: dict[str, Any] = {
            "temperature": 0.0,
            "word_timestamps": True,
            "condition_on_previous_text": True,
            "seed": SEED,
        }

    def transcribe(
        self, wav: Path, *, source_id: str, language: str, vocab_hints: list[str]
    ) -> list[Segment]:
        try:
            import mlx.core as mx
            import mlx_whisper
        except ImportError as exc:
            raise EngineUnavailableError(
                f"mlx-whisper could not be imported ({exc}); {INSTALL_HINT}",
                details={"engine": self.name, "install": INSTALL_HINT},
            ) from exc
        mx.random.seed(SEED)
        waveform = _load_normalized_waveform(wav, mx)
        result = mlx_whisper.transcribe(
            waveform,
            path_or_hf_repo=self.model,
            verbose=None,
            temperature=self.params["temperature"],
            initial_prompt=build_initial_prompt(vocab_hints, language=language),
            word_timestamps=self.params["word_timestamps"],
            condition_on_previous_text=self.params["condition_on_previous_text"],
            language=language,
        )
        return segments_from_dicts(result.get("segments") or [], source_id=source_id)


def segments_from_dicts(
    raw_segments: Iterable[Mapping[str, Any]], *, source_id: str
) -> list[Segment]:
    """Convert Whisper-style segment dicts (mlx-whisper / openai-whisper) to ``Segment``s.

    Empty segments (hallucination-suppressed) are dropped; ids are re-numbered contiguously.
    """
    segments: list[Segment] = []
    for item in raw_segments:
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        words = [
            Word(
                start=round(float(word["start"]), 3),
                end=round(float(word["end"]), 3),
                text=str(word["word"]).strip(),
                confidence=(
                    round(float(word["probability"]), 4)
                    if word.get("probability") is not None
                    else None
                ),
            )
            for word in item.get("words") or []
        ]
        start = round(float(item["start"]), 3)
        end = max(round(float(item["end"]), 3), start)
        segments.append(
            Segment(
                id=segment_id(source_id, len(segments)),
                start=start,
                end=end,
                text=text,
                confidence=confidence_from_logprob(item.get("avg_logprob")),
                words=words or None,
            )
        )
    return segments
