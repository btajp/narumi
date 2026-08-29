"""Connected API/local minutes use only saved models, credentials and bounded attempts."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from narumi.bundle import Bundle
from narumi.errors import (
    AuthenticationRequiredError,
    BusyError,
    CancelledError,
    ConfigurationConflictError,
    EngineUnavailableError,
    InvalidArgumentError,
    ModelUnavailableError,
    NarumiError,
    PolicyViolationError,
)
from narumi.generate import run_generate
from narumi.model_selection import ModelSelection
from narumi.models import MeetingConfig
from narumi.pipeline import process_meeting, refresh_meeting, regenerate_meeting
from narumi.providers import generation as generation_module
from narumi.providers.generation import MinutesResolver
from narumi.providers.service import ProviderService

from .provider_fakes import (
    FakeHTTPBackend,
    FakeMetadata,
    FakeRuntimeInspector,
    MemorySecretStore,
    create_connection,
    prepared_http_connection,
)
from .test_provider_generation import generation_bundle


@pytest.fixture(params=["openai-api", "anthropic-api", "ollama"])
def http_generation(tmp_path, request):
    backend = FakeHTTPBackend()
    backend.response = "## アジェンダ\n公開手順\n## 決定事項\n検証して公開する\n"
    service = ProviderService(
        tmp_path / "providers-home",
        secret_store=MemorySecretStore(),
        metadata_client=FakeMetadata(),
        runtime_inspector=FakeRuntimeInspector(),
        http_backend=backend,
    )
    record = prepared_http_connection(service, request.param)
    model = service.store.read()["catalogs"][record["connection_id"]]["models"][0]
    backend.returned_model = {
        "openai-api": "gpt-4.1-2025-04-14",
        "ollama": model["model_id"] + ":local",
    }.get(request.param)
    config = MeetingConfig(
        external_send_policy="local_only" if request.param == "ollama" else "api_ok",
        minutes_model=ModelSelection(
            provider=request.param,
            connection_id=record["connection_id"],
            connection_revision=record["revision"],
            model_id=model["model_id"],
            parameters={"max_tokens": 512},
        ),
    )
    yield service, backend, config
    service.close()


@pytest.mark.parametrize("usage", [None, {"input_tokens": 19, "output_tokens": 7}])
def test_saved_http_selection_generates_exact_model_and_reuses_minutes(
    tmp_path, http_generation, usage
):
    service, backend, config = http_generation
    backend.usage = usage
    bundle = generation_bundle(tmp_path, config)
    resolver = MinutesResolver(service)
    run_generate(Bundle.open(bundle.path), minutes_resolver=resolver)
    assert len(backend.calls) == 2
    for call in backend.calls:
        assert call[1] == config.minutes_model.provider
        assert call[3] == (None if call[1] == "ollama" else "fixture-key")
        assert call[4]["model_id"] == config.minutes_model.model_id
        assert call[5] == {"max_tokens": 512}
    reopened = Bundle.open(bundle.path)
    params = reopened.read_json("minutes/v1/meta.json")["params"]
    assert params["model_id"] == config.minutes_model.model_id
    assert params["effective_parameters"] == {"max_tokens": 512}
    assert params["runtime_version"] == service.runtime.inspector.version
    assert params["model_capabilities_sha256"]
    assert params["cost_class"] == ("local" if config.minutes_model.provider == "ollama" else "api")
    assert "usage" not in params
    receipts = list((bundle.path / "minutes" / "checkpoints").glob("*.json"))
    entries = json.loads(receipts[0].read_text())["entries"].values()
    assert len(entries) == 2
    assert all(
        entry["returned_model"] == (backend.returned_model or config.minutes_model.model_id)
        for entry in entries
    )
    assert all(entry["usage"] == usage for entry in entries)
    assert run_generate(reopened, minutes_resolver=resolver).skipped
    assert len(backend.calls) == 2
    for path in (bundle.path / "minutes").rglob("*.json"):
        contents = path.read_text()
        assert "fixture-key" not in contents and '"secret_account"' not in contents


def test_api_send_requires_api_policy_and_ollama_stays_local(http_generation):
    service, backend, config = http_generation
    for policy in ("local_only", "subscription_ok", "api_ok"):
        config.external_send_policy = policy
        backend.calls.clear()
        service.secrets.calls.clear()
        if config.minutes_model.provider != "ollama" and policy != "api_ok":
            with pytest.raises(PolicyViolationError):
                MinutesResolver(service).resolve(config)
            assert backend.calls == [] and service.secrets.calls == []
        else:
            provider = MinutesResolver(service).resolve(config)
            assert provider.complete("fixture prompt") == backend.response
            assert len(backend.calls) == 1
            if config.minutes_model.provider == "ollama":
                assert provider.profile.data_destination == "local"
                assert service.secrets.calls == []


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("revision", ConfigurationConflictError),
        ("disabled", ConfigurationConflictError),
        ("auth_state", AuthenticationRequiredError),
        ("auth_method", AuthenticationRequiredError),
        ("credential", AuthenticationRequiredError),
        ("runtime", EngineUnavailableError),
        ("runtime_changed", EngineUnavailableError),
        ("catalog_runtime", ModelUnavailableError),
        ("availability", ModelUnavailableError),
        ("billing", ModelUnavailableError),
        ("source", ModelUnavailableError),
        ("model_revision", ConfigurationConflictError),
        ("schema", InvalidArgumentError),
        ("endpoint", InvalidArgumentError),
    ],
)
def test_each_http_call_revalidates_the_saved_selection(http_generation, mutation, error):
    service, backend, config = http_generation
    provider = MinutesResolver(service).resolve(config)
    provider_id = config.minutes_model.provider
    if mutation == "runtime_changed":
        service.runtime.inspector.version = "2.0.0"
    with service.store.transaction() as document:
        record = document["connections"][config.minutes_model.connection_id]
        catalog = document["catalogs"][record["connection_id"]]
        model = catalog["models"][0]
        if mutation == "revision":
            record["revision"] += 1
        if mutation == "disabled":
            record["enabled"] = False
        if mutation == "auth_state":
            record["auth_state"] = "unverified"
        if mutation == "auth_method":
            record["auth_method"] = "chatgpt"
        if mutation == "credential":
            record["credential_present"] = provider_id == "ollama"
        if mutation == "runtime":
            document["runtimes"][provider_id]["state"] = "not_prepared"
        if mutation == "catalog_runtime":
            catalog["runtime_catalog_revision"] = "changed"
        if mutation == "availability":
            model["availability"] = "unverified"
        if mutation == "billing":
            model["billing"]["kind"] = "subscription"
        if mutation == "source":
            model["source"] = "local_catalog"
        if mutation == "model_revision":
            model["resolved_revision"] = "sha256:" + "c" * 64
        if mutation == "schema":
            model["parameter_schema"]["properties"]["max_tokens"]["maximum"] = 511
        if mutation == "endpoint":
            record["endpoint"] = "https://example.invalid"
    service.secrets.calls.clear()
    with pytest.raises(error):
        provider.complete("fixture prompt")
    assert backend.calls == [] and service.secrets.calls == []


@pytest.mark.parametrize(("known_max", "effective"), [(None, 4096), (128, 128), (10000, 4096)])
def test_output_defaults_distinguish_model_capacity_from_app_limit(
    http_generation, known_max, effective
):
    service, backend, config = http_generation
    config.minutes_model.parameters = {}
    with service.store.transaction() as document:
        model = document["catalogs"][config.minutes_model.connection_id]["models"][0]
        model["max_output_tokens"] = known_max
        model["context_window"] = None
    provider = MinutesResolver(service).resolve(config)
    assert provider.generation_params["effective_parameters"] == {"max_tokens": effective}
    assert provider.generation_params["max_output_tokens"] == known_max
    assert provider.generation_params["context_window"] is None
    provider.complete("fixture prompt")
    assert backend.calls[0][5] == {"max_tokens": effective}
    if known_max is not None:
        config.minutes_model.parameters["max_tokens"] = known_max + 1
        with pytest.raises(InvalidArgumentError):
            MinutesResolver(service).validate(config)
        assert len(backend.calls) == 1


@pytest.mark.parametrize("value", [True, False, 1.0, "512", 0, -1, 32769, None])
def test_mutated_token_parameter_is_strict_and_never_sends(http_generation, value):
    service, backend, config = http_generation
    config.minutes_model.parameters["max_tokens"] = value
    with pytest.raises(InvalidArgumentError):
        MinutesResolver(service).resolve(config)
    assert backend.calls == []


def test_keys_are_explicit_and_cannot_fall_back_to_another_connection(http_generation, monkeypatch):
    service, backend, config = http_generation
    provider_id = config.minutes_model.provider
    if provider_id == "ollama":
        return
    create_connection(service, provider_id=provider_id, key="other-fixture-key", request_id="other")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-fixture-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-fixture-key")
    account = service.store.read()["connections"][config.minutes_model.connection_id][
        "secret_account"
    ]
    service.secrets.calls.clear()
    provider = MinutesResolver(service).resolve(config)
    provider.complete("fixture prompt")
    assert backend.calls[0][3] == "fixture-key"
    assert service.secrets.calls == [("get", account)]
    service.secrets.values.pop(account)
    with pytest.raises(NarumiError) as error:
        provider.complete("fixture prompt")
    assert error.value.code == "authentication_required"
    assert len(backend.calls) == 1
    assert service.secrets.calls == [("get", account), ("get", account)]


def test_api_key_reflection_never_becomes_a_checkpoint(tmp_path, http_generation):
    service, backend, config = http_generation
    backend.response = "The response repeats fixture-key."
    provider = MinutesResolver(service).resolve(config)
    if config.minutes_model.provider == "ollama":
        # The local connection has no such credential; no global API key is loaded.
        assert provider.complete("fixture prompt") == backend.response
        return
    bundle = generation_bundle(tmp_path, config)
    with pytest.raises(NarumiError) as initial:
        run_generate(bundle, minutes_resolver=MinutesResolver(service))
    assert initial.value.details["outcome_unknown"] is True
    assert "fixture-key" not in str(initial.value)
    assert all("fixture-key" not in path.read_text() for path in bundle.path.rglob("*.json"))


@pytest.mark.parametrize(
    "failure",
    [
        EngineUnavailableError("fixture-key", details={"outcome_unknown": True}),
        EngineUnavailableError(
            "fixture-key", details={"reason": "codex_generation_outcome_unknown"}
        ),
        EngineUnavailableError(
            "fixture-key", details={"reason": "provider_generation_outcome_unknown"}
        ),
        CancelledError("fixture-key", details={"outcome_unknown": True}),
    ],
)
def test_unknown_http_reply_is_not_resent_without_new_attempt(tmp_path, http_generation, failure):
    service, backend, config = http_generation
    bundle = generation_bundle(tmp_path, config)
    resolver = MinutesResolver(service)
    backend.complete_error = failure
    with pytest.raises(NarumiError) as initial:
        run_generate(bundle, minutes_resolver=resolver)
    assert "fixture-key" not in str(initial.value)
    assert initial.value.details["outcome_unknown"] is True
    backend.complete_error = None
    with pytest.raises(EngineUnavailableError):
        run_generate(bundle, minutes_resolver=resolver)
    assert len(backend.calls) == 1
    bundle.manifest.config.minutes_model.cache_epoch += 1
    bundle.save()
    run_generate(bundle, minutes_resolver=resolver)
    assert len(backend.calls) == 3


def test_http_successful_chunks_survive_known_failure(tmp_path, http_generation):
    service, backend, config = http_generation
    bundle = generation_bundle(tmp_path, config)
    original = backend.complete
    dispatched = []

    def fail_second(*args, **kwargs):
        dispatched.append(args[5])
        if len(dispatched) == 2:
            raise EngineUnavailableError("fixture-key must not leak")
        return original(*args, **kwargs)

    backend.complete = fail_second
    resolver = MinutesResolver(service)
    with pytest.raises(NarumiError):
        run_generate(bundle, minutes_resolver=resolver)
    run_generate(bundle, minutes_resolver=resolver)
    assert dispatched.count(dispatched[0]) == 1
    assert len(backend.calls) == 2


def test_lease_blocks_http_replacement_and_delete_until_current_send_finishes(http_generation):
    service, backend, config = http_generation
    provider = MinutesResolver(service).resolve(config)
    entered, release = threading.Event(), threading.Event()
    original = backend.complete

    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    backend.complete = blocking
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(provider.complete, "fixture prompt")
        assert entered.wait(5)
        try:
            args = {"connection_id": config.minutes_model.connection_id, "expected_revision": 1}
            with pytest.raises(BusyError):
                service.set_connection(
                    {**args, "display_name": "renamed", "request_id": "http-rename"}
                )
            with pytest.raises(BusyError):
                service.delete_connection({**args, "confirm": True, "request_id": "http-delete"})
            service.set_connection({**args, "enabled": False, "request_id": "http-disable"})
        finally:
            release.set()
        assert future.result(timeout=5) == backend.response
    with pytest.raises(ConfigurationConflictError):
        provider.complete("next prompt")
    assert service.store.read()["checks"] == {}


@pytest.mark.parametrize(
    "operation", [run_generate, process_meeting, regenerate_meeting, refresh_meeting]
)
def test_force_cannot_bypass_http_attempt_boundaries(tmp_path, http_generation, operation):
    service, backend, config = http_generation
    bundle = generation_bundle(tmp_path, config)
    previous = bundle.manifest.model_dump()
    with pytest.raises(InvalidArgumentError):
        operation(bundle, force=True, minutes_resolver=MinutesResolver(service))
    assert previous == bundle.manifest.model_dump() and backend.calls == []


@pytest.mark.parametrize("expires", ["2026-08-28", "2026-08-29", "invalid", "2026-02-30", True])
def test_expiry_is_rechecked_after_resolution_and_rejects_invalid_dates(
    http_generation, monkeypatch, expires
):
    service, backend, config = http_generation

    class FrozenDatetime:
        @staticmethod
        def now(tz):
            assert tz == UTC
            return datetime(2026, 8, 29, tzinfo=UTC)

    monkeypatch.setattr(generation_module, "datetime", FrozenDatetime)
    provider = MinutesResolver(service).resolve(config)
    with service.store.transaction() as document:
        document["catalogs"][config.minutes_model.connection_id]["models"][0][
            "availability_expires_on"
        ] = expires
    with pytest.raises(ModelUnavailableError):
        provider.complete("fixture prompt")
    with pytest.raises(ModelUnavailableError):
        MinutesResolver(service).validate(config)
    assert backend.calls == []


def test_observation_refresh_does_not_create_a_new_generation_attempt(http_generation):
    service, backend, config = http_generation
    provider = MinutesResolver(service).resolve(config)
    params = deepcopy(provider.generation_params)
    with service.store.transaction() as document:
        catalog = document["catalogs"][config.minutes_model.connection_id]
        catalog["fetched_at"] = "2027-01-01T00:00:00Z"
        catalog["catalog_id"] = "a-fresh-observation"
        catalog["models"][0]["fetched_at"] = catalog["fetched_at"]
        catalog["models"][0]["billing"]["fetched_at"] = catalog["fetched_at"]
        catalog["models"][0]["availability_expires_on"] = "2999-01-01"
    assert MinutesResolver(service).validate(config) == params
    provider.complete("fixture prompt")
    assert len(backend.calls) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("response", ""),
        ("returned_model", ""),
        ("usage", {"input_tokens": True}),
        ("usage", {"output_tokens": -1}),
        ("usage", {"api_key": "fixture-key"}),
    ],
)
def test_invalid_completion_metadata_stays_unknown_without_a_resend(
    tmp_path, http_generation, field, value
):
    service, backend, config = http_generation
    bundle = generation_bundle(tmp_path, config)
    resolver = MinutesResolver(service)
    setattr(backend, field, value)
    with pytest.raises(NarumiError) as initial:
        run_generate(bundle, minutes_resolver=resolver)
    assert initial.value.details["outcome_unknown"] is True
    with pytest.raises(EngineUnavailableError):
        run_generate(bundle, minutes_resolver=resolver)
    assert len(backend.calls) == 1
    assert bundle.manifest.latest_minutes_version is None
    assert all("fixture-key" not in path.read_text() for path in bundle.path.rglob("*.json"))


def test_saved_expiry_blocks_the_next_call_when_utc_date_reaches_it(http_generation, monkeypatch):
    service, backend, config = http_generation
    today = [datetime(2026, 8, 29, 23, 59, tzinfo=UTC)]

    class FrozenDatetime:
        @staticmethod
        def now(tz):
            assert tz == UTC
            return today[0]

    monkeypatch.setattr(generation_module, "datetime", FrozenDatetime)
    with service.store.transaction() as document:
        model = document["catalogs"][config.minutes_model.connection_id]["models"][0]
        model["availability_expires_on"] = "2026-08-30"
    provider = MinutesResolver(service).resolve(config)
    provider.complete("first prompt")
    today[0] = datetime(2026, 8, 30, tzinfo=UTC)
    with pytest.raises(ModelUnavailableError):
        provider.complete("next prompt")
    assert len(backend.calls) == 1


@pytest.mark.parametrize(
    ("model_id", "effort"), [("gpt-4.1", None), ("gpt-5.4", "none"), ("gpt-5.6-sol", "medium")]
)
def test_openai_catalog_projection_drives_effective_generation_defaults(tmp_path, model_id, effort):
    from narumi.providers.metadata.openai import fetch_models

    def model_list(method, route):
        assert (method, route) == ("GET", "/v1/models")
        return {
            "object": "list",
            "data": [{"object": "model", "id": model_id, "created": 1, "owned_by": "openai"}],
        }

    models = fetch_models(
        model_list, fetched_at="2026-08-29T00:00:00Z", now=datetime(2026, 8, 29, tzinfo=UTC)
    )
    backend = FakeHTTPBackend()
    service = ProviderService(
        tmp_path / "providers-home",
        secret_store=MemorySecretStore(),
        metadata_client=FakeMetadata(),
        runtime_inspector=FakeRuntimeInspector(),
        http_backend=backend,
    )
    try:
        record = prepared_http_connection(service, models=models)
        config = MeetingConfig(
            external_send_policy="api_ok",
            minutes_model=ModelSelection(
                provider="openai-api",
                connection_id=record["connection_id"],
                connection_revision=record["revision"],
                model_id=model_id,
            ),
        )
        provider = MinutesResolver(service).resolve(config)
        expected = {
            "max_tokens": 4096,
            **({"reasoning_effort": effort} if effort is not None else {}),
        }
        provider.complete("fixture prompt")
        assert backend.calls[0][5] == expected
        assert provider.generation_params["effective_parameters"] == expected
        assert provider.generation_params["context_window"] == models[0]["context_window"]
        config.minutes_model.parameters = {"reasoning_effort": "unsupported"}
        with pytest.raises(InvalidArgumentError):
            MinutesResolver(service).validate(config)
        assert len(backend.calls) == 1
    finally:
        service.close()
