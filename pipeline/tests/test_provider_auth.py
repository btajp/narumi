"""API verification recovery never starts new authentication or revives cancelled work."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from narumi.contracts.loader import load_contracts
from narumi.errors import (
    AuthenticationRequiredError,
    BusyError,
    CancelledError,
    EngineUnavailableError,
    InvalidArgumentError,
    NarumiError,
    NotFoundError,
)
from narumi.providers.service import ProviderService

from .provider_fakes import (
    INSTANCE_ONE,
    INSTANCE_TWO,
    FakeCodexBackend,
    FakeMetadata,
    FakeRuntimeInspector,
    JobQueue,
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


@pytest.mark.parametrize("provider_id", ["anthropic-api", "openai-api"])
def test_auth_start_is_durable_before_work_and_replays_one_acceptance(auth_setup, provider_id):
    service, _, metadata, executor = auth_setup
    record = create_connection(service, provider_id=provider_id)
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


def test_authentication_identifier_check_fails_closed_when_keychain_is_unavailable(auth_setup):
    service, secrets, metadata, executor = auth_setup
    record = create_connection(service)
    before = service.store.path.read_text()
    secrets.calls.clear()
    secrets.fail_get = True
    with pytest.raises(BusyError) as failure:
        service.authenticate(start_args(record, request_id="safe-authentication-request"))
    assert failure.value.details == {"reason": "credential_unavailable"}
    assert "fixture-key" not in str(failure.value)
    assert service.store.path.read_text() == before
    assert metadata.calls == []
    assert executor.pending == []


@pytest.mark.parametrize("lookup", ["start_request_id", "operation_id"])
def test_auth_status_never_reflects_a_request_id_that_now_equals_the_saved_credential(
    auth_setup, caplog, lookup
):
    service, secrets, _, _ = auth_setup
    record = create_connection(service)
    request_id = "credential-shaped-authentication-request"
    operation = service.authenticate(start_args(record, request_id=request_id))["operation"]
    private = service.store.read()["connections"][record["connection_id"]]
    secrets.values[private["secret_account"]] = request_id
    query = {
        "connection_id": record["connection_id"],
        lookup: request_id if lookup == "start_request_id" else operation["operation_id"],
    }
    with pytest.raises(InvalidArgumentError) as failure:
        service.auth_status(query)
    assert request_id not in str(failure.value)
    assert request_id not in caplog.text


def test_restart_pending_auth_is_unknown_and_never_automatically_reissued(auth_setup, tmp_path):
    service, secrets, metadata, executor = auth_setup
    record = create_connection(service)
    args = start_args(record)
    original = service.authenticate(args)["operation"]
    with service.store.transaction() as document:
        document["auth_operations"][original["operation_id"]].pop("user_code")
        document["requests"][args["request_id"]]["response"]["operation"].pop("user_code")
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
    assert recovered["user_code"] is None
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


@pytest.mark.parametrize("provider_id", ["anthropic-api", "openai-api"])
def test_auth_cancel_is_idempotent_and_cannot_be_overwritten(auth_setup, provider_id):
    service, _, metadata, executor = auth_setup
    record = create_connection(service, provider_id=provider_id)
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


@pytest.fixture
def codex_auth_setup(tmp_path):
    backend, executor, secrets = FakeCodexBackend(), ManualExecutor(), MemorySecretStore()
    jobs = JobQueue()
    service = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=FakeMetadata(),
        auth_executor=executor,
        server_instance_id=INSTANCE_ONE,
        runtime_inspector=FakeRuntimeInspector(),
        codex_backend=backend,
        submit_job=jobs,
    )
    runtime = service.runtime._current("codex-app-server", service.store.read())
    service.prepare_runtime(
        {
            "provider_id": "codex-app-server",
            "resource_id": runtime["resources"][0]["resource_id"],
            "expected_catalog_revision": runtime["catalog_revision"],
            "action": "prepare",
            "request_id": "prepare-codex-auth-runtime",
        }
    )
    jobs.run()
    backend.calls.clear()
    yield service, backend, executor, secrets
    service.close()


def test_codex_login_publishes_only_pending_challenge_and_does_not_fetch_models(codex_auth_setup):
    service, backend, executor, secrets = codex_auth_setup
    record = create_connection(service, provider_id="codex-app-server")
    args = start_args(record)
    accepted = service.authenticate(args)
    assert accepted["operation"]["state"] == "pending"
    assert accepted["operation"]["authorization_url"] is None
    assert accepted["operation"]["user_code"] is None
    assert service.authenticate(args) == accepted
    assert backend.calls == secrets.calls == []
    observed = []

    def on_auth(_connection_id, _cancelled):
        operation = service.auth_status(
            {
                "connection_id": record["connection_id"],
                "start_request_id": args["request_id"],
            }
        )["operation"]
        load_contracts().validate_output("get_provider_auth_status", {"operation": operation})
        assert service.authenticate(args) == {"operation": operation}
        persisted = service.store.path.read_text()
        assert backend.authorization_url not in persisted
        assert backend.user_code not in persisted
        observed.append(operation)

    backend.on_auth = on_auth
    executor.run_next()
    assert observed[0]["authorization_url"] == backend.authorization_url
    assert observed[0]["user_code"] == backend.user_code
    assert observed[0]["state"] == "pending"
    completed = service.authenticate(args)
    assert completed["operation"]["state"] == "succeeded"
    assert completed["operation"]["authorization_url"] is None
    assert completed["operation"]["user_code"] is None
    assert service.auth.codex._challenges == {}
    saved = service.list_connections()["connections"][0]
    assert saved["auth_state"] == "authenticated"
    assert saved["credential_present"] is True
    assert saved["revision"] == 1
    assert saved["catalog_state"] == "unfetched"
    assert saved["last_generation_state"] == "never"
    assert backend.calls == [
        ("cancel_auth", record["connection_id"]),
        ("authenticate", record["connection_id"]),
    ]
    assert backend.authorization_url not in service.store.path.read_text()
    assert secrets.calls == []


def test_codex_reauthentication_failure_clears_the_old_credential_first(codex_auth_setup):
    service, backend, executor, _ = codex_auth_setup
    record = create_connection(service, provider_id="codex-app-server")
    first = start_args(record, request_id="initial-codex-login")
    service.authenticate(first)
    executor.run_next()
    assert record["connection_id"] in backend.authenticated
    backend.error = AuthenticationRequiredError("fixture replacement login rejected")

    second = start_args(record, request_id="replacement-codex-login")
    accepted = service.authenticate(second)["operation"]
    assert service.list_connections()["connections"][0]["credential_present"] is True
    executor.run_next()

    completed = service.authenticate(second)["operation"]
    saved = service.store.read()["connections"][record["connection_id"]]
    assert completed["operation_id"] == accepted["operation_id"]
    assert completed["state"] == "failed"
    assert completed["reason"] == "credential_rejected"
    assert saved["credential_present"] is False
    assert saved["auth_state"] == "failed"
    assert record["connection_id"] not in backend.authenticated
    assert backend.calls == [
        ("cancel_auth", record["connection_id"]),
        ("authenticate", record["connection_id"]),
        ("cancel_auth", record["connection_id"]),
        ("authenticate", record["connection_id"]),
    ]


def test_codex_reauthentication_cleanup_failure_never_starts_a_new_login(
    codex_auth_setup, monkeypatch
):
    service, backend, executor, _ = codex_auth_setup
    record = create_connection(service, provider_id="codex-app-server")
    first = start_args(record, request_id="initial-codex-login")
    service.authenticate(first)
    executor.run_next()
    cleanup_targets = []

    def reject_replacement_cleanup(connection_id, *, operation_id=None):
        cleanup_targets.append(operation_id)
        return False

    monkeypatch.setattr(backend, "cancel_auth", reject_replacement_cleanup)
    second = start_args(record, request_id="blocked-replacement-codex-login")
    accepted = service.authenticate(second)["operation"]
    executor.run_next()

    completed = service.authenticate(second)["operation"]
    saved = service.store.read()["connections"][record["connection_id"]]
    assert completed["operation_id"] == accepted["operation_id"]
    assert completed["state"] == "unknown"
    assert saved["credential_present"] is True
    assert saved["auth_state"] == "unknown"
    assert saved["active_auth"]["operation_id"] == accepted["operation_id"]
    assert record["connection_id"] in backend.authenticated
    assert cleanup_targets == [accepted["operation_id"]]
    assert backend.calls == [
        ("cancel_auth", record["connection_id"]),
        ("authenticate", record["connection_id"]),
    ]


@pytest.mark.parametrize("cleanup_verified", [True, False])
def test_codex_ambiguous_credential_install_uses_verified_cleanup_outcome(
    codex_auth_setup, monkeypatch, cleanup_verified
):
    service, backend, executor, _ = codex_auth_setup
    record = create_connection(service, provider_id="codex-app-server")
    args = start_args(record, request_id="ambiguous-codex-credential-install")
    operation = service.authenticate(args)["operation"]
    original_authenticate = backend.authenticate
    original_cancel = backend.cancel_auth
    cleanup_targets = []

    def ambiguous_install(*auth_args, **auth_kwargs):
        original_authenticate(*auth_args, **auth_kwargs)
        raise EngineUnavailableError(
            "fixture credential install outcome unknown",
            details={"reason": "codex_credential_install_outcome_unknown"},
        )

    def tracked_cleanup(connection_id, *, operation_id=None):
        cleanup_targets.append(operation_id)
        if len(cleanup_targets) == 1 or cleanup_verified:
            return original_cancel(connection_id, operation_id=operation_id)
        return False

    monkeypatch.setattr(backend, "authenticate", ambiguous_install)
    monkeypatch.setattr(backend, "cancel_auth", tracked_cleanup)
    executor.run_next()

    completed = service.auth_status(
        {"connection_id": record["connection_id"], "operation_id": operation["operation_id"]}
    )["operation"]
    saved = service.store.read()["connections"][record["connection_id"]]
    assert cleanup_targets == [operation["operation_id"], operation["operation_id"]]
    if cleanup_verified:
        assert completed["state"] == "failed"
        assert saved["auth_state"] == "unconfigured"
        assert saved["credential_present"] is False
        assert saved["active_auth"] is None
        assert record["connection_id"] not in backend.authenticated
    else:
        assert completed["state"] == "unknown"
        assert saved["auth_state"] == "unknown"
        assert saved["credential_present"] is True
        assert saved["active_auth"]["operation_id"] == operation["operation_id"]
        assert record["connection_id"] in backend.authenticated


@pytest.mark.parametrize("commit_installed", [False, True])
@pytest.mark.parametrize("failure_kind", ["generic", "cancelled", "authentication"])
def test_codex_login_final_commit_failure_clears_credential_before_marking_failed(
    codex_auth_setup, monkeypatch, commit_installed, failure_kind
):
    service, backend, executor, _ = codex_auth_setup
    record = create_connection(service, provider_id="codex-app-server")
    args = start_args(record, request_id="codex-login-final-commit-failure")
    operation = service.authenticate(args)["operation"]
    original_commit = service.store.commit
    original_cancel = backend.cancel_auth
    cleanup_targets = []
    rejected = False

    def verified_cleanup(connection_id, *, operation_id=None):
        cleanup_targets.append(operation_id)
        backend.authenticated.discard(connection_id)
        return original_cancel(connection_id, operation_id=operation_id)

    def reject_final_success(document):
        nonlocal rejected
        current = document["auth_operations"].get(operation["operation_id"])
        if not rejected and current is not None and current["state"] == "succeeded":
            rejected = True
            if commit_installed:
                original_commit(document)
            if failure_kind == "cancelled":
                raise CancelledError("fixture final registry commit cancelled")
            if failure_kind == "authentication":
                raise AuthenticationRequiredError("fixture final registry commit rejected")
            raise NarumiError("fixture final registry commit failed")
        return original_commit(document)

    monkeypatch.setattr(service.store, "commit", reject_final_success)
    monkeypatch.setattr(backend, "cancel_auth", verified_cleanup)
    executor.run_next()

    saved = service.store.read()["connections"][record["connection_id"]]
    completed = service.auth_status(
        {"connection_id": record["connection_id"], "operation_id": operation["operation_id"]}
    )["operation"]
    assert rejected
    assert completed["state"] == "failed"
    assert completed["reason"] == "authentication_verification_unavailable"
    assert saved["credential_present"] is False
    assert saved["auth_state"] == "unconfigured"
    assert saved["active_auth"] is None
    assert record["connection_id"] not in backend.authenticated
    assert cleanup_targets == [operation["operation_id"], operation["operation_id"]]
    assert backend.calls == [
        ("cancel_auth", record["connection_id"]),
        ("authenticate", record["connection_id"]),
        ("cancel_auth", record["connection_id"]),
    ]


@pytest.mark.parametrize("commit_installed", [False, True])
def test_codex_login_final_commit_and_cleanup_failure_remain_unknown(
    codex_auth_setup, monkeypatch, commit_installed
):
    service, backend, executor, _ = codex_auth_setup
    record = create_connection(service, provider_id="codex-app-server")
    args = start_args(record, request_id="codex-login-unresolved-final-commit")
    operation = service.authenticate(args)["operation"]
    original_commit = service.store.commit
    original_cancel = backend.cancel_auth
    rejected = False

    def reject_final_success(document):
        nonlocal rejected
        current = document["auth_operations"].get(operation["operation_id"])
        if not rejected and current is not None and current["state"] == "succeeded":
            rejected = True
            if commit_installed:
                original_commit(document)
            raise NarumiError("fixture final registry commit failed")
        return original_commit(document)

    cleanup_targets = []

    def reject_cleanup(connection_id, *, operation_id=None):
        cleanup_targets.append(operation_id)
        if len(cleanup_targets) == 1:
            return original_cancel(connection_id, operation_id=operation_id)
        backend.calls.append(("cancel_auth", connection_id))
        raise RuntimeError("fixture credential cleanup failed")

    monkeypatch.setattr(service.store, "commit", reject_final_success)
    monkeypatch.setattr(backend, "cancel_auth", reject_cleanup)
    executor.run_next()

    saved = service.store.read()["connections"][record["connection_id"]]
    completed = service.auth_status(
        {"connection_id": record["connection_id"], "operation_id": operation["operation_id"]}
    )["operation"]
    assert rejected
    assert completed["state"] == "unknown"
    assert completed["reason"] == "authentication_operation_interrupted"
    assert saved["credential_present"] is True
    assert saved["auth_state"] == "unknown"
    assert saved["active_auth"]["operation_id"] == operation["operation_id"]
    assert saved["active_auth"]["state"] == "unknown"
    assert record["connection_id"] in backend.authenticated
    assert cleanup_targets == [operation["operation_id"], operation["operation_id"]]
    assert backend.calls == [
        ("cancel_auth", record["connection_id"]),
        ("authenticate", record["connection_id"]),
        ("cancel_auth", record["connection_id"]),
    ]


def test_old_login_commit_failure_never_cleans_a_new_active_login(codex_auth_setup):
    service, backend, executor, _ = codex_auth_setup
    record = create_connection(service, provider_id="codex-app-server")
    first_args = start_args(record, request_id="first-codex-login")
    first = service.authenticate(first_args)["operation"]
    executor.run_next()
    second_args = start_args(record, request_id="new-codex-login")
    second = service.authenticate(second_args)["operation"]
    observed = []

    def while_new_login_is_active(_connection_id, _cancelled):
        service.auth.codex._resolve_login_commit_failure(first["operation_id"], record)
        document = service.store.read()
        saved = document["connections"][record["connection_id"]]
        observed.append(
            (
                document["auth_operations"][first["operation_id"]]["state"],
                saved["active_auth"]["operation_id"],
                saved["auth_state"],
            )
        )

    backend.on_auth = while_new_login_is_active
    executor.run_next()

    assert observed == [("unknown", second["operation_id"], "authenticating")]
    assert service.authenticate(second_args)["operation"]["state"] == "succeeded"
    saved = service.store.read()["connections"][record["connection_id"]]
    assert saved["auth_state"] == "authenticated"
    assert saved["active_auth"] is None
    assert record["connection_id"] in backend.authenticated
    assert backend.calls == [
        ("cancel_auth", record["connection_id"]),
        ("authenticate", record["connection_id"]),
        ("cancel_auth", record["connection_id"]),
        ("authenticate", record["connection_id"]),
    ]


def test_codex_auth_requires_preparation_before_scheduling(tmp_path):
    backend, executor = FakeCodexBackend(), ManualExecutor()
    service = ProviderService(
        tmp_path, secret_store=MemorySecretStore(), codex_backend=backend, auth_executor=executor
    )
    record = create_connection(service, provider_id="codex-app-server")
    with pytest.raises(EngineUnavailableError):
        service.authenticate(start_args(record))
    assert executor.pending == backend.calls == []
    assert service.store.read()["auth_operations"] == {}
    service.close()


@pytest.mark.parametrize("action", ["cancel", "disable", "logout", "close"])
def test_codex_late_login_cannot_restore_cancelled_or_closed_connection(codex_auth_setup, action):
    service, backend, executor, _ = codex_auth_setup
    record = create_connection(service, provider_id="codex-app-server")
    operation = service.authenticate(start_args(record))["operation"]
    inspected = []

    def on_auth(_connection_id, _cancelled):
        assert service.auth.codex._challenges
        if action == "close":
            service.close()
        elif action == "disable":
            service.set_connection(
                {
                    "connection_id": record["connection_id"],
                    "expected_revision": 1,
                    "enabled": False,
                    "request_id": "disable-codex-login",
                }
            )
        else:
            args = {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "action": action,
                "request_id": "cancel-or-logout-codex-login",
            }
            if action == "cancel":
                args["operation_id"] = operation["operation_id"]
            service.authenticate(args)
        assert service.auth.codex._challenges == {}
        with pytest.raises(CancelledError):
            service.auth.codex._publish_code(
                operation["operation_id"], record, backend.authorization_url, backend.user_code
            )
        inspected.append(True)

    backend.on_auth = on_auth
    executor.run_next()
    assert inspected == [True]
    if action == "logout":
        executor.run_next()
    document = service.store.read()
    saved = document["connections"][record["connection_id"]]
    completed = document["auth_operations"][operation["operation_id"]]
    assert completed["state"] == ("unknown" if action == "close" else "cancelled")
    assert completed["authorization_url"] is None
    assert completed["user_code"] is None
    assert saved["credential_present"] is False
    assert saved["auth_state"] != "authenticated"
    assert record["connection_id"] not in backend.authenticated
    if action != "close":
        assert ("cancel_auth", record["connection_id"]) in backend.calls
    else:
        assert ("close",) in backend.calls


def test_codex_restart_removes_challenge_and_never_reissues_login(codex_auth_setup):
    service, backend, executor, secrets = codex_auth_setup
    record = create_connection(service, provider_id="codex-app-server")
    args = start_args(record)
    operation = service.authenticate(args)["operation"]
    reopened = []
    next_backend, next_executor = FakeCodexBackend(), ManualExecutor()

    def restart(_connection_id, _cancelled):
        reopened.append(
            ProviderService(
                service.root,
                secret_store=secrets,
                codex_backend=next_backend,
                auth_executor=next_executor,
                server_instance_id=INSTANCE_TWO,
            )
        )

    backend.on_auth = restart
    executor.run_next()
    restarted = reopened[0]
    recovered = restarted.authenticate(args)["operation"]
    assert recovered["operation_id"] == operation["operation_id"]
    assert recovered["state"] == "unknown"
    assert recovered["authorization_url"] is None
    assert recovered["user_code"] is None
    assert service.auth.codex._challenges == restarted.auth.codex._challenges == {}
    assert next_backend.calls == next_executor.pending == []
    with pytest.raises(BusyError):
        restarted.authenticate(start_args(record, request_id="not-an-automatic-retry"))
    restarted.authenticate(
        {
            "connection_id": record["connection_id"],
            "expected_revision": 1,
            "action": "cancel",
            "operation_id": operation["operation_id"],
            "request_id": "cancel-unresolved-codex-login",
        }
    )
    restarted.authenticate(start_args(record, request_id="explicit-new-codex-login"))
    next_executor.run_next()
    assert restarted.list_connections()["connections"][0]["credential_present"] is True
    restarted.close()


@pytest.mark.parametrize(
    "url, code",
    [
        ("http://auth.openai.com/codex/device", "FIXTURE-CODE"),
        ("https://auth.openai.com.example.com/codex/device", "FIXTURE-CODE"),
        ("https://auth.openai.com/oauth/authorize?state=fixture", "FIXTURE-CODE"),
        ("https://auth.openai.com/codex/device?code=fixture-secret", "FIXTURE-CODE"),
        ("https://auth.openai.com/codex/device#fixture-secret", "FIXTURE-CODE"),
        ("https://auth.openai.com/codex/device", None),
        ("https://auth.openai.com/codex/device", ""),
        ("https://auth.openai.com/codex/device", "A" * 33),
        ("https://auth.openai.com/codex/device", "fixture_code"),
        ("https://auth.openai.com/codex/device", "fixture\ncode"),
        ("https://auth.openai.com/codex/device", "ＦＩＸＴＵＲＥ"),
    ],
)
def test_codex_rejects_untrusted_challenges_without_persisting_them(codex_auth_setup, url, code):
    service, backend, executor, _ = codex_auth_setup
    backend.authorization_url = url
    backend.user_code = code
    record = create_connection(service, provider_id="codex-app-server")
    args = start_args(record)
    service.authenticate(args)
    executor.run_next()
    operation = service.authenticate(args)["operation"]
    assert operation["state"] == "failed"
    assert operation["authorization_url"] is None
    assert operation["user_code"] is None
    assert service.auth.codex._challenges == {}
    assert url not in service.store.path.read_text()
    assert "fixture-secret" not in service.store.path.read_text()


@pytest.mark.parametrize(
    "error, reason",
    [
        (
            RuntimeError("FIXTURE-USER-CODE /private/fixture-session"),
            "authentication_verification_unavailable",
        ),
        (
            AuthenticationRequiredError(
                "FIXTURE-USER-CODE", details={"reason": "FIXTURE-USER-CODE"}
            ),
            "credential_rejected",
        ),
        (
            AuthenticationRequiredError(
                "FIXTURE-USER-CODE", details={"reason": "device_code_login_unavailable"}
            ),
            "device_code_login_unavailable",
        ),
    ],
)
def test_codex_upstream_auth_failure_is_static_and_clears_challenge(
    codex_auth_setup, caplog, error, reason
):
    service, backend, executor, _ = codex_auth_setup
    backend.error = error
    record = create_connection(service, provider_id="codex-app-server")
    args = start_args(record)
    service.authenticate(args)
    executor.run_next()
    operation = service.authenticate(args)["operation"]
    assert operation["reason"] == reason
    assert operation["state"] == "failed"
    assert operation["authorization_url"] is None
    assert operation["user_code"] is None
    assert service.auth.codex._challenges == {}
    assert backend.user_code not in service.store.path.read_text() + caplog.text + json.dumps(
        operation
    )


def test_codex_logout_is_scoped_pending_and_replayable(codex_auth_setup):
    service, backend, executor, _ = codex_auth_setup
    first = create_connection(service, provider_id="codex-app-server", request_id="first")
    second = create_connection(service, provider_id="codex-app-server", request_id="second")
    for record in (first, second):
        service.authenticate(start_args(record, request_id="login-" + record["connection_id"]))
        executor.run_next()
    args = {
        "connection_id": first["connection_id"],
        "expected_revision": 1,
        "action": "logout",
        "request_id": "logout-first-codex",
    }
    accepted = service.authenticate(args)
    assert accepted["operation"]["state"] == "pending"
    assert accepted["operation"]["connection_revision"] == 2
    records = {r["connection_id"]: r for r in service.list_connections()["connections"]}
    assert records[first["connection_id"]]["credential_present"] is False
    assert records[second["connection_id"]]["credential_present"] is True
    assert service.authenticate(args) == accepted
    executor.run_next()
    completed = service.authenticate(args)
    assert completed["operation"]["state"] == "succeeded"
    assert completed["operation"]["authorization_url"] is None
    assert backend.authenticated == {second["connection_id"]}
    assert [call for call in backend.calls if call[0] == "logout"] == [
        ("logout", first["connection_id"]),
    ]


def test_codex_executor_failure_never_starts_login(codex_auth_setup):
    service, backend, _, _ = codex_auth_setup

    class RejectingExecutor:
        def submit(self, *_args):
            raise RuntimeError("fixture-secret")

    service.auth_executor = RejectingExecutor()
    record = create_connection(service, provider_id="codex-app-server")
    args = start_args(record)
    with pytest.raises(NarumiError, match="could not be started"):
        service.authenticate(args)
    replay = service.authenticate(args)["operation"]
    assert replay["state"] == "failed"
    assert backend.calls == []
    assert "fixture-secret" not in service.store.path.read_text()


@pytest.mark.parametrize("action", ["cancel", "disable"])
def test_late_codex_cancellation_targets_only_its_original_operation(codex_auth_setup, action):
    service, _, executor, _ = codex_auth_setup
    cancel_entered, release_cancel = threading.Event(), threading.Event()
    next_login_entered, release_login = threading.Event(), threading.Event()

    class OperationBackend(FakeCodexBackend):
        active_operation = None

        def __init__(self):
            super().__init__()
            self.cancel_targets = []
            self.cancelled_operations = set()

        def authenticate(
            self, connection_id, *, on_authorization_code, cancelled, operation_id=None
        ):
            self.active_operation = operation_id

            def during_auth(_connection_id, _cancelled):
                next_login_entered.set()
                assert release_login.wait(5)
                if operation_id in self.cancelled_operations:
                    raise CancelledError("fixture new login was wrongly cancelled")

            self.on_auth = during_auth
            try:
                return super().authenticate(
                    connection_id,
                    on_authorization_code=on_authorization_code,
                    cancelled=cancelled,
                    operation_id=operation_id,
                )
            finally:
                self.active_operation = None

        def cancel_auth(self, connection_id, *, operation_id=None):
            self.cancel_targets.append(operation_id)
            cancel_entered.set()
            assert release_cancel.wait(5)
            if operation_id is None or operation_id == self.active_operation:
                self.cancelled_operations.add(self.active_operation)

    backend = OperationBackend()
    service._codex_backend = backend
    record = create_connection(service, provider_id="codex-app-server")
    first = service.authenticate(start_args(record))["operation"]

    def cancel_first():
        if action == "disable":
            return service.set_connection(
                {
                    "connection_id": record["connection_id"],
                    "expected_revision": 1,
                    "enabled": False,
                    "request_id": "delayed-disable-first-codex-login",
                }
            )
        return service.authenticate(
            {
                "connection_id": record["connection_id"],
                "expected_revision": 1,
                "action": "cancel",
                "operation_id": first["operation_id"],
                "request_id": "delayed-cancel-first-codex-login",
            }
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancellation = pool.submit(cancel_first)
        try:
            assert cancel_entered.wait(3)
            executor.run_next()  # The cancelled old worker releases its own store lease.
            if action == "disable":
                with pytest.raises(BusyError):
                    service.set_connection(
                        {
                            "connection_id": record["connection_id"],
                            "expected_revision": 2,
                            "enabled": True,
                            "request_id": "blocked-reenable-during-cancellation",
                        }
                    )
            else:
                with pytest.raises(BusyError):
                    service.authenticate(
                        start_args(record, request_id="blocked-login-during-cancellation")
                    )
            release_cancel.set()
            cancellation.result(timeout=3)
            if action == "disable":
                record = service.set_connection(
                    {
                        "connection_id": record["connection_id"],
                        "expected_revision": 2,
                        "enabled": True,
                        "request_id": "explicit-reenable-before-new-login",
                    }
                )["connection"]
            next_args = start_args(record, request_id="explicit-second-codex-login")
            second = service.authenticate(next_args)["operation"]
            next_login = pool.submit(executor.run_next)
            assert next_login_entered.wait(3)
            release_login.set()
            next_login.result(timeout=3)
        finally:
            release_cancel.set()
            release_login.set()
    assert backend.cancel_targets == [first["operation_id"], second["operation_id"]]
    assert second["operation_id"] not in backend.cancelled_operations
    assert service.authenticate(next_args)["operation"]["state"] == "succeeded"


@pytest.mark.parametrize("action", ["cancel", "disable"])
def test_codex_cancel_failure_requires_a_new_explicit_cleanup_request(codex_auth_setup, action):
    service, _, _, _ = codex_auth_setup

    class FailingCancellationBackend(FakeCodexBackend):
        def __init__(self):
            super().__init__()
            self.fail_cancellation = True
            self.cancel_targets = []

        def cancel_auth(self, connection_id, *, operation_id=None):
            self.cancel_targets.append((connection_id, operation_id))
            if self.fail_cancellation:
                raise RuntimeError("fixture private cancellation failure")

    backend = FailingCancellationBackend()
    service._codex_backend = backend
    record = create_connection(service, provider_id="codex-app-server")
    operation = service.authenticate(start_args(record))["operation"]
    if action == "cancel":
        args = {
            "connection_id": record["connection_id"],
            "expected_revision": 1,
            "action": "cancel",
            "operation_id": operation["operation_id"],
            "request_id": "failed-explicit-codex-cancel",
        }
    else:
        args = {
            "connection_id": record["connection_id"],
            "expected_revision": 1,
            "enabled": False,
            "request_id": "failed-codex-disable-cancel",
        }
    with pytest.raises(NarumiError, match="cancellation is unresolved"):
        (service.authenticate(args) if action == "cancel" else service.set_connection(args))
    document = service.store.read()
    assert document["requests"][args["request_id"]]["state"] == "unknown"
    assert document["requests"][args["request_id"]]["response"] is None
    assert document["auth_operations"][operation["operation_id"]]["state"] == "unknown"
    assert document["connections"][record["connection_id"]]["active_auth"]["state"] == "unknown"
    with pytest.raises(NarumiError, match="original provider change is unresolved"):
        (service.authenticate(args) if action == "cancel" else service.set_connection(args))
    assert len(backend.cancel_targets) == 1

    backend.fail_cancellation = False
    if action == "cancel":
        repaired = service.authenticate({**args, "request_id": "explicit-codex-cancel-repair"})[
            "operation"
        ]
        assert repaired["state"] == "cancelled"
    else:
        repaired = service.set_connection(
            {
                **args,
                "expected_revision": 2,
                "request_id": "explicit-codex-disable-repair",
            }
        )["connection"]
        assert repaired["enabled"] is False
        assert repaired["revision"] == 3
    assert len(backend.cancel_targets) == 2
    saved = service.store.read()["connections"][record["connection_id"]]
    assert saved["active_auth"] is None


@pytest.mark.parametrize("instance_id", [INSTANCE_ONE, INSTANCE_TWO])
def test_nonowner_context_never_recovers_or_interrupts_resident_auth(auth_setup, instance_id):
    owner, secrets, metadata, executor = auth_setup
    record = create_connection(owner)
    args = start_args(record)
    accepted = owner.authenticate(args)
    before = owner.store.path.read_bytes()
    backend = FakeCodexBackend()
    secrets.calls.clear()
    observer = ProviderService(
        owner.root,
        server_instance_id=instance_id,
        recover=False,
        secret_store=secrets,
        metadata_client=FakeMetadata(),
        auth_executor=ManualExecutor(),
        codex_backend=backend,
    )
    assert (
        observer.auth_status(
            {
                "connection_id": record["connection_id"],
                "start_request_id": args["request_id"],
            }
        )
        == accepted
    )
    observer.close()
    assert owner.store.path.read_bytes() == before
    private = owner.store.read()["connections"][record["connection_id"]]
    assert secrets.calls == [
        ("get", f"providers:{owner.namespace}:request-hmac"),
        ("get", private["secret_account"]),
        ("get", private["secret_account"]),
    ]
    assert backend.calls == [("close",)]
    executor.run_next()
    assert owner.authenticate(args)["operation"]["state"] == "succeeded"
    assert len(metadata.calls) == 1


@pytest.mark.parametrize("instance_id", [INSTANCE_ONE, INSTANCE_TWO])
def test_device_challenge_is_visible_only_to_its_live_service(codex_auth_setup, instance_id):
    owner, backend, executor, _ = codex_auth_setup
    record = create_connection(owner, provider_id="codex-app-server")
    args = start_args(record)
    accepted = owner.authenticate(args)
    query = {"connection_id": record["connection_id"], "start_request_id": args["request_id"]}

    def inspect(_connection_id, _cancelled):
        before = owner.store.path.read_bytes()
        observer = ProviderService(
            owner.root,
            server_instance_id=instance_id,
            recover=False,
            secret_store=MemorySecretStore(),
            auth_executor=ManualExecutor(),
            codex_backend=FakeCodexBackend(),
        )
        try:
            assert observer.auth_status(query) == accepted
        finally:
            observer.close()
        assert owner.store.path.read_bytes() == before
        assert owner.auth_status(query)["operation"]["user_code"] == backend.user_code

    backend.on_auth = inspect
    executor.run_next()
    assert owner.authenticate(args)["operation"]["state"] == "succeeded"
    assert owner.auth.codex._challenges == {}


def test_shutdown_discards_challenge_even_when_registry_cleanup_fails(
    codex_auth_setup, monkeypatch
):
    service, backend, executor, _ = codex_auth_setup
    record = create_connection(service, provider_id="codex-app-server")
    service.authenticate(start_args(record))
    inspected = []

    def unavailable_registry():
        raise NarumiError("fixture registry unavailable")

    def shutdown(_connection_id, _cancelled):
        assert service.auth.codex._challenges
        with monkeypatch.context() as patch:
            patch.setattr(service, "_interrupt_owned_operations", unavailable_registry)
            with pytest.raises(NarumiError, match="fixture registry unavailable"):
                service.close()
        assert service.auth.codex._challenges == {}
        assert ("close",) in backend.calls
        inspected.append(True)

    backend.on_auth = shutdown
    executor.run_next()
    assert inspected == [True]
    assert backend.user_code not in service.store.path.read_text()
