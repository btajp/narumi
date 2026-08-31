"""One explicit multipart audio upload with no retries, SDK or ambient credentials."""

from __future__ import annotations

import math
import secrets
import struct
from collections.abc import Callable
from typing import Any

from narumi.errors import (
    AuthenticationRequiredError,
    CancelledError,
    EngineUnavailableError,
    InvalidArgumentError,
    ModelUnavailableError,
    NarumiError,
)
from narumi.providers.audio_response import (
    TRANSCRIPTION_OUTCOME_UNKNOWN,
    AudioTranscriptionResult,
    parse_audio_response,
    transcription_unknown,
)
from narumi.providers.metadata.audio_capabilities import audio_model_capabilities
from narumi.providers.metadata.endpoints import OPENAI_ENDPOINT, validate_endpoint
from narumi.providers.metadata.http import JSONHTTPClient

AUDIO_ADAPTER_VERSION = "1"
MAX_AUDIO_BYTES = 24_000_000
AUDIO_SAMPLE_RATE = 16_000
AUDIO_TIMEOUT = 600.0
_KNOWN_REJECTIONS = {400, 401, 403, 413, 429}
CancelCheck = Callable[[], bool]


def fixed_transcription_parameters(model_id: str, language: str) -> dict[str, Any]:
    """Canonical fixed options for fingerprints, excluding the model and file parts."""
    from narumi.transcription_selection import normalize_transcription_language

    capabilities = audio_model_capabilities(model_id)
    if capabilities is None or capabilities.availability != "available":
        raise ModelUnavailableError("The audio model does not have verified timestamp support")
    try:
        normalized = normalize_transcription_language(language)
    except ValueError:
        raise InvalidArgumentError(
            "The transcription language must be auto or an ISO 639-1 code"
        ) from None
    result = capabilities.wire_parameters
    if normalized is not None:
        result["language"] = normalized
    return result


class AudioTranscriptionBackend:
    """Only immutable, metadata-free PCM WAV chunks reach the saved OpenAI endpoint."""

    def __init__(self, *, http: JSONHTTPClient | None = None) -> None:
        self._http = http if http is not None else JSONHTTPClient()

    def transcribe(
        self,
        endpoint: str,
        api_key: str,
        model_id: str,
        audio: bytes,
        *,
        language: str = "auto",
        parameters: dict[str, Any] | None = None,
        chunk_duration: float,
        should_cancel: CancelCheck | None = None,
    ) -> AudioTranscriptionResult:
        _check_cancelled(should_cancel)
        origin = validate_endpoint("openai-api", endpoint)
        if origin != OPENAI_ENDPOINT:
            raise InvalidArgumentError("Audio transcription requires the saved OpenAI origin")
        _credential(api_key)
        if parameters is not None and (not isinstance(parameters, dict) or parameters):
            raise InvalidArgumentError("Custom transcription parameters are not supported")
        options = fixed_transcription_parameters(model_id, language)
        _validate_audio(audio, chunk_duration)
        raw_body, content_type = _multipart(model_id, options, audio)
        _check_cancelled(should_cancel)
        try:
            body = self._http.request(
                "POST",
                origin + "/v1/audio/transcriptions",
                headers={"Authorization": "Bearer " + api_key, "Content-Type": content_type},
                raw_body=raw_body,
                timeout=AUDIO_TIMEOUT,
                response_kind="transcription",
                should_cancel=should_cancel,
            )
        except NarumiError as error:
            raise _safe_error(error) from None
        except Exception:
            raise transcription_unknown() from None
        # A fully received result is returned for durable saving even when a user
        # cancels just after the response. The coordinator checks before its next call.
        return parse_audio_response(model_id, body, chunk_duration=chunk_duration, api_key=api_key)


def _credential(key: Any) -> None:
    if key is None or key == "":
        raise AuthenticationRequiredError(
            "A saved API key is required", details={"reason": "credential_required"}
        )
    if (
        not isinstance(key, str)
        or len(key) > 4096
        or any(not 33 <= ord(character) <= 126 for character in key)
    ):
        raise InvalidArgumentError("The saved API key has an invalid format")


