"""Runtime inspection jobs have durable receipts, process leases and safe cancellation."""

import importlib.metadata
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from narumi.contracts.loader import load_contracts
from narumi.errors import (
    BusyError,
    CancelledError,
    ConfigurationConflictError,
    EngineUnavailableError,
    NarumiError,
)
from narumi.providers import _io
from narumi.providers import runtime as runtime_module
from narumi.providers.runtime import RuntimeInspector
from narumi.providers.service import ProviderService

from .provider_fakes import (
    INSTANCE_ONE,
    INSTANCE_TWO,
    FakeMetadata,
    FakeProgress,
    FakeRuntimeInspector,
    JobQueue,
    ManualExecutor,
    MemorySecretStore,
)


@pytest.fixture
def runtime_setup(tmp_path):
    jobs, inspector, secrets = JobQueue(), FakeRuntimeInspector(), MemorySecretStore()
    service = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=FakeMetadata(),
        auth_executor=ManualExecutor(),
        server_instance_id=INSTANCE_ONE,
        submit_job=jobs,
        runtime_inspector=inspector,
    )
    yield service, jobs, inspector, secrets
    service.close()


def provider(service, provider_id="claude-agent-sdk"):
    return next(
        item for item in service.list_providers()["providers"] if item["provider_id"] == provider_id
    )


def prepare_args(service, provider_id="claude-agent-sdk", request_id="prepare-runtime-request"):
    runtime = provider(service, provider_id)["runtime"]
    return {
        "provider_id": provider_id,
        "resource_id": runtime["resources"][0]["resource_id"],
        "expected_catalog_revision": runtime["catalog_revision"],
        "action": "prepare",
        "request_id": request_id,
    }


def test_prepare_receipt_is_durable_and_replays_without_running_again(runtime_setup):
    service, jobs, inspector, secrets = runtime_setup
    args = prepare_args(service)
    result = service.prepare_runtime(args)
    assert service.prepare_runtime(args) == result
    assert len(jobs.calls) == 1
    assert inspector.calls == secrets.calls == []
    runtime = provider(service)["runtime"]
    assert runtime["active_setup"]["job_id"] == result["job_id"]
    assert runtime["active_setup"]["start_request_id"] == args["request_id"]
    assert runtime["active_setup"]["state"] == "queued"
    assert result["job_id"] in service.store.path.read_text()
    jobs.run()
    completed = provider(service)
    assert completed["runtime"]["state"] == "ready"
    assert completed["runtime"]["active_setup"] is None
    assert completed["runtime"]["last_setup"]["state"] == "succeeded"
    assert completed["availability"] == "unverified"
    assert completed["reason"] == "sdk_execution_isolation_unverified"
    load_contracts().validate_output("list_providers", service.list_providers())


def test_prepare_is_exclusive_per_provider_and_stale_catalog_cannot_execute(runtime_setup):
    service, jobs, inspector, _ = runtime_setup
    args = prepare_args(service)
    service.prepare_runtime(args)
    with pytest.raises(BusyError):
        service.prepare_runtime({**args, "request_id": "second-runtime-request"})
    service.prepare_runtime(prepare_args(service, "ollama", "other-provider-request"))
    assert len(jobs.calls) == 2
    jobs.run(0)
    inspector.version = "2.0.0"
    with pytest.raises(ConfigurationConflictError):
        service.prepare_runtime({**args, "request_id": "stale-catalog-request"})
    assert len(jobs.calls) == 2


def test_queued_job_cancel_observation_releases_provider(runtime_setup):
    service, jobs, inspector, _ = runtime_setup
    first = service.prepare_runtime(prepare_args(service))
    service.observe_job(first["job_id"], "cancelled")
    runtime = provider(service)["runtime"]
    assert runtime["active_setup"] is None
    assert runtime["last_setup"]["state"] == "cancelled"
    service.prepare_runtime(prepare_args(service, request_id="explicit-next-prepare"))
    with pytest.raises(CancelledError):
        jobs.run(0)
    assert inspector.calls == []
    jobs.run(1)
    assert provider(service)["runtime"]["state"] == "ready"


def test_cancelled_progress_never_publishes_runtime_ready(runtime_setup):
    service, jobs, inspector, _ = runtime_setup
    service.prepare_runtime(prepare_args(service))
    with pytest.raises(CancelledError):
        jobs.run(cancelled=True)
    assert inspector.calls == []
    runtime = provider(service)["runtime"]
    assert runtime["state"] == "not_prepared"
    assert runtime["last_setup"]["state"] == "cancelled"


