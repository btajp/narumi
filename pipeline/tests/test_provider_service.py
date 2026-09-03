"""Provider settings retain secrets outside files and keep observations separate from config."""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from narumi.contracts.loader import load_contracts
from narumi.errors import BusyError, ConfigurationConflictError, InvalidArgumentError, NarumiError
from narumi.providers.codex import _session
from narumi.providers.service import ProviderService

from .provider_fakes import (
    INSTANCE_ONE,
    INSTANCE_TWO,
    FakeCodexBackend,
    FakeMetadata,
    FakeRuntimeInspector,
    ManualExecutor,
    MemorySecretStore,
    create_connection,
    prepared_codex_connection,
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
        codex_backend=FakeCodexBackend(),
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
        "codex-app-server",
        "ollama",
        "openai-api",
        "openai-compatible-api",
    }
    assert service.list_connections() == {"connections": []}
    assert secrets.calls == metadata.calls == []
    assert service.codex_backend.calls == []


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
    generation = service.store.read()["request_hmac_generation"]
    assert generation["scheme"] == "sha256"
    assert len(generation["digest"]) == 64
    assert "fixture-key" not in saved
    assert '"api_key":' not in saved
    assert "hmac-sha256" in saved
    with pytest.raises(ConfigurationConflictError):
        restarted.set_connection({**args, "api_key": "another-key"})
    assert len(service.list_connections()["connections"]) == 1
    assert "secret_account" not in json.dumps(first)
    restarted.close()


def _crashed_codex_run(root, connection_id, marker="a"):
    run = _session.connection_directory(root, connection_id) / "runs" / (marker * 32)
    state = run / "state"
    state.mkdir(parents=True, mode=0o700)
    credential = state / "auth.json"
    credential.write_bytes(b'{"fixture_token":"crashed private credential"}')
    credential.chmod(0o600)
    return run


def test_resident_startup_recovers_only_registered_codex_crash_runs(service, effects, tmp_path):
    secrets, metadata, executor = effects
    selected = create_connection(service, provider_id="codex-app-server", request_id="codex")
    other_provider = create_connection(service, request_id="api")
    selected_run = _crashed_codex_run(tmp_path, selected["connection_id"])
    other_run = _crashed_codex_run(tmp_path, other_provider["connection_id"], marker="b")
    unregistered = _crashed_codex_run(tmp_path, "conn-aaaaaaaaaaaa", marker="c")

    restarted = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_TWO,
        codex_backend=FakeCodexBackend(),
    )

    assert not selected_run.exists()
    assert other_run.exists() and unregistered.exists()
    records = {
        record["connection_id"]: record for record in restarted.list_connections()["connections"]
    }
    assert records[selected["connection_id"]]["auth_state"] == "unconfigured"
    assert records[selected["connection_id"]]["credential_present"] is False
    restarted.close()


def test_nonowner_startup_never_removes_an_active_owner_codex_run(service, effects, tmp_path):
    secrets, metadata, executor = effects
    record = create_connection(service, provider_id="codex-app-server")
    active_run = _crashed_codex_run(tmp_path, record["connection_id"])
    before = service.store.path.read_bytes()

    observer = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_TWO,
        codex_backend=FakeCodexBackend(),
        recover=False,
    )

    observer.close()
    assert active_run.exists()
    assert service.store.path.read_bytes() == before


def test_resident_codex_recovery_rejects_run_symlink_and_isolates_failure(
    service, effects, tmp_path
):
    secrets, metadata, executor = effects
    unsafe = create_connection(service, provider_id="codex-app-server", request_id="unsafe")
    safe = create_connection(service, provider_id="codex-app-server", request_id="safe")
    safe_run = _crashed_codex_run(tmp_path, safe["connection_id"])
    outside = tmp_path / "unrelated-active-process"
    outside.mkdir(mode=0o700)
    outside_credential = outside / "auth.json"
    outside_credential.write_bytes(b"unrelated active process credential")
    connection = _session.connection_directory(tmp_path, unsafe["connection_id"])
    connection.mkdir(parents=True, mode=0o700)
    runs = connection / "runs"
    runs.symlink_to(outside, target_is_directory=True)

    restarted = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_TWO,
        codex_backend=FakeCodexBackend(),
    )

    assert runs.is_symlink()
    assert outside_credential.read_bytes() == b"unrelated active process credential"
    assert not safe_run.exists()
    records = {
        record["connection_id"]: record for record in restarted.list_connections()["connections"]
    }
    assert records[unsafe["connection_id"]]["auth_state"] == "unknown"
    assert records[unsafe["connection_id"]]["credential_present"] is True
    assert records[safe["connection_id"]]["auth_state"] == "unconfigured"
    assert records[safe["connection_id"]]["credential_present"] is False
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
    assert renamed["catalog_state"] == "stale"
    stale_models = service.list_models({"connection_id": record["connection_id"]})
    assert stale_models["catalog_state"] == "stale"
    assert stale_models["models"]
    assert {model["availability"] for model in stale_models["models"]} == {"unverified"}
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
    with pytest.raises(BusyError, match="credential recovery"):
        service.test_connection({"connection_id": record["connection_id"], "expected_revision": 2})
    assert metadata.calls == []


