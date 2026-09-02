"""Runtime inspection jobs have durable receipts, process leases and safe cancellation."""

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
from narumi.providers import claude as claude_module
from narumi.providers import runtime as runtime_module
from narumi.providers import runtime_catalog as runtime_catalog_module
from narumi.providers.metadata.openai_compatible import model_descriptor
from narumi.providers.runtime import RuntimeInspector
from narumi.providers.service import ProviderService

from .provider_fakes import (
    INSTANCE_ONE,
    INSTANCE_TWO,
    FakeCodexBackend,
    FakeMetadata,
    FakeProgress,
    FakeRuntimeInspector,
    JobQueue,
    ManualExecutor,
    MemorySecretStore,
    create_connection,
    prepared_codex_connection,
)


@pytest.fixture(autouse=True)
def stable_claude_runtime_evidence(monkeypatch):
    evidence = {
        "resource_id": "claude-agent-sdk-0-2-144",
        "sdk_version": "0.2.144",
        "cli_version": "2.1.239",
        "cli_sha256": "c" * 64,
        "sdk_source_sha256": "d" * 64,
        "isolation_profile_sha256": "e" * 64,
    }
    monkeypatch.setattr(claude_module, "runtime_evidence", lambda: dict(evidence))
    return evidence


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
        codex_backend=FakeCodexBackend(),
    )
    yield service, jobs, inspector, secrets
    service.close()


@pytest.fixture
def openai_source_package(tmp_path, monkeypatch):
    package = tmp_path / "installed-source" / "narumi"
    source_paths = set().union(*runtime_catalog_module._PROVIDER_SOURCE_SETS.values())
    for relative_path in sorted(source_paths):
        source = package / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"fixture source: {relative_path}\n")
    monkeypatch.setattr(runtime_catalog_module, "_PACKAGE_ROOT", package)

    class Distribution:
        version = "1.2.3"
        metadata = {"License-Expression": "MIT"}

        @staticmethod
        def read_text(name):
            return {"METADATA": "fixture metadata", "RECORD": "fixture record"}[name]

    monkeypatch.setattr(importlib.metadata, "distribution", lambda _: Distribution())
    return package


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
    assert completed["availability"] == "available"
    assert completed["reason"] is None
    load_contracts().validate_output("list_providers", service.list_providers())


def test_claude_workspace_recovery_failure_is_not_advertised_ready(tmp_path):
    runtime_root = tmp_path / "providers" / "runtime" / "claude-agent-sdk"
    runtime_root.mkdir(parents=True, mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (runtime_root / "runs").symlink_to(outside, target_is_directory=True)
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        metadata_client=FakeMetadata(),
        auth_executor=ManualExecutor(),
        server_instance_id=INSTANCE_ONE,
        submit_job=JobQueue(),
        runtime_inspector=FakeRuntimeInspector(),
        codex_backend=FakeCodexBackend(),
    )
    try:
        current = provider(service)
        assert current["availability"] == "not_prepared"
        assert current["reason"] == "claude_sdk_workspace_unavailable"
        assert current["runtime"]["state"] == "failed"
        assert list(outside.iterdir()) == []
    finally:
        service.close()


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
        codex_backend=FakeCodexBackend(),
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
        codex_backend=FakeCodexBackend(),
    )
    resource = provider(service)["runtime"]["resources"][0]
    assert resource["source"] == "installed"
    assert resource["version"] == "0.2.144"
    assert resource["license"] == "MIT"
    assert len(resource["sha256"]) == 64
    service.prepare_runtime(prepare_args(service))
    jobs.run()
    evidence = tmp_path / "providers/runtime/claude-agent-sdk/inspection.json"
    assert stat.S_IMODE(evidence.stat().st_mode) == 0o600
    inspection = json.loads(evidence.read_text())
    assert inspection["sdk_execution_verified"] is True
    assert inspection["runtime_evidence"] == service.runtime.inspector.expected_runtime(
        "claude-agent-sdk", resource
    )
    assert provider(service)["availability"] == "available"
    service.close()


def test_claude_resource_uses_the_closed_runtime_evidence_fingerprint(
    openai_source_package,
    stable_claude_runtime_evidence,
):
    inspector = RuntimeInspector()
    resource = inspector.resource("claude-agent-sdk")
    source_digest = hashlib.sha256()
    for relative_path in runtime_catalog_module._CLAUDE_ADAPTER_SOURCES:
        digest = hashlib.sha256((openai_source_package / relative_path).read_bytes()).digest()
        source_digest.update(relative_path.encode("ascii") + b"\0" + digest)
    expected = runtime_catalog_module.claude_resource_sha256(
        claude_module.runtime_fingerprint(stable_claude_runtime_evidence),
        source_digest.digest(),
    )
    assert resource["sha256"] == expected
    assert inspector.expected_runtime("claude-agent-sdk", resource) == {
        **stable_claude_runtime_evidence,
        "resource_sha256": expected,
    }

    changed = {**resource, "sha256": "0" * 64}
    with pytest.raises(EngineUnavailableError):
        inspector.expected_runtime("claude-agent-sdk", changed)


