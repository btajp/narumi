"""Public HTTP-provider minutes flows with fake credentials, metadata and generation."""

from __future__ import annotations

import copy
import json
from uuid import uuid4

import pytest
from conftest import make_recorded_bundle
from narumi.bundle import Bundle
from narumi.contracts import load_contracts
from narumi.errors import EngineUnavailableError
from narumi.models import MeetingConfig
from narumi.providers.generation import OUTCOME_UNKNOWN
from narumi_server.app import dispatch
from narumi_server.context import build_context
from narumi_server.handlers import processing
from test_provider_tools import selected_generation
from test_surface_tools import write_silence_wav

from pipeline.tests.provider_fakes import (
    FakeCodexBackend,
    FakeHTTPBackend,
    FakeMetadata,
    FakeRuntimeInspector,
    MemorySecretStore,
)

PROVIDERS = ("openai-api", "anthropic-api", "ollama")
SECRET = "http-minutes-integration-fixture-secret"
MEETING_ID = "20260829T000000Z-0000face"


def result(ctx, tool, args):
    outcome = dispatch(ctx, tool, args)
    assert not outcome.is_error, outcome.payload
    ctx.contracts.validate_output(tool, outcome.payload)
    assert SECRET not in json.dumps(outcome.payload)
    return outcome.payload


def reject(ctx, tool, args, code):
    outcome = dispatch(ctx, tool, args)
    assert outcome.is_error, outcome.payload
    assert outcome.payload["error"]["code"] == code, outcome.payload
    assert SECRET not in json.dumps(outcome.payload)


def context(home, secrets, metadata, backend, *, transport="streamable-http"):
    ctx = build_context(
        home,
        transports=[transport],
        validate_output=True,
        provider_secret_store=secrets,
        provider_metadata_client=metadata,
        provider_codex_backend=FakeCodexBackend(),
        provider_http_backend=backend,
    )
    ctx.providers.runtime.inspector = FakeRuntimeInspector()
    return ctx


@pytest.fixture(params=PROVIDERS)
def http_context(home, request, monkeypatch):
    provider = request.param
    model = copy.deepcopy(load_contracts()["list_provider_models"].output_examples[0]["models"][0])
    model.update(
        model_id="gpt-4.1" if provider == "openai-api" else "fixture-text-model",
        availability="available",
        reason=None,
        source="runtime" if provider == "ollama" else "provider_api",
        resolved_revision="sha256:" + "b" * 64 if provider == "ollama" else None,
    )
    model["billing"]["kind"] = "local" if provider == "ollama" else "api"
    secrets, metadata, backend = MemorySecretStore(), FakeMetadata([model]), FakeHTTPBackend()
    backend.response = "## アジェンダ\n確認\n## 決定事項\n継続する\n"
    monkeypatch.setenv("OPENAI_API_KEY", "ignored-ambient-openai-fixture-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ignored-ambient-anthropic-fixture-key")
    ctx = context(home, secrets, metadata, backend)
    try:
        created = result(
            ctx,
            "set_provider_connection",
            {
                "provider_id": provider,
                "display_name": "議事録用の検証接続",
                "auth_method": "none" if provider == "ollama" else "api_key",
                **({} if provider == "ollama" else {"api_key": SECRET}),
                "request_id": str(uuid4()),
            },
        )["connection"]
        cid = created["connection_id"]
        assert result(ctx, "list_provider_connections", {})["connections"] == [created]
        assert result(ctx, "list_provider_models", {"connection_id": cid})["models"] == []
        assert metadata.calls == backend.calls == []
        providers = result(ctx, "list_providers", {})["providers"]
        runtime = next(item["runtime"] for item in providers if item["provider_id"] == provider)
        prepared = result(
            ctx,
            "prepare_provider_runtime",
            {
                "provider_id": provider,
                "resource_id": runtime["resources"][0]["resource_id"],
                "expected_catalog_revision": runtime["catalog_revision"],
                "action": "prepare",
                "request_id": str(uuid4()),
            },
        )
        assert ctx.jobs.wait(prepared["job_id"], timeout=5)["status"] == "succeeded"
        fetched = result(ctx, "list_provider_models", {"connection_id": cid, "refresh": True})
        assert fetched["catalog_state"] == "ready"
        assert fetched["models"][0]["availability"] == "available"
        endpoint = {
            "openai-api": "https://api.openai.com",
            "anthropic-api": "https://api.anthropic.com",
            "ollama": "http://127.0.0.1:11434",
        }[provider]
        assert metadata.calls == [(provider, endpoint, None if provider == "ollama" else SECRET)]
        assert backend.calls == []
        connection = result(ctx, "list_provider_connections", {})["connections"][0]
        assert connection["auth_state"] == "authenticated"
        assert connection["last_generation_state"] == "never"
        config = MeetingConfig.model_validate(
            {
                "transcription_engine": "fake",
                "diarization_engine": "fake",
                "llm_provider": "fake",
                "external_send_policy": "local_only" if provider == "ollama" else "api_ok",
                "minutes_model": {
                    "provider": provider,
                    "connection_id": cid,
                    "connection_revision": connection["revision"],
                    "model_id": model["model_id"],
                    "parameters": {"max_tokens": 512},
                },
            }
        )
        yield ctx, secrets, metadata, backend, config
    finally:
        ctx.close()


