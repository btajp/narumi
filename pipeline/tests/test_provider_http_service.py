"""HTTP connections expose metadata checks without implying a successful generation."""

import copy
import json
import sys
from types import SimpleNamespace

import pytest
from narumi.contracts.loader import load_contracts
from narumi.errors import BusyError, InvalidArgumentError, NarumiError
from narumi.providers.service import ProviderService

from .provider_fakes import (
    INSTANCE_ONE,
    FakeCodexBackend,
    FakeHTTPBackend,
    FakeMetadata,
    FakeRuntimeInspector,
    JobQueue,
    ManualExecutor,
    MemorySecretStore,
    create_connection,
    prepared_http_connection,
)


@pytest.fixture
def http_setup(tmp_path):
    secrets, metadata, executor = MemorySecretStore(), FakeMetadata(), ManualExecutor()
    inspector, jobs, backend = FakeRuntimeInspector(), JobQueue(), FakeHTTPBackend()
    service = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_ONE,
        runtime_inspector=inspector,
        submit_job=jobs,
        codex_backend=FakeCodexBackend(),
        http_backend=backend,
    )
    yield service, jobs, backend
    service.close()


def prepare(service, jobs, provider_id, *, request_id):
    descriptor = next(
        item for item in service.list_providers()["providers"] if item["provider_id"] == provider_id
    )
    runtime = descriptor["runtime"]
    service.prepare_runtime(
        {
            "provider_id": provider_id,
            "resource_id": runtime["resources"][0]["resource_id"],
            "expected_catalog_revision": runtime["catalog_revision"],
            "action": "prepare",
            "request_id": request_id,
        }
    )
    jobs.run(len(jobs.calls) - 1)


def test_openai_save_and_readback_do_not_check_or_generate(http_setup):
    service, _, backend = http_setup
    record = create_connection(service, provider_id="openai-api")
    assert record["endpoint"] == "https://api.openai.com"
    assert record["auth_method"] == "api_key"
    assert record["credential_present"] is True
    assert record["auth_state"] == "unverified"
    assert record["last_generation_state"] == "never"
    calls = list(service.secrets.calls)
    assert service.list_connections() == {"connections": [record]}
    assert service.list_models({"connection_id": record["connection_id"]})["models"] == []
    assert service.secrets.calls == calls
    assert service.metadata.calls == backend.calls == service.codex_backend.calls == []
    load_contracts().validate_output("set_provider_connection", {"connection": record})
    assert "fixture-key" not in json.dumps(record)
    assert "fixture-key" not in service.store.path.read_text()


@pytest.mark.parametrize("key", ['fixture"escaped-key', "fixture\\escaped-key"])
def test_escaped_credentials_cannot_be_saved_as_public_fields(http_setup, key):
    service, _, backend = http_setup
    with pytest.raises(InvalidArgumentError) as failure:
        service.set_connection(
            {
                "provider_id": "openai-api",
                "auth_method": "api_key",
                "display_name": key,
                "api_key": key,
                "request_id": "reject-escaped-key-in-display-name",
            }
        )
    assert key not in str(failure.value)
    assert service.list_connections() == {"connections": []}
    assert service.secrets.calls == service.metadata.calls == backend.calls == []


@pytest.mark.parametrize("key", ['fixture"escaped-key', "fixture\\escaped-key"])
def test_escaped_credentials_in_metadata_are_rejected_before_caching(http_setup, key):
    service, _, backend = http_setup
    record = create_connection(service, provider_id="openai-api", key=key)
    service.metadata.models[0]["display_name"] = key
    result = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 1}
    )
    assert result["connected"] is False
    assert result["reason"] == "metadata_response_rejected"
    assert service.list_models({"connection_id": record["connection_id"]})["models"] == []
    assert service.store.read()["catalogs"] == {}
    assert backend.calls == []


