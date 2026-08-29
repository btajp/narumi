"""Fixed multipart audio requests and safe failures without a real API or credential."""

from __future__ import annotations

import copy
import logging
import struct
import threading
import traceback
from email import policy
from email.parser import BytesParser

import pytest
from narumi.errors import (
    AuthenticationRequiredError,
    CancelledError,
    EngineUnavailableError,
    InvalidArgumentError,
    ModelUnavailableError,
)
from narumi.providers import audio_transcription as audio_module
from narumi.providers.audio_response import (
    TRANSCRIPTION_OUTCOME_UNKNOWN,
    AudioSegment,
    AudioTranscriptionResult,
    AudioWord,
)
from narumi.providers.audio_transcription import (
    AudioTranscriptionBackend,
    fixed_transcription_parameters,
)

from .audio_provider_fakes import synthetic_wav

KEY = "fixture-private-audio-key-not-real"
ENDPOINT = "https://api.openai.com"
MODELS = ("whisper-1", "gpt-4o-transcribe-diarize")
UNKNOWN = {"reason": TRANSCRIPTION_OUTCOME_UNKNOWN, "outcome_unknown": True}


def reply(model_id="whisper-1", *, duration=1.0):
    segment = {"start": 0, "end": duration, "text": "Fixture audio"}
    body = {"duration": duration, "text": segment["text"], "segments": [segment]}
    if model_id == "whisper-1":
        body["language"] = "english"
        body["words"] = [{"start": 0, "end": duration, "word": segment["text"]}]
        segment.update(
            id=0,
            seek=0,
            tokens=[1, 2],
            temperature=0,
            avg_logprob=-0.2,
            compression_ratio=1.1,
            no_speech_prob=0.01,
        )
    else:
        body["task"] = "transcribe"
        segment.update(id="segment-0", type="transcript.text.segment", speaker="A")
    return body


class FakeHTTP:
    def __init__(self, body=None, *, error=None, on_request=None):
        self.body = reply() if body is None else body
        self.error = error
        self.on_request = on_request
        self.calls = []

    def request(self, method, url, **options):
        self.calls.append({"method": method, "url": url, **options})
        if self.on_request is not None:
            self.on_request()
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.body)


def transcribe(http, **updates):
    options = {
        "endpoint": ENDPOINT,
        "api_key": KEY,
        "model_id": "whisper-1",
        "audio": synthetic_wav(),
        "chunk_duration": 1.0,
    }
    options.update(updates)
    return AudioTranscriptionBackend(http=http).transcribe(**options)


def multipart_parts(call):
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + call["headers"]["Content-Type"].encode("ascii")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + call["raw_body"]
    )
    assert message.is_multipart() and not message.defects
    fields, files = {}, []
    for part in message.iter_parts():
        assert part.get_content_disposition() == "form-data"
        name = part.get_param("name", header="content-disposition")
        if part.get_filename() is not None:
            files.append(
                (name, part.get_filename(), part.get_content_type(), part.get_payload(decode=True))
            )
        else:
            fields.setdefault(name, []).append(part.get_payload(decode=True).decode("ascii"))
    return fields, files


def assert_private(error, caplog):
    assert KEY not in str(error.to_payload())
    assert KEY not in "".join(traceback.format_exception(error))
    logging.getLogger(__name__).error(
        "Fixture transcription failed", exc_info=(type(error), error, error.__traceback__)
    )
    assert KEY not in caplog.text


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize("language", ["auto", "ja", "en"])
def test_model_specific_multipart_contains_only_fixed_fields(model_id, language, monkeypatch):
    for name, value in {
        "OPENAI_API_KEY": "fixture-ambient-key",
        "OPENAI_BASE_URL": "https://unapproved.invalid",
        "OPENAI_MODEL": "fixture-ambient-model",
        "OPENAI_ORG_ID": "fixture-ambient-organization",
        "OPENAI_LOG": "debug",
    }.items():
        monkeypatch.setenv(name, value)
    audio = synthetic_wav()
    http = FakeHTTP(reply(model_id))

    def should_cancel():
        return False

    result = transcribe(
        http,
        model_id=model_id,
        audio=audio,
        language=language,
        parameters={},
        endpoint=ENDPOINT + ":443/",
        should_cancel=should_cancel,
    )
    assert len(http.calls) == 1
    call = http.calls[0]
    assert set(call) == {
        "method",
        "url",
        "headers",
        "raw_body",
        "timeout",
        "response_kind",
        "should_cancel",
    }
    assert call["method"] == "POST"
    assert call["url"] == ENDPOINT + "/v1/audio/transcriptions"
    assert set(call["headers"]) == {"Authorization", "Content-Type"}
    assert call["headers"]["Authorization"] == "Bearer " + KEY
    assert call["timeout"] == 600.0
    assert call["response_kind"] == "transcription"
    assert call["should_cancel"] is should_cancel
    fields, files = multipart_parts(call)
    expected = {"model": [model_id]}
    if model_id == "whisper-1":
        expected.update(response_format=["verbose_json"])
        expected["timestamp_granularities[]"] = ["segment", "word"]
    else:
        expected.update(
            response_format=["diarized_json"], chunking_strategy=["auto"], stream=["false"]
        )
    if language != "auto":
        expected["language"] = [language]
    assert fields == expected
    assert files == [("file", "audio.wav", "audio/wav", audio)]
    assert result == AudioTranscriptionResult(
        text="Fixture audio",
        duration=1.0,
        segments=(
            AudioSegment(
                "segment-0" if model_id != "whisper-1" else 0,
                0,
                1,
                "Fixture audio",
                "A" if model_id != "whisper-1" else None,
            ),
        ),
        words=(AudioWord(0, 1, "Fixture audio"),) if model_id == "whisper-1" else None,
        language="english" if model_id == "whisper-1" else None,
    )


def test_fixed_parameters_are_fresh_and_exclude_upload_identity():
    first = fixed_transcription_parameters("whisper-1", "ja")
    first["timestamp_granularities"].append("fixture-unsupported")
    first["model"] = "fixture-model"
    assert fixed_transcription_parameters("whisper-1", "auto") == {
        "response_format": "verbose_json",
        "timestamp_granularities": ["segment", "word"],
    }
    assert fixed_transcription_parameters("gpt-4o-transcribe-diarize", "ja") == {
        "response_format": "diarized_json",
        "chunking_strategy": "auto",
        "stream": False,
        "language": "ja",
    }


@pytest.mark.parametrize(
    "model_id",
    ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-2", "gpt-5.4", "", None, [], {}],
)
def test_unverified_models_fail_before_http(model_id):
    http = FakeHTTP()
    with pytest.raises(ModelUnavailableError):
        transcribe(http, model_id=model_id)
    assert not http.calls


@pytest.mark.parametrize(
    "language", ["", "ja-JP", "JA", "japanese", "zz", "ja\n", " ja", None, True, 1, []]
)
def test_invalid_language_is_rejected_without_guessing_or_sending(language):
    http = FakeHTTP()
    with pytest.raises(InvalidArgumentError):
        transcribe(http, language=language)
    assert not http.calls


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize(
    "parameters",
    [
        {"prompt": "fixture vocabulary"},
        {"temperature": 0},
        {"stream": True},
        {"response_format": "text"},
        {"language": "ja"},
        {"timestamp_granularities": ["word"]},
        {"chunking_strategy": "auto"},
        {"known_speaker_names": ["A"]},
        {"known_speaker_references": ["data:audio/wav;base64,fixture"]},
        {"store": False},
        {"background": False},
        {"tools": []},
        {"max_output_tokens": 100},
        {"reasoning": {"effort": "none"}},
        {"file": "private-meeting.wav"},
        {"model": "whisper-1"},
        {"future_option": True},
        [],
        "{}",
        False,
    ],
)
def test_all_custom_parameters_are_rejected_before_http(model_id, parameters):
    http = FakeHTTP()
    with pytest.raises(InvalidArgumentError):
        transcribe(http, model_id=model_id, parameters=parameters)
    assert not http.calls


@pytest.mark.parametrize("api_key", [None, ""])
def test_missing_saved_key_never_falls_back_to_environment(api_key, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-ambient-key")
    http = FakeHTTP()
    with pytest.raises(AuthenticationRequiredError) as failure:
        transcribe(http, api_key=api_key)
    assert failure.value.details == {"reason": "credential_required"}
    assert not http.calls


@pytest.mark.parametrize(
    "api_key",
    [
        KEY + "\n",
        " " + KEY,
        KEY + "\x00",
        KEY + "\x7f",
        KEY + "日本語",
        "x" * 4097,
        1,
        True,
        b"fixture-key",
    ],
)
def test_malformed_saved_key_is_private_and_never_sent(api_key, caplog):
    http = FakeHTTP()
    with pytest.raises(InvalidArgumentError) as failure:
        transcribe(http, api_key=api_key)
    assert not http.calls
    assert_private(failure.value, caplog)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.openai.com",
        "https://unapproved.invalid",
        "http://127.0.0.1:8765",
        ENDPOINT + "/v1",
        ENDPOINT + ":8443",
        ENDPOINT + "?key=" + KEY,
        "https://" + KEY + "@api.openai.com",
        ENDPOINT + ".unapproved.invalid",
        " " + ENDPOINT,
        ENDPOINT + "\n" + KEY,
        None,
    ],
)
def test_noncanonical_origin_is_rejected_without_exposing_input(endpoint, caplog):
    http = FakeHTTP()
    with pytest.raises(InvalidArgumentError) as failure:
        transcribe(http, endpoint=endpoint)
    assert failure.value.details == {"reason": "invalid_endpoint"}
    assert not http.calls
    assert_private(failure.value, caplog)


@pytest.mark.parametrize(
    "duration",
    [
        None,
        True,
        False,
        "1",
        0,
        -1,
        0.5,
        1.0001,
        float("nan"),
        float("inf"),
        float("-inf"),
        600.001,
        pytest.param(10**1000, id="huge-int"),
    ],
)
def test_chunk_duration_is_finite_bounded_and_matches_sample_count(duration):
    http = FakeHTTP()
    with pytest.raises(InvalidArgumentError):
        transcribe(http, chunk_duration=duration)
    assert not http.calls


@pytest.mark.parametrize("sample_count", [1, 16_000, 9_600_000])
def test_canonical_pcm_chunks_accept_sample_boundaries(sample_count):
    duration = sample_count / 16_000
    audio = synthetic_wav(sample_count=sample_count)
    http = FakeHTTP(reply(duration=duration))
    result = transcribe(http, audio=audio, chunk_duration=duration)
    assert result.duration == duration
    assert multipart_parts(http.calls[0])[1][0][-1] == audio


@pytest.mark.parametrize(
    "audio",
    [
        None,
        "audio",
        b"",
        b"not-a-wave-file",
        synthetic_wav()[:44],
        bytearray(synthetic_wav()),
        memoryview(synthetic_wav()),
    ],
)
def test_audio_requires_complete_immutable_wav_bytes(audio):
    http = FakeHTTP()
    with pytest.raises(InvalidArgumentError):
        transcribe(http, audio=audio)
    assert not http.calls


@pytest.mark.parametrize(
    "offset,format,value",
    [
        (0, "4s", b"RF64"),
        (4, "I", 0),
        (8, "4s", b"wave"),
        (12, "4s", b"junk"),
        (16, "I", 18),
        (20, "H", 3),
        (22, "H", 2),
        (24, "I", 8_000),
        (28, "I", 16_000),
        (32, "H", 4),
        (34, "H", 8),
        (36, "4s", b"JUNK"),
        (40, "I", 31_999),
    ],
)
def test_noncanonical_wav_headers_never_reach_http(offset, format, value):
    data = bytearray(synthetic_wav())
    struct.pack_into("<" + format, data, offset, value)
    http = FakeHTTP()
    with pytest.raises(InvalidArgumentError):
        transcribe(http, audio=bytes(data))
    assert not http.calls


@pytest.mark.parametrize("kind", ["truncated", "trailing", "metadata", "too_large"])
def test_truncation_metadata_and_oversized_audio_are_not_uploaded(kind):
    audio = synthetic_wav()
    if kind == "truncated":
        audio = audio[:-2]
    elif kind == "trailing":
        audio += b"fixture-private-meeting-path"
    elif kind == "metadata":
        metadata = struct.pack("<4sI", b"LIST", 4) + b"INFO"
        expanded = bytearray(audio[:36] + metadata + audio[36:])
        struct.pack_into("<I", expanded, 4, len(expanded) - 8)
        audio = bytes(expanded)
    else:
        audio = synthetic_wav(sample_count=12_000_000)
        assert len(audio) > 24_000_000
    http = FakeHTTP()
    with pytest.raises(InvalidArgumentError):
        transcribe(http, audio=audio)
    assert not http.calls


@pytest.mark.parametrize("always_collides", [False, True])
def test_multipart_boundary_cannot_intersect_audio(always_collides, monkeypatch):
    suffix = "a" * 48
    marker = ("narumi-audio-" + suffix).encode("ascii")
    audio = synthetic_wav()
    audio = audio[:44] + marker + audio[44 + len(marker) :]
    draws = []

    def token_hex(size):
        draws.append(size)
        return suffix if always_collides or len(draws) == 1 else "b" * 48

    monkeypatch.setattr(audio_module.secrets, "token_hex", token_hex)
    http = FakeHTTP()
    if always_collides:
        with pytest.raises(InvalidArgumentError):
            transcribe(http, audio=audio)
        assert len(draws) == 4 and not http.calls
    else:
        transcribe(http, audio=audio)
        assert len(draws) == 2 and len(http.calls) == 1
        assert "boundary=narumi-audio-" + "b" * 48 in http.calls[0]["headers"]["Content-Type"]
        assert multipart_parts(http.calls[0])[1][0][-1] == audio


@pytest.mark.parametrize("cancel_at", [1, 2])
def test_cancellation_before_upload_sends_nothing(cancel_at):
    checks = []

    def cancelled():
        checks.append(True)
        return len(checks) >= cancel_at

    http = FakeHTTP()
    with pytest.raises(CancelledError) as failure:
        transcribe(http, should_cancel=cancelled)
    assert len(checks) == cancel_at and not http.calls
    assert failure.value.details == {"reason": "provider_transcription_cancelled"}


def test_cancellation_callback_error_is_private_and_prevents_upload(caplog):
    def failed_check():
        raise RuntimeError(KEY)

    http = FakeHTTP()
    with pytest.raises(CancelledError) as failure:
        transcribe(http, should_cancel=failed_check)
    assert failure.value.details == {"reason": "provider_transcription_cancelled"}
    assert not http.calls
    assert_private(failure.value, caplog)


@pytest.mark.parametrize("started", [False, True])
def test_transport_cancellation_preserves_the_send_boundary_and_never_retries(started, caplog):
    details = (
        {"reason": "provider_generation_outcome_unknown", "outcome_unknown": True}
        if started
        else {"reason": "provider_generation_cancelled"}
    )
    http = FakeHTTP(error=CancelledError(KEY, details=details))
    with pytest.raises(CancelledError) as failure:
        transcribe(http)
    assert failure.value.details == (
        UNKNOWN if started else {"reason": "provider_transcription_cancelled"}
    )
    assert len(http.calls) == 1
    assert_private(failure.value, caplog)