def test_resident_startup_removes_unreachable_keychain_accounts(service, effects, tmp_path):
    secrets, metadata, executor = effects
    record = create_connection(service)
    with service.store.transaction() as document:
        private = document["connections"][record["connection_id"]]
        old_account = private["secret_account"]
        replacement_account = old_account.rsplit(":", 1)[0] + ":" + "b" * 32
        secrets.values[replacement_account] = "replacement-key"
        private.update(
            secret_account=None,
            credential_present=False,
            auth_state="unconfigured",
            pending_secret_accounts=[old_account, replacement_account],
        )
    service.close()

    restarted = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_TWO,
    )
    private = restarted.store.read()["connections"][record["connection_id"]]
    assert private["pending_secret_accounts"] == []
    assert private["credential_present"] is False
    assert old_account not in secrets.values
    assert replacement_account not in secrets.values
    assert "fixture-key" not in restarted.store.path.read_text()
    assert "replacement-key" not in restarted.store.path.read_text()
    restarted.close()


def _inject_legacy_provider_secret_reflections(service, connection_id: str, secret: str) -> None:
    reflected_request = f"legacy-{secret}-request"
    with service.store.transaction() as document:
        record = document["connections"][connection_id]
        record["display_name"] = f"legacy-{secret}-name"
        record["active_auth"] = {
            "operation_id": "auth-legacy-reflection",
            "start_request_id": reflected_request,
            "server_instance_id": INSTANCE_ONE,
            "state": "unknown",
        }
        record["auth_state"] = "unknown"
        document["requests"][reflected_request] = {
            "fingerprint": {"scheme": "sha256", "digest": "a" * 64},
            "state": "unknown",
            "response": None,
            "server_instance_id": INSTANCE_ONE,
            "created_at": "2026-09-02T00:00:00Z",
        }
        document["auth_operations"]["auth-legacy-reflection"] = {
            "operation_id": "auth-legacy-reflection",
            "connection_id": connection_id,
            "start_request_id": reflected_request,
            "state": "unknown",
        }
        document["checks"][record["provider_id"]] = {
            "token": reflected_request,
            "server_instance_id": INSTANCE_ONE,
            "connection_id": connection_id,
            "kind": "authentication",
        }
        document["catalogs"][connection_id] = {"legacy": reflected_request}
        document["runtimes"][record["provider_id"]] = {"legacy": reflected_request}


def test_resident_startup_scrubs_legacy_provider_secret_reflections(service, effects, tmp_path):
    secrets, metadata, executor = effects
    secret = "legacy-provider-secret-7219"
    record = create_connection(service, key=secret)
    service.close()
    _inject_legacy_provider_secret_reflections(service, record["connection_id"], secret)
    assert secret in service.store.path.read_text()

    restarted = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_TWO,
        runtime_inspector=FakeRuntimeInspector(),
        codex_backend=FakeCodexBackend(),
    )
    persisted = restarted.store.read()
    saved = persisted["connections"][record["connection_id"]]
    assert saved["display_name"].startswith("Recovered connection ")
    assert saved["active_auth"] is None
    assert saved["auth_state"] == "unverified"
    assert "auth-legacy-reflection" not in persisted["auth_operations"]
    assert all(secret not in request_id for request_id in persisted["requests"])
    assert record["connection_id"] not in persisted["catalogs"]
    assert record["provider_id"] not in persisted["checks"]
    assert record["provider_id"] not in persisted["runtimes"]
    assert secret not in restarted.store.path.read_text()
    assert secret not in json.dumps(restarted.list_connections())
    restarted.close()


def test_unowned_context_rejects_unmigrated_provider_secret_reflections(service, effects, tmp_path):
    secrets, metadata, executor = effects
    secret = "legacy-provider-secret-8821"
    record = create_connection(service, key=secret)
    service.close()
    _inject_legacy_provider_secret_reflections(service, record["connection_id"], secret)

    with pytest.raises(BusyError) as failure:
        ProviderService(
            tmp_path,
            secret_store=secrets,
            metadata_client=metadata,
            auth_executor=executor,
            server_instance_id=INSTANCE_TWO,
            recover=False,
        )
    assert failure.value.details == {"reason": "credential_unavailable"}
    assert secret not in str(failure.value)
    assert secret in service.store.path.read_text()


