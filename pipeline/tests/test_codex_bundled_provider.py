"""Provider readiness for an unavailable bundled Codex, without external calls."""

import pytest
from narumi.contracts.loader import load_contracts
from narumi.errors import EngineUnavailableError
from narumi.providers.service import ProviderService

from .provider_fakes import (
    FakeCodexBackend,
    FakeMetadata,
    FakeRuntimeInspector,
    JobQueue,
    ManualExecutor,
    MemorySecretStore,
)


class MissingBundledCodex(FakeCodexBackend):
    def resource(self):
        return {
            "resource_id": "codex-app-server-0-150-1",
            "display_name": "Bundled Codex App Server 0.150.1",
            "kind": "runtime",
            "version": None,
            "source": "bundled",
            "download_host": None,
            "sha256": None,
            "license": "Apache-2.0",
        }

    def prepare(self, resource, progress):
        self.calls.append(("prepare", resource))
        raise EngineUnavailableError("fixture bundle is unavailable")


def make_service(tmp_path):
    jobs = JobQueue()
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        metadata_client=FakeMetadata(),
        auth_executor=ManualExecutor(),
        submit_job=jobs,
        runtime_inspector=FakeRuntimeInspector(),
        codex_backend=MissingBundledCodex(),
    )
    return service, jobs


def test_missing_bundled_codex_reports_explicit_fail_closed_reason(tmp_path):
    service, _ = make_service(tmp_path)
    try:
        descriptor = next(
            item
            for item in service.list_providers()["providers"]
            if item["provider_id"] == "codex-app-server"
        )
        assert descriptor["availability"] == "not_prepared"
        assert descriptor["reason"] == "bundled_runtime_unavailable"
        assert descriptor["runtime"]["resources"][0]["source"] == "bundled"
        load_contracts().validate_output("list_providers", service.list_providers())
    finally:
        service.close()


def test_missing_bundled_codex_prepare_stays_unprepared_and_never_falls_back(tmp_path):
    service, jobs = make_service(tmp_path)
    try:
        descriptor = next(
            item
            for item in service.list_providers()["providers"]
            if item["provider_id"] == "codex-app-server"
        )
        runtime = descriptor["runtime"]
        service.prepare_runtime(
            {
                "provider_id": "codex-app-server",
                "resource_id": runtime["resources"][0]["resource_id"],
                "expected_catalog_revision": runtime["catalog_revision"],
                "action": "prepare",
                "request_id": "missing-bundled-codex",
            }
        )
        with pytest.raises(EngineUnavailableError):
            jobs.run()
        current = next(
            item
            for item in service.list_providers()["providers"]
            if item["provider_id"] == "codex-app-server"
        )
        assert current["runtime"]["state"] == "not_prepared"
        assert current["reason"] == "bundled_runtime_unavailable"
    finally:
        service.close()
