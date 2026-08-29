"""Saved model selection, guarded sends and durable partial minutes, without a live provider."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from narumi.bundle import Bundle, MinutesVersionRecord
from narumi.errors import (
    AuthenticationRequiredError,
    BusyError,
    CancelledError,
    ConfigurationConflictError,
    EngineUnavailableError,
    InvalidArgumentError,
    NarumiError,
)
from narumi.generate import checkpoints, run_generate
from narumi.model_selection import ModelSelection
from narumi.models import MeetingConfig, MergedSegment, MergedTranscript
from narumi.pipeline import process_meeting, refresh_meeting, regenerate_meeting
from narumi.providers.codex._models import fetch_models
from narumi.providers.generation import OUTCOME_UNKNOWN, MinutesResolver
from narumi.providers.service import ProviderService
from pydantic import ValidationError

from .provider_fakes import FakeCodexBackend, MemorySecretStore, prepared_codex_connection


@pytest.fixture
def generation(tmp_path):
    backend = FakeCodexBackend()
    backend.models[0]["parameter_schema"]["properties"]["reasoning_effort"]["default"] = "medium"
    backend.response = "## アジェンダ\n議事録生成の確認\n## 決定事項\n公開する\n"
    service = ProviderService(
        tmp_path / "providers-home", secret_store=MemorySecretStore(), codex_backend=backend
    )
    record = prepared_codex_connection(service)
    config = MeetingConfig(
        external_send_policy="subscription_ok",
        minutes_model=ModelSelection(
            provider="codex-app-server",
            connection_id=record["connection_id"],
            connection_revision=record["revision"],
            model_id=backend.models[0]["model_id"],
            parameters={"reasoning_effort": "high"},
        ),
    )
    yield service, backend, config
    service.close()


def generation_bundle(tmp_path, config, *, segments=1):
    bundle = Bundle.create(tmp_path / "meetings", meeting_name="モデル指定の確認", config=config)
    merged = MergedTranscript(
        segments=[
            MergedSegment(id=f"segment-{i}", start=i, end=i + 1, text="会議の発言。" * 30)
            for i in range(segments)
        ]
    )
    bundle.run_stage(
        "merged/merged",
        inputs={},
        params={},
        producer=("fixture", "1"),
        output="merged/merged.json",
        fn=lambda _: bundle.write_json("merged/merged.json", merged),
    )
    return bundle


def completions(backend):
    return [call for call in backend.calls if call[0] == "complete"]


def test_saved_selection_uses_exact_model_and_effective_parameters(tmp_path, generation):
    service, backend, config = generation
    bundle = generation_bundle(tmp_path, config)
    reopened = Bundle.open(bundle.path)
    first = run_generate(reopened, minutes_resolver=MinutesResolver(service))
    assert first.skipped is False
    assert [(call[2], call[3]) for call in completions(backend)] == [
        (config.minutes_model.model_id, {"reasoning_effort": "high"}),
        (config.minutes_model.model_id, {"reasoning_effort": "high"}),
    ]
    params = reopened.read_json("minutes/v1/meta.json")["params"]
    assert params["model_id"] == config.minutes_model.model_id
    assert params["effective_parameters"] == {"reasoning_effort": "high"}
    assert params["runtime_version"] == backend.version and params["adapter_version"]
    before = len(completions(backend))
    assert run_generate(Bundle.open(bundle.path), minutes_resolver=MinutesResolver(service)).skipped
    assert len(completions(backend)) == before
    for path in (bundle.path / "minutes").rglob("*.json"):
        stored = path.read_text()
        assert "fixture-key" not in stored
        assert all(
            f'"{field}"' not in stored
            for field in ("auth_state", "access_token", "refresh_token", "api_key")
        )


def test_real_catalog_projection_can_be_saved_resolved_and_used(tmp_path, generation):
    service, backend, config = generation

    def model_list(method, args):
        assert method == "model/list"
        return {
            "data": [
                {
                    "model": config.minutes_model.model_id,
                    "displayName": "Codex model fixture",
                    "hidden": False,
                    "inputModalities": ["text", "image"],
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "medium"},
                        {"reasoningEffort": "high"},
                    ],
                    "defaultReasoningEffort": "medium",
                }
            ],
            "nextCursor": None,
        }

    projected = fetch_models(model_list)
    backend.models = projected
    tested = service.test_connection(
        {
            "connection_id": config.minutes_model.connection_id,
            "expected_revision": config.minutes_model.connection_revision,
        }
    )
    assert tested["connected"]
    bundle = generation_bundle(tmp_path, config)
    run_generate(bundle, minutes_resolver=MinutesResolver(service))
    assert [call[2] for call in completions(backend)] == [config.minutes_model.model_id] * 2
    provenance = bundle.read_json("minutes/v1/meta.json")["params"]
    assert provenance["context_window"] is None
    assert provenance["effective_parameters"] == {"reasoning_effort": "high"}


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("policy", "policy_violation"),
        ("provider", "configuration_conflict"),
        ("revision", "configuration_conflict"),
        ("disabled", "configuration_conflict"),
        ("auth_method", "authentication_required"),
        ("auth_state", "authentication_required"),
        ("credential_present", "authentication_required"),
        ("runtime", "engine_unavailable"),
        ("runtime_changed", "engine_unavailable"),
        ("catalog", "model_unavailable"),
        ("catalog_revision", "model_unavailable"),
        ("catalog_runtime", "model_unavailable"),
        ("missing_model", "model_unavailable"),
        ("availability", "model_unavailable"),
        ("billing", "model_unavailable"),
        ("modalities", "model_unavailable"),
        ("parameters", "invalid_argument"),
    ],
)
def test_invalid_selection_never_sends(generation, mutation, error_code):
    service, backend, config = generation
    if mutation == "policy":
        config.external_send_policy = "local_only"
    if mutation == "parameters":
        config.minutes_model.parameters = {"reasoning_effort": "unsupported"}
    if mutation == "runtime_changed":
        backend.version = "2.0.0"
    with service.store.transaction() as document:
        record = document["connections"][config.minutes_model.connection_id]
        catalog = document["catalogs"][record["connection_id"]]
        model = catalog["models"][0]
        if mutation == "provider":
            record["provider_id"] = "anthropic-api"
        if mutation == "revision":
            record["revision"] += 1
        if mutation == "disabled":
            record["enabled"] = False
        if mutation == "auth_method":
            record["auth_method"] = "api_key"
        if mutation == "auth_state":
            record["auth_state"] = "failed"
        if mutation == "credential_present":
            record["credential_present"] = False
        if mutation == "runtime":
            document["runtimes"]["codex-app-server"]["state"] = "not_prepared"
        if mutation == "catalog":
            record["catalog_state"] = "stale"
        if mutation == "catalog_revision":
            catalog["connection_revision"] += 1
        if mutation == "catalog_runtime":
            catalog["runtime_catalog_revision"] = "stale-runtime"
        if mutation == "missing_model":
            catalog["models"] = []
        if mutation == "availability":
            model["availability"] = "unverified"
        if mutation == "billing":
            model["billing"]["kind"] = "api"
        if mutation == "modalities":
            model["output_modalities"] = []
    with pytest.raises(NarumiError) as failure:
        MinutesResolver(service).resolve(config)
    assert failure.value.code == error_code
    assert completions(backend) == []


def test_selection_requires_injected_resolver_and_default_effort_is_recorded(tmp_path, generation):
    service, backend, config = generation
    bundle = generation_bundle(tmp_path, config)
    with pytest.raises(AuthenticationRequiredError):
        run_generate(bundle)
    config.minutes_model.parameters = {}
    provider = MinutesResolver(service).resolve(config)
    assert provider.generation_params["effective_parameters"] == {"reasoning_effort": "medium"}
    assert completions(backend) == []


def test_successful_chunks_survive_a_known_failure(tmp_path, generation):
    service, backend, config = generation
    bundle = generation_bundle(tmp_path, config, segments=15)
    with service.store.transaction() as document:
        document["catalogs"][config.minutes_model.connection_id]["models"][0]["context_window"] = 1
    real_complete = backend.complete
    sent = []

    def failing(*args, **kwargs):
        sent.append(args[3])
        if len(sent) == 2:
            raise EngineUnavailableError("fixture-key must not leak")
        return real_complete(*args, **kwargs)

    backend.complete = failing
    resolver = MinutesResolver(service)
    with pytest.raises(NarumiError) as failure:
        run_generate(bundle, minutes_resolver=resolver)
    assert "fixture-key" not in str(failure.value)
    assert bundle.manifest.latest_minutes_version is None
    first_prompt = sent[0]
    run_generate(bundle, minutes_resolver=resolver)
    assert sent.count(first_prompt) == 1
    assert bundle.manifest.latest_minutes_version == 1


def test_unknown_outcome_is_not_retried_without_new_epoch(tmp_path, generation):
    service, backend, config = generation
    bundle = generation_bundle(tmp_path, config)
    backend.complete_error = EngineUnavailableError(
        "fixture-key", details={"reason": OUTCOME_UNKNOWN}
    )
    resolver = MinutesResolver(service)
    with pytest.raises(NarumiError) as failure:
        run_generate(bundle, minutes_resolver=resolver)
    assert "fixture-key" not in str(failure.value)
    backend.complete_error = None
    with pytest.raises(EngineUnavailableError) as failure:
        run_generate(bundle, minutes_resolver=resolver)
    assert failure.value.details["reason"] == OUTCOME_UNKNOWN
    assert len(completions(backend)) == 1
    bundle.manifest.config.minutes_model.cache_epoch += 1
    bundle.save()
    run_generate(bundle, minutes_resolver=resolver)
    assert len(completions(backend)) == 3


@pytest.mark.parametrize(
    "operation", [run_generate, process_meeting, regenerate_meeting, refresh_meeting]
)
def test_force_cannot_bypass_codex_attempt_boundaries(tmp_path, generation, operation):
    service, backend, config = generation
    bundle = generation_bundle(tmp_path, config)
    previous = bundle.manifest.model_dump()
    with pytest.raises(InvalidArgumentError):
        operation(bundle, force=True, minutes_resolver=MinutesResolver(service))
    assert bundle.manifest.model_dump() == previous
    assert completions(backend) == []


def test_known_cancellation_keeps_the_completed_chunk_for_retry(tmp_path, generation):
    service, backend, config = generation
    bundle = generation_bundle(tmp_path, config)
    cancelled = threading.Event()
    real_complete = backend.complete

    def cancelling(*args, **kwargs):
        answer = real_complete(*args, **kwargs)
        cancelled.set()
        return answer

    backend.complete = cancelling
    resolver = MinutesResolver(service)
    with pytest.raises(CancelledError):
        run_generate(bundle, minutes_resolver=resolver, should_cancel=cancelled.is_set)
    assert bundle.manifest.latest_minutes_version is None
    backend.complete = real_complete
    run_generate(bundle, minutes_resolver=resolver)
    # One saved chunk response + one new final request, not the same chunk sent twice.
    assert len(completions(backend)) == 2
    assert bundle.manifest.latest_minutes_version == 1


def test_generation_lease_blocks_replacement_logout_and_next_send_after_disable(generation):
    service, backend, config = generation
    provider = MinutesResolver(service).resolve(config)
    entered, release = threading.Event(), threading.Event()

    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return "completed reply"

    backend.complete = blocking
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(provider.complete, "fixture prompt")
        assert entered.wait(5)
        try:
            with pytest.raises(BusyError):
                service.authenticate(
                    {
                        "connection_id": config.minutes_model.connection_id,
                        "expected_revision": 1,
                        "action": "logout",
                        "request_id": "blocked-logout",
                    }
                )
            with pytest.raises(BusyError):
                service.set_connection(
                    {
                        "connection_id": config.minutes_model.connection_id,
                        "expected_revision": 1,
                        "display_name": "changed",
                        "request_id": "blocked-rename",
                    }
                )
            service.set_connection(
                {
                    "connection_id": config.minutes_model.connection_id,
                    "expected_revision": 1,
                    "enabled": False,
                    "request_id": "disable-during-send",
                }
            )
            assert service.store.read()["checks"]["codex-app-server"]["kind"] == "generation"
        finally:
            release.set()
        assert future.result(timeout=5) == "completed reply"
    with pytest.raises(ConfigurationConflictError):
        provider.complete("next prompt")
    assert service.store.read()["checks"] == {}


def test_cancellation_is_checked_while_awaiting_reply(tmp_path, generation):
    service, backend, config = generation
    entered, cancelled = threading.Event(), threading.Event()
    bundle = generation_bundle(tmp_path, config)

    def waiting(*args, should_cancel, **kwargs):
        entered.set()
        assert cancelled.wait(5)
        assert should_cancel()
        raise CancelledError("fixture-key", details={"outcome_unknown": True})

    backend.complete = waiting
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            run_generate,
            bundle,
            minutes_resolver=MinutesResolver(service),
            should_cancel=cancelled.is_set,
        )
        assert entered.wait(5)
        cancelled.set()
        with pytest.raises(CancelledError) as failure:
            future.result(timeout=5)
    assert "fixture-key" not in str(failure.value)
    assert bundle.manifest.latest_minutes_version is None
    assert service.store.read()["checks"] == {}
    with pytest.raises(EngineUnavailableError):
        run_generate(bundle, minutes_resolver=MinutesResolver(service))


@pytest.mark.parametrize("failure_point", ["receipt", "checkpoint"])
def test_reply_persistence_failure_does_not_resend(
    tmp_path, generation, monkeypatch, failure_point
):
    service, backend, config = generation
    bundle = generation_bundle(tmp_path, config)
    original_commit = service.store.commit
    original_write = bundle.write_json
    broken = [True]

    def commit(document):
        if failure_point == "receipt" and completions(backend) and broken[0]:
            raise NarumiError("fixture persistence failure")
        return original_commit(document)

    def write(path, document):
        if failure_point == "checkpoint" and completions(backend) and broken[0]:
            raise NarumiError("fixture persistence failure")
        return original_write(path, document)

    monkeypatch.setattr(service.store, "commit", commit)
    monkeypatch.setattr(bundle, "write_json", write)
    resolver = MinutesResolver(service)
    with pytest.raises(NarumiError) as initial:
        run_generate(bundle, minutes_resolver=resolver)
    assert initial.value.details["reason"] == OUTCOME_UNKNOWN
    broken[0] = False
    # A fresh service normally recovers abandoned leases after a restart. The checkpoint is
    # still authoritative even when the non-secret operation lease has been recovered.
    with service.store.transaction() as document:
        document["checks"].clear()
    with pytest.raises(EngineUnavailableError) as failure:
        run_generate(bundle, minutes_resolver=resolver)
    assert failure.value.details["reason"] == OUTCOME_UNKNOWN
    assert len(completions(backend)) == 1


def test_checkpoint_is_flushed_before_each_outgoing_request(tmp_path, generation, monkeypatch):
    service, backend, config = generation
    bundle = generation_bundle(tmp_path, config)
    original_sync, original_complete = checkpoints._sync_checkpoint, backend.complete
    synced = []

    def sync(path):
        original_sync(path)
        synced.append(path)

    def complete(*args, **kwargs):
        assert synced
        document = bundle.read_json(str(synced[-1].relative_to(bundle.path)))
        assert any(entry["state"] == "pending" for entry in document["entries"].values())
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(checkpoints, "_sync_checkpoint", sync)
    backend.complete = complete
    run_generate(bundle, minutes_resolver=MinutesResolver(service))
    assert len(synced) == 4 and len(completions(backend)) == 2


def test_failed_pending_checkpoint_flush_never_dispatches(tmp_path, generation, monkeypatch):
    service, backend, config = generation
    bundle = generation_bundle(tmp_path, config)

    def broken_sync(path):
        raise OSError("fixture checkpoint flush failure")

    monkeypatch.setattr(checkpoints, "_sync_checkpoint", broken_sync)
    with pytest.raises(EngineUnavailableError) as failure:
        run_generate(bundle, minutes_resolver=MinutesResolver(service))
    assert failure.value.details["reason"] == "minutes_checkpoint_unavailable"
    assert completions(backend) == []


def test_model_observation_change_between_chunks_is_rejected(generation):
    service, backend, config = generation
    provider = MinutesResolver(service).resolve(config)
    with service.store.transaction() as document:
        model = document["catalogs"][config.minutes_model.connection_id]["models"][0]
        model["resolved_revision"] = "changed-revision"
    with pytest.raises(ConfigurationConflictError):
        provider.complete("fixture prompt")
    assert completions(backend) == []


@pytest.mark.parametrize(
    ("provider", "parameters"),
    [
        ("codex-app-server", {"max_tokens": 1}),
        ("anthropic-api", {"reasoning_effort": "high"}),
        ("ollama", {"reasoning_effort": "low"}),
        ("openai-api", {"max_tokens": True}),
        ("openai-api", {"max_tokens": 1.0}),
        ("openai-api", {"max_tokens": "512"}),
        ("openai-api", {"max_tokens": "high"}),
        ("openai-api", {"reasoning_effort": 1}),
        ("openai-api", {"max_tokens": 0}),
        ("openai-api", {"max_tokens": 32769}),
        ("openai-api", {"endpoint": "fixture"}),
        ("claude-agent-sdk", {}),
    ],
)
def test_model_selection_closes_parameters_by_provider(provider, parameters):
    with pytest.raises(ValidationError):
        ModelSelection(
            provider=provider,
            connection_id="conn-0123456789ab",
            connection_revision=1,
            model_id="fixture-model",
            parameters=parameters,
        )


@pytest.mark.parametrize("field", ["connection_revision", "cache_epoch"])
@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_model_selection_revisions_use_strict_integers(field, value):
    selection = {
        "provider": "openai-api",
        "connection_id": "conn-0123456789ab",
        "connection_revision": 1,
        "model_id": "gpt-4.1",
        field: value,
    }
    with pytest.raises(ValidationError):
        ModelSelection.model_validate(selection)


def test_codex_v030_provenance_is_reused_without_an_adapter_migration_send(tmp_path, generation):
    service, backend, config = generation
    bundle = generation_bundle(tmp_path, config)
    runtime = service.store.read()["runtimes"]["codex-app-server"]
    # Independently spell the shipping v0.3 fingerprint. An extra HTTP-only field or
    # adapter-version bump must not invalidate a completed Codex generation.
    original_params = {
        "provider": "codex-app-server",
        "prompt_version": "minutes-v2",
        "language": "ja",
        "minutes_model": config.minutes_model.model_dump(mode="json"),
        "model_id": config.minutes_model.model_id,
        "resolved_revision": None,
        "effective_parameters": {"reasoning_effort": "high"},
        "runtime_version": "1.0.0",
        "runtime_sha256": "a" * 64,
        "runtime_catalog_revision": runtime["catalog_revision"],
        "adapter_version": "1",
        "context_window": None,
        "max_output_tokens": None,
        "data_destination": "openai",
        "cost_class": "subscription",
        "generation_limits": {
            "bounded_prompt_version": "minutes-reduce-v1",
            "input_chars": 12000,
            "max_requests": 64,
            "max_reductions": 6,
        },
    }
    original = bundle.run_stage(
        "minutes/v1",
        inputs={"merged/merged": bundle.artifact_hash("merged/merged")},
        params=original_params,
        producer=("generate", "1"),
        output="minutes/v1/minutes.md",
        fn=lambda path: path.write_text("# Existing Codex minutes\n"),
    )
    bundle.manifest.minutes_versions.append(
        MinutesVersionRecord(
            version=1,
            path="minutes/v1/minutes.md",
            generated_at=original.record.created_at,
            provider="codex-app-server",
        )
    )
    bundle.save()
    result = run_generate(Bundle.open(bundle.path), minutes_resolver=MinutesResolver(service))
    assert result.skipped and result.path.read_text() == "# Existing Codex minutes\n"
    assert completions(backend) == []