@pytest.mark.parametrize("provider_id", ["claude-agent-sdk", "openai-api"])
def test_missing_dependency_remains_not_prepared_without_installer(
    tmp_path, monkeypatch, provider_id
):
    def missing(_name):
        raise importlib.metadata.PackageNotFoundError()

    monkeypatch.setattr(importlib.metadata, "distribution", missing)
    if provider_id == "claude-agent-sdk":
        monkeypatch.setattr(
            claude_module,
            "runtime_evidence",
            lambda: (_ for _ in ()).throw(RuntimeError("fixture runtime missing")),
        )
    jobs = JobQueue()
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        metadata_client=FakeMetadata(),
        auth_executor=ManualExecutor(),
        submit_job=jobs,
        runtime_inspector=RuntimeInspector(),
        codex_backend=FakeCodexBackend(),
    )
    service.prepare_runtime(prepare_args(service, provider_id))
    with pytest.raises(EngineUnavailableError):
        jobs.run()
    result = provider(service, provider_id)
    assert result["runtime"]["state"] == "not_prepared"
    assert result["reason"] == "runtime_dependency_missing"
    assert result["runtime"]["resources"][0]["version"] is None
    service.close()


def test_missing_claude_sdk_evidence_does_not_break_provider_listing(tmp_path, monkeypatch):
    def missing_runtime():
        raise importlib.metadata.PackageNotFoundError("claude-agent-sdk")

    monkeypatch.setattr(claude_module, "runtime_evidence", missing_runtime)
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        metadata_client=FakeMetadata(),
        auth_executor=ManualExecutor(),
        runtime_inspector=RuntimeInspector(),
        codex_backend=FakeCodexBackend(),
    )
    listed = service.list_providers()
    descriptor = next(
        item for item in listed["providers"] if item["provider_id"] == "claude-agent-sdk"
    )
    assert descriptor["availability"] == "not_prepared"
    assert descriptor["runtime"]["state"] == "not_prepared"
    assert descriptor["runtime"]["resources"][0]["version"] is None
    assert descriptor["runtime"]["resources"][0]["sha256"] is None
    load_contracts().validate_output("list_providers", listed)
    service.close()


def test_http_adapters_prepare_from_narumi_metadata_without_external_sdks(tmp_path, monkeypatch):
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
        codex_backend=FakeCodexBackend(),
    )
    for index, provider_id in enumerate(
        ("openai-api", "openai-compatible-api", "anthropic-api", "ollama")
    ):
        args = prepare_args(service, provider_id, f"prepare-http-adapter-{index}")
        service.prepare_runtime(args)
        jobs.run(index)
        runtime = provider(service, provider_id)["runtime"]
        assert runtime["state"] == "ready"
        evidence = tmp_path / "providers" / "runtime" / provider_id / "inspection.json"
        inspected_resource = json.loads(evidence.read_text())["resource"]
        assert inspected_resource == runtime["resources"][0]
        assert stat.S_IMODE(evidence.stat().st_mode) == 0o600
    assert provider(service, "openai-api")["runtime"]["resources"][0]["resource_id"] == (
        "openai-client"
    )
    assert set(inspected) == {"narumi", "claude-agent-sdk"}
    assert service.metadata.calls == service.secrets.calls == []
    assert provider(service)["runtime"]["resources"][0]["version"] == "0.2.144"
    service.close()


@pytest.mark.parametrize(
    "provider_id",
    [
        "openai-api",
        "openai-compatible-api",
        "anthropic-api",
        "ollama",
        "claude-agent-sdk",
    ],
)
@pytest.mark.parametrize("missing_metadata", ["METADATA", "RECORD"])
def test_distribution_fingerprint_is_required_only_for_http_runtime(
    tmp_path, monkeypatch, provider_id, missing_metadata
):
    class Distribution:
        version = "1.2.3"
        metadata = {"License-Expression": "MIT"}

        @staticmethod
        def read_text(name):
            if name == missing_metadata:
                return None
            return "fixture distribution metadata"

    monkeypatch.setattr(importlib.metadata, "distribution", lambda _: Distribution())
    jobs = JobQueue()
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        metadata_client=FakeMetadata(),
        auth_executor=ManualExecutor(),
        submit_job=jobs,
        runtime_inspector=RuntimeInspector(),
        codex_backend=FakeCodexBackend(),
    )
    resource = provider(service, provider_id)["runtime"]["resources"][0]
    assert resource["version"] == ("0.2.144" if provider_id == "claude-agent-sdk" else "1.2.3")
    assert (resource["sha256"] is not None) == (provider_id == "claude-agent-sdk")
    service.prepare_runtime(prepare_args(service, provider_id))
    if provider_id == "claude-agent-sdk":
        jobs.run()
        result = provider(service, provider_id)
        assert result["runtime"]["state"] == "ready"
        assert result["availability"] == "available"
        assert result["reason"] is None
    else:
        with pytest.raises(EngineUnavailableError, match="Provider runtime preparation failed"):
            jobs.run()
        result = provider(service, provider_id)
        assert result["runtime"]["state"] == "failed"
        assert result["runtime"]["last_setup"]["state"] == "failed"
        assert result["reason"] == "runtime_preparation_failed"
        evidence = tmp_path / "providers" / "runtime" / provider_id / "inspection.json"
        assert not evidence.exists()
    assert service.metadata.calls == service.secrets.calls == []
    service.close()


def test_openai_api_runtime_uses_the_complete_v4_fingerprint_format(
    openai_source_package,
):
    source_digest = hashlib.sha256()
    for relative_path in runtime_catalog_module._OPENAI_API_SOURCES:
        digest = hashlib.sha256((openai_source_package / relative_path).read_bytes()).digest()
        source_digest.update(relative_path.encode("ascii") + b"\0" + digest)
    payload = (
        b"fixture metadata\nfixture record"
        + b"\0narumi-openai-api-sources-v4\0"
        + source_digest.digest()
    )
    assert (
        RuntimeInspector().resource("openai-api")["sha256"] == hashlib.sha256(payload).hexdigest()
    )


