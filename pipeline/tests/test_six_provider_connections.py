"""Six-provider connection registry integration without external provider access."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from narumi.errors import ConfigurationConflictError, InvalidArgumentError
from narumi.providers.runtime_catalog import RuntimeInspector
from narumi.providers.service import ProviderService
from narumi.providers.store import ProviderStore

from .provider_fakes import FakeCodexBackend, MemorySecretStore

PROVIDER_IDS = [
    "codex-app-server",
    "claude-agent-sdk",
    "openai-api",
    "openai-compatible-api",
    "anthropic-api",
    "ollama",
]
AUTH_METHODS = {
    "codex-app-server": ["chatgpt"],
    "claude-agent-sdk": ["api_key"],
    "openai-api": ["api_key"],
    "openai-compatible-api": ["api_key", "none"],
    "anthropic-api": ["api_key"],
    "ollama": ["none"],
}
SECRET = "fixture-compatible-api-key-9137"


class StaticRuntimeInspector(RuntimeInspector):
    """Stable installed-runtime metadata with no import, execution or network effects."""

    def resource(self, provider_id: str) -> dict:
        assert provider_id in set(PROVIDER_IDS) - {"codex-app-server"}
        return {
            "resource_id": {
                "claude-agent-sdk": "claude-sdk",
                "openai-api": "openai-client",
                "openai-compatible-api": "openai-compatible-client",
                "anthropic-api": "anthropic-client",
                "ollama": "local-ollama",
            }[provider_id],
            "display_name": f"{provider_id} fixture runtime",
            "kind": "runtime",
            "version": "1.0.0",
            "source": "installed",
            "download_host": None,
            "sha256": "a" * 64,
            "license": "Fixture license",
        }


@pytest.fixture
def service(tmp_path: Path):
    secrets = MemorySecretStore()
    instance = ProviderService(
        tmp_path,
        secret_store=secrets,
        codex_backend=FakeCodexBackend(),
        runtime_inspector=StaticRuntimeInspector(),
    )
    try:
        yield instance, secrets
    finally:
        instance.close()


def compatible_connection(**updates) -> dict:
    return {
        "provider_id": "openai-compatible-api",
        "display_name": "Compatible fixture",
        "endpoint": "https://models.example.com/v1",
        "auth_method": "api_key",
        "api_surface": "chat_completions",
        "chat_max_tokens_field": "max_completion_tokens",
        "api_key": SECRET,
        "request_id": "compatible-create-001",
        **updates,
    }


def test_provider_order_and_auth_methods_are_exact_and_stable(service):
    provider_service, _ = service
    providers = provider_service.list_providers()["providers"]
    assert [provider["provider_id"] for provider in providers] == PROVIDER_IDS
    assert {
        provider["provider_id"]: provider["auth_methods"] for provider in providers
    } == AUTH_METHODS


def test_compatible_settings_round_trip_cas_and_auth_switching(service):
    provider_service, secrets = service
    created = provider_service.set_connection(compatible_connection())["connection"]
    assert {
        key: created[key]
        for key in (
            "provider_id",
            "endpoint",
            "auth_method",
            "api_surface",
            "chat_max_tokens_field",
            "credential_present",
        )
    } == {
        "provider_id": "openai-compatible-api",
        "endpoint": "https://models.example.com/v1",
        "auth_method": "api_key",
        "api_surface": "chat_completions",
        "chat_max_tokens_field": "max_completion_tokens",
        "credential_present": True,
    }
    assert "secret_account" not in created
    assert provider_service.list_connections()["connections"] == [created]
    assert SECRET not in json.dumps(created)

    with pytest.raises(ConfigurationConflictError):
        provider_service.set_connection(
            {
                "connection_id": created["connection_id"],
                "expected_revision": 2,
                "display_name": "stale update",
                "request_id": "compatible-stale-001",
            }
        )

    with pytest.raises(InvalidArgumentError):
        provider_service.set_connection(
            {
                "connection_id": created["connection_id"],
                "expected_revision": 1,
                "endpoint": "http://127.0.0.1:8080/v1",
                "auth_method": "none",
                "request_id": "compatible-none-without-key-deletion-001",
            }
        )
    assert provider_service.list_connections()["connections"] == [created]

    unauthenticated = provider_service.set_connection(
        {
            "connection_id": created["connection_id"],
            "expected_revision": 1,
            "endpoint": "http://127.0.0.1:8080/v1",
            "auth_method": "none",
            "api_key": None,
            "request_id": "compatible-none-001",
        }
    )["connection"]
    assert unauthenticated["revision"] == 2
    assert unauthenticated["auth_method"] == "none"
    assert unauthenticated["credential_present"] is False
    assert unauthenticated["api_surface"] == "chat_completions"
    assert unauthenticated["chat_max_tokens_field"] == "max_completion_tokens"
    assert SECRET not in secrets.values.values()

    authenticated = provider_service.set_connection(
        {
            "connection_id": created["connection_id"],
            "expected_revision": 2,
            "endpoint": "https://models.example.com/v1",
            "auth_method": "api_key",
            "api_key": SECRET,
            "api_surface": "responses",
            "chat_max_tokens_field": None,
            "request_id": "compatible-key-002",
        }
    )["connection"]
    assert authenticated["revision"] == 3
    assert authenticated["auth_method"] == "api_key"
    assert authenticated["credential_present"] is True
    assert authenticated["api_surface"] == "responses"
    assert "chat_max_tokens_field" not in authenticated


def test_remote_compatible_endpoint_rejects_unauthenticated_access(service):
    provider_service, secrets = service
    with pytest.raises(InvalidArgumentError):
        provider_service.set_connection(
            compatible_connection(
                auth_method="none",
                api_key=None,
                api_surface="responses",
                chat_max_tokens_field=None,
                request_id="compatible-remote-none-001",
            )
        )
    assert not provider_service.list_connections()["connections"]
    assert SECRET not in secrets.values.values()


def test_legacy_registry_is_normalized_in_memory_then_persisted_by_transaction(
    tmp_path: Path,
):
    root = tmp_path / "legacy"
    store = ProviderStore(root)
    with store.transaction() as document:
        document["connections"]["conn-0123456789ab"] = {
            "connection_id": "conn-0123456789ab",
            "revision": 4,
            "provider_id": "openai-api",
            "display_name": "Legacy OpenAI",
            "enabled": True,
            "endpoint": "https://api.openai.com",
            "auth_method": "api_key",
            "credential_present": False,
            "auth_state": "unconfigured",
            "catalog_state": "unfetched",
            "checked_at": None,
            "active_auth": None,
            "last_generation_state": "never",
            "secret_account": None,
            "pending_secret_accounts": [],
        }
    legacy = json.loads(store.path.read_text())
    legacy["connections"]["conn-0123456789ab"].pop("api_surface", None)
    legacy["connections"]["conn-0123456789ab"].pop("chat_max_tokens_field", None)
    original = json.dumps(legacy, sort_keys=True)
    store.path.write_text(original)

    reopened = ProviderStore(root)
    migrated = reopened.read()["connections"]["conn-0123456789ab"]
    assert migrated["api_surface"] == "responses"
    assert migrated["chat_max_tokens_field"] is None
    assert migrated["revision"] == 4
    assert store.path.read_text() == original

    with reopened.transaction():
        pass
    persisted = json.loads(store.path.read_text())["connections"]["conn-0123456789ab"]
    assert persisted["api_surface"] == "responses"
    assert persisted["chat_max_tokens_field"] is None
    assert persisted["revision"] == 4