def test_complete_response_is_returned_for_saving_if_cancel_arrives_after_receipt():
    cancelled = threading.Event()
    http = FakeHTTP(on_request=cancelled.set)
    result = transcribe(http, should_cancel=cancelled.is_set)
    assert cancelled.is_set() and result.text == "Fixture audio"
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    "status",
    [400, 401, 403, 413, 429, 404, 408, 409, 422, 500, 502, 503, 504, True, "400", 400.0, None],
)
def test_http_rejections_are_distinguished_from_unknown_completion_without_retry(status, caplog):
    http = FakeHTTP(
        error=EngineUnavailableError(
            KEY, details={"reason": "metadata_http_error", "status": status, "body": KEY}
        )
    )
    with pytest.raises(EngineUnavailableError) as failure:
        transcribe(http)
    if type(status) is int and status in {400, 401, 403, 413, 429}:
        assert failure.value.details == {
            "reason": "provider_transcription_rejected",
            "status": status,
        }
    else:
        assert failure.value.details == UNKNOWN
    assert len(http.calls) == 1
    assert_private(failure.value, caplog)


@pytest.mark.parametrize("reason", ["metadata_connection_failed", "invalid_http_options"])
def test_verified_presend_transport_failure_is_not_marked_as_unknown(reason, caplog):
    http = FakeHTTP(error=EngineUnavailableError(KEY, details={"reason": reason, "body": KEY}))
    with pytest.raises(EngineUnavailableError) as failure:
        transcribe(http)
    assert failure.value.details == {"reason": "provider_transcription_not_sent"}
    assert len(http.calls) == 1
    assert_private(failure.value, caplog)


def test_confirmed_credential_rejection_keeps_only_safe_authentication_status(caplog):
    http = FakeHTTP(
        error=AuthenticationRequiredError(
            KEY, details={"reason": "credential_rejected", "body": KEY}
        )
    )
    with pytest.raises(AuthenticationRequiredError) as failure:
        transcribe(http)
    assert failure.value.details == {"reason": "credential_rejected"}
    assert len(http.calls) == 1
    assert_private(failure.value, caplog)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(KEY),
        TimeoutError(KEY),
        OSError(KEY),
        InvalidArgumentError(KEY),
        EngineUnavailableError(KEY, details={"reason": "provider_generation_outcome_unknown"}),
        EngineUnavailableError(
            KEY, details={"reason": "fixture_unknown_reason", "outcome_unknown": True}
        ),
        EngineUnavailableError(KEY, details={"reason": KEY, "body": KEY}),
    ],
)
def test_unclassified_postsend_failures_are_private_unknown_and_not_retried(error, caplog):
    http = FakeHTTP(error=error)
    with pytest.raises(EngineUnavailableError) as failure:
        transcribe(http)
    assert failure.value.details == UNKNOWN
    assert len(http.calls) == 1
    assert_private(failure.value, caplog)


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize("location", ["text", "segment", "context", "field_name"])
def test_reflected_key_anywhere_in_reply_is_unknown_not_a_saved_result(model_id, location, caplog):
    body = reply(model_id)
    if location == "text":
        body["text"] = "Bearer " + KEY
    elif location == "segment":
        body["segments"][0]["text"] = KEY
    elif location == "field_name":
        body[KEY] = "unused extension"
    else:
        body["context"] = {"nested": ["Bearer " + KEY]}
    http = FakeHTTP(body)
    with pytest.raises(EngineUnavailableError) as failure:
        transcribe(http, model_id=model_id)
    assert failure.value.details == UNKNOWN
    assert len(http.calls) == 1
    assert_private(failure.value, caplog)


@pytest.mark.parametrize(
    "body",
    ["fixture upstream text", [], {"text": "missing timestamps"}, {"error": {"message": KEY}}],
)
def test_invalid_native_reply_does_not_become_success_or_retry(body, caplog):
    http = FakeHTTP(body)
    with pytest.raises(EngineUnavailableError) as failure:
        transcribe(http)
    assert failure.value.details == UNKNOWN
    assert len(http.calls) == 1
    assert_private(failure.value, caplog)