def test_provider_runtime_source_sets_cover_dispatch_checkpoints_prompts_and_policy():
    common = {
        "brief/__init__.py",
        "brief/builder.py",
        "brief/gaia_context.py",
        "brief/models.py",
        "bundle/hashing.py",
        "errors.py",
        "generate/bounded.py",
        "generate/checkpoints.py",
        "generate/minutes.py",
        "generate/prompts.py",
        "generate/prompts/minutes_chunk.md",
        "generate/prompts/minutes_final.md",
        "generate/prompts/minutes_reduce.md",
        "llm/base.py",
        "llm/policy.py",
        "llm/registry.py",
        "model_selection.py",
        "models.py",
        "providers/_common.py",
        "providers/auth.py",
        "providers/catalog.py",
        "providers/connections.py",
        "providers/generation.py",
        "providers/runtime.py",
        "providers/runtime_catalog.py",
        "providers/secrets.py",
        "providers/service.py",
        "providers/store.py",
    }
    for provider_id, sources in runtime_catalog_module._PROVIDER_SOURCE_SETS.items():
        assert len(sources) == len(set(sources)), provider_id
        assert common <= set(sources), provider_id
        assert set(runtime_catalog_module._BRIEF_EXECUTION_SOURCES) <= set(sources), provider_id

    assert {
        "providers/codex/_generation.py",
        "providers/codex/_models.py",
        "providers/codex/_policy.py",
        "providers/codex/_rpc.py",
        "providers/codex/_runtime.py",
        "providers/codex/_session.py",
        "providers/codex/_supervisor.py",
        "providers/codex/backend.py",
    } <= set(runtime_catalog_module._CODEX_APP_SERVER_SOURCES)
    assert {
        "providers/audio_response.py",
        "providers/audio_transcription.py",
        "providers/metadata/audio_capabilities.py",
        "providers/transcription.py",
        "transcribe/_checkpoint_format.py",
        "transcribe/_storage.py",
        "transcribe/_wav.py",
        "transcribe/api_stage.py",
        "transcribe/api_transcript.py",
        "transcribe/checkpoints.py",
        "transcribe/chunks.py",
        "transcribe/policy.py",
        "transcribe/stage.py",
        "transcription_selection.py",
    } <= set(runtime_catalog_module._OPENAI_API_SOURCES)
    assert {
        "providers/metadata/openai_compatible.py",
        "providers/metadata/openai_compatible_transport.py",
        "providers/openai_compatible.py",
        "providers/openai_compatible_response.py",
    } <= set(runtime_catalog_module._OPENAI_COMPATIBLE_SOURCES)
    assert {
        "providers/http_generation.py",
        "providers/http_generation_response.py",
        "providers/metadata/anthropic.py",
    } <= set(runtime_catalog_module._ANTHROPIC_API_SOURCES)
    assert {
        "providers/http_generation.py",
        "providers/http_generation_response.py",
        "providers/metadata/ollama.py",
    } <= set(runtime_catalog_module._OLLAMA_SOURCES)
    assert {
        "providers/claude/backend.py",
        "providers/claude/protocol.py",
        "providers/claude/runtime.py",
        "providers/claude/snapshot.py",
        "providers/claude/transport.py",
        "providers/claude/worker.py",
        "providers/claude_sdk_backend.py",
    } <= set(runtime_catalog_module._CLAUDE_ADAPTER_SOURCES)


@pytest.mark.parametrize("relative_path", runtime_catalog_module._BRIEF_EXECUTION_SOURCES)
def test_each_brief_source_changes_every_text_provider_runtime_identity(
    openai_source_package, relative_path
):
    inspector = RuntimeInspector()
    before = {
        provider_id: inspector.resource(provider_id)
        for provider_id in runtime_catalog_module.RESOURCES
    }
    codex_resource = {
        "resource_id": "codex-runtime",
        "sha256": "a" * 64,
        "version": "fixture",
    }
    codex_before = inspector.catalog_revision(codex_resource)
    source = openai_source_package / relative_path
    original = source.read_bytes()
    source.write_bytes(original + b"fixture changed meeting brief behavior\n")
    try:
        after = {
            provider_id: inspector.resource(provider_id)
            for provider_id in runtime_catalog_module.RESOURCES
        }
        for provider_id in runtime_catalog_module.RESOURCES:
            assert after[provider_id]["sha256"] != before[provider_id]["sha256"]
            assert inspector.catalog_revision(after[provider_id]) != inspector.catalog_revision(
                before[provider_id]
            )
        assert inspector.catalog_revision(codex_resource) != codex_before
    finally:
        source.write_bytes(original)


def test_openai_compatible_runtime_uses_the_complete_v4_fingerprint_format(
    openai_source_package,
):
    source_digest = hashlib.sha256()
    for relative_path in runtime_catalog_module._OPENAI_COMPATIBLE_SOURCES:
        digest = hashlib.sha256((openai_source_package / relative_path).read_bytes()).digest()
        source_digest.update(relative_path.encode("ascii") + b"\0" + digest)
    payload = (
        b"fixture metadata\nfixture record"
        + b"\0narumi-openai-compatible-api-sources-v4\0"
        + source_digest.digest()
    )
    assert (
        RuntimeInspector().resource("openai-compatible-api")["sha256"]
        == hashlib.sha256(payload).hexdigest()
    )