def test_runtime_failure_cannot_leak_untrusted_exception(runtime_setup):
    service, jobs, inspector, _ = runtime_setup
    inspector.error = RuntimeError("fixture-secret /private/provider-runtime")
    service.prepare_runtime(prepare_args(service))
    with pytest.raises(EngineUnavailableError) as failure:
        jobs.run()
    assert "fixture-secret" not in str(failure.value)
    assert "fixture-secret" not in service.store.path.read_text()
    assert provider(service)["runtime"]["last_setup"]["state"] == "failed"


def test_job_receipt_write_failure_releases_only_its_acceptance(runtime_setup, monkeypatch):
    service, jobs, inspector, _ = runtime_setup
    original_commit = service.store.commit
    writes = []

    def fail_receipt_once(document):
        writes.append(1)
        if len(writes) == 2:
            raise NarumiError("fixture receipt persistence failed")
        original_commit(document)

    monkeypatch.setattr(service.store, "commit", fail_receipt_once)
    args = prepare_args(service)
    with pytest.raises(NarumiError):
        service.prepare_runtime(args)
    first_job_id = jobs.calls[0][0]
    with pytest.raises(EngineUnavailableError):
        jobs.run()
    service.observe_job(first_job_id, "failed")
    saved = provider(service)["runtime"]
    assert saved["active_setup"] is None
    assert saved["last_setup"]["job_id"] == first_job_id
    assert saved["last_setup"]["state"] == "failed"
    assert service.prepare_runtime(args) == {"job_id": first_job_id}
    service.prepare_runtime({**args, "request_id": "prepare-after-known-failure"})
    assert inspector.calls == []
    jobs.run(1)
    assert provider(service)["runtime"]["state"] == "ready"


def test_worker_timeout_during_slow_receipt_commit_releases_accepted_lease(
    runtime_setup, monkeypatch
):
    service, jobs, inspector, _ = runtime_setup
    abort_started = threading.Event()
    original_abort = service.runtime._abort_submission
    original_commit = service.store.commit
    commits = []

    def observed_abort(args, job_id):
        abort_started.set()
        original_abort(args, job_id)

    def slow_receipt_commit(document):
        commits.append(1)
        if len(commits) == 2:
            assert abort_started.wait(3)
        original_commit(document)

    monkeypatch.setattr(runtime_module, "ACCEPTANCE_TIMEOUT", 0)
    monkeypatch.setattr(service.runtime, "_abort_submission", observed_abort)
    monkeypatch.setattr(service.store, "commit", slow_receipt_commit)
    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = []

        def submit(function):
            job_id = jobs(function)
            futures.append(executor.submit(function, FakeProgress(job_id)))
            return job_id

        service.runtime.submit_job = submit
        result = service.prepare_runtime(prepare_args(service))
        with pytest.raises(EngineUnavailableError):
            futures[0].result(timeout=3)
    assert service.runtime._leases == {}
    saved = provider(service)["runtime"]
    assert saved["active_setup"] is None
    assert saved["last_setup"]["job_id"] == result["job_id"]
    assert saved["last_setup"]["state"] == "failed"
    assert inspector.calls == []
    monkeypatch.setattr(runtime_module, "ACCEPTANCE_TIMEOUT", 30)
    service.runtime.submit_job = jobs
    service.prepare_runtime(prepare_args(service, request_id="prepare-after-worker-timeout"))
    jobs.run(1)
    assert provider(service)["runtime"]["state"] == "ready"


def test_restart_preserves_unknown_and_checks_old_process_lease(runtime_setup, tmp_path):
    service, jobs, inspector, secrets = runtime_setup
    args = prepare_args(service)
    original = service.prepare_runtime(args)
    next_jobs = JobQueue()
    restarted = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=FakeMetadata(),
        auth_executor=ManualExecutor(),
        server_instance_id=INSTANCE_TWO,
        submit_job=next_jobs,
        runtime_inspector=inspector,
    )
    runtime = provider(restarted)["runtime"]
    assert runtime["active_setup"]["state"] == "unknown"
    assert restarted.prepare_runtime(args) == original
    assert next_jobs.calls == []
    with pytest.raises(BusyError):
        restarted.prepare_runtime({**args, "request_id": "explicit-recovery-prepare"})
    # A process exit closes its descriptors; explicitly release the simulated old process.
    service.runtime._release_lease(original["job_id"])
    replacement = restarted.prepare_runtime({**args, "request_id": "explicit-recovery-prepare"})
    assert replacement["job_id"] != original["job_id"]
    with pytest.raises(CancelledError):
        jobs.run()
    next_jobs.run()
    assert provider(restarted)["runtime"]["last_setup"]["job_id"] == replacement["job_id"]
    restarted.close()


