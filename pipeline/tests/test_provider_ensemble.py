"""Ensemble provider preflight stays explicit, non-secret and side-effect free."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
from narumi.bundle.hashing import sha256_params
from narumi.errors import (
    ConfigurationConflictError,
    InvalidArgumentError,
    ModelUnavailableError,
    PolicyViolationError,
)
from narumi.minutes_ensemble import MinutesEnsembleGenerator, MinutesEnsembleSelection
from narumi.model_selection import ModelSelection
from narumi.models import MeetingConfig
from narumi.providers.ensemble import EnsembleResolver
from narumi.providers.generation import MinutesResolver
from narumi.providers.minutes_selection import SELECTION_SCOPE_SCHEMA_VERSION
from narumi.providers.service import ProviderService

from .provider_fakes import (
    FakeCodexBackend,
    FakeHTTPBackend,
    FakeMetadata,
    FakeRuntimeInspector,
    MemorySecretStore,
    prepared_codex_connection,
    prepared_http_connection,
)

GENERATOR_IDS = {
    "codex-app-server": "gen-" + "1" * 32,
    "openai-api": "gen-" + "2" * 32,
    "anthropic-api": "gen-" + "3" * 32,
    "ollama": "gen-" + "4" * 32,
}


@dataclass
class EnsembleFixture:
    service: ProviderService
    codex: FakeCodexBackend
    http: FakeHTTPBackend
    selections: dict[str, ModelSelection]

    def config(self) -> MeetingConfig:
        order = ("anthropic-api", "ollama", "codex-app-server", "openai-api")
        return MeetingConfig(
            external_send_policy="api_ok",
            minutes_ensemble=MinutesEnsembleSelection(
                generators=[
                    MinutesEnsembleGenerator(
                        id=GENERATOR_IDS[provider],
                        label=f"{provider}案",
                        selection=self.selections[provider].model_copy(deep=True),
                    )
                    for provider in order
                ],
                synthesizer=self.selections["openai-api"].model_copy(deep=True),
            ),
        )


@pytest.fixture
def ensemble_fixture(tmp_path):
    codex = FakeCodexBackend()
    codex.models[0]["parameter_schema"]["properties"]["reasoning_effort"]["default"] = "medium"
    http = FakeHTTPBackend()
    secrets = MemorySecretStore()
    service = ProviderService(
        tmp_path / "providers-home",
        secret_store=secrets,
        codex_backend=codex,
        metadata_client=FakeMetadata(),
        runtime_inspector=FakeRuntimeInspector(),
        http_backend=http,
    )
    records = {"codex-app-server": prepared_codex_connection(service, request_id="ensemble-codex")}
    for provider in ("openai-api", "anthropic-api", "ollama"):
        records[provider] = prepared_http_connection(
            service,
            provider,
            request_id=f"ensemble-{provider}",
        )
    document = service.store.read()
    selections = {}
    for provider, record in records.items():
        model = document["catalogs"][record["connection_id"]]["models"][0]
        parameters = (
            {"reasoning_effort": "high"} if provider == "codex-app-server" else {"max_tokens": 512}
        )
        selections[provider] = ModelSelection(
            provider=provider,
            connection_id=record["connection_id"],
            connection_revision=record["revision"],
            model_id=model["model_id"],
            parameters=parameters,
        )
    codex.calls.clear()
    http.calls.clear()
    secrets.calls.clear()
    yield EnsembleFixture(service, codex, http, selections)
    service.close()


def _complete_calls(fixture: EnsembleFixture) -> list:
    return [call for call in fixture.codex.calls if call[0] == "complete"] + list(
        fixture.http.calls
    )


def _inspection(fixture: EnsembleFixture, config: MeetingConfig):
    resolver = EnsembleResolver(fixture.service)
    with fixture.service.store.transaction() as document:
        return resolver.validate_in_transaction(config, document)


def test_resolve_preflights_four_providers_in_one_transaction(ensemble_fixture, monkeypatch):
    fixture = ensemble_fixture
    config = fixture.config()
    real_transaction = fixture.service.store.transaction
    transaction_count = 0

    @contextmanager
    def counted_transaction():
        nonlocal transaction_count
        transaction_count += 1
        with real_transaction() as document:
            yield document

    monkeypatch.setattr(fixture.service.store, "transaction", counted_transaction)
    resolved = EnsembleResolver(fixture.service).resolve(config)

    assert transaction_count == 1
    assert [item.generator_id for item in resolved.generators] == [
        generator.id for generator in config.minutes_ensemble.generators
    ]
    assert {item.binding.provider.name for item in resolved.generators} == set(GENERATOR_IDS)
    assert resolved.synthesizer.provider.name == "openai-api"
    assert [item.display_order for item in resolved.generators] == [0, 1, 2, 3]
    semantic_keys = [item.semantic_key for item in resolved.canonical_generators]
    assert semantic_keys == sorted(semantic_keys)
    assert [item.canonical_ordinal for item in resolved.canonical_generators] == [0, 1, 2, 3]
    assert _complete_calls(fixture) == []
    assert fixture.service.secrets.calls == []


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("policy", PolicyViolationError),
        ("revision", ConfigurationConflictError),
        ("availability", ModelUnavailableError),
    ],
)
def test_one_invalid_member_rejects_the_whole_ensemble_without_sending(
    ensemble_fixture, mutation, error
):
    fixture = ensemble_fixture
    config = fixture.config()
    anthropic = fixture.selections["anthropic-api"]
    if mutation == "policy":
        config.external_send_policy = "local_only"
    with fixture.service.store.transaction() as document:
        record = document["connections"][anthropic.connection_id]
        model = document["catalogs"][anthropic.connection_id]["models"][0]
        if mutation == "revision":
            record["revision"] += 1
        if mutation == "availability":
            model["availability"] = "unverified"

    with pytest.raises(error):
        EnsembleResolver(fixture.service).resolve(config)
    assert _complete_calls(fixture) == []
    assert fixture.service.secrets.calls == []


def test_invalid_synthesizer_is_rejected_after_all_generator_preflight_without_sending(
    ensemble_fixture,
):
    fixture = ensemble_fixture
    config = fixture.config()
    config.minutes_ensemble.synthesizer.connection_revision += 1

    with pytest.raises(ConfigurationConflictError):
        EnsembleResolver(fixture.service).resolve(config)
    assert _complete_calls(fixture) == []
    assert fixture.service.secrets.calls == []


def test_id_label_order_and_credential_rotation_do_not_change_canonical_semantics(
    ensemble_fixture,
):
    fixture = ensemble_fixture
    config = fixture.config()
    before = _inspection(fixture, config)
    before_by_provider = {item.selection.authorization.provider: item for item in before.generators}
    before_semantics = [item.semantic_key for item in before.canonical_generators]
    openai = fixture.selections["openai-api"]

    with fixture.service.store.transaction() as document:
        record = document["connections"][openai.connection_id]
        record["revision"] += 1
        record["secret_account"] = (
            f"providers:{fixture.service.namespace}:{openai.connection_id}:" + "c" * 32
        )
        catalog = document["catalogs"][openai.connection_id]
        catalog["connection_revision"] = record["revision"]
        catalog["catalog_id"] = "changed-observation"
        catalog["fetched_at"] = "2030-01-01T00:00:00Z"
        current_revision = record["revision"]
    generator_ids = [generator.id for generator in config.minutes_ensemble.generators]
    for generator, replacement_id in zip(
        config.minutes_ensemble.generators, reversed(generator_ids), strict=True
    ):
        generator.label += " 更新"
        generator.id = replacement_id
        if generator.selection.connection_id == openai.connection_id:
            generator.selection.connection_revision = current_revision
    config.minutes_ensemble.generators.reverse()
    config.minutes_ensemble.synthesizer.connection_revision = current_revision

    after = _inspection(fixture, config)
    after_by_provider = {item.selection.authorization.provider: item for item in after.generators}
    for provider in GENERATOR_IDS:
        assert (
            before_by_provider[provider].selection.content_conditions_sha256
            == after_by_provider[provider].selection.content_conditions_sha256
        )
        assert (
            before_by_provider[provider].selection.selection_scope_sha256
            == after_by_provider[provider].selection.selection_scope_sha256
        )
    assert [item.semantic_key for item in after.canonical_generators] == before_semantics
    assert [item.generator_id for item in after.generators] == [
        generator.id for generator in config.minutes_ensemble.generators
    ]
    assert before.synthesizer.content_conditions_sha256 == (
        after.synthesizer.content_conditions_sha256
    )
    assert after.synthesizer.authorization.connection_revision == current_revision
    assert _complete_calls(fixture) == []


def test_recreated_connection_cannot_bypass_the_content_unknown_barrier(ensemble_fixture):
    fixture = ensemble_fixture
    original = fixture.selections["openai-api"]
    replacement = prepared_http_connection(
        fixture.service,
        "openai-api",
        request_id="ensemble-openai-replacement",
    )
    model = fixture.service.store.read()["catalogs"][replacement["connection_id"]]["models"][0]
    replacement_selection = ModelSelection(
        provider="openai-api",
        connection_id=replacement["connection_id"],
        connection_revision=replacement["revision"],
        model_id=model["model_id"],
        parameters={"max_tokens": 512},
    )
    resolver = MinutesResolver(fixture.service)
    with fixture.service.store.transaction() as document:
        first = resolver.validate_selection_in_transaction(original, "api_ok", document)
        second = resolver.validate_selection_in_transaction(
            replacement_selection, "api_ok", document
        )

    assert first.content_conditions_sha256 == second.content_conditions_sha256
    assert first.selection_scope_sha256 != second.selection_scope_sha256
    assert first.connection_scope != second.connection_scope
    assert first.authorization.connection_id != second.authorization.connection_id
    assert _complete_calls(fixture) == []


def test_content_conditions_cover_endpoint_model_parameters_and_runtime(ensemble_fixture):
    fixture = ensemble_fixture
    selected = fixture.selections["ollama"].model_copy(deep=True)
    resolver = MinutesResolver(fixture.service)
    with fixture.service.store.transaction() as document:
        before = resolver.validate_selection_in_transaction(selected, "local_only", document)
    conditions = before.content_conditions
    assert conditions.provider == "ollama"
    assert conditions.endpoint == "http://127.0.0.1:11434"
    assert conditions.model_id == selected.model_id
    assert conditions.effective_parameters == {"max_tokens": 512}
    assert conditions.runtime_version == "1.0.0"
    assert conditions.runtime_sha256 == "a" * 64
    assert conditions.adapter_version
    assert conditions.model_capabilities_sha256
    assert "connection_id" not in conditions.to_dict()
    assert before.selection_scope_sha256 == sha256_params(
        {
            "schema_version": SELECTION_SCOPE_SCHEMA_VERSION,
            "connection_scope": before.connection_scope.to_dict(),
            "content_conditions_sha256": before.content_conditions_sha256,
        }
    )

    with fixture.service.store.transaction() as document:
        record = document["connections"][selected.connection_id]
        record["endpoint"] = "http://127.0.0.1:11435"
        record["revision"] += 1
        document["catalogs"][selected.connection_id]["connection_revision"] = record["revision"]
        selected.connection_revision = record["revision"]
    with fixture.service.store.transaction() as document:
        after = resolver.validate_selection_in_transaction(selected, "local_only", document)
    assert after.content_conditions.endpoint == "http://127.0.0.1:11435"
    assert before.content_conditions_sha256 != after.content_conditions_sha256


def test_effective_defaults_share_ensemble_content_but_not_legacy_single_params(
    ensemble_fixture,
):
    fixture = ensemble_fixture
    selected = fixture.selections["openai-api"].model_copy(deep=True)
    selected.parameters = {}
    explicit = selected.model_copy(deep=True)
    explicit.parameters = {"max_tokens": 4096}
    resolver = MinutesResolver(fixture.service)
    with fixture.service.store.transaction() as document:
        omitted = resolver.validate_selection_in_transaction(selected, "api_ok", document)
        supplied = resolver.validate_selection_in_transaction(explicit, "api_ok", document)

    assert omitted.content_conditions_sha256 == supplied.content_conditions_sha256
    assert omitted.selection_scope_sha256 == supplied.selection_scope_sha256
    assert omitted.content_conditions.effective_parameters == {"max_tokens": 4096}
    assert omitted.legacy_generation_params != supplied.legacy_generation_params


def test_cache_epoch_is_explicit_and_does_not_change_selection_scope(ensemble_fixture):
    fixture = ensemble_fixture
    selected = fixture.selections["openai-api"].model_copy(deep=True)
    resolver = MinutesResolver(fixture.service)
    with fixture.service.store.transaction() as document:
        before = resolver.validate_selection_in_transaction(selected, "api_ok", document)
        selected.cache_epoch += 1
        after = resolver.validate_selection_in_transaction(selected, "api_ok", document)

    assert before.content_conditions_sha256 == after.content_conditions_sha256
    assert before.selection_scope_sha256 == after.selection_scope_sha256
    assert before.cache_epoch == 0
    assert after.cache_epoch == 1


def test_same_openai_connection_can_bind_and_call_two_exact_models(ensemble_fixture):
    fixture = ensemble_fixture
    selected = fixture.selections["openai-api"]
    second_model = copy.deepcopy(
        fixture.service.store.read()["catalogs"][selected.connection_id]["models"][0]
    )
    second_model.update(model_id="gpt-4.1-mini", display_name="GPT-4.1 mini fixture")
    with fixture.service.store.transaction() as document:
        document["catalogs"][selected.connection_id]["models"].append(second_model)
    second = selected.model_copy(deep=True)
    second.model_id = "gpt-4.1-mini"
    config = MeetingConfig(
        external_send_policy="api_ok",
        minutes_ensemble=MinutesEnsembleSelection(
            generators=[
                MinutesEnsembleGenerator(id="gen-" + "a" * 32, label="alias", selection=selected),
                MinutesEnsembleGenerator(id="gen-" + "b" * 32, label="mini", selection=second),
            ],
            synthesizer=selected,
        ),
    )
    resolved = EnsembleResolver(fixture.service).resolve(config)
    for item in resolved.generators:
        item.binding.provider.complete("fixture prompt")

    assert [call[4]["model_id"] for call in fixture.http.calls] == [
        "gpt-4.1",
        "gpt-4.1-mini",
    ]
    hashes = {item.binding.inspection.content_conditions_sha256 for item in resolved.generators}
    assert len(hashes) == 2


def test_canonical_view_groups_duplicate_selections_for_one_shared_call(ensemble_fixture):
    fixture = ensemble_fixture
    openai = fixture.selections["openai-api"]
    anthropic = fixture.selections["anthropic-api"]
    config = MeetingConfig(
        external_send_policy="api_ok",
        minutes_ensemble=MinutesEnsembleSelection(
            generators=[
                MinutesEnsembleGenerator(id="gen-" + "a" * 32, label="first", selection=openai),
                MinutesEnsembleGenerator(id="gen-" + "b" * 32, label="other", selection=anthropic),
                MinutesEnsembleGenerator(id="gen-" + "c" * 32, label="duplicate", selection=openai),
            ],
            synthesizer=openai,
        ),
    )
    resolved = EnsembleResolver(fixture.service).resolve(config)
    openai_key = (
        resolved.generators[0].binding.inspection.selection_scope_sha256,
        resolved.generators[0].binding.inspection.cache_epoch,
    )
    openai_slots = [
        item for item in resolved.canonical_generators if item.semantic_key == openai_key
    ]

    assert len(resolved.generators) == 3
    assert len(resolved.canonical_generators) == 3
    assert len(resolved.call_generators) == 2
    assert [item.canonical_ordinal for item in resolved.canonical_generators] == [0, 1, 2]
    assert [item.semantic_key for item in resolved.canonical_generators] == sorted(
        item.semantic_key for item in resolved.canonical_generators
    )
    assert [item.provenance.display_order for item in openai_slots] == [0, 2]
    assert [item.duplicate_ordinal for item in openai_slots] == [0, 1]
    assert [item.provenance.duplicate_ordinal for item in openai_slots] == [0, 1]
    assert len({item.shared_call_canonical_ordinal for item in openai_slots}) == 1
    assert all(item.binding is resolved.generators[0].binding for item in openai_slots)

    for item in resolved.call_generators:
        item.binding.provider.complete("fixture prompt")
    assert len(fixture.http.calls) == 2


def test_inspection_is_secret_free_and_returns_isolated_legacy_params(ensemble_fixture):
    fixture = ensemble_fixture
    inspection = _inspection(fixture, fixture.config()).synthesizer
    visible = repr(inspection) + repr(inspection.content_conditions.to_dict())
    assert "fixture-key" not in visible
    assert "secret_account" not in visible
    with pytest.raises(TypeError):
        inspection.content_conditions.effective_parameters["max_tokens"] = 1
    legacy = inspection.legacy_generation_params
    legacy["model_id"] = "mutated"
    assert inspection.legacy_generation_params["model_id"] == "gpt-4.1"


def test_single_resolver_refuses_an_ensemble_instead_of_falling_back(ensemble_fixture):
    fixture = ensemble_fixture
    with pytest.raises(InvalidArgumentError):
        MinutesResolver(fixture.service).resolve(fixture.config())
    assert _complete_calls(fixture) == []