def test_codex_catalog_revision_binds_binary_resource_to_v4_adapter_sources(
    openai_source_package,
):
    resource = {
        "resource_id": "codex-runtime",
        "sha256": "a" * 64,
        "version": "fixture",
    }
    source_digest = hashlib.sha256()
    for relative_path in runtime_catalog_module._CODEX_APP_SERVER_SOURCES:
        digest = hashlib.sha256((openai_source_package / relative_path).read_bytes()).digest()
        source_digest.update(relative_path.encode("ascii") + b"\0" + digest)
    payload = (
        json.dumps(resource, sort_keys=True).encode()
        + b"\0narumi-codex-app-server-sources-v5\0"
        + source_digest.digest()
    )
    assert RuntimeInspector.catalog_revision(resource) == hashlib.sha256(payload).hexdigest()


def test_each_codex_source_changes_codex_catalog_revision(openai_source_package):
    inspector = RuntimeInspector()
    resource = {
        "resource_id": "codex-runtime",
        "sha256": "a" * 64,
        "version": "fixture",
    }
    for relative_path in runtime_catalog_module._CODEX_APP_SERVER_SOURCES:
        before = inspector.catalog_revision(resource)
        source = openai_source_package / relative_path
        original = source.read_bytes()
        source.write_bytes(original + b"fixture changed Codex adapter behavior\n")
        assert inspector.catalog_revision(resource) != before
        source.write_bytes(original)


def test_codex_supervisor_changes_only_the_codex_runtime_identity(openai_source_package):
    inspector = RuntimeInspector()
    provider_before = {
        provider_id: inspector.resource(provider_id)
        for provider_id in runtime_catalog_module.RESOURCES
    }
    codex_resource = {
        "resource_id": "codex-runtime",
        "sha256": "a" * 64,
        "version": "fixture",
    }
    codex_before = inspector.catalog_revision(codex_resource)
    source = openai_source_package / "providers/codex/_supervisor.py"
    original = source.read_bytes()
    source.write_bytes(original + b"fixture changed Codex supervision behavior\n")
    try:
        assert inspector.catalog_revision(codex_resource) != codex_before
        assert {
            provider_id: inspector.resource(provider_id)
            for provider_id in runtime_catalog_module.RESOURCES
        } == provider_before
    finally:
        source.write_bytes(original)


def test_missing_codex_source_cannot_publish_an_executable_only_revision(
    openai_source_package,
):
    resource = {"resource_id": "codex-runtime", "sha256": "a" * 64}
    source = openai_source_package / runtime_catalog_module._CODEX_APP_SERVER_SOURCES[0]
    source.unlink()
    with pytest.raises(EngineUnavailableError, match="source inventory is unavailable"):
        RuntimeInspector.catalog_revision(resource)


@pytest.mark.parametrize("relative_path", runtime_catalog_module._OPENAI_API_SOURCES)
def test_each_openai_api_source_changes_the_affected_runtime_identities(
    openai_source_package, relative_path
):
    inspector = RuntimeInspector()
    before = {
        provider_id: inspector.resource(provider_id)
        for provider_id in runtime_catalog_module.RESOURCES
    }
    source = openai_source_package / relative_path
    source.write_bytes(source.read_bytes() + b"fixture changed OpenAI API behavior\n")
    after = {
        provider_id: inspector.resource(provider_id)
        for provider_id in runtime_catalog_module.RESOURCES
    }
    assert before["openai-api"]["sha256"] is not None
    assert after["openai-api"]["sha256"] != before["openai-api"]["sha256"]
    assert inspector.catalog_revision(after["openai-api"]) != inspector.catalog_revision(
        before["openai-api"]
    )
    affected = {
        provider_id
        for provider_id, sources in runtime_catalog_module._PROVIDER_SOURCE_SETS.items()
        if relative_path in sources
    }
    for provider_id in runtime_catalog_module.RESOURCES:
        if provider_id in affected:
            assert after[provider_id]["sha256"] != before[provider_id]["sha256"]
        else:
            assert after[provider_id] == before[provider_id]


@pytest.mark.parametrize(
    ("provider_id", "sources"),
    [
        ("anthropic-api", runtime_catalog_module._ANTHROPIC_API_SOURCES),
        ("ollama", runtime_catalog_module._OLLAMA_SOURCES),
    ],
)
def test_http_runtime_uses_the_complete_v4_fingerprint_format(
    openai_source_package, provider_id, sources
):
    source_digest = hashlib.sha256()
    for relative_path in sources:
        digest = hashlib.sha256((openai_source_package / relative_path).read_bytes()).digest()
        source_digest.update(relative_path.encode("ascii") + b"\0" + digest)
    payload = (
        b"fixture metadata\nfixture record"
        + f"\0narumi-{provider_id}-sources-v4\0".encode("ascii")
        + source_digest.digest()
    )
    assert RuntimeInspector().resource(provider_id)["sha256"] == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("provider_id", ["anthropic-api", "ollama"])
def test_each_http_provider_source_changes_every_affected_runtime_identity(
    openai_source_package, provider_id
):
    inspector = RuntimeInspector()
    for relative_path in runtime_catalog_module._PROVIDER_SOURCE_SETS[provider_id]:
        before = {
            candidate: inspector.resource(candidate)
            for candidate in runtime_catalog_module.RESOURCES
        }
        source = openai_source_package / relative_path
        original = source.read_bytes()
        source.write_bytes(original + f"fixture changed {provider_id} behavior\n".encode())
        after = {
            candidate: inspector.resource(candidate)
            for candidate in runtime_catalog_module.RESOURCES
        }
        affected = {
            candidate
            for candidate, sources in runtime_catalog_module._PROVIDER_SOURCE_SETS.items()
            if relative_path in sources
        }
        for candidate in runtime_catalog_module.RESOURCES:
            if candidate in affected:
                assert after[candidate]["sha256"] != before[candidate]["sha256"]
                assert inspector.catalog_revision(after[candidate]) != inspector.catalog_revision(
                    before[candidate]
                )
            else:
                assert after[candidate] == before[candidate]
        source.write_bytes(original)


