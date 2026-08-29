"""Native audio replies and validated, serializable chunk results.

Timestamps stay relative to one uploaded chunk. Track offsets, speaker namespaces
and word-to-segment association belong to the deterministic transcription stage.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.providers.metadata.validation import MAX_PUBLIC_PAYLOAD_NODES, check_public_payload

TRANSCRIPTION_OUTCOME_UNKNOWN = "provider_transcription_outcome_unknown"
MAX_AUDIO_SEGMENTS = 10_000
MAX_AUDIO_WORDS = 20_000
MAX_AUDIO_TEXT_CHARS = 1_048_576
MAX_AUDIO_RESPONSE_NODES = MAX_PUBLIC_PAYLOAD_NODES
_MODELS = {"whisper-1", "gpt-4o-transcribe-diarize"}


@dataclass(frozen=True)
class AudioWord:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class AudioSegment:
    native_id: int | str
    start: float
    end: float
    text: str
    speaker: str | None = None
    """An opaque native label, never a verified person or cross-chunk identity."""


@dataclass(frozen=True)
class AudioTranscriptionResult:
    text: str
    duration: float
    segments: tuple[AudioSegment, ...]
    words: tuple[AudioWord, ...] | None = None
    language: str | None = None
    usage: dict[str, Any] | None = None


def transcription_unknown() -> EngineUnavailableError:
    return EngineUnavailableError(
        "The transcription outcome is unknown; confirm the affected chunk before resending",
        details={"reason": TRANSCRIPTION_OUTCOME_UNKNOWN, "outcome_unknown": True},
    )


def parse_audio_response(
    model_id: str, body: Any, *, chunk_duration: float, api_key: str | None = None
) -> AudioTranscriptionResult:
    """Accept fully received native JSON, not a fabricated Responses-style status."""
    _context(model_id, chunk_duration)
    try:
        value = _object(body)
        secrets = (api_key, "Bearer " + api_key) if api_key else ()
        check_public_payload(
            value,
            secrets=secrets,
            reject_credentials=False,
            max_nodes=MAX_AUDIO_RESPONSE_NODES,
        )
        _require(all(value.get(key) is None for key in ("error", "refusal", "incomplete_details")))
        # No model/status field is required or manufactured for this API. Unknown
        # extension fields are inspected for secrets but are not stored as diagnostics.
        duration = _time(value.get("duration"), chunk_duration)
        text = _text(value.get("text"))
        if model_id == "whisper-1":
            _require(value.get("task", "transcribe") == "transcribe")
            language = _label(value.get("language"))
            segments = _native_whisper_segments(value.get("segments"), chunk_duration)
            words = _words(value.get("words"), chunk_duration, native=True)
        else:
            _require(value.get("task") == "transcribe")
            language, words = None, None
            segments = _native_diarized_segments(value.get("segments"), chunk_duration)
        result = AudioTranscriptionResult(
            text,
            duration,
            segments,
            words,
            language,
            _usage(value.get("usage"), model_id=model_id),
        )
        _validate_result(result)
        return result
    except Exception:
        raise transcription_unknown() from None


def parse_saved_result(
    payload: Any, *, model_id: str, chunk_duration: float
) -> AudioTranscriptionResult:
    """Recheck normalized artifacts without treating corruption as a cache miss."""
    _context(model_id, chunk_duration)
    try:
        value = _object(payload)
        check_public_payload(value, reject_credentials=False, max_nodes=MAX_AUDIO_RESPONSE_NODES)
        _require(set(value) == {"text", "duration", "segments", "words", "language", "usage"})
        segments = []
        for raw in _array(value["segments"], MAX_AUDIO_SEGMENTS, saved=True):
            item = _object(raw)
            _require(set(item) == {"native_id", "start", "end", "text", "speaker"})
            native_id = _native_id(item["native_id"], model_id)
            speaker = _label(item["speaker"]) if model_id != "whisper-1" else None
            if model_id == "whisper-1":
                _require(item["speaker"] is None)
            text, start, end = _span(item, chunk_duration)
            segments.append(AudioSegment(native_id, start, end, text, speaker))
        language = _label(value["language"]) if model_id == "whisper-1" else None
        if model_id != "whisper-1":
            _require(value["language"] is None and value["words"] is None)
        result = AudioTranscriptionResult(
            _text(value["text"]),
            _time(value["duration"], chunk_duration),
            tuple(segments),
            _words(value["words"], chunk_duration, native=False),
            language,
            _usage(value["usage"], model_id=model_id, saved=True),
        )
        _validate_result(result)
        return result
    except Exception:
        raise EngineUnavailableError(
            "The saved transcription result could not be verified",
            details={"reason": "transcription_saved_result_invalid"},
        ) from None


def _native_whisper_segments(raw: Any, duration: float) -> tuple[AudioSegment, ...]:
    if raw is None:
        return ()
    segments = []
    for value in _array(raw, MAX_AUDIO_SEGMENTS):
        item = _object(value)
        native_id = _native_id(item.get("id"), "whisper-1")
        _integer(item.get("seek"))
        tokens = _array(item.get("tokens"), MAX_AUDIO_WORDS)
        for token in tokens:
            _integer(token)
        for field in ("temperature", "avg_logprob", "compression_ratio", "no_speech_prob"):
            _number(item.get(field))
        text, start, end = _span(item, duration)
        segments.append(AudioSegment(native_id, start, end, text))
    return tuple(segments)


def _native_diarized_segments(raw: Any, duration: float) -> tuple[AudioSegment, ...]:
    segments = []
    for value in _array(raw, MAX_AUDIO_SEGMENTS):
        item = _object(value)
        _require(item.get("type") == "transcript.text.segment")
        native_id = _native_id(item.get("id"), "gpt-4o-transcribe-diarize")
        text, start, end = _span(item, duration)
        segments.append(AudioSegment(native_id, start, end, text, _label(item.get("speaker"))))
    return tuple(segments)


def _words(raw: Any, duration: float, *, native: bool) -> tuple[AudioWord, ...] | None:
    if raw is None:
        return None
    result = []
    for value in _array(raw, MAX_AUDIO_WORDS, saved=not native):
        item = _object(value)
        if not native:
            _require(set(item) == {"start", "end", "text"})
        text = _text(item.get("word" if native else "text"))
        _require(bool(text.strip()))
        start, end = _time(item.get("start"), duration), _time(item.get("end"), duration)
        _require(start <= end)
        result.append(AudioWord(start, end, text))
    return tuple(result)


def _span(item: dict[str, Any], duration: float) -> tuple[str, float, float]:
    text = _text(item.get("text"))
    start, end = _time(item.get("start"), duration), _time(item.get("end"), duration)
    _require(start <= end and (not text.strip() or start < end))
    return text, start, end


def _validate_result(result: AudioTranscriptionResult) -> None:
    ids = [segment.native_id for segment in result.segments]
    _require(len(ids) == len(set(ids)))
    has_segment_text = any(segment.text.strip() for segment in result.segments)
    _require(bool(result.text.strip()) == has_segment_text)
    _require(all(segment.end <= result.duration for segment in result.segments))
    if result.words:
        _require(bool(result.text.strip()))
        _require(all(word.end <= result.duration for word in result.words))


def _usage(raw: Any, *, model_id: str, saved: bool = False) -> dict[str, Any] | None:
    if raw is None:
        return None
    value = _object(raw)
    if value.get("type") == "duration":
        if saved:
            _require(set(value) == {"type", "seconds"})
        seconds = _number(value.get("seconds"))
        _require(seconds >= 0)
        return {"type": "duration", "seconds": seconds}
    _require(model_id == "gpt-4o-transcribe-diarize" and value.get("type") == "tokens")
    if saved:
        _require(
            set(value)
            <= {"type", "input_tokens", "output_tokens", "total_tokens", "input_token_details"}
        )
    result = {"type": "tokens"}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        result[key] = _integer(value.get(key))
    if value.get("input_token_details") is not None:
        details = _object(value["input_token_details"])
        if saved:
            _require(set(details) <= {"audio_tokens", "text_tokens"})
        result["input_token_details"] = {
            key: _integer(details[key]) for key in ("audio_tokens", "text_tokens") if key in details
        }
    return result


def _context(model_id: Any, chunk_duration: Any) -> None:
    if (
        not isinstance(model_id, str)
        or model_id not in _MODELS
        or type(chunk_duration) not in (int, float)
        or not 0 < chunk_duration <= 600
        or not math.isfinite(chunk_duration)
    ):
        raise InvalidArgumentError(
            "A supported audio model and bounded chunk duration are required"
        )


def _native_id(value: Any, model_id: str) -> int | str:
    return _integer(value) if model_id == "whisper-1" else _label(value)


def _integer(value: Any) -> int:
    _require(type(value) is int and 0 <= value <= 2**53 - 1)
    return value


def _number(value: Any) -> float:
    _require(type(value) in (int, float) and abs(value) <= 2**53 - 1 and math.isfinite(value))
    return float(value)


def _time(value: Any, maximum: float) -> float:
    result = _number(value)
    _require(0 <= result <= maximum)
    return result


def _label(value: Any) -> str:
    _require(
        isinstance(value, str)
        and 0 < len(value) <= 256
        and value == value.strip()
        and value.isprintable()
    )
    return value


def _text(value: Any) -> str:
    _require(isinstance(value, str) and len(value) <= MAX_AUDIO_TEXT_CHARS)
    return value


def _array(value: Any, maximum: int, *, saved: bool = False) -> list | tuple:
    _require(isinstance(value, (list, tuple) if saved else list) and len(value) <= maximum)
    return value


def _object(value: Any) -> dict:
    _require(isinstance(value, dict))
    return value


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError
