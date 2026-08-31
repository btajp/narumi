"""Strict API transcription settings without changing local language handling."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator
from narumi.contracts import load_contracts
from narumi.models import MeetingConfig
from narumi.transcription_selection import (
    ISO_639_1_LANGUAGES,
    TranscriptionModelSelection,
    TranscriptionRetry,
    normalize_transcription_language,
)
from pydantic import ValidationError

SELECTION = {
    "provider": "openai-api",
    "connection_id": "conn-0123456789ab",
    "connection_revision": 1,
    "model_id": "whisper-1",
}
RETRY = {
    "input_fingerprint": "a" * 64,
    "chunk_fingerprint": "b" * 64,
    "blocked_epoch": 0,
}


def test_registered_languages_roundtrip_without_rewriting() -> None:
    language_schema = load_contracts().schema_for_def("api_transcription_language")
    validator = Draft202012Validator(language_schema)
    for language in sorted(ISO_639_1_LANGUAGES):
        validator.validate(language)
        config = MeetingConfig.model_validate(
            {"transcription_model": SELECTION, "language": language}
        )
        assert normalize_transcription_language(config.language) == language
        assert MeetingConfig.model_validate_json(config.model_dump_json()).language == language


@pytest.mark.parametrize("model_id", ["whisper-1", "gpt-4o-transcribe-diarize"])
def test_auto_language_is_stored_explicitly_and_omitted_only_for_api(model_id: str) -> None:
    config = MeetingConfig.model_validate(
        {"transcription_model": {**SELECTION, "model_id": model_id}, "language": "auto"}
    )
    restored = MeetingConfig.model_validate_json(config.model_dump_json())
    assert restored.language == "auto"
    assert normalize_transcription_language(restored.language) is None
    assert restored.transcription_model is not None
    assert restored.transcription_model.model_id == model_id


@pytest.mark.parametrize("language", ["xx", "zz", "qq"])
def test_unregistered_two_letter_language_is_rejected_by_runtime(language: str) -> None:
    # Contracts constrain syntax; membership is checked before saving or sending.
    validator = Draft202012Validator(load_contracts().schema_for_def("api_transcription_language"))
    validator.validate(language)
    with pytest.raises(ValueError, match="ISO 639-1"):
        normalize_transcription_language(language)
    with pytest.raises(ValidationError, match="ISO 639-1"):
        MeetingConfig.model_validate({"transcription_model": SELECTION, "language": language})


@pytest.mark.parametrize(
    "language",
    ["ja-JP", "en-US", "JA", "Auto", "jpn", "", "ja\n", "auto\n", " ja", None, 1, True, [], {}],
)
def test_api_language_rejects_instead_of_normalizing_invalid_values(language: Any) -> None:
    with pytest.raises(ValueError, match="ISO 639-1"):
        normalize_transcription_language(language)
    with pytest.raises(ValidationError):
        MeetingConfig.model_validate({"transcription_model": SELECTION, "language": language})


def test_api_language_helper_does_not_coerce_bytes() -> None:
    with pytest.raises(ValueError, match="ISO 639-1"):
        normalize_transcription_language(b"ja")  # type: ignore[arg-type]


@pytest.mark.parametrize("language", ["ja-JP", "JA", "zz", "", "auto", "custom-language"])
@pytest.mark.parametrize("override", [{}, {"transcription_model": None}])
def test_local_language_settings_keep_their_legacy_values(
    language: str, override: dict[str, Any]
) -> None:
    config = MeetingConfig.model_validate(
        {"transcription_engine": "fake", "language": language, **override}
    )
    restored = MeetingConfig.model_validate_json(config.model_dump_json())
    assert restored.language == language
    assert restored.transcription_model is None
    assert restored.transcription_engine == "fake"


@pytest.mark.parametrize("field,value", [("connection_revision", 1.0), ("cache_epoch", 0.0)])
def test_selection_numbers_are_not_coerced_to_integers(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        TranscriptionModelSelection.model_validate({**SELECTION, field: value})


@pytest.mark.parametrize("model_id", ["whisper-1", "gpt-4o-transcribe-diarize"])
@pytest.mark.parametrize(
    "parameter", ["prompt", "language", "max_tokens", "known_speaker_references"]
)
def test_selection_rejects_custom_api_parameters(model_id: str, parameter: str) -> None:
    with pytest.raises(ValidationError):
        TranscriptionModelSelection.model_validate(
            {
                **SELECTION,
                "model_id": model_id,
                "parameters": {parameter: "unsupported-fixture-value"},
            }
        )


def test_retry_confirmation_roundtrips_without_becoming_saved_configuration() -> None:
    confirmation = TranscriptionRetry.model_validate(RETRY)
    assert TranscriptionRetry.model_validate_json(confirmation.model_dump_json()) == confirmation
    with pytest.raises(ValidationError):
        MeetingConfig.model_validate(
            {"transcription_model": SELECTION, "transcription_retry": RETRY}
        )


@pytest.mark.parametrize("blocked_epoch", [True, 0.0, "0", -1])
def test_retry_epoch_is_a_strict_nonnegative_integer(blocked_epoch: Any) -> None:
    with pytest.raises(ValidationError):
        TranscriptionRetry.model_validate({**RETRY, "blocked_epoch": blocked_epoch})