def saved_meeting(ctx, config):
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_ID)
    result(
        ctx,
        "set_meeting_config",
        {
            "meeting_id": bundle.meeting_id,
            **config.model_dump(mode="json"),
            "request_id": str(uuid4()),
        },
    )
    return bundle.meeting_id


def regenerate(ctx, meeting_id):
    config = result(ctx, "get_meeting", {"meeting_id": meeting_id})["config"]
    receipt = result(
        ctx,
        "regenerate",
        {"meeting_id": meeting_id, "expected_config": config, "request_id": str(uuid4())},
    )
    ctx.jobs.wait(receipt["job_id"], timeout=10)
    return result(ctx, "get_job_status", {"job_id": receipt["job_id"]})["job"]


def assert_no_persisted_secret(root, caplog):
    assert SECRET not in caplog.text
    for path in root.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path.name


def test_http_profile_and_meeting_survive_restart_and_run_only_selected_minutes(
    http_context, tmp_path, caplog
):
    ctx, secrets, metadata, backend, config = http_context
    profile = result(
        ctx,
        "set_profile",
        {
            "name": "http-minutes",
            "config": config.model_dump(mode="json"),
            "request_id": str(uuid4()),
        },
    )["profile"]
    imported = result(
        ctx,
        "import_recording",
        {
            "meeting_name": "合成音声の検証会議",
            "profile": "http-minutes",
            "mic_path": str(write_silence_wav(tmp_path / "mic.wav")),
            "system_path": str(write_silence_wav(tmp_path / "system.wav")),
            "auto_process": False,
            "request_id": str(uuid4()),
        },
    )
    meeting_id = imported["meeting_id"]
    changed = result(
        ctx,
        "set_meeting_config",
        {"meeting_id": meeting_id, "language": "en", "request_id": str(uuid4())},
    )["config"]
    assert changed["minutes_model"] == config.minutes_model.model_dump(mode="json")
    assert backend.calls == []
    ctx.close()
    resumed = context(ctx.data_root, secrets, metadata, backend)
    try:
        assert result(resumed, "get_profile", {"name": "http-minutes"})["profile"] == profile
        assert result(resumed, "get_meeting", {"meeting_id": meeting_id})["config"] == changed
        job = regenerate(resumed, meeting_id)
        assert job["status"] == "succeeded", job
        minutes = result(resumed, "get_minutes", {"meeting_id": meeting_id})
        assert minutes["provider"] == config.minutes_model.provider
        assert "継続する" in minutes["markdown"]
        assert len(backend.calls) == 2
        for call in backend.calls:
            assert call[1] == config.minutes_model.provider
            assert call[2:4] == metadata.calls[0][1:3]
            assert call[4]["model_id"] == config.minutes_model.model_id
            assert call[5] == {"max_tokens": 512}
        bundle = Bundle.find(resumed.meetings_root, meeting_id)
        assert bundle.read_json("transcripts/own-mic.json")["engine"]["name"] == "fake"
        assert bundle.read_json("merged/merged.json")["provider"] == "fake"
        assert bundle.manifest.config.llm_provider == "fake"
        params = bundle.read_json("minutes/v1/meta.json")["params"]
        assert params["minutes_model"] == changed["minutes_model"]
        again = regenerate(resumed, meeting_id)
        assert again["status"] == "succeeded", again
        assert len(backend.calls) == 2 and len(metadata.calls) == 1
        assert result(resumed, "get_minutes", {"meeting_id": meeting_id})["version"] == 1
        assert_no_persisted_secret(resumed.data_root, caplog)
    finally:
        resumed.close()


