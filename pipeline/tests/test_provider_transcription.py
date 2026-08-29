"""Explicit audio selection, per-chunk permission checks and secret-free results."""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from narumi.errors import (
    BusyError,
    CancelledError,
    ConfigurationConflictError,
    EngineUnavailableError,
    NarumiError,
)
from narumi.models import MeetingConfig
from narumi.providers.generation import MinutesResolver
from narumi.providers.service import ProviderService
from narumi.providers.transcription import TranscriptionResolver
from narumi.transcription_selection import TranscriptionModelSelection

from .audio_provider_fakes import (
    FakeAudioBackend,
    audio_model_descriptor,
    audio_result,
    prepared_audio_connection,
    synthetic_wav,
)
from .provider_fakes import (
    FakeCodexBackend,
    FakeMetadata,
    FakeRuntimeInspector,
    ManualExecutor,
    MemorySecretStore,
    prepared_http_connection,
)


@pytest.fixture
def audio_setup(tmp_path):
    backend = FakeAudioBackend()
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        metadata_client=FakeMetadata(),
        runtime_inspector=FakeRuntimeInspector(),
        auth_executor=ManualExecutor(),
        codex_backend=FakeCodexBackend(),
        audio_backend=backend,
    )
    record = prepared_audio_connection(
        service,
        models=[audio_model_descriptor(), audio_model_descriptor("gpt-4o-transcribe-diarize")],
    )
    config = MeetingConfig(
        transcription_model=TranscriptionModelSelection(
            provider="openai-api",
            connection_id=record["connection_id"],
            connection_revision=record["revision"],
            model_id="whisper-1",
        ),
        external_send_policy="api_ok",
        vocab_hints=["This vocabulary belongs to integration, not an audio prompt"],
    )
    service.secrets.calls.clear()
    yield service, backend, config
    service.close()


@pytest.mark.parametrize("model_id", ["whisper-1", "gpt-4o-transcribe-diarize"])
@pytest.mark.parametrize("language", ["ja", "auto"])
def test_saved_audio_selection_uses_exact_connection_model_language(
    audio_setup, model_id, language
):
    service, backend, config = audio_setup
    config.transcription_model.model_id = model_id
    config.language = language
    resolver = TranscriptionResolver(service)
    params = resolver.validate(config)
    assert service.secrets.calls == backend.calls == []
    assert params["model_id"] == model_id and params["language"] == language
    assert ("language" in params["effective_parameters"]) is (language != "auto")
    assert "cache_epoch" not in json.dumps(params)
    assert "This vocabulary" not in json.dumps(params)
    assert "prompt" not in params["effective_parameters"]
    client = resolver.resolve(config)
    audio = synthetic_wav()
    result = client.transcribe_chunk(audio, 1.0)
    assert result == audio_result(model_id)
    assert backend.calls == [
        {
            "endpoint": "https://api.openai.com",
            "api_key": "fixture-key",
            "model_id": model_id,
            "audio": audio,
            "language": language,
            "parameters": {},
            "chunk_duration": 1.0,
        }
    ]
    assert len(service.secrets.calls) == 1 and service.secrets.calls[0][0] == "get"
    document = service.store.read()
    assert document["checks"] == {}
    assert (
        document["connections"][config.transcription_model.connection_id]["last_generation_state"]
        == "never"
    )
    assert "fixture-key" not in service.store.path.read_text()