def test_legacy_secret_migration_keychain_failure_is_busy_without_partial_cleanup(
    service, effects, tmp_path
):
    secrets, metadata, executor = effects
    secret = "legacy-provider-secret-9931"
    record = create_connection(service, key=secret)
    service.close()
    _inject_legacy_provider_secret_reflections(service, record["connection_id"], secret)
    before = service.store.path.read_text()
    secrets.fail_get = True

    with pytest.raises(BusyError) as failure:
        ProviderService(
            tmp_path,
            secret_store=secrets,
            metadata_client=metadata,
            auth_executor=executor,
            server_instance_id=INSTANCE_TWO,
        )
    assert failure.value.details == {"reason": "credential_unavailable"}
    assert secret not in str(failure.value)
    assert service.store.path.read_text() == before


def test_legacy_registry_version_drops_receipts_after_their_connection_key_was_deleted(
    service, effects, tmp_path
):
    secrets, metadata, executor = effects
    deleted_key = "deleted-legacy-provider-secret-4412"
    record = create_connection(service, key=deleted_key)
    private = service.store.read()["connections"][record["connection_id"]]
    service.close()
    secrets.delete(private["secret_account"])
    reflected_request = f"legacy-{deleted_key}-request"
    with service.store.transaction() as document:
        document["version"] = 1
        document["connections"].pop(record["connection_id"])
        document["requests"][reflected_request] = {
            "fingerprint": {"scheme": "sha256", "digest": "b" * 64},
            "state": "unknown",
            "response": None,
            "server_instance_id": INSTANCE_ONE,
            "created_at": "2026-09-02T00:00:00Z",
        }
    assert deleted_key in service.store.path.read_text()
    assert deleted_key not in secrets.values.values()

    restarted = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_TWO,
        runtime_inspector=FakeRuntimeInspector(),
    )
    persisted = restarted.store.read()
    assert persisted["version"] == 2
    assert persisted["requests"] == {}
    assert deleted_key not in restarted.store.path.read_text()
    restarted.close()


def test_failed_startup_credential_cleanup_stays_busy_until_explicit_repair(
    service, effects, tmp_path
):
    secrets, metadata, executor = effects
    record = create_connection(service)
    with service.store.transaction() as document:
        private = document["connections"][record["connection_id"]]
        old_account = private["secret_account"]
        private.update(
            secret_account=None,
            credential_present=False,
            auth_state="unconfigured",
            pending_secret_accounts=[old_account],
        )
    service.close()
    secrets.fail_delete = True
    restarted = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_TWO,
    )
    with pytest.raises(BusyError, match="credential recovery"):
        restarted.test_connection(
            {"connection_id": record["connection_id"], "expected_revision": 1}
        )
    with pytest.raises(BusyError, match="credential recovery"):
        restarted.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "display_name": "Blocked rename",
                "request_id": "blocked-pending-credential-recovery",
            }
        )
    with pytest.raises(BusyError, match="credential recovery"):
        restarted.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "enabled": False,
                "request_id": "blocked-disable-during-credential-recovery",
            }
        )
    secrets.fail_delete = False
    repaired = restarted.set_connection(
        {
            "connection_id": record["connection_id"],
            "expected_revision": 1,
            "api_key": "explicit-repair-key",
            "request_id": "explicit-pending-credential-repair",
        }
    )["connection"]
    assert repaired["credential_present"] is True
    private = restarted.store.read()["connections"][record["connection_id"]]
    assert private["pending_secret_accounts"] == []
    assert old_account not in secrets.values
    assert "explicit-repair-key" not in restarted.store.path.read_text()
    restarted.close()


def test_credential_recovery_never_deletes_an_unrelated_keychain_account(
    service, effects, tmp_path
):
    secrets, metadata, executor = effects
    record = create_connection(service)
    unrelated = f"providers:{service.namespace}:request-hmac"
    assert unrelated in secrets.values
    with service.store.transaction() as document:
        private = document["connections"][record["connection_id"]]
        private.update(
            secret_account=None,
            credential_present=False,
            auth_state="unconfigured",
            pending_secret_accounts=[unrelated],
        )
    service.close()
    restarted = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_TWO,
    )
    assert unrelated in secrets.values
    assert restarted.store.read()["connections"][record["connection_id"]][
        "pending_secret_accounts"
    ] == [unrelated]
    repaired = restarted.set_connection(
        {
            "connection_id": record["connection_id"],
            "expected_revision": 1,
            "api_key": "explicit-safe-repair-key",
            "request_id": "explicit-safe-credential-repair",
        }
    )["connection"]
    assert repaired["credential_present"] is True
    assert unrelated in secrets.values
    restarted.close()


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


@pytest.mark.parametrize("provider_id", ["anthropic-api", "openai-api"])
def test_untrusted_metadata_cannot_echo_credential(service, effects, provider_id):
    record = create_connection(service, provider_id=provider_id)
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