@pytest.mark.parametrize("http_context", ["openai-api", "anthropic-api"], indirect=True)
@pytest.mark.parametrize("policy", ["local_only", "subscription_ok"])
@pytest.mark.parametrize("tool", ["set_profile", "set_meeting_config"])
def test_api_minutes_require_explicit_api_policy_before_saving(http_context, policy, tool):
    ctx, _, _, backend, config = http_context
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_ID)
    payload = config.model_dump(mode="json")
    payload["external_send_policy"] = policy
    args = (
        {"name": "rejected", "config": payload}
        if tool == "set_profile"
        else {"meeting_id": bundle.meeting_id, **payload}
    )
    reject(ctx, tool, {**args, "request_id": str(uuid4())}, "policy_violation")
    assert ctx.profiles.peek("rejected") is None
    assert Bundle.find(ctx.meetings_root, bundle.meeting_id).manifest.config == MeetingConfig()
    assert backend.calls == []
    assert not ctx.jobs.has_active(bundle.meeting_id)


@pytest.mark.parametrize("change", ["missing_expected", "stale_expected", "force"])
def test_http_generation_requires_current_config_and_disallows_force(http_context, change):
    ctx, _, _, backend, config = http_context
    meeting_id = saved_meeting(ctx, config)
    args = {"meeting_id": meeting_id, "request_id": str(uuid4())}
    if change != "missing_expected":
        args["expected_config"] = config.model_dump(mode="json")
    if change == "stale_expected":
        args["expected_config"]["language"] = "en"
    if change == "force":
        args["force"] = True
    reject(
        ctx,
        "regenerate",
        args,
        "invalid_argument" if change == "force" else "configuration_conflict",
    )
    assert backend.calls == []
    assert not ctx.jobs.has_active(meeting_id)


def test_unknown_http_result_survives_restart_and_requires_new_epoch(
    http_context, monkeypatch, caplog
):
    ctx, secrets, metadata, backend, config = http_context
    meeting_id = saved_meeting(ctx, config)
    monkeypatch.setattr(processing.narumi_pipeline, "refresh_meeting", selected_generation)
    backend.complete_error = EngineUnavailableError(
        SECRET, details={"reason": OUTCOME_UNKNOWN, "outcome_unknown": True, "upstream": SECRET}
    )
    failed = regenerate(ctx, meeting_id)
    assert failed["status"] == "failed"
    assert failed["error"]["details"]["reason"] == OUTCOME_UNKNOWN
    assert len(backend.calls) == 1
    assert (
        result(ctx, "list_provider_connections", {})["connections"][0]["last_generation_state"]
        == "unknown"
    )
    ctx.close()
    backend.complete_error = None
    resumed = context(ctx.data_root, secrets, metadata, backend)
    try:
        retry = regenerate(resumed, meeting_id)
        assert retry["status"] == "failed"
        assert retry["error"]["details"]["reason"] == OUTCOME_UNKNOWN
        assert len(backend.calls) == 1
        selected = config.minutes_model.model_dump(mode="json")
        selected["cache_epoch"] += 1
        result(
            resumed,
            "set_meeting_config",
            {"meeting_id": meeting_id, "minutes_model": selected, "request_id": str(uuid4())},
        )
        completed = regenerate(resumed, meeting_id)
        assert completed["status"] == "succeeded", completed
        assert len(backend.calls) == 3
        assert (
            result(resumed, "get_minutes", {"meeting_id": meeting_id})["provider"]
            == selected["provider"]
        )
        assert_no_persisted_secret(resumed.data_root, caplog)
    finally:
        resumed.close()


@pytest.mark.parametrize("transport", ["streamable-http", "stdio", "in-process"])
def test_minutes_capabilities_only_advertise_resident_adapters(home, transport):
    ctx = context(home, MemorySecretStore(), FakeMetadata(), FakeHTTPBackend(), transport=transport)
    try:
        capabilities = result(ctx, "get_server_info", {})["capabilities"]
        resident = transport == "streamable-http"
        assert capabilities["workflow"]["stage_model_selection"] is resident
        assert set(capabilities["minutes_model_providers"]) == (
            {*PROVIDERS, "codex-app-server"} if resident else set()
        )
    finally:
        ctx.close()
