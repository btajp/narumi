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
    assert backend.calls == [("authenticate", record["connection_id"])]
    assert backend.authorization_url not in service.store.path.read_text()
    assert secrets.calls == []


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
            release_cancel.set()
            cancellation.result(timeout=3)
            release_login.set()
            next_login.result(timeout=3)
        finally:
            release_cancel.set()
            release_login.set()
    assert backend.cancel_targets == [first["operation_id"]]
    assert second["operation_id"] not in backend.cancelled_operations
    assert service.authenticate(next_args)["operation"]["state"] == "succeeded"


@pytest.mark.parametrize("instance_id", [INSTANCE_ONE, INSTANCE_TWO])
def test_nonowner_context_never_recovers_or_interrupts_resident_auth(auth_setup, instance_id):
    owner, _, metadata, executor = auth_setup
    record = create_connection(owner)
    args = start_args(record)
    accepted = owner.authenticate(args)
    before = owner.store.path.read_bytes()
    backend, secrets = FakeCodexBackend(), MemorySecretStore()
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
    assert secrets.calls == []
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