def test_installed_inspection_creates_private_evidence_without_starting_sdk(tmp_path, monkeypatch):
    class Distribution:
        version = "1.2.3"
        metadata = {"License-Expression": "MIT"}

        @staticmethod
        def read_text(name):
            return {"METADATA": "fixture package metadata", "RECORD": "fixture installed files"}[
                name
            ]

    monkeypatch.setattr(importlib.metadata, "distribution", lambda _: Distribution())
    jobs = JobQueue()
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        metadata_client=FakeMetadata(),
        auth_executor=ManualExecutor(),
        submit_job=jobs,
        runtime_inspector=RuntimeInspector(),
    )
    resource = provider(service)["runtime"]["resources"][0]
    assert resource["source"] == "installed"
    assert resource["version"] == "1.2.3"
    assert resource["license"] == "MIT"
    assert len(resource["sha256"]) == 64
    service.prepare_runtime(prepare_args(service))
    jobs.run()
    evidence = tmp_path / "providers/runtime/claude-agent-sdk/inspection.json"
    assert stat.S_IMODE(evidence.stat().st_mode) == 0o600
    assert json.loads(evidence.read_text())["sdk_execution_verified"] is False
    assert provider(service)["availability"] == "unverified"
    service.close()


def test_missing_dependency_remains_not_prepared_without_installer(tmp_path, monkeypatch):
    def missing(_name):
        raise importlib.metadata.PackageNotFoundError()

    monkeypatch.setattr(importlib.metadata, "distribution", missing)
    jobs = JobQueue()
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        metadata_client=FakeMetadata(),
        auth_executor=ManualExecutor(),
        submit_job=jobs,
        runtime_inspector=RuntimeInspector(),
    )
    service.prepare_runtime(prepare_args(service))
    with pytest.raises(EngineUnavailableError):
        jobs.run()
    result = provider(service)
    assert result["runtime"]["state"] == "not_prepared"
    assert result["reason"] == "runtime_dependency_missing"
    assert result["runtime"]["resources"][0]["version"] is None
    service.close()


def test_http_adapters_prepare_without_unused_anthropic_or_httpx_packages(tmp_path, monkeypatch):
    inspected = []

    class NarumiDistribution:
        version = "1.2.3"
        metadata = {"License-Expression": "MIT"}

        @staticmethod
        def read_text(name):
            return {"METADATA": "narumi distribution", "RECORD": "narumi HTTP adapters"}[name]

    def distribution(name):
        inspected.append(name)
        if name != "narumi":
            raise importlib.metadata.PackageNotFoundError(name)
        return NarumiDistribution()

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    jobs = JobQueue()
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        metadata_client=FakeMetadata(),
        auth_executor=ManualExecutor(),
        submit_job=jobs,
    )
    for index, provider_id in enumerate(("anthropic-api", "ollama")):
        args = prepare_args(service, provider_id, f"prepare-http-adapter-{index}")
        service.prepare_runtime(args)
        jobs.run(index)
        assert provider(service, provider_id)["runtime"]["state"] == "ready"
    assert set(inspected) == {"narumi", "claude-agent-sdk"}
    assert provider(service)["runtime"]["resources"][0]["version"] is None
    service.close()


def test_runtime_parent_allow_acl_rejects_before_acceptance_and_can_be_repaired(
    runtime_setup, monkeypatch
):
    service, jobs, inspector, _ = runtime_setup
    directory = service.root / "providers" / "runtime"
    directory.mkdir(mode=0o700)
    target = directory.stat().st_ino
    original_guard = _io.ensure_no_extended_allow_acl

    def deny_shared_parent(descriptor):
        if os.fstat(descriptor).st_ino == target:
            raise OSError("fixture extended allow ACL")
        original_guard(descriptor)

    monkeypatch.setattr(_io, "ensure_no_extended_allow_acl", deny_shared_parent)
    args = prepare_args(service)
    with pytest.raises(EngineUnavailableError):
        service.prepare_runtime(args)
    assert jobs.calls == inspector.calls == []
    assert service.store.read()["requests"] == {}
    monkeypatch.setattr(_io, "ensure_no_extended_allow_acl", original_guard)
    service.prepare_runtime(args)
    jobs.run()
    assert provider(service)["runtime"]["state"] == "ready"
