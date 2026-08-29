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
from narumi.providers import runtime as runtime_module
from narumi.providers import runtime_catalog as runtime_catalog_module
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
    for relative_path in runtime_catalog_module._OPENAI_AUDIO_SOURCES:
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


@pytest.mark.parametrize("provider_id", ["claude-agent-sdk", "openai-api"])
def test_missing_dependency_remains_not_prepared_without_installer(
    tmp_path, monkeypatch, provider_id
):
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
    for index, provider_id in enumerate(("openai-api", "anthropic-api", "ollama")):
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
    assert provider(service)["runtime"]["resources"][0]["version"] is None
    service.close()


@pytest.mark.parametrize(
    "provider_id", ["openai-api", "anthropic-api", "ollama", "claude-agent-sdk"]
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
    assert resource["version"] == "1.2.3"
    assert resource["sha256"] is None
    service.prepare_runtime(prepare_args(service, provider_id))
    if provider_id == "claude-agent-sdk":
        jobs.run()
        result = provider(service, provider_id)
        assert result["runtime"]["state"] == "ready"
        assert result["availability"] == "unverified"
        assert result["reason"] == "sdk_execution_isolation_unverified"
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


@pytest.mark.parametrize("relative_path", runtime_catalog_module._OPENAI_AUDIO_SOURCES)
def test_each_audio_source_changes_only_openai_runtime_identity(
    openai_source_package, relative_path
):
    inspector = RuntimeInspector()
    before = {
        provider_id: inspector.resource(provider_id)
        for provider_id in runtime_catalog_module.RESOURCES
    }
    source = openai_source_package / relative_path
    source.write_bytes(source.read_bytes() + b"fixture changed audio behavior\n")
    after = {
        provider_id: inspector.resource(provider_id)
        for provider_id in runtime_catalog_module.RESOURCES
    }
    assert before["openai-api"]["sha256"] is not None
    assert after["openai-api"]["sha256"] != before["openai-api"]["sha256"]
    assert inspector.catalog_revision(after["openai-api"]) != inspector.catalog_revision(
        before["openai-api"]
    )
    legacy_digest = hashlib.sha256(b"fixture metadata\nfixture record").hexdigest()
    for provider_id in ("anthropic-api", "ollama", "claude-agent-sdk"):
        assert after[provider_id] == before[provider_id]
        assert after[provider_id]["sha256"] == legacy_digest


def test_openai_runtime_identity_ignores_unlisted_files_and_directory_bookkeeping(
    openai_source_package, monkeypatch
):
    inspector = RuntimeInspector()
    before = inspector.resource("openai-api")
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
    assert inspector.resource("openai-api") == before


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
def test_unsafe_audio_source_cannot_prepare_openai_runtime(
    openai_source_package, tmp_path, monkeypatch, failure
):
    source = openai_source_package / runtime_catalog_module._OPENAI_AUDIO_SOURCES[0]
    inspector = RuntimeInspector()
    other_provider = inspector.resource("anthropic-api")
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
    resource = inspector.resource("openai-api")
    assert resource["sha256"] is None
    with pytest.raises(EngineUnavailableError, match="runtime distribution metadata is incomplete"):
        inspector.prepare(
            tmp_path / "runtime-state", "openai-api", resource, FakeProgress("fixture-job")
        )
    assert inspector.resource("anthropic-api") == other_provider
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
    assert secrets.calls == secret_calls
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