def _validate_audio(audio: Any, duration: Any) -> None:
    valid = (
        type(audio) is bytes
        and 44 < len(audio) <= MAX_AUDIO_BYTES
        and type(duration) in (int, float)
        and 0 < duration <= 600
        and math.isfinite(duration)
    )
    if not valid:
        raise InvalidArgumentError("A bounded PCM WAV chunk and its duration are required")
    try:
        (
            riff,
            size,
            wave,
            fmt,
            fmt_size,
            codec,
            channels,
            rate,
            byte_rate,
            align,
            bits,
            data,
            data_size,
        ) = struct.unpack("<4sI4s4sIHHIIHH4sI", audio[:44])
        valid = (
            (riff, wave, fmt, data) == (b"RIFF", b"WAVE", b"fmt ", b"data")
            and size == len(audio) - 8
            and fmt_size == 16
            and (codec, channels, rate, byte_rate, align, bits) == (1, 1, 16_000, 32_000, 2, 16)
            and data_size == len(audio) - 44
            and data_size % 2 == 0
            and data_size / 2 / AUDIO_SAMPLE_RATE == duration
        )
    except Exception:
        valid = False
    if not valid:
        raise InvalidArgumentError(
            "The audio chunk is not a complete, metadata-free PCM WAV"
        ) from None


def _multipart(model_id: str, options: dict[str, Any], audio: bytes) -> tuple[bytes, str]:
    # Every field comes from the fixed capability table or a validated language.
    # Only the boundary is random; it is not a generation parameter or artifact.
    for _ in range(4):
        boundary = "narumi-audio-" + secrets.token_hex(24)
        marker = boundary.encode("ascii")
        if marker not in audio:
            break
    else:
        raise InvalidArgumentError("The audio request could not be encoded")
    fields = [("model", model_id)]
    for name, value in options.items():
        if isinstance(value, list):
            fields.extend((name + "[]", part) for part in value)
        else:
            fields.append((name, "true" if value is True else "false" if value is False else value))
    parts: list[bytes] = []
    for name, value in fields:
        parts.append(
            b"--"
            + marker
            + b'\r\nContent-Disposition: form-data; name="'
            + name.encode("ascii")
            + b'"\r\n\r\n'
            + value.encode("ascii")
            + b"\r\n"
        )
    parts.extend(
        (
            b"--"
            + marker
            + b'\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
            + b"Content-Type: audio/wav\r\n\r\n",
            audio,
            b"\r\n--" + marker + b"--\r\n",
        )
    )
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def _safe_error(error: NarumiError) -> NarumiError:
    details = error.details if isinstance(error.details, dict) else {}
    reason = details.get("reason")
    unknown = bool(details.get("outcome_unknown")) or reason in (
        TRANSCRIPTION_OUTCOME_UNKNOWN,
        "provider_generation_outcome_unknown",
    )
    if isinstance(error, CancelledError):
        if unknown:
            return CancelledError(
                "The audio upload was cancelled; provider completion is unknown",
                details={"reason": TRANSCRIPTION_OUTCOME_UNKNOWN, "outcome_unknown": True},
            )
        if reason == "provider_generation_cancelled":
            return _cancelled()
    if unknown:
        return transcription_unknown()
    if isinstance(error, AuthenticationRequiredError) and reason == "credential_rejected":
        return AuthenticationRequiredError(
            "The provider rejected the saved credentials", details={"reason": "credential_rejected"}
        )
    status = details.get("status")
    if reason == "metadata_http_error" and type(status) is int and status in _KNOWN_REJECTIONS:
        return EngineUnavailableError(
            "The provider rejected this transcription request",
            details={"reason": "provider_transcription_rejected", "status": status},
        )
    if reason in ("metadata_connection_failed", "invalid_http_options"):
        return EngineUnavailableError(
            "The audio transcription request could not be sent",
            details={"reason": "provider_transcription_not_sent"},
        )
    return transcription_unknown()


def _check_cancelled(should_cancel: CancelCheck | None) -> None:
    try:
        cancelled = should_cancel is not None and should_cancel()
    except Exception:
        raise _cancelled() from None
    if cancelled:
        raise _cancelled()


def _cancelled() -> CancelledError:
    return CancelledError(
        "Audio transcription was cancelled before sending",
        details={"reason": "provider_transcription_cancelled"},
    )