@pytest.mark.parametrize("relative_path", runtime_catalog_module._OPENAI_COMPATIBLE_SOURCES)
def test_each_compatible_source_changes_the_affected_runtime_identities(
    openai_source_package, relative_path
):
    inspector = RuntimeInspector()
    before = {
        provider_id: inspector.resource(provider_id)
        for provider_id in runtime_catalog_module.RESOURCES
    }
    source = openai_source_package / relative_path
    source.write_bytes(source.read_bytes() + b"fixture changed compatible behavior\n")
    after = {
        provider_id: inspector.resource(provider_id)
        for provider_id in runtime_catalog_module.RESOURCES
    }
    affected = {
        provider_id
        for provider_id, sources in runtime_catalog_module._PROVIDER_SOURCE_SETS.items()
        if relative_path in sources
    }
    for provider_id in runtime_catalog_module.RESOURCES:
        if provider_id in affected:
            assert after[provider_id]["sha256"] != before[provider_id]["sha256"]
            assert inspector.catalog_revision(after[provider_id]) != inspector.catalog_revision(
                before[provider_id]
            )
        else:
            assert after[provider_id] == before[provider_id]


@pytest.mark.parametrize("relative_path", runtime_catalog_module._CLAUDE_ADAPTER_SOURCES)
def test_each_claude_adapter_source_changes_the_affected_runtime_identities(
    openai_source_package, relative_path
):
    inspector = RuntimeInspector()
    before = {
        provider_id: inspector.resource(provider_id)
        for provider_id in runtime_catalog_module.RESOURCES
    }
    source = openai_source_package / relative_path
    source.write_bytes(source.read_bytes() + b"fixture changed Claude adapter behavior\n")
    after = {
        provider_id: inspector.resource(provider_id)
        for provider_id in runtime_catalog_module.RESOURCES
    }
    affected = {
        provider_id
        for provider_id, sources in runtime_catalog_module._PROVIDER_SOURCE_SETS.items()
        if relative_path in sources
    }
    for provider_id in runtime_catalog_module.RESOURCES:
        if provider_id in affected:
            assert after[provider_id]["sha256"] != before[provider_id]["sha256"]
            assert inspector.catalog_revision(after[provider_id]) != inspector.catalog_revision(
                before[provider_id]
            )
        else:
            assert after[provider_id] == before[provider_id]


def test_provider_runtime_identities_ignore_unlisted_files_and_directory_bookkeeping(
    openai_source_package, monkeypatch
):
    inspector = RuntimeInspector()
    before = {
        provider_id: inspector.resource(provider_id)
        for provider_id in runtime_catalog_module.RESOURCES
    }
    codex_resource = {"resource_id": "codex-runtime", "sha256": "a" * 64}
    codex_before = inspector.catalog_revision(codex_resource)
    (openai_source_package / runtime_catalog_module._OPENAI_AUDIO_SOURCES[0]).touch()
    unlisted = openai_source_package / "unlisted-runtime.py"
    unlisted.write_text("fixture not part of audio runtime\n")
    original_digest = runtime_catalog_module._source_digest

    def create_bytecode_cache(descriptor, directory, name):
        cache = openai_source_package / "providers" / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "fixture.pyc").write_bytes(b"fixture cache")
        return original_digest(descriptor, directory, name)

    monkeypatch.setattr(runtime_catalog_module, "_source_digest", create_bytecode_cache)
    assert {
        provider_id: inspector.resource(provider_id)
        for provider_id in runtime_catalog_module.RESOURCES
    } == before
    assert inspector.catalog_revision(codex_resource) == codex_before


def test_runtime_import_through_symlink_ancestor_uses_canonical_root(
    openai_source_package, tmp_path
):
    package = openai_source_package
    (package / "providers" / "runtime_catalog.py").write_bytes(
        Path(runtime_catalog_module.__file__).read_bytes()
    )
    alias = tmp_path / "import-alias"
    alias.symlink_to(package.parent, target_is_directory=True)
    spec = importlib.util.spec_from_file_location(
        "fixture_runtime_catalog_alias", alias / "narumi" / "providers" / "runtime_catalog.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resource = module.RuntimeInspector().resource("openai-api")
    assert module._PACKAGE_ROOT == package.resolve()
    assert resource["sha256"] is not None
    assert resource == RuntimeInspector().resource("openai-api")


@pytest.mark.parametrize(
    "failure",
    ["missing", "unreadable", "symlink", "directory_symlink", "directory", "fifo", "oversized"],
)
@pytest.mark.parametrize("provider_id", ["openai-api", "openai-compatible-api", "claude-agent-sdk"])
def test_unsafe_provider_source_cannot_prepare_runtime(
    openai_source_package, tmp_path, monkeypatch, failure, provider_id
):
    relative_path = {
        "openai-api": "providers/audio_transcription.py",
        "openai-compatible-api": "providers/openai_compatible_response.py",
        "claude-agent-sdk": "providers/claude/backend.py",
    }[provider_id]
    source = openai_source_package / relative_path
    inspector = RuntimeInspector()
    unaffected_provider = "anthropic-api"
    unaffected = inspector.resource(unaffected_provider)
    if failure == "missing":
        source.unlink()
    elif failure == "unreadable":
        original_open = os.open

        def denied_open(path, *args, **kwargs):
            if path == source.name and kwargs.get("dir_fd") is not None:
                raise PermissionError("fixture unreadable source")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", denied_open)
    elif failure == "symlink":
        target = openai_source_package / "unapproved.py"
        source.rename(target)
        source.symlink_to(target)
    elif failure == "directory_symlink":
        target = tmp_path / "unapproved-provider-directory"
        source.parent.rename(target)
        source.parent.symlink_to(target, target_is_directory=True)
    elif failure == "oversized":
        with source.open("wb") as stream:
            stream.truncate(runtime_catalog_module._MAX_SOURCE_BYTES + 1)
    else:
        source.unlink()
        if failure == "directory":
            source.mkdir()
        else:
            os.mkfifo(source, mode=0o600)
    resource = inspector.resource(provider_id)
    assert resource["sha256"] is None
    message = (
        "dependency is not installed"
        if provider_id == "claude-agent-sdk"
        else "runtime distribution metadata is incomplete"
    )
    with pytest.raises(EngineUnavailableError, match=message):
        inspector.prepare(
            tmp_path / "runtime-state", provider_id, resource, FakeProgress("fixture-job")
        )
    if failure == "directory_symlink" and source.parent == openai_source_package / "providers":
        # Replacing the shared providers directory invalidates every inventory rooted there.
        assert inspector.resource(unaffected_provider)["sha256"] is None
    else:
        assert inspector.resource(unaffected_provider) == unaffected
    assert not (tmp_path / "runtime-state").exists()


def test_audio_source_mutation_during_read_rejects_identity(openai_source_package, monkeypatch):
    source = openai_source_package / runtime_catalog_module._OPENAI_AUDIO_SOURCES[0]
    inode = source.stat().st_ino
    original_read = os.read
    changed = False

    def changing_read(descriptor, size):
        nonlocal changed
        block = original_read(descriptor, size)
        if not changed and os.fstat(descriptor).st_ino == inode:
            changed = True
            with source.open("ab") as stream:
                stream.write(b"fixture concurrent source edit\n")
        return block

    monkeypatch.setattr(os, "read", changing_read)
    assert RuntimeInspector().resource("openai-api")["sha256"] is None
    assert changed


def test_claude_source_mutation_during_read_rejects_identity(openai_source_package, monkeypatch):
    source = openai_source_package / runtime_catalog_module._CLAUDE_ADAPTER_SOURCES[0]
    inode = source.stat().st_ino
    original_read = os.read
    changed = False

    def changing_read(descriptor, size):
        nonlocal changed
        block = original_read(descriptor, size)
        if not changed and os.fstat(descriptor).st_ino == inode:
            changed = True
            with source.open("ab") as stream:
                stream.write(b"fixture concurrent Claude source edit\n")
        return block

    monkeypatch.setattr(os, "read", changing_read)
    assert RuntimeInspector().resource("claude-agent-sdk")["sha256"] is None
    assert changed


@pytest.mark.parametrize("location", ["ancestor", "package", "providers", "metadata"])
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_audio_source_parent_replacement_rejects_identity(
    openai_source_package, tmp_path, monkeypatch, location, replacement
):
    package = openai_source_package
    target = {
        "ancestor": package.parent,
        "package": package,
        "providers": package / "providers",
        "metadata": package / "providers" / "metadata",
    }[location]
    relative = runtime_catalog_module._OPENAI_AUDIO_SOURCES[2 if location == "metadata" else 0]
    trigger = (package / relative).name
    original_digest = runtime_catalog_module._source_digest
    changed = False
    inspector = RuntimeInspector()
    before = inspector.resource("openai-api")
    assert before["sha256"] is not None

    def replace_parent(descriptor, directory, name):
        nonlocal changed
        if not changed and name == trigger:
            changed = True
            previous = target.with_name(target.name + ".old")
            target.rename(previous)
            if replacement == "symlink":
                target.symlink_to(previous, target_is_directory=True)
            else:
                shutil.copytree(previous, target)
                (package / relative).write_text("fixture replaced audio source\n")
        return original_digest(descriptor, directory, name)

    monkeypatch.setattr(runtime_catalog_module, "_source_digest", replace_parent)
    assert inspector.resource("openai-api")["sha256"] is None
    assert changed
    with pytest.raises(EngineUnavailableError, match="runtime changed during preparation"):
        inspector.prepare(
            tmp_path / "runtime-state", "openai-api", before, FakeProgress("fixture-job")
        )


def test_audio_source_change_requires_repreparation_and_model_refresh(
    openai_source_package, tmp_path
):
    jobs = JobQueue()
    metadata = FakeMetadata()
    service = ProviderService(
        tmp_path / "runtime-state",
        secret_store=MemorySecretStore(),
        metadata_client=metadata,
        auth_executor=ManualExecutor(),
        submit_job=jobs,
        runtime_inspector=RuntimeInspector(),
        codex_backend=FakeCodexBackend(),
    )
    service.prepare_runtime(prepare_args(service, "openai-api"))
    jobs.run()
    record = create_connection(service, provider_id="openai-api")
    args = {"connection_id": record["connection_id"], "expected_revision": record["revision"]}
    assert service.test_connection(args)["connected"] is True
    before_codex = provider(service, "codex-app-server")
    source = openai_source_package / runtime_catalog_module._OPENAI_AUDIO_SOURCES[0]
    source.write_bytes(source.read_bytes() + b"fixture upgraded audio behavior\n")
    assert provider(service, "openai-api")["runtime"]["state"] == "not_prepared"
    assert (
        service.list_models({"connection_id": record["connection_id"]})["catalog_state"] == "stale"
    )
    service.prepare_runtime(prepare_args(service, "openai-api", "prepare-upgraded-audio-runtime"))
    jobs.run(1)
    assert (
        service.list_models({"connection_id": record["connection_id"]})["catalog_state"] == "stale"
    )
    assert service.test_connection(args)["connected"] is True
    assert (
        service.list_models({"connection_id": record["connection_id"]})["catalog_state"] == "ready"
    )
    assert provider(service, "codex-app-server") == before_codex
    service.close()


def test_claude_source_change_requires_repreparation_and_model_refresh(
    openai_source_package, tmp_path
):
    jobs = JobQueue()
    service = ProviderService(
        tmp_path / "runtime-state",
        secret_store=MemorySecretStore(),
        metadata_client=FakeMetadata(),
        auth_executor=ManualExecutor(),
        submit_job=jobs,
        runtime_inspector=RuntimeInspector(),
        codex_backend=FakeCodexBackend(),
    )
    service.prepare_runtime(prepare_args(service, "claude-agent-sdk"))
    jobs.run()
    record = create_connection(service, provider_id="claude-agent-sdk")
    args = {"connection_id": record["connection_id"], "expected_revision": record["revision"]}
    assert service.test_connection(args)["connected"] is True
    before = provider(service, "claude-agent-sdk")["runtime"]
    source = openai_source_package / "providers/claude/backend.py"
    source.write_bytes(source.read_bytes() + b"fixture upgraded Claude adapter behavior\n")
    changed = provider(service, "claude-agent-sdk")["runtime"]
    assert changed["catalog_revision"] != before["catalog_revision"]
    assert changed["state"] == "not_prepared"
    assert (
        service.list_models({"connection_id": record["connection_id"]})["catalog_state"] == "stale"
    )
    with pytest.raises(EngineUnavailableError):
        service.runtime.inspector.expected_runtime("claude-agent-sdk", before["resources"][0])
    service.prepare_runtime(
        prepare_args(service, "claude-agent-sdk", "prepare-upgraded-claude-runtime")
    )
    jobs.run(1)
    assert (
        service.list_models({"connection_id": record["connection_id"]})["catalog_state"] == "stale"
    )
    assert service.test_connection(args)["connected"] is True
    assert (
        service.list_models({"connection_id": record["connection_id"]})["catalog_state"] == "ready"
    )
    service.close()


def test_compatible_source_change_invalidates_runtime_and_model_verification(
    openai_source_package, tmp_path
):
    model_id = "compatible-fixture-model"

    class CompatibleMetadata:
        def fetch_openai_compatible(self, endpoint, api_key, *, auth_method, api_surface):
            assert endpoint == "https://127.0.0.1:9443/v1"
            assert api_key == "fixture-compatible-key"
            assert auth_method == "api_key"
            assert api_surface == "responses"
            return [
                model_descriptor(
                    model_id,
                    fetched_at="2026-09-02T00:00:00Z",
                    verified=False,
                )
            ]

    class CompatibleBackend:
        def __init__(self):
            self.calls = 0

        def verify_model(self, endpoint, api_key, selected_model, **options):
            assert endpoint == "https://127.0.0.1:9443/v1"
            assert api_key == "fixture-compatible-key"
            assert selected_model == model_id
            assert options["auth_method"] == "api_key"
            assert options["api_surface"] == "responses"
            self.calls += 1
            return model_descriptor(
                model_id,
                fetched_at="2026-09-02T00:00:01Z",
                verified=True,
            )

        def close(self):
            pass

    jobs = JobQueue()
    backend = CompatibleBackend()
    service = ProviderService(
        tmp_path / "runtime-state",
        secret_store=MemorySecretStore(),
        metadata_client=CompatibleMetadata(),
        submit_job=jobs,
        runtime_inspector=RuntimeInspector(),
        codex_backend=FakeCodexBackend(),
        openai_compatible_backend=backend,
    )
    record = service.set_connection(
        {
            "provider_id": "openai-compatible-api",
            "display_name": "Compatible fixture",
            "endpoint": "https://127.0.0.1:9443/v1",
            "auth_method": "api_key",
            "api_surface": "responses",
            "api_key": "fixture-compatible-key",
            "request_id": "create-compatible-runtime-fixture",
        }
    )["connection"]
    with service.store.transaction() as document:
        runtime = service.runtime._current("openai-compatible-api", document)
        runtime["state"] = "ready"
        document["runtimes"]["openai-compatible-api"] = runtime
    checked = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": record["revision"]}
    )["connection"]
    verify = {
        "connection_id": record["connection_id"],
        "expected_revision": record["revision"],
        "model_id": model_id,
        "confirmation": "send_test_prompt_and_may_charge",
        "request_id": "verify-compatible-runtime-fixture",
    }
    assert checked["catalog_state"] == "ready"
    assert service.verify_model(verify)["model"]["availability"] == "available"
    before = provider(service, "openai-compatible-api")["runtime"]
    source = openai_source_package / runtime_catalog_module._OPENAI_COMPATIBLE_SOURCES[0]
    source.write_bytes(source.read_bytes() + b"fixture upgraded compatible behavior\n")
    changed = provider(service, "openai-compatible-api")["runtime"]
    assert changed["catalog_revision"] != before["catalog_revision"]
    assert changed["state"] == "not_prepared"
    assert service.list_models({"connection_id": record["connection_id"]})["catalog_state"] == (
        "stale"
    )
    service.prepare_runtime(
        prepare_args(service, "openai-compatible-api", "prepare-upgraded-compatible-runtime")
    )
    jobs.run()
    assert (
        service.test_connection(
            {"connection_id": record["connection_id"], "expected_revision": record["revision"]}
        )["connected"]
        is True
    )
    refreshed = service.list_models({"connection_id": record["connection_id"]})
    assert refreshed["models"][0]["availability"] == "unverified"
    assert service.store.read()["catalogs"][record["connection_id"]]["verified_models"] == {}
    assert backend.calls == 1
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


def test_codex_runtime_uses_fixed_backend_catalog_and_explicit_job(runtime_setup):
    service, jobs, inspector, secrets = runtime_setup
    backend = service.codex_backend
    descriptor = provider(service, "codex-app-server")
    resource = descriptor["runtime"]["resources"][0]
    assert descriptor["auth_methods"] == ["chatgpt"]
    assert resource["source"] == "approved_download"
    assert resource["download_host"] == "github.com"
    args = prepare_args(service, "codex-app-server")
    response = service.prepare_runtime(args)
    assert service.prepare_runtime(args) == response
    assert backend.calls == inspector.calls == secrets.calls == []
    jobs.run()
    assert backend.calls == [("prepare", resource)]
    assert inspector.calls == []
    ready = provider(service, "codex-app-server")
    assert ready["runtime"]["state"] == "ready"
    assert ready["runtime"]["last_setup"]["state"] == "succeeded"
    assert service.list_connections()["connections"] == []
    load_contracts().validate_output("list_providers", service.list_providers())


def test_codex_runtime_failure_and_cancellation_never_publish_ready(runtime_setup):
    service, jobs, _, _ = runtime_setup
    backend = service.codex_backend
    service.prepare_runtime(prepare_args(service, "codex-app-server"))
    with pytest.raises(CancelledError):
        jobs.run(cancelled=True)
    assert backend.calls == []
    backend.error = RuntimeError("fixture-secret /private/codex-runtime")
    service.prepare_runtime(prepare_args(service, "codex-app-server", "retry-codex-prepare"))
    with pytest.raises(EngineUnavailableError, match="preparation failed"):
        jobs.run(1)
    assert provider(service, "codex-app-server")["runtime"]["state"] == "failed"
    assert "fixture-secret" not in service.store.path.read_text()


def test_codex_runtime_update_marks_previous_model_observations_stale(runtime_setup):
    service, jobs, _, _ = runtime_setup
    record = prepared_codex_connection(service)
    service.codex_backend.version = "2.0.0"
    service.prepare_runtime(prepare_args(service, "codex-app-server"))
    jobs.run()
    models = service.list_models({"connection_id": record["connection_id"]})
    assert models["catalog_state"] == "stale"
    assert models["models"][0]["availability"] == "unverified"
    assert service.list_connections()["connections"][0]["credential_present"] is True


@pytest.mark.parametrize("provider_id", ["openai-api", "anthropic-api", "ollama"])
@pytest.mark.parametrize("runtime_changed", [False, True])
def test_http_runtime_preparation_invalidates_only_changed_model_observations(
    runtime_setup, provider_id, runtime_changed
):
    service, jobs, inspector, secrets = runtime_setup
    service.metadata.models = [
        {**service.metadata.models[0], "availability": "available", "reason": None}
    ]
    service.prepare_runtime(prepare_args(service, provider_id))
    jobs.run()
    record = create_connection(service, provider_id=provider_id)
    result = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": record["revision"]}
    )
    assert result["connected"] is True
    codex_record = prepared_codex_connection(service)
    cached = service.store.read()["catalogs"][record["connection_id"]]
    secret_calls, metadata_calls = list(secrets.calls), list(service.metadata.calls)
    if runtime_changed:
        inspector.version = "2.0.0"
        assert provider(service, provider_id)["runtime"]["state"] == "not_prepared"
    service.prepare_runtime(prepare_args(service, provider_id, "repeat-runtime-prepare"))
    jobs.run(1)
    models = service.list_models({"connection_id": record["connection_id"]})
    assert models["catalog_state"] == ("stale" if runtime_changed else "ready")
    assert models["models"][0]["availability"] == ("unverified" if runtime_changed else "available")
    saved = service.store.read()
    assert saved["catalogs"][record["connection_id"]] == cached
    saved_connection = saved["connections"][record["connection_id"]]
    assert saved_connection["credential_present"] == record["credential_present"]
    assert saved_connection["auth_state"] == "authenticated"
    assert (
        service.list_models({"connection_id": codex_record["connection_id"]})["catalog_state"]
        == "ready"
    )
    expected_secret_reads = 0 if provider_id == "ollama" else 1
    assert secrets.calls[: len(secret_calls)] == secret_calls
    assert len(secrets.calls) == len(secret_calls) + expected_secret_reads
    assert all(call[0] == "get" for call in secrets.calls[len(secret_calls) :])
    assert service.metadata.calls == metadata_calls


@pytest.mark.parametrize("instance_id", [INSTANCE_ONE, INSTANCE_TWO])
def test_nonowner_job_observation_and_close_preserve_resident_preparation(
    runtime_setup, instance_id
):
    owner, jobs, _, _ = runtime_setup
    result = owner.prepare_runtime(prepare_args(owner))
    before = owner.store.path.read_bytes()
    observer = ProviderService(
        owner.root,
        recover=False,
        server_instance_id=instance_id,
        secret_store=MemorySecretStore(),
        auth_executor=ManualExecutor(),
    )
    observer.observe_job(result["job_id"], "cancelled")
    observer.close()
    assert owner.store.path.read_bytes() == before
    jobs.run()
    assert provider(owner)["runtime"]["state"] == "ready"
