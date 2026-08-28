"""API verification recovery never starts new authentication or revives cancelled work."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from narumi.contracts.loader import load_contracts
from narumi.errors import AuthenticationRequiredError, BusyError, NotFoundError
from narumi.providers.service import ProviderService

from .provider_fakes import (
    INSTANCE_ONE,
    INSTANCE_TWO,
    FakeMetadata,
    ManualExecutor,
    MemorySecretStore,
    create_connection,
)


@pytest.fixture
def auth_setup(tmp_path):
    secrets, metadata, executor = MemorySecretStore(), FakeMetadata(), ManualExecutor()
    service = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=executor,
        server_instance_id=INSTANCE_ONE,
    )
    yield service, secrets, metadata, executor
    service.close()


def start_args(record, *, request_id="authentication-start"):
    return {
        "connection_id": record["connection_id"],
        "expected_revision": record["revision"],
        "action": "start",
        "request_id": request_id,
    }


def test_auth_start_is_durable_before_work_and_replays_one_acceptance(auth_setup):
    service, _, metadata, executor = auth_setup
    record = create_connection(service)
    args = start_args(record)
    accepted = service.authenticate(args)
    assert accepted["operation"]["state"] == "pending"
    assert metadata.calls == []
    assert service.authenticate(args) == accepted
    assert len(executor.pending) == 1
    recovered = service.auth_status(
        {"connection_id": record["connection_id"], "start_request_id": args["request_id"]}
    )
    assert recovered == accepted
    executor.run_next()
    completed = service.auth_status(
        {
            "connection_id": record["connection_id"],
            "operation_id": accepted["operation"]["operation_id"],
        }
    )
    assert completed["operation"]["state"] == "succeeded"
    assert len(metadata.calls) == 1
    assert service.authenticate(args) == completed
    assert len(executor.pending) == 0
    saved = service.list_connections()["connections"][0]
    assert saved["revision"] == 1
    assert saved["auth_state"] == "authenticated"
    assert saved["last_generation_state"] == "never"
    load_contracts().validate_output("authenticate_provider_connection", completed)


def test_restart_pending_auth_is_unknown_and_never_automatically_reissued(auth_setup, tmp_path):
    service, secrets, metadata, executor = auth_setup
    record = create_connection(service)
    args = start_args(record)
    original = service.authenticate(args)["operation"]
    restarted_executor = ManualExecutor()
    restarted = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        auth_executor=restarted_executor,
        server_instance_id=INSTANCE_TWO,
    )
    recovered = restarted.auth_status(
        {"connection_id": record["connection_id"], "start_request_id": args["request_id"]}
    )["operation"]
    assert recovered["state"] == "unknown"
    assert recovered["server_instance_id"] == INSTANCE_ONE
    assert restarted.authenticate(args)["operation"]["operation_id"] == original["operation_id"]
    assert restarted_executor.pending == metadata.calls == []
    with pytest.raises(BusyError):
        restarted.authenticate(start_args(record, request_id="new-start-without-cancel"))
    restarted.authenticate(
        {
            "connection_id": record["connection_id"],
            "expected_revision": 1,
            "action": "cancel",
            "operation_id": original["operation_id"],
            "request_id": "explicit-cancel-unknown",
        }
    )
    assert (
        restarted.authenticate(start_args(record, request_id="explicit-new-start"))["operation"][
            "state"
        ]
        == "pending"
    )
    executor.run_next()  # The original server cannot overwrite the replacement operation.
    assert metadata.calls == []
    restarted.close()


def test_auth_cancel_is_idempotent_and_cannot_be_overwritten(auth_setup):
    service, _, metadata, executor = auth_setup
    record = create_connection(service)
    operation = service.authenticate(start_args(record))["operation"]
    args = {
        "connection_id": record["connection_id"],
        "expected_revision": 1,
        "action": "cancel",
        "operation_id": operation["operation_id"],
        "request_id": "cancel-authentication",
    }
    cancelled = service.authenticate(args)
    assert cancelled["operation"]["state"] == "cancelled"
    assert cancelled["operation"]["start_request_id"] == "authentication-start"
    assert service.authenticate(args) == cancelled
    executor.run_next()
    assert metadata.calls == []
    assert (
        service.auth_status(
            {"connection_id": record["connection_id"], "operation_id": operation["operation_id"]}
        )
        == cancelled
    )


def test_provider_wide_exclusion_preserves_disable_and_cancellation(auth_setup):
    service, _, _, executor = auth_setup
    first = create_connection(service, request_id="first")
    second = create_connection(service, request_id="second")
    operation = service.authenticate(start_args(first))["operation"]
    with pytest.raises(BusyError):
        service.authenticate(start_args(second, request_id="authenticate-second"))
    with pytest.raises(BusyError):
        service.set_connection(
            {
                "connection_id": first["connection_id"],
                "expected_revision": 1,
                "display_name": "Change while running",
                "request_id": "rename-during-auth",
            }
        )
    service.set_connection(
        {
            "connection_id": first["connection_id"],
            "expected_revision": 1,
            "enabled": False,
            "request_id": "disable-during-auth",
        }
    )
    executor.run_next()
    assert (
        service.auth_status(
            {"connection_id": first["connection_id"], "operation_id": operation["operation_id"]}
        )["operation"]["state"]
        == "cancelled"
    )


def test_inflight_auth_result_is_discarded_after_disable(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class BlockingMetadata(FakeMetadata):
        def fetch(self, *args):
            entered.set()
            assert release.wait(5)
            return super().fetch(*args)

    with ThreadPoolExecutor(max_workers=1) as executor:
        service = ProviderService(
            tmp_path,
            secret_store=MemorySecretStore(),
            metadata_client=BlockingMetadata(),
            auth_executor=executor,
        )
        record = create_connection(service)
        operation = service.authenticate(start_args(record))["operation"]
        assert entered.wait(3)
        service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "enabled": False,
                "request_id": "disable-inflight-auth",
            }
        )
        release.set()
    saved = service.list_connections()["connections"][0]
    assert saved["enabled"] is False
    assert saved["auth_state"] != "authenticated"
    assert saved["catalog_state"] != "ready"
    assert (
        service.auth_status(
            {"connection_id": record["connection_id"], "operation_id": operation["operation_id"]}
        )["operation"]["state"]
        == "cancelled"
    )
    service.close()


def test_rejected_credentials_return_static_failure_and_no_secret(auth_setup):
    service, _, metadata, executor = auth_setup
    record = create_connection(service)
    metadata.error = AuthenticationRequiredError("fixture-key /private/sdk-state")
    operation = service.authenticate(start_args(record))["operation"]
    executor.run_next()
    result = service.auth_status(
        {"connection_id": record["connection_id"], "operation_id": operation["operation_id"]}
    )
    assert result["operation"]["state"] == "failed"
    assert result["operation"]["reason"] == "credential_rejected"
    assert "fixture-key" not in json.dumps(result)
    assert "fixture-key" not in service.store.path.read_text()


def test_operation_lookup_is_connection_scoped_and_logout_is_recoverable(auth_setup):
    service, _, _, _ = auth_setup
    first = create_connection(service, request_id="first")
    second = create_connection(service, request_id="second")
    result = service.authenticate(
        {
            "connection_id": first["connection_id"],
            "expected_revision": 1,
            "action": "logout",
            "request_id": "logout-response-lost",
        }
    )
    assert result["operation"]["state"] == "succeeded"
    assert (
        service.auth_status(
            {"connection_id": first["connection_id"], "start_request_id": "logout-response-lost"}
        )
        == result
    )
    with pytest.raises(NotFoundError):
        service.auth_status(
            {
                "connection_id": second["connection_id"],
                "operation_id": result["operation"]["operation_id"],
            }
        )


def test_missing_credential_never_uses_ambient_key(auth_setup, monkeypatch):
    service, _, metadata, executor = auth_setup
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-fixture-key")
    record = create_connection(service, key=None)
    operation = service.authenticate(start_args(record))["operation"]
    executor.run_next()
    result = service.auth_status(
        {"connection_id": record["connection_id"], "operation_id": operation["operation_id"]}
    )
    assert result["operation"]["reason"] == "credential_required"
    assert metadata.calls == []
