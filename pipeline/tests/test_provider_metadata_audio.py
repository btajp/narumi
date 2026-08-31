"""Audio discovery remains explicit, timestamp gated and independent of text models."""

from __future__ import annotations

from datetime import timedelta

import pytest
from jsonschema import Draft202012Validator
from narumi.contracts import load_contracts
from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.providers.metadata import MetadataClient
from narumi.providers.metadata.audio_capabilities import audio_model_capabilities
from narumi.providers.metadata.openai_capabilities import model_capabilities
from narumi.providers.service import ProviderService

from .provider_fakes import (
    INSTANCE_ONE,
    FakeCodexBackend,
    FakeRuntimeInspector,
    JobQueue,
    ManualExecutor,
    MemorySecretStore,
    create_connection,
)
from .test_provider_metadata import KEY, NOW, FakeHTTP, client, validate_models
from .test_provider_metadata_openai import API, openai_model, page

SUPPORTED = ("whisper-1", "gpt-4o-transcribe-diarize")
UNTIMED = ("gpt-4o-transcribe", "gpt-4o-mini-transcribe")


@pytest.mark.parametrize(
    ("model_id", "timestamp_support"),
    [("whisper-1", "word"), ("gpt-4o-transcribe-diarize", "diarized_segment")],
)
def test_only_verified_native_timestamp_audio_models_become_selectable(model_id, timestamp_support):
    metadata, http = client(page(openai_model(model_id)))
    models = metadata.fetch("openai-api", API, KEY)
    validate_models(models)
    model = models[0]
    assert model["model_id"] == model_id
    assert model["availability"] == "available" and model["reason"] is None
    assert model["roles"] == ["transcription"]
    assert model["input_modalities"] == ["audio"] and model["output_modalities"] == ["text"]
    assert model["timestamp_support"] == timestamp_support
    assert model["context_window"] is model["max_output_tokens"] is None
    assert model["resolved_revision"] is None
    assert model["parameter_schema"]["properties"] == {}
    assert model["source"] == "provider_api" and model["billing"]["kind"] == "api"
    assert all(value is None for key, value in model["billing"].items() if key != "kind")
    assert model_capabilities(model_id) is None
    assert len(http.calls) == 1 and http.calls[0]["url"] == API + "/v1/models"
    assert http.calls[0]["headers"] == {"Authorization": "Bearer " + KEY}
    assert http.calls[0]["payload"] is None


@pytest.mark.parametrize("model_id", UNTIMED)
def test_known_audio_models_without_native_timestamps_remain_visible_but_unselectable(model_id):
    metadata, _ = client(page(openai_model(model_id)))
    models = metadata.fetch("openai-api", API, KEY)
    validate_models(models)
    model = models[0]
    assert model["availability"] == "unsupported"
    assert model["reason"] == "timestamp_support_required"
    assert model["roles"] == ["transcription"]
    assert model["timestamp_support"] == "none"
    assert model["context_window"] is model["max_output_tokens"] is None
    assert model["parameter_schema"]["properties"] == {}
    capabilities = audio_model_capabilities(model_id)
    assert capabilities.availability == "unsupported"
    assert capabilities.wire_parameters == {}


@pytest.mark.parametrize(
    "model_id",
    [
        "whisper-1-2099-01-01",
        "gpt-4o-transcribe-diarize-2099-01-01",
        "gpt-4o-mini-transcribe-2025-03-20",
        "ft:whisper-1:fixture:custom:id",
        "whisper-future",
    ],
)
def test_audio_names_and_untrusted_metadata_do_not_create_capabilities(model_id):
    metadata, _ = client(
        page(
            openai_model(
                model_id,
                roles=["transcription"],
                timestamp_support="word",
                response_format="verbose_json",
                max_input_tokens=1_000_000,
                max_tokens=65536,
                capabilities={"audio": True},
            )
        )
    )
    models = metadata.fetch("openai-api", API, KEY)
    validate_models(models)
    model = models[0]
    assert model["availability"] == "unverified"
    assert model["reason"] == "model_capabilities_unavailable"
    assert model["roles"] == model["input_modalities"] == model["output_modalities"] == []
    assert model["timestamp_support"] == "none"
    assert model["context_window"] is model["max_output_tokens"] is None
    assert audio_model_capabilities(model_id) is None


def test_audio_capability_table_does_not_add_models_absent_from_the_api_catalog():
    metadata, _ = client(page(openai_model("gpt-5.4")))
    assert [model["model_id"] for model in metadata.fetch("openai-api", API, KEY)] == ["gpt-5.4"]
    metadata, _ = client(page())
    assert metadata.fetch("openai-api", API, KEY) == []
    metadata, _ = client(page(openai_model("whisper-1")))
    assert [model["model_id"] for model in metadata.fetch("openai-api", API, KEY)] == ["whisper-1"]
    assert audio_model_capabilities("gpt-5.4") is None


@pytest.mark.parametrize("model_id", SUPPORTED)
def test_audio_parameters_do_not_inherit_text_options_or_allow_prompts_and_speaker_references(
    model_id,
):
    capabilities = audio_model_capabilities(model_id)
    schema = capabilities.parameter_schema()
    validator = Draft202012Validator(schema)
    validator.validate({})
    for parameters in (
        {"max_tokens": 4096},
        {"reasoning_effort": "high"},
        {"prompt": "private words"},
        {"known_speaker_names": ["Speaker"]},
        {"known_speaker_references": ["data:audio/wav;..."]},
        {"language": "ja"},
        {"temperature": 0},
        {"stream": True},
        {"response_format": "text"},
        {"tools": []},
        {"store": False},
        {"chunking_strategy": "auto"},
    ):
        assert not validator.is_valid(parameters)


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        (
            "whisper-1",
            {"response_format": "verbose_json", "timestamp_granularities": ["segment", "word"]},
        ),
        (
            "gpt-4o-transcribe-diarize",
            {"response_format": "diarized_json", "chunking_strategy": "auto", "stream": False},
        ),
    ],
)
def test_fixed_request_options_are_model_specific_and_cannot_mutate_the_table(model_id, expected):
    capabilities = audio_model_capabilities(model_id)
    assert capabilities.wire_parameters == expected
    mutated = capabilities.wire_parameters
    mutated["prompt"] = "must not enter future requests"
    if "timestamp_granularities" in mutated:
        mutated["timestamp_granularities"].clear()
    assert capabilities.wire_parameters == expected
    assert capabilities.resolved_revision is None


@pytest.mark.parametrize("model_id", SUPPORTED)
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_audio_shutdown_date_keeps_the_same_utc_retirement_rule(model_id, delta):
    expires_on = (NOW.date() + timedelta(days=delta)).isoformat()
    metadata, _ = client(page(openai_model(model_id, shutdown_date=expires_on)))
    models = metadata.fetch("openai-api", API, KEY)
    validate_models(models)
    assert models[0]["availability_expires_on"] == expires_on
    assert models[0]["availability"] == ("available" if delta > 0 else "retired")
    assert models[0]["reason"] == (None if delta > 0 else "model_retired")


@pytest.mark.parametrize("model_id", SUPPORTED)
def test_audio_metadata_reflection_is_rejected_before_exposing_any_candidate(model_id):
    metadata, _ = client(page(openai_model(model_id, unused={"secret": KEY})))
    with pytest.raises(EngineUnavailableError) as failure:
        metadata.fetch("openai-api", API, KEY)
    assert failure.value.details["reason"] == "unsafe_metadata"
    assert KEY not in str(failure.value.to_payload())


@pytest.fixture
def provider_service(tmp_path):
    http = FakeHTTP([])
    secrets = MemorySecretStore()
    jobs = JobQueue()
    service = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=MetadataClient(http=http, now=lambda: NOW),
        auth_executor=ManualExecutor(),
        server_instance_id=INSTANCE_ONE,
        submit_job=jobs,
        runtime_inspector=FakeRuntimeInspector(),
        codex_backend=FakeCodexBackend(),
    )
    yield service, http, secrets, jobs
    service.close()


def prepare_openai_runtime(service, jobs):
    provider = next(
        item
        for item in service.list_providers()["providers"]
        if item["provider_id"] == "openai-api"
    )
    runtime = provider["runtime"]
    service.prepare_runtime(
        {
            "provider_id": "openai-api",
            "resource_id": runtime["resources"][0]["resource_id"],
            "expected_catalog_revision": runtime["catalog_revision"],
            "action": "prepare",
            "request_id": "fixture-audio-runtime",
        }
    )
    jobs.run()


def test_only_openai_advertises_transcription_without_accessing_credentials_or_network(
    provider_service,
):
    service, http, secrets, _ = provider_service
    result = service.list_providers()
    load_contracts().validate_output("list_providers", result)
    for provider in result["providers"]:
        if provider["provider_id"] == "openai-api":
            assert set(provider["roles"]) == {"llm", "transcription"}
            assert provider["availability"] == "not_prepared"
        else:
            assert provider["roles"] == ["llm"]
    assert http.calls == secrets.calls == []


def test_connection_catalog_separates_text_and_audio_models_without_new_network_requests(
    provider_service,
):
    service, http, _, jobs = provider_service
    prepare_openai_runtime(service, jobs)
    record = create_connection(service, provider_id="openai-api", key=KEY)
    http.responses.append(
        page(*(openai_model(model) for model in ("gpt-5.4", *SUPPORTED, *UNTIMED)))
    )
    tested = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 1}
    )
    assert tested["connected"] is True
    llm = service.list_models({"connection_id": record["connection_id"], "role": "llm"})
    audio = service.list_models({"connection_id": record["connection_id"], "role": "transcription"})
    assert [model["model_id"] for model in llm["models"]] == ["gpt-5.4"]
    assert [model["model_id"] for model in audio["models"]] == [*SUPPORTED, *UNTIMED]
    assert [
        model["model_id"] for model in audio["models"] if model["availability"] == "available"
    ] == list(SUPPORTED)
    for result in (llm, audio):
        load_contracts().validate_output("list_provider_models", result)
    assert len(http.calls) == 1


def test_model_pagination_cursor_cannot_cross_from_text_to_audio_role(provider_service):
    service, http, _, jobs = provider_service
    prepare_openai_runtime(service, jobs)
    record = create_connection(service, provider_id="openai-api", key=KEY)
    http.responses.append(
        page(
            openai_model("gpt-5.4"),
            openai_model("whisper-1"),
            *(openai_model(f"metadata-unknown-{i}") for i in range(101)),
        )
    )
    service.test_connection({"connection_id": record["connection_id"], "expected_revision": 1})
    llm = service.list_models({"connection_id": record["connection_id"], "role": "llm"})
    assert llm["next_cursor"] is not None
    with pytest.raises(InvalidArgumentError):
        service.list_models(
            {
                "connection_id": record["connection_id"],
                "role": "transcription",
                "cursor": llm["next_cursor"],
            }
        )
    assert len(http.calls) == 1
