"""Provider settings retain secrets outside files and keep observations separate from config."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from narumi.contracts.loader import load_contracts
from narumi.errors import ConfigurationConflictError, InvalidArgumentError, NarumiError
from narumi.providers.service import ProviderService

from .provider_fakes import (
    INSTANCE_ONE,
    INSTANCE_TWO,
    FakeMetadata,
    FakeRuntimeInspector,
    ManualExecutor,
    MemorySecretStore,
    create_connection,
)


@pytest.fixture
def effects():
    return MemorySecretStore(), FakeMetadata(), ManualExecutor()


@pytest.fixture
def service(tmp_path, effects):
    secrets, metadata, executor = effects
    instance = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_ONE,
        runtime_inspector=FakeRuntimeInspector(),
    )
    yield instance
    instance.close()


def test_lists_do_not_access_secrets_or_network(service, effects):
    secrets, metadata, _ = effects
    contracts = load_contracts()
    providers = service.list_providers()
    contracts.validate_output("list_providers", providers)
    assert {item["provider_id"] for item in providers["providers"]} == {
        "anthropic-api",
        "claude-agent-sdk",
        "ollama",
    }
    assert service.list_connections() == {"connections": []}
    assert secrets.calls == metadata.calls == []


def test_connection_replay_survives_restart_and_does_not_write_secret_twice(
    service, effects, tmp_path
):
    secrets, metadata, executor = effects
    args = {
        "provider_id": "anthropic-api",
        "display_name": "API",
        "auth_method": "api_key",
        "api_key": "fixture-key",
        "request_id": "same-request",
    }
    first = service.set_connection(args)
    record = first["connection"]
    assert record["credential_present"] is True
    assert record["auth_state"] == "unverified"
    assert service.set_connection(args) == first
    restarted = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_TWO,
    )
    assert restarted.set_connection(args) == first
    assert len([call for call in secrets.calls if call[0] == "set"]) == 2  # HMAC + key
    saved = service.store.path.read_text()
    assert "fixture-key" not in saved
    assert '"api_key":' not in saved
    assert "hmac-sha256" in saved
    with pytest.raises(ConfigurationConflictError):
        restarted.set_connection({**args, "api_key": "another-key"})
    assert len(service.list_connections()["connections"]) == 1
    assert "secret_account" not in json.dumps(first)
    restarted.close()


def test_partial_updates_keep_keys_but_credential_updates_invalidate_cache(service, effects):
    secrets, metadata, _ = effects
    record = create_connection(service)
    tested = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 1}
    )
    assert tested["connected"] is True
    assert tested["connection"]["revision"] == 1
    renamed = service.set_connection(
        {
            "connection_id": record["connection_id"],
            "expected_revision": 1,
            "display_name": "Renamed",
            "request_id": "rename-connection",
        }
    )["connection"]
    assert renamed["revision"] == 2
    assert renamed["credential_present"] is True
    assert renamed["catalog_state"] == "ready"
    updated = service.set_connection(
        {
            "connection_id": record["connection_id"],
            "expected_revision": 2,
            "api_key": "new-fixture-key",
            "request_id": "replace-connection",
        }
    )["connection"]
    assert updated["revision"] == 3
    assert updated["catalog_state"] == "unfetched"
    assert updated["auth_state"] == "unverified"
    assert "fixture-key" not in secrets.values.values()
    assert service.list_models({"connection_id": record["connection_id"]})["models"] == []
    assert len(metadata.calls) == 1


def test_keychain_failure_cannot_reuse_old_key(service, effects):
    secrets, metadata, _ = effects
    record = create_connection(service)
    secrets.fail_set = True
    with pytest.raises(NarumiError) as failure:
        service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "api_key": "replacement-key",
                "request_id": "failing-change",
            }
        )
    assert "fixture-key" not in str(failure.value)
    saved = service.list_connections()["connections"][0]
    assert saved["credential_present"] is False
    assert saved["auth_state"] == "unconfigured"
    tested = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 2}
    )
    assert tested["connected"] is False
    assert metadata.calls == []


def test_failed_final_metadata_write_leaves_new_key_unreachable(service, effects, monkeypatch):
    record = create_connection(service)
    original_save = service.store.commit
    commits = []

    def fail_second(document):
        commits.append(1)
        if len(commits) > 1:
            raise NarumiError("fixture write failure")
        original_save(document)

    # The final transaction commit uses the same path as explicit write-ahead commits.
    monkeypatch.setattr(service.store, "commit", fail_second)
    with pytest.raises(NarumiError):
        service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "api_key": "replacement-key",
                "request_id": "incomplete-change",
            }
        )
    assert service.list_connections()["connections"][0]["credential_present"] is False
    monkeypatch.setattr(service.store, "commit", original_save)
    service.delete_connection(
        {
            "connection_id": record["connection_id"],
            "expected_revision": 2,
            "confirm": True,
            "request_id": "delete-interrupted-change",
        }
    )
    assert "replacement-key" not in effects[0].values.values()


def test_two_instances_only_one_expected_revision_update_wins(service, effects, tmp_path):
    record = create_connection(service)
    secrets, metadata, executor = effects
    other = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_TWO,
    )

    def rename(instance, name):
        try:
            return instance.set_connection(
                {
                    "connection_id": record["connection_id"],
                    "expected_revision": 1,
                    "display_name": name,
                    "request_id": "rename-" + name,
                }
            )
        except ConfigurationConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda item: rename(*item), [(service, "first"), (other, "second")])
        )
    assert sum(result is not None for result in results) == 1
    assert service.list_connections()["connections"][0]["revision"] == 2
    other.close()


def test_connection_metadata_and_logout_are_isolated(service, effects):
    first = create_connection(service, key="key-one", request_id="first")
    second = create_connection(service, key="key-two", request_id="second")
    service.test_connection({"connection_id": first["connection_id"], "expected_revision": 1})
    assert service.list_models({"connection_id": first["connection_id"]})["models"]
    assert service.list_models({"connection_id": second["connection_id"]})["models"] == []
    service.authenticate(
        {
            "connection_id": first["connection_id"],
            "expected_revision": 1,
            "action": "logout",
            "request_id": "logout-first",
        }
    )
    tested = service.test_connection(
        {"connection_id": second["connection_id"], "expected_revision": 1}
    )
    assert tested["connected"] is True
    assert effects[1].calls[-1][-1] == "key-two"


def test_metadata_failure_is_safe_and_does_not_mark_generation_succeeded(service, effects):
    record = create_connection(service)
    effects[1].error = RuntimeError("fixture-key and /private/connection details")
    result = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 1}
    )
    assert result["connected"] is False
    assert result["reason"] == "metadata_unavailable"
    assert result["connection"]["last_generation_state"] == "never"
    assert "fixture-key" not in json.dumps(result)
    assert "fixture-key" not in service.store.path.read_text()


def test_untrusted_metadata_cannot_echo_credential(service, effects):
    record = create_connection(service)
    effects[1].models[0]["display_name"] = "Echo fixture-key"
    result = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 1}
    )
    assert result["connected"] is False
    assert result["reason"] == "metadata_response_rejected"
    assert "fixture-key" not in service.store.path.read_text()


def test_pagination_is_bound_to_configuration_and_catalog(service, effects):
    import copy

    record = create_connection(service)
    model = effects[1].models[0]
    effects[1].models = [
        {**copy.deepcopy(model), "model_id": f"fixture-model-{index}"} for index in range(102)
    ]
    first = service.list_models({"connection_id": record["connection_id"], "refresh": True})
    assert len(first["models"]) == 100
    second = service.list_models(
        {"connection_id": record["connection_id"], "cursor": first["next_cursor"]}
    )
    assert len(second["models"]) == 2
    assert second["next_cursor"] is None
    assert len(effects[1].calls) == 1
    service.list_models({"connection_id": record["connection_id"], "refresh": True})
    with pytest.raises(InvalidArgumentError):
        service.list_models(
            {"connection_id": record["connection_id"], "cursor": first["next_cursor"]}
        )


def test_disabled_connection_stays_visible_but_cannot_contact_provider(service, effects):
    record = create_connection(service)
    service.set_connection(
        {
            "connection_id": record["connection_id"],
            "expected_revision": 1,
            "enabled": False,
            "request_id": "disable-connection",
        }
    )
    assert service.list_connections()["connections"][0]["enabled"] is False
    result = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 2}
    )
    assert result["connected"] is False
    assert effects[1].calls == []


def test_changed_connection_releases_inflight_metadata_lease(service, effects, monkeypatch):
    record = create_connection(service)
    entered, release = threading.Event(), threading.Event()
    original = effects[1].fetch

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(effects[1], "fetch", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            service.test_connection,
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
            },
        )
        assert entered.wait(3)
        service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "enabled": False,
                "request_id": "disable-inflight-check",
            }
        )
        release.set()
        with pytest.raises(ConfigurationConflictError):
            future.result(timeout=3)
    assert service.store.read()["checks"] == {}
    enabled = service.set_connection(
        {
            "connection_id": record["connection_id"],
            "expected_revision": 2,
            "enabled": True,
            "request_id": "reenable-after-check",
        }
    )["connection"]
    assert enabled["auth_state"] == "unverified"
    assert enabled["catalog_state"] == "unfetched"
    assert (
        service.test_connection({"connection_id": record["connection_id"], "expected_revision": 3})[
            "connected"
        ]
        is True
    )


def test_delete_clears_only_selected_credential_and_replays(service, effects):
    first = create_connection(service, key="key-one", request_id="first")
    second = create_connection(service, key="key-two", request_id="second")
    args = {
        "connection_id": first["connection_id"],
        "expected_revision": 1,
        "confirm": True,
        "request_id": "delete-connection",
    }
    assert service.delete_connection(args) == service.delete_connection(args)
    assert [record["connection_id"] for record in service.list_connections()["connections"]] == [
        second["connection_id"]
    ]
    assert "key-one" not in effects[0].values.values()
    assert "key-two" in effects[0].values.values()


@pytest.mark.parametrize("key", ["key\nheader", "key\rheader", "key with space", "キー"])
def test_reject_header_unsafe_api_keys_without_echo(service, key):
    with pytest.raises(InvalidArgumentError) as error:
        create_connection(service, key=key)
    assert key not in str(error.value)


def test_known_credential_in_public_field_is_rejected_before_persistence(service, effects):
    with pytest.raises(InvalidArgumentError):
        service.set_connection(
            {
                "provider_id": "anthropic-api",
                "display_name": "Copy fixture-key",
                "auth_method": "api_key",
                "api_key": "fixture-key",
                "request_id": "reject-public-credential",
            }
        )
    assert service.list_connections()["connections"] == []
    assert effects[0].calls == []


@pytest.mark.parametrize("failure_stage", ["keychain", "final_commit"])
def test_explicit_save_recovery_does_not_repeat_failed_creation(
    service,
    effects,
    tmp_path,
    monkeypatch,
    failure_stage,
):
    secrets, metadata, executor = effects
    args = {
        "provider_id": "anthropic-api",
        "display_name": "Unknown save",
        "auth_method": "api_key",
        "api_key": "fixture-key",
        "request_id": "uncertain-create-request",
    }
    original_commit = service.store.commit
    commits = []

    def fail_final_commit(document):
        commits.append(1)
        if len(commits) == 2:
            raise NarumiError("fixture final commit failed")
        original_commit(document)

    if failure_stage == "keychain":
        secrets.fail_set = True
    else:
        monkeypatch.setattr(service.store, "commit", fail_final_commit)
    with pytest.raises(NarumiError):
        service.set_connection(args)
    secrets.fail_set = False
    monkeypatch.setattr(service.store, "commit", original_commit)
    restarted = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_TWO,
    )
    secret_writes = [call for call in secrets.calls if call[0] in ("set", "delete")]
    with pytest.raises(NarumiError):
        restarted.set_connection(args)
    assert [call for call in secrets.calls if call[0] in ("set", "delete")] == secret_writes
    assert len(restarted.list_connections()["connections"]) == 1
    adopted = restarted.list_connections()["connections"][0]
    assert adopted["credential_present"] is False
    recovered = restarted.set_connection(
        {
            "connection_id": adopted["connection_id"],
            "expected_revision": adopted["revision"],
            "api_key": "fixture-key",
            "request_id": "explicit-repair-request",
        }
    )
    assert recovered["connection"]["connection_id"] == adopted["connection_id"]
    assert recovered["connection"]["credential_present"] is True
    assert len(restarted.list_connections()["connections"]) == 1
    restarted.close()