def test_openai_key_omission_preserves_and_null_explicitly_clears(http_setup):
    service, _, backend = http_setup
    record = prepared_http_connection(service)
    calls = list(service.secrets.calls)
    renamed = service.set_connection(
        {
            "connection_id": record["connection_id"],
            "expected_revision": 1,
            "display_name": "Renamed API connection",
            "request_id": "rename-openai",
        }
    )["connection"]
    assert renamed["revision"] == 2 and renamed["credential_present"] is True
    assert service.secrets.calls == calls
    result = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 2}
    )
    assert result["connected"] is True
    assert service.metadata.calls[-1] == ("openai-api", "https://api.openai.com", "fixture-key")
    cleared = service.set_connection(
        {
            "connection_id": record["connection_id"],
            "expected_revision": 2,
            "api_key": None,
            "request_id": "clear-openai-key",
        }
    )["connection"]
    assert cleared["credential_present"] is False
    assert cleared["auth_state"] == "unconfigured"
    assert "fixture-key" not in service.secrets.values.values()
    checked = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 3}
    )
    assert checked["connected"] is False and checked["reason"] == "credential_required"
    assert len(service.metadata.calls) == 2
    assert service.list_models({"connection_id": record["connection_id"]})["models"] == []
    assert backend.calls == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.anthropic.com",
        "https://chatgpt.com",
        "https://example.com",
        "https://api.openai.com.example.com",
        "https://fixture-user:fixture-secret@api.openai.com",
        "http://api.openai.com",
        "https://api.openai.com:444",
        "https://api.openai.com/v1",
        "https://api.openai.com?key=fixture-secret",
        "https://api.openai.com#fixture-secret",
        "https://api.openai.com\n",
    ],
)
def test_openai_cannot_switch_to_an_unapproved_endpoint(http_setup, endpoint):
    service, _, backend = http_setup
    record = create_connection(service, provider_id="openai-api")
    before = service.store.path.read_bytes()
    with pytest.raises(InvalidArgumentError) as failure:
        service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "endpoint": endpoint,
                "api_key": "replacement-fixture-key",
                "request_id": "change-openai-endpoint",
            }
        )
    assert "fixture-secret" not in str(failure.value)
    assert service.store.path.read_bytes() == before
    assert service.metadata.calls == backend.calls == []


def test_openai_connection_check_only_reports_model_list_access(http_setup):
    service, _, backend = http_setup
    record = create_connection(service, provider_id="openai-api")
    result = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 1}
    )
    assert result["connected"] is True
    assert result["reason"] == "model_list_verified_generation_unchecked"
    assert result["connection"]["auth_state"] == "authenticated"
    assert result["connection"]["last_generation_state"] == "never"
    assert service.metadata.calls == [("openai-api", "https://api.openai.com", "fixture-key")]
    assert backend.calls == []
    load_contracts().validate_output("test_provider_connection", result)


@pytest.mark.parametrize("provider_id", ["openai-api", "anthropic-api", "ollama"])
def test_unprepared_metadata_needs_preparation_and_explicit_refresh(http_setup, provider_id):
    service, jobs, backend = http_setup
    record = create_connection(service, provider_id=provider_id)
    service.metadata.models[0].update(availability="available", reason=None)
    args = {"connection_id": record["connection_id"]}
    observed = service.list_models({**args, "refresh": True})
    assert observed["catalog_state"] == "stale"
    assert observed["models"][0]["availability"] == "unverified"
    assert (
        service.store.read()["catalogs"][record["connection_id"]]["runtime_catalog_revision"]
        is None
    )
    prepare(service, jobs, provider_id, request_id="prepare-http-after-metadata")
    still_stale = service.list_models(args)
    assert still_stale["catalog_state"] == "stale"
    assert len(service.metadata.calls) == 1
    refreshed = service.list_models({**args, "refresh": True})
    assert refreshed["catalog_state"] == "ready"
    assert refreshed["models"][0]["availability"] == "available"
    saved = service.store.read()
    assert (
        saved["catalogs"][record["connection_id"]]["runtime_catalog_revision"]
        == (saved["runtimes"][provider_id]["catalog_revision"])
    )
    assert backend.calls == []


@pytest.mark.parametrize("provider_id", ["openai-api", "anthropic-api", "ollama"])
def test_runtime_update_is_stale_even_before_a_new_preparation(http_setup, provider_id):
    service, jobs, backend = http_setup
    record = prepared_http_connection(service, provider_id)
    args = {"connection_id": record["connection_id"]}
    assert service.list_models(args)["catalog_state"] == "ready"
    service.runtime.inspector.version = "2.0.0"
    before = service.store.path.read_bytes()
    assert service.list_models(args)["catalog_state"] == "stale"
    assert service.store.path.read_bytes() == before
    assert len(service.metadata.calls) == 1
    assert service.list_models({**args, "refresh": True})["catalog_state"] == "stale"
    prepare(service, jobs, provider_id, request_id="prepare-new-http-version")
    assert service.list_models(args)["catalog_state"] == "stale"
    assert service.list_models({**args, "refresh": True})["catalog_state"] == "ready"
    assert backend.calls == []