def test_new_credential_cannot_be_used_as_its_request_identifier(service, effects):
    credential = "11111111-2222-4333-8444-555555555555"
    request_id = f"prefix-{credential}-suffix"
    with pytest.raises(InvalidArgumentError) as failure:
        service.set_connection(
            {
                "provider_id": "anthropic-api",
                "display_name": "Safe name",
                "auth_method": "api_key",
                "api_key": credential,
                "request_id": request_id,
            }
        )
    assert credential not in str(failure.value)
    assert service.list_connections() == {"connections": []}
    assert effects[0].calls == []
    assert not service.store.path.exists() or request_id not in service.store.path.read_text()


@pytest.mark.parametrize("operation", ["set", "delete", "authenticate", "verify", "prepare"])
def test_saved_credential_cannot_become_any_mutating_provider_request_id(
    service, effects, operation
):
    credential = "22222222-3333-4444-8555-666666666666"
    record = create_connection(service, key=credential)
    private = service.store.read()["connections"][record["connection_id"]]
    before = service.store.path.read_text()
    effects[0].calls.clear()
    effects[1].calls.clear()
    effects[2].pending.clear()
    runtime = next(
        provider
        for provider in service.list_providers()["providers"]
        if provider["provider_id"] == record["provider_id"]
    )["runtime"]
    calls = {
        "set": lambda: service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "display_name": "Still safe",
                "request_id": credential,
            }
        ),
        "delete": lambda: service.delete_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "confirm": True,
                "request_id": credential,
            }
        ),
        "authenticate": lambda: service.authenticate(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "action": "start",
                "request_id": credential,
            }
        ),
        "verify": lambda: service.verify_model(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "model_id": "fixture-model",
                "confirmation": "send_test_prompt_and_may_charge",
                "request_id": credential,
            }
        ),
        "prepare": lambda: service.prepare_runtime(
            {
                "provider_id": record["provider_id"],
                "resource_id": runtime["resources"][0]["resource_id"],
                "expected_catalog_revision": runtime["catalog_revision"],
                "action": "prepare",
                "request_id": credential,
            }
        ),
    }
    with pytest.raises(InvalidArgumentError) as failure:
        calls[operation]()
    assert credential not in str(failure.value)
    assert service.store.path.read_text() == before
    assert effects[0].calls == [("get", private["secret_account"])]
    assert effects[1].calls == []
    assert effects[2].pending == []


@pytest.mark.parametrize("field", ["display_name", "request_id"])
def test_saved_uuid_credential_cannot_be_reflected_into_public_connection_metadata(
    service, effects, field
):
    credential = "123e4567-e89b-12d3-a456-426614174000"
    record = create_connection(service, key=credential)
    private = service.store.read()["connections"][record["connection_id"]]
    effects[0].calls.clear()
    args = {
        "connection_id": record["connection_id"],
        "expected_revision": 1,
        "display_name": "Safe rename",
        "request_id": "reject-saved-credential-reflection",
    }
    args[field] = credential
    with pytest.raises(InvalidArgumentError) as failure:
        service.set_connection(args)
    assert credential not in str(failure.value)
    assert credential not in service.store.path.read_text()
    assert service.list_connections()["connections"][0]["revision"] == 1
    assert effects[0].calls == [("get", private["secret_account"])]


def test_pending_credential_cannot_be_reflected_during_explicit_repair(service, effects):
    pending_credential = "87654321-4321-4321-8321-210987654321"
    record = create_connection(service)
    with service.store.transaction() as document:
        private = document["connections"][record["connection_id"]]
        old_account = private["secret_account"]
        pending_account = old_account.rsplit(":", 1)[0] + ":" + "c" * 32
        effects[0].values[pending_account] = pending_credential
        private.update(
            secret_account=None,
            credential_present=False,
            auth_state="unconfigured",
            pending_secret_accounts=[old_account, pending_account],
        )
    effects[0].calls.clear()
    with pytest.raises(InvalidArgumentError):
        service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "display_name": pending_credential,
                "api_key": "explicit-repair-key",
                "request_id": "reject-pending-credential-reflection",
            }
        )
    assert pending_credential not in service.store.path.read_text()
    assert all(call[0] == "get" for call in effects[0].calls)
    assert effects[0].values[pending_account] == pending_credential


def test_old_and_pending_credentials_cannot_become_authentication_request_ids(service, effects):
    old_credential = "33333333-4444-4555-8666-777777777777"
    pending_credential = "44444444-5555-4666-8777-888888888888"
    record = create_connection(service, key=old_credential)
    with service.store.transaction() as document:
        private = document["connections"][record["connection_id"]]
        old_account = private["secret_account"]
        pending_account = old_account.rsplit(":", 1)[0] + ":" + "d" * 32
        effects[0].values[pending_account] = pending_credential
        private.update(
            secret_account=None,
            credential_present=False,
            auth_state="unconfigured",
            pending_secret_accounts=[old_account, pending_account],
        )
    before = service.store.path.read_text()
    for credential in (old_credential, pending_credential):
        request_id = f"prefix-{credential}-suffix"
        effects[0].calls.clear()
        with pytest.raises(InvalidArgumentError) as failure:
            service.authenticate(
                {
                    "connection_id": record["connection_id"],
                    "expected_revision": 1,
                    "action": "start",
                    "request_id": request_id,
                }
            )
        assert credential not in str(failure.value)
        assert effects[0].calls == [("get", old_account), ("get", pending_account)]
        assert request_id not in service.store.path.read_text()
        assert service.store.path.read_text() == before


def test_another_connection_credential_cannot_become_an_authentication_request_id(service, effects):
    first = create_connection(service, key="first-connection-key", request_id="create-first-owner")
    second_key = "55555555-6666-4777-8888-999999999999"
    second = create_connection(service, key=second_key, request_id="create-second-owner")
    document = service.store.read()
    accounts = [
        document["connections"][record["connection_id"]]["secret_account"]
        for record in (first, second)
    ]
    request_id = f"prefix-{second_key}-suffix"
    before = service.store.path.read_text()
    effects[0].calls.clear()
    with pytest.raises(InvalidArgumentError) as failure:
        service.authenticate(
            {
                "connection_id": first["connection_id"],
                "expected_revision": 1,
                "action": "start",
                "request_id": request_id,
            }
        )
    assert second_key not in str(failure.value)
    assert service.store.path.read_text() == before
    assert sorted(effects[0].calls) == sorted(("get", account) for account in accounts)
    assert effects[2].pending == []


def test_another_connection_credential_cannot_become_public_connection_metadata(service, effects):
    first = create_connection(service, key="first-public-owner-key", request_id="public-owner-one")
    second_key = "66666666-7777-4888-8999-000000000000"
    second = create_connection(service, key=second_key, request_id="public-owner-two")
    document = service.store.read()
    accounts = [
        document["connections"][record["connection_id"]]["secret_account"]
        for record in (first, second)
    ]
    reflected = f"prefix-{second_key}-suffix"
    before = service.store.path.read_text()
    effects[0].calls.clear()
    with pytest.raises(InvalidArgumentError) as failure:
        service.set_connection(
            {
                "connection_id": first["connection_id"],
                "expected_revision": 1,
                "display_name": reflected,
                "request_id": "safe-cross-connection-metadata-request",
            }
        )
    assert second_key not in str(failure.value)
    assert service.store.path.read_text() == before
    assert reflected not in service.store.path.read_text()
    assert sorted(effects[0].calls) == sorted(("get", account) for account in accounts)


def test_saved_credential_read_failure_is_busy_without_mutation(service, effects):
    record = create_connection(service)
    effects[0].calls.clear()
    effects[0].fail_get = True
    with pytest.raises(BusyError) as failure:
        service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "display_name": "Must not be saved",
                "request_id": "credential-read-unavailable",
            }
        )
    assert failure.value.details == {"reason": "credential_unavailable"}
    assert "fixture-key" not in str(failure.value)
    document = service.store.read()
    assert document["connections"][record["connection_id"]]["revision"] == 1
    assert "credential-read-unavailable" not in document["requests"]


def test_request_fingerprint_keychain_failure_is_retryable_and_does_not_create_connection(
    service, effects
):
    effects[0].fail_hmac = True
    with pytest.raises(BusyError) as failure:
        service.set_connection(
            {
                "provider_id": "anthropic-api",
                "display_name": "Must not be created",
                "auth_method": "api_key",
                "api_key": "uuid-like-123e4567-e89b-12d3-a456-426614174000",
                "request_id": "hmac-keychain-unavailable",
            }
        )
    assert failure.value.details == {"reason": "credential_unavailable"}
    assert service.list_connections() == {"connections": []}
    assert service.store.read()["request_hmac_generation"] is None
    assert "uuid-like" not in (
        service.store.path.read_text() if service.store.path.exists() else ""
    )


@pytest.mark.parametrize("evidence", ["marker", "hmac_receipt", "semantic_receipt", "credential"])
def test_missing_established_request_hmac_key_blocks_startup_without_registry_mutation(
    service, effects, tmp_path, evidence
):
    secrets, metadata, executor = effects
    create_connection(service, key="established-provider-key-8241")
    with service.store.transaction() as document:
        if evidence != "marker":
            document["request_hmac_generation"] = None
        if evidence in {"hmac_receipt", "semantic_receipt"}:
            document["connections"].clear()
            document["requests"] = {
                "historical-request": {
                    "fingerprint": {
                        "scheme": "hmac-sha256" if evidence == "hmac_receipt" else "sha256",
                        "digest": "a" * 64,
                    },
                    **(
                        {"semantic_fingerprint": {"scheme": "sha256", "digest": "b" * 64}}
                        if evidence == "semantic_receipt"
                        else {}
                    ),
                }
            }
        elif evidence == "credential":
            document["requests"].clear()
    service.close()
    secrets.delete(f"providers:{service.namespace}:request-hmac")
    before = service.store.path.read_bytes()

    with pytest.raises(BusyError) as failure:
        ProviderService(
            tmp_path,
            secret_store=secrets,
            metadata_client=metadata,
            auth_executor=executor,
            server_instance_id=INSTANCE_TWO,
        )
    assert failure.value.details == {"reason": "credential_unavailable"}
    assert service.store.path.read_bytes() == before


def test_replaced_request_hmac_key_blocks_startup_without_registry_mutation(
    service, effects, tmp_path
):
    secrets, metadata, executor = effects
    create_connection(service, key="established-provider-key-4182")
    service.close()
    secrets.set(
        f"providers:{service.namespace}:request-hmac",
        "substituted-request-hmac-key-1842",
    )
    before = service.store.path.read_bytes()

    with pytest.raises(BusyError) as failure:
        ProviderService(
            tmp_path,
            secret_store=secrets,
            metadata_client=metadata,
            auth_executor=executor,
            server_instance_id=INSTANCE_TWO,
        )
    assert failure.value.details == {"reason": "credential_unavailable"}
    assert service.store.path.read_bytes() == before


def test_legacy_request_hmac_marker_is_upgraded_only_with_the_existing_key(
    service, effects, tmp_path
):
    secrets, metadata, executor = effects
    create_connection(service, key="established-provider-key-7841")
    account = f"providers:{service.namespace}:request-hmac"
    key = secrets.values[account]
    with service.store.transaction() as document:
        document["request_hmac_generation"] = 1
    service.close()

    restarted = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_TWO,
    )
    assert restarted.store.read()["request_hmac_generation"] == {
        "scheme": "sha256",
        "digest": hashlib.sha256(key.encode()).hexdigest(),
    }
    restarted.close()


def test_connection_fingerprint_is_checked_for_saved_secret_reflection(
    service, effects, monkeypatch
):
    credential = "123e4567-e89b-12d3-a456-426614174000"
    record = create_connection(service, key=credential)
    monkeypatch.setattr(
        service.requests,
        "fingerprint",
        lambda _tool, _args, *, document: {"scheme": "sha256", "digest": credential},
    )
    with pytest.raises(InvalidArgumentError):
        service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "display_name": "Safe rename",
                "request_id": "reject-secret-fingerprint",
            }
        )
    assert "reject-secret-fingerprint" not in service.store.read()["requests"]


def test_secret_identifier_scan_reads_another_connection_only_through_its_own_record(
    service, effects
):
    first = create_connection(service, key="first-key", request_id="first-secret-owner")
    second = create_connection(service, key="second-key", request_id="second-secret-owner")
    document = service.store.read()
    second_account = document["connections"][second["connection_id"]]["secret_account"]
    with service.store.transaction() as document:
        record = document["connections"][first["connection_id"]]
        record.update(
            secret_account=None,
            credential_present=False,
            auth_state="unconfigured",
            pending_secret_accounts=[second_account],
        )
    effects[0].calls.clear()
    repaired = service.set_connection(
        {
            "connection_id": first["connection_id"],
            "expected_revision": 1,
            "api_key": "repaired-first-key",
            "request_id": "repair-with-unrelated-pending-account",
        }
    )["connection"]
    assert repaired["credential_present"] is True
    assert effects[0].calls.count(("get", second_account)) == 2
    assert ("delete", second_account) not in effects[0].calls
    assert effects[0].values[second_account] == "second-key"


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


def test_codex_connection_saves_without_credentials_or_external_effects(service, effects):
    record = create_connection(service, provider_id="codex-app-server")
    assert record["endpoint"] == "https://chatgpt.com"
    assert record["auth_method"] == "chatgpt"
    assert record["credential_present"] is False
    assert record["auth_state"] == "unconfigured"
    assert service.codex_backend.calls == effects[0].calls == effects[1].calls == []
    tested = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 1}
    )
    assert tested["connected"] is False
    assert tested["reason"] == "credential_required"
    assert service.codex_backend.calls == []
    load_contracts().validate_output("set_provider_connection", {"connection": record})


@pytest.mark.parametrize("key", [None, "fixture-secret"])
def test_codex_update_rejects_even_null_api_key_before_keychain_access(service, effects, key):
    record = create_connection(service, provider_id="codex-app-server")
    with pytest.raises(InvalidArgumentError):
        service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "api_key": key,
                "request_id": "reject-codex-api-key",
            }
        )
    assert effects[0].calls == service.codex_backend.calls == []
    assert service.list_connections()["connections"][0]["revision"] == 1
    assert "fixture-secret" not in service.store.path.read_text()


@pytest.mark.parametrize(
    "update",
    [
        {"endpoint": "https://example.com"},
        {"endpoint": "https://api.openai.com"},
        {"auth_method": "api_key"},
    ],
)
def test_codex_connection_cannot_change_to_custom_endpoint_or_api_auth(service, update):
    record = create_connection(service, provider_id="codex-app-server")
    with pytest.raises(InvalidArgumentError):
        service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "request_id": "reject-codex-endpoint",
                **update,
            }
        )
    assert service.codex_backend.calls == []


def test_codex_metadata_uses_only_authenticated_backend_and_never_marks_generation(
    service, effects
):
    record = prepared_codex_connection(service)
    result = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 1}
    )
    assert result["connected"] is True
    assert result["connection"]["last_generation_state"] == "never"
    models = service.list_models({"connection_id": record["connection_id"]})
    assert models["models"] == service.codex_backend.models
    assert effects[0].calls == effects[1].calls == []
    assert service.codex_backend.calls == [("list_models", record["connection_id"])]
    document = service.store.read()
    assert (
        document["catalogs"][record["connection_id"]]["runtime_catalog_revision"]
        == (document["runtimes"][record["provider_id"]]["catalog_revision"])
    )
    load_contracts().validate_output("list_provider_models", models)


def test_codex_missing_session_invalidates_presence_and_requires_explicit_login(service):
    record = prepared_codex_connection(service)
    service.codex_backend.authenticated.clear()
    args = {"connection_id": record["connection_id"], "expected_revision": 1}
    result = service.test_connection(args)
    assert result["reason"] == "credential_rejected"
    assert result["connection"]["credential_present"] is False
    assert result["connection"]["catalog_state"] == "authentication_required"
    service.test_connection(args)
    assert service.codex_backend.calls == [("list_models", record["connection_id"])]


def test_codex_stale_runtime_prevents_metadata_request(service):
    record = prepared_codex_connection(service)
    service.codex_backend.version = "2.0.0"
    result = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 1}
    )
    assert result["reason"] == "runtime_preparation_required"
    assert result["connection"]["catalog_state"] == "stale"
    assert service.codex_backend.calls == []


def test_codex_metadata_rejects_credential_like_payload(service):
    record = prepared_codex_connection(service)
    service.codex_backend.models[0]["display_name"] = "Bearer fixture-secret"
    result = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": 1}
    )
    assert result["connected"] is False
    assert "fixture-secret" not in service.store.path.read_text()


def test_codex_delete_cleans_only_selected_session_and_replays(service):
    first = prepared_codex_connection(service, request_id="codex-one")
    second = prepared_codex_connection(service, request_id="codex-two")
    args = {
        "connection_id": first["connection_id"],
        "expected_revision": 1,
        "confirm": True,
        "request_id": "delete-codex-one",
    }
    assert service.delete_connection(args) == service.delete_connection(args)
    assert service.codex_backend.authenticated == {second["connection_id"]}
    assert service.codex_backend.calls == [("logout", first["connection_id"])]
    assert service.store.read()["checks"] == {}


def test_codex_failed_cleanup_leaves_session_unreachable_and_does_not_retry(service):
    record = prepared_codex_connection(service)
    backend = service.codex_backend
    backend.error = RuntimeError("fixture-secret /private/fixture-session")
    args = {
        "connection_id": record["connection_id"],
        "expected_revision": 1,
        "confirm": True,
        "request_id": "delete-codex-failure",
    }
    with pytest.raises(NarumiError, match="removed securely"):
        service.delete_connection(args)
    saved = service.list_connections()["connections"][0]
    assert saved["credential_present"] is False
    assert saved["enabled"] is False
    assert saved["revision"] == 2
    with pytest.raises(NarumiError):
        service.delete_connection(args)
    assert backend.calls == [("logout", record["connection_id"])]
    assert "fixture-secret" not in service.store.path.read_text()
    backend.error = None
    service.delete_connection(
        {**args, "expected_revision": 2, "request_id": "explicit-delete-repair"}
    )
    assert service.list_connections()["connections"] == []


def test_codex_generation_lease_allows_disable_but_blocks_logout_and_mutation(service):
    record = prepared_codex_connection(service)
    with service.store.transaction() as document:
        document["checks"][record["provider_id"]] = {
            "token": "generation-fixture",
            "connection_id": record["connection_id"],
            "server_instance_id": service.server_instance_id,
            "kind": "generation",
        }
    for action in ("start", "logout"):
        with pytest.raises(BusyError):
            service.authenticate(
                {
                    "connection_id": record["connection_id"],
                    "expected_revision": 1,
                    "action": action,
                    "request_id": "auth-during-generation-" + action,
                }
            )
    with pytest.raises(BusyError):
        service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "display_name": "Not allowed",
                "request_id": "rename-during-generation",
            }
        )
    disabled = service.set_connection(
        {
            "connection_id": record["connection_id"],
            "expected_revision": 1,
            "enabled": False,
            "request_id": "disable-during-generation",
        }
    )["connection"]
    assert disabled["enabled"] is False
    assert service.store.read()["checks"][record["provider_id"]]["token"] == "generation-fixture"
    assert service.codex_backend.calls == []


@pytest.mark.parametrize("interruption", ["close", "restart"])
def test_codex_metadata_result_after_shutdown_or_restart_is_discarded(
    service, effects, monkeypatch, interruption
):
    record = prepared_codex_connection(service)
    backend = service.codex_backend
    initial_catalog = service.store.read()["catalogs"][record["connection_id"]]
    entered, release = threading.Event(), threading.Event()
    fetch = backend.list_models

    def blocked(connection_id):
        entered.set()
        assert release.wait(5)
        return fetch(connection_id)

    monkeypatch.setattr(backend, "list_models", blocked)
    reopened = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            service.test_connection,
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
            },
        )
        try:
            assert entered.wait(3)
            if interruption == "close":
                service.close()
            else:
                reopened = ProviderService(
                    service.root,
                    secret_store=effects[0],
                    codex_backend=FakeCodexBackend(),
                    auth_executor=ManualExecutor(),
                    server_instance_id=INSTANCE_TWO,
                )
        finally:
            release.set()
        with pytest.raises(ConfigurationConflictError):
            result.result(timeout=3)
    assert service.store.read()["catalogs"][record["connection_id"]] == initial_catalog
    if reopened is not None:
        reopened.close()


def test_lazy_codex_backend_is_closed_when_shutdown_overlaps_construction(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    entered, release = threading.Event(), threading.Event()
    backend = FakeCodexBackend()

    def construct(_root):
        entered.set()
        assert release.wait(5)
        return backend

    monkeypatch.setitem(
        sys.modules, "narumi.providers.codex", SimpleNamespace(CodexBackend=construct)
    )
    service = ProviderService(
        tmp_path, secret_store=MemorySecretStore(), auth_executor=ManualExecutor()
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        construction = pool.submit(lambda: service.codex_backend)
        assert entered.wait(3)
        closure = pool.submit(service.close)
        try:
            assert service.closed.wait(3)
            assert not closure.done()
        finally:
            release.set()
        construction.result(timeout=3)
        closure.result(timeout=3)
    assert backend.calls == [("close",)]


def test_failed_codex_delete_commit_releases_cleanup_lease_for_explicit_repair(
    service, monkeypatch
):
    record = prepared_codex_connection(service)
    original = service.store.commit
    writes = []

    def fail_final_once(document):
        writes.append(1)
        if len(writes) == 3:
            raise NarumiError("fixture-secret")
        original(document)

    monkeypatch.setattr(service.store, "commit", fail_final_once)
    args = {
        "connection_id": record["connection_id"],
        "expected_revision": 1,
        "confirm": True,
        "request_id": "delete-codex-final-write-failure",
    }
    with pytest.raises(NarumiError, match="could not be confirmed"):
        service.delete_connection(args)
    assert service.store.read()["checks"] == {}
    saved = service.list_connections()["connections"][0]
    assert saved["credential_present"] is False
    assert saved["enabled"] is False
    assert "fixture-secret" not in service.store.path.read_text()
    service.delete_connection(
        {**args, "expected_revision": 2, "request_id": "explicit-delete-after-write-failure"}
    )
    assert service.list_connections()["connections"] == []


@pytest.mark.parametrize("instance_id", [INSTANCE_ONE, INSTANCE_TWO])
def test_nonowner_context_preserves_resident_generation_lease(service, instance_id):
    record = prepared_codex_connection(service)
    with service.store.transaction() as document:
        document["checks"][record["provider_id"]] = {
            "token": "active-generation",
            "kind": "generation",
            "connection_id": record["connection_id"],
            "server_instance_id": INSTANCE_ONE,
        }
    before = service.store.path.read_bytes()
    observer = ProviderService(
        service.root,
        recover=False,
        server_instance_id=instance_id,
        secret_store=MemorySecretStore(),
        auth_executor=ManualExecutor(),
    )
    observer.close()
    assert service.store.path.read_bytes() == before
    with pytest.raises(BusyError):
        service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "display_name": "Blocked during generation",
                "request_id": "blocked-after-observer",
            }
        )