def test_epoch_vocabulary_and_observation_times_do_not_change_audio_identity(audio_setup):
    service, _, config = audio_setup
    resolver = TranscriptionResolver(service)
    before = resolver.validate(config)
    config.transcription_model.cache_epoch = 42
    config.vocab_hints = ["Different integration vocabulary"]
    with service.store.transaction() as document:
        cached = document["catalogs"][config.transcription_model.connection_id]
        cached["fetched_at"] = "2026-08-30T00:00:00Z"
        cached["models"][0]["fetched_at"] = "2026-08-30T00:00:00Z"
    assert resolver.validate(config) == before
    assert service.secrets.calls == []


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("local_policy", "policy_violation"),
        ("subscription_policy", "policy_violation"),
        ("language", "invalid_argument"),
        ("parameters", "invalid_argument"),
        ("epoch_bool", "invalid_argument"),
        ("provider", "configuration_conflict"),
        ("disabled", "configuration_conflict"),
        ("revision", "configuration_conflict"),
        ("endpoint", "invalid_argument"),
        ("auth_method", "authentication_required"),
        ("auth_state", "authentication_required"),
        ("credential_present", "authentication_required"),
        ("active_auth", "authentication_required"),
        ("secret_account", "authentication_required"),
        ("runtime", "engine_unavailable"),
        ("runtime_changed", "engine_unavailable"),
        ("catalog", "model_unavailable"),
        ("catalog_revision", "model_unavailable"),
        ("catalog_runtime", "model_unavailable"),
        ("missing_model", "model_unavailable"),
        ("duplicate_model", "model_unavailable"),
        ("availability", "model_unavailable"),
        ("source", "model_unavailable"),
        ("role", "model_unavailable"),
        ("input", "model_unavailable"),
        ("output", "model_unavailable"),
        ("timestamps", "model_unavailable"),
        ("parameter_schema", "model_unavailable"),
        ("billing", "model_unavailable"),
        ("expired", "model_unavailable"),
        ("invalid_expiry", "model_unavailable"),
    ],
)
def test_invalid_audio_selection_never_reads_keys_or_uploads(audio_setup, mutation, code):
    service, backend, config = audio_setup
    if mutation.endswith("policy"):
        config.external_send_policy = (
            "local_only" if mutation == "local_policy" else "subscription_ok"
        )
    if mutation == "language":
        config = config.model_copy(update={"language": "zz"})
    if mutation == "parameters":
        config.transcription_model.parameters["prompt"] = "Not supported"
    if mutation == "epoch_bool":
        config.transcription_model.cache_epoch = True
    if mutation == "runtime_changed":
        service.runtime.inspector.version = "9.0.0"
    with service.store.transaction() as document:
        record = document["connections"][config.transcription_model.connection_id]
        catalog = document["catalogs"][record["connection_id"]]
        model = catalog["models"][0]
        updates = {
            "provider": ("provider_id", "anthropic-api"),
            "disabled": ("enabled", False),
            "revision": ("revision", 2),
            "endpoint": ("endpoint", "https://example.com"),
            "auth_method": ("auth_method", "none"),
            "auth_state": ("auth_state", "unverified"),
            "credential_present": ("credential_present", False),
            "active_auth": ("active_auth", {"state": "pending"}),
            "secret_account": ("secret_account", "providers:other-connection"),
            "catalog": ("catalog_state", "stale"),
        }
        if mutation in updates:
            field, value = updates[mutation]
            record[field] = value
        if mutation == "runtime":
            document["runtimes"]["openai-api"]["state"] = "not_prepared"
        if mutation == "catalog_revision":
            catalog["connection_revision"] = 2
        if mutation == "catalog_runtime":
            catalog["runtime_catalog_revision"] = "0" * 64
        if mutation == "missing_model":
            catalog["models"] = []
        if mutation == "duplicate_model":
            catalog["models"].append(copy.deepcopy(model))
        updates = {
            "availability": ("availability", "unverified"),
            "source": ("source", "runtime"),
            "role": ("roles", ["llm"]),
            "input": ("input_modalities", ["text"]),
            "output": ("output_modalities", []),
            "timestamps": ("timestamp_support", "none"),
            "parameter_schema": ("parameter_schema", {}),
            "billing": ("billing", {"kind": "subscription"}),
            "expired": ("availability_expires_on", "2000-01-01"),
            "invalid_expiry": ("availability_expires_on", "2000-01-99"),
        }
        if mutation in updates:
            field, value = updates[mutation]
            model[field] = value
    with pytest.raises(NarumiError) as failure:
        TranscriptionResolver(service).resolve(config)
    assert failure.value.code == code
    assert backend.calls == service.secrets.calls == []


@pytest.mark.parametrize("change", ["disable", "revision", "runtime", "expiry"])
def test_every_chunk_revalidates_before_another_upload(audio_setup, change):
    service, backend, config = audio_setup
    client = TranscriptionResolver(service).resolve(config)
    client.transcribe_chunk(synthetic_wav(), 1.0)
    with service.store.transaction() as document:
        record = document["connections"][config.transcription_model.connection_id]
        if change == "disable":
            record["enabled"] = False
        elif change == "revision":
            record["revision"] += 1
        elif change == "runtime":
            document["runtimes"]["openai-api"]["state"] = "not_prepared"
        else:
            document["catalogs"][record["connection_id"]]["models"][0][
                "availability_expires_on"
            ] = "2000-01-01"
    before = list(service.secrets.calls)
    with pytest.raises(NarumiError):
        client.transcribe_chunk(synthetic_wav(), 1.0)
    assert len(backend.calls) == 1
    assert service.secrets.calls == before


def test_connection_lease_blocks_mutations_and_cleanup_during_audio_upload(audio_setup):
    service, backend, config = audio_setup
    connection_id = config.transcription_model.connection_id

    def in_flight(_call):
        assert service.store.read()["checks"]["openai-api"]["kind"] == "generation"
        with pytest.raises(BusyError):
            service.authenticate(
                {
                    "connection_id": connection_id,
                    "expected_revision": 1,
                    "action": "logout",
                    "request_id": "logout-during-audio",
                }
            )
        with pytest.raises(BusyError):
            service.set_connection(
                {
                    "connection_id": connection_id,
                    "expected_revision": 1,
                    "api_key": "replacement-key",
                    "request_id": "key-during-audio",
                }
            )
        service.set_connection(
            {
                "connection_id": connection_id,
                "expected_revision": 1,
                "enabled": False,
                "request_id": "disable-during-audio",
            }
        )

    backend.on_call = in_flight
    client = TranscriptionResolver(service).resolve(config)
    assert client.transcribe_chunk(synthetic_wav(), 1.0).text
    with pytest.raises(ConfigurationConflictError):
        client.transcribe_chunk(synthetic_wav(), 1.0)
    assert len(backend.calls) == 1 and service.store.read()["checks"] == {}


def test_missing_selected_key_never_uses_ambient_or_another_key(audio_setup, monkeypatch):
    service, backend, config = audio_setup
    client = TranscriptionResolver(service).resolve(config)
    document = service.store.read()
    record = document["connections"][config.transcription_model.connection_id]
    service.secrets.values.pop(record["secret_account"])
    service.secrets.values["another-connection"] = "other-fixture-key"
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-fixture-key")
    with pytest.raises(NarumiError) as failure:
        client.transcribe_chunk(synthetic_wav(), 1.0)
    assert failure.value.code == "authentication_required"
    assert backend.calls == []
    assert service.store.read()["checks"] == {}


@pytest.mark.parametrize("where", ["text", "segment", "word", "language"])
def test_reflected_keys_in_any_native_field_are_unknown_and_never_returned(audio_setup, where):
    service, backend, config = audio_setup
    result = audio_result()
    if where == "text":
        result = replace(result, text="fixture-key")
    elif where == "segment":
        result = replace(result, segments=(replace(result.segments[0], text="fixture-key"),))
    elif where == "word":
        result = replace(result, words=(replace(result.words[0], text="fixture-key"),))
    else:
        result = replace(result, language="fixture-key")
    backend.response = result
    with pytest.raises(NarumiError) as failure:
        TranscriptionResolver(service).resolve(config).transcribe_chunk(synthetic_wav(), 1.0)
    assert failure.value.details["outcome_unknown"] is True
    assert "fixture-key" not in str(failure.value)
    assert "fixture-key" not in service.store.path.read_text()
    assert service.store.read()["checks"] == {}


def test_cancellation_after_a_valid_reply_preserves_it_for_the_ledger(audio_setup):
    service, backend, config = audio_setup
    cancelled = []
    backend.on_call = lambda _: cancelled.append(True)
    client = TranscriptionResolver(service).resolve(config, should_cancel=lambda: bool(cancelled))
    assert client.transcribe_chunk(synthetic_wav(), 1.0) == audio_result()
    with pytest.raises(CancelledError):
        client.transcribe_chunk(synthetic_wav(), 1.0)
    assert len(backend.calls) == 1


@pytest.mark.parametrize("when", ["before_call", "after_key"])
def test_pre_send_cancellation_does_not_upload_and_releases_the_lease(audio_setup, when):
    service, backend, config = audio_setup
    client = TranscriptionResolver(service).resolve(
        config,
        should_cancel=lambda: when == "before_call" or bool(service.secrets.calls),
    )
    with pytest.raises(CancelledError) as failure:
        client.transcribe_chunk(synthetic_wav(), 1.0)
    assert not failure.value.details.get("outcome_unknown", False)
    assert backend.calls == []
    assert len(service.secrets.calls) == (1 if when == "after_key" else 0)
    assert service.store.read()["checks"] == {}


@pytest.mark.parametrize("error_type", [EngineUnavailableError, CancelledError])
def test_post_send_errors_are_redacted_and_lease_is_released(audio_setup, error_type):
    service, backend, config = audio_setup
    backend.failures[1] = error_type(
        "fixture-key upstream diagnostic", details={"outcome_unknown": True}
    )
    with pytest.raises(NarumiError) as failure:
        TranscriptionResolver(service).resolve(config).transcribe_chunk(synthetic_wav(), 1.0)
    assert failure.value.details["outcome_unknown"] is True
    assert "fixture-key" not in str(failure.value)
    assert service.store.read()["checks"] == {}


def test_minutes_and_audio_validate_inside_one_caller_transaction(audio_setup):
    service, _, config = audio_setup
    record = prepared_http_connection(service, request_id="text-connection")
    from narumi.model_selection import ModelSelection

    config.minutes_model = ModelSelection(
        provider="openai-api",
        connection_id=record["connection_id"],
        connection_revision=record["revision"],
        model_id="gpt-4.1",
    )
    service.secrets.calls.clear()
    with service.store.transaction() as document:
        minutes = MinutesResolver(service).validate_in_transaction(config, document)
        audio = TranscriptionResolver(service).validate_in_transaction(config, document)
    assert minutes["model_id"] == "gpt-4.1" and audio["model_id"] == "whisper-1"
    assert service.secrets.calls == []


def test_audio_backend_is_lazy_and_never_constructed_by_read_only_views(tmp_path, monkeypatch):
    constructed, backend = [], FakeAudioBackend()

    def construct():
        constructed.append(True)
        return backend

    monkeypatch.setitem(
        sys.modules,
        "narumi.providers.audio_transcription",
        SimpleNamespace(AudioTranscriptionBackend=construct),
    )
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        auth_executor=ManualExecutor(),
        runtime_inspector=FakeRuntimeInspector(),
        codex_backend=FakeCodexBackend(),
    )
    try:
        service.list_providers()
        service.list_connections()
        assert constructed == []
        assert service.audio_backend is backend
        assert service.audio_backend is backend
        assert constructed == [True]
    finally:
        service.close()
    with pytest.raises(NarumiError):
        _ = service.audio_backend