def test_runtime_change_during_metadata_fetch_cannot_claim_new_revision(http_setup, monkeypatch):
    service, _, backend = http_setup
    record = prepared_http_connection(service)
    previous = copy.deepcopy(service.store.read()["catalogs"][record["connection_id"]])
    original = service.metadata.fetch

    def update_after_snapshot(*args):
        service.runtime.inspector.version = "2.0.0"
        return original(*args)

    monkeypatch.setattr(service.metadata, "fetch", update_after_snapshot)
    result = service.list_models({"connection_id": record["connection_id"], "refresh": True})
    saved = service.store.read()["catalogs"][record["connection_id"]]
    assert saved["runtime_catalog_revision"] == previous["runtime_catalog_revision"]
    assert result["catalog_state"] == "stale"
    assert backend.calls == []


def test_legacy_http_catalog_requires_a_fresh_observation(http_setup):
    service, _, backend = http_setup
    record = prepared_http_connection(service, "anthropic-api")
    with service.store.transaction() as document:
        document["catalogs"][record["connection_id"]].pop("runtime_catalog_revision")
    before = service.store.path.read_bytes()
    args = {"connection_id": record["connection_id"]}
    assert service.list_models(args)["catalog_state"] == "stale"
    assert service.store.path.read_bytes() == before
    assert len(service.metadata.calls) == 1
    assert service.list_models({**args, "refresh": True})["catalog_state"] == "ready"
    assert backend.calls == []


def test_openai_secret_replacement_failure_cannot_fallback_to_another_key(http_setup, monkeypatch):
    service, _, backend = http_setup
    first = prepared_http_connection(service, key="first-fixture-key")
    second = create_connection(
        service, provider_id="openai-api", key="second-fixture-key", request_id="second-key"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-fixture-key")
    service.secrets.fail_set = True
    with pytest.raises(NarumiError):
        service.set_connection(
            {
                "connection_id": first["connection_id"],
                "expected_revision": 1,
                "api_key": "new-fixture-key",
                "request_id": "fail-openai-replacement",
            }
        )
    result = service.test_connection(
        {"connection_id": first["connection_id"], "expected_revision": 2}
    )
    assert result["connected"] is False and result["reason"] == "credential_required"
    assert len(service.metadata.calls) == 1
    assert (
        service.test_connection({"connection_id": second["connection_id"], "expected_revision": 1})[
            "connected"
        ]
        is True
    )
    assert service.metadata.calls[-1][-1] == "second-fixture-key"
    assert "fixture-key" not in service.store.path.read_text()
    assert backend.calls == []


@pytest.mark.parametrize("provider_id", ["openai-api", "anthropic-api", "ollama"])
def test_http_generation_lease_blocks_key_mutation_but_allows_disable(http_setup, provider_id):
    service, _, backend = http_setup
    record = prepared_http_connection(service, provider_id)
    with service.store.transaction() as document:
        document["checks"][provider_id] = {
            "token": "generation-fixture",
            "connection_id": record["connection_id"],
            "server_instance_id": service.server_instance_id,
            "kind": "generation",
        }
    with pytest.raises(BusyError):
        service.authenticate(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "action": "logout",
                "request_id": "logout-during-http-generation",
            }
        )
    with pytest.raises(BusyError):
        service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "display_name": "Blocked rename",
                "request_id": "rename-during-http-generation",
            }
        )
    disabled = service.set_connection(
        {
            "connection_id": record["connection_id"],
            "expected_revision": 1,
            "enabled": False,
            "request_id": "disable-http-generation",
        }
    )["connection"]
    assert disabled["enabled"] is False
    assert (
        service.list_models({"connection_id": record["connection_id"]})["catalog_state"] == "stale"
    )
    assert service.store.read()["checks"][provider_id]["token"] == "generation-fixture"
    assert backend.calls == []


def test_http_backend_is_lazy_and_uses_the_injected_metadata_client(tmp_path, monkeypatch):
    calls, backend, metadata = [], FakeHTTPBackend(), FakeMetadata()

    def construct(**kwargs):
        calls.append(kwargs)
        return backend

    monkeypatch.setitem(
        sys.modules,
        "narumi.providers.http_generation",
        SimpleNamespace(HTTPMinutesBackend=construct),
    )
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        metadata_client=metadata,
        auth_executor=ManualExecutor(),
        runtime_inspector=FakeRuntimeInspector(),
        codex_backend=FakeCodexBackend(),
    )
    try:
        service.list_connections()
        service.list_providers()
        assert calls == []
        assert service.http_backend is backend
        assert service.http_backend is backend
        assert calls == [{"metadata": metadata}]
        assert backend.calls == metadata.calls == []
    finally:
        service.close()
    with pytest.raises(NarumiError, match="closed"):
        _ = service.http_backend
