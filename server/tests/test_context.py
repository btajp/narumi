"""``ServerContext.close``: recording finalization first, then jobs; safe to call twice."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

import pytest
from conftest import TRANSPORT
from narumi.bundle import Bundle
from narumi.catalog import Catalog
from narumi.config import catalog_path
from narumi_server.context import build_context
from narumi_server.jobs import JobProgress

from pipeline.tests.provider_fakes import (
    FakeCodexBackend,
    MemorySecretStore,
    prepared_codex_connection,
)


def test_close_finalizes_recording_before_waiting_for_jobs(home: Path):
    """A long job (a transcription can take minutes) must not delay the recording finalization:
    narumi.app SIGKILLs the server after its stop timeout, and the manifest update is the one
    thing that cannot be redone."""
    ctx = build_context(home, transports=[TRANSPORT], validate_output=True)
    gate = threading.Event()

    def blocking(progress: JobProgress) -> dict:
        assert gate.wait(30)
        return {}

    job_id = ctx.jobs.submit("process", "some-other-meeting", blocking)
    started = ctx.handlers["start_recording"](ctx, {"request_id": str(uuid.uuid4())})
    meeting_id = started["meeting_id"]

    closer = threading.Thread(target=ctx.close, name="close")
    closer.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            manifest = Bundle.find(ctx.meetings_root, meeting_id).manifest
            if manifest.status == "recorded":
                break
            time.sleep(0.05)
        assert manifest.status == "recorded", manifest.status
        assert manifest.recording.stopped_at is not None
        assert closer.is_alive(), "close() must still be waiting for the job at this point"
        assert not ctx.recorder.is_active
    finally:
        gate.set()
        closer.join(timeout=30)
    assert not closer.is_alive()
    ctx.close()  # idempotent: nothing to finalize, executor already down, catalog closed

    catalog = Catalog(catalog_path(home))  # ctx.catalog is closed; read back through a new one
    try:
        job = catalog.get_job(job_id)
        assert job is not None and job["status"] == "succeeded"
    finally:
        catalog.close()


def test_provider_shutdown_failure_does_not_skip_jobs_or_catalog(home: Path, monkeypatch):
    closed = []

    class BrokenProvider:
        def close(self):
            closed.append("providers")
            raise RuntimeError("fixture provider shutdown failure")

    ctx = build_context(home, provider_service=BrokenProvider())
    shutdown_jobs = ctx.jobs.shutdown
    close_catalog = ctx.catalog.close

    def jobs(**kwargs):
        closed.append("jobs")
        shutdown_jobs(**kwargs)

    def catalog():
        closed.append("catalog")
        close_catalog()

    monkeypatch.setattr(ctx.jobs, "shutdown", jobs)
    monkeypatch.setattr(ctx.catalog, "close", catalog)
    with pytest.raises(RuntimeError, match="fixture provider shutdown failure"):
        ctx.close()
    ctx.close()
    assert closed == ["providers", "jobs", "catalog"]


@pytest.mark.parametrize("transport", ["stdio", "in-process"])
@pytest.mark.parametrize("same_instance", [False, True])
@pytest.mark.parametrize("lease", ["authentication", "generation"])
def test_nonresident_context_does_not_reconcile_an_owner_provider_lease(
    home: Path, transport: str, same_instance: bool, lease: str
):
    secrets = MemorySecretStore()
    owner = build_context(
        home,
        transports=["streamable-http"],
        provider_secret_store=secrets,
        provider_codex_backend=FakeCodexBackend(),
    )
    connection = prepared_codex_connection(owner.providers)
    connection_id = connection["connection_id"]
    with owner.providers.store.transaction() as document:
        if lease == "authentication":
            operation_id = "auth-" + uuid.uuid4().hex
            document["auth_operations"][operation_id] = {
                "operation_id": operation_id,
                "connection_id": connection_id,
                "server_instance_id": owner.server_instance_id,
                "state": "pending",
                "authorization_url": None,
                "user_code": None,
            }
            document["connections"][connection_id]["active_auth"] = {
                "operation_id": operation_id,
                "state": "pending",
            }
        else:
            document["checks"]["codex-app-server"] = {
                "token": "fixture-generation-lease",
                "server_instance_id": owner.server_instance_id,
                "connection_id": connection_id,
                "kind": "generation",
            }
    expected = owner.providers.store.read()
    observer = None
    try:
        observer = build_context(
            home,
            transports=[transport],
            provider_secret_store=secrets,
            provider_codex_backend=FakeCodexBackend(),
            server_instance_id=owner.server_instance_id if same_instance else None,
        )
        assert owner.providers.store.read() == expected
        observer.close()
        assert owner.providers.store.read() == expected
    finally:
        if observer is not None:
            observer.close()
        owner.close()


@pytest.mark.parametrize("transport", [None, "stdio", "in-process"])
@pytest.mark.parametrize("same_instance", [False, True])
def test_nonresident_context_keeps_resident_running_and_queued_jobs(
    home: Path, transport: str | None, same_instance: bool
):
    owner = build_context(
        home,
        transports=["streamable-http"],
        provider_secret_store=MemorySecretStore(),
        provider_codex_backend=FakeCodexBackend(),
    )
    entered = threading.Event()
    gate = threading.Event()
    observer = None

    def work(progress: JobProgress) -> dict:
        entered.set()
        assert gate.wait(20)
        return {"completed": True}

    try:
        running = owner.jobs.submit("regenerate", "resident-running", work)
        assert entered.wait(10)
        queued = owner.jobs.submit("regenerate", "resident-queued", work)
        before = {job_id: owner.catalog.get_job(job_id) for job_id in (running, queued)}
        assert before[running]["status"] == "running"
        assert before[queued]["status"] == "queued"
        observer = build_context(
            home,
            transports=[] if transport is None else [transport],
            provider_secret_store=MemorySecretStore(),
            provider_codex_backend=FakeCodexBackend(),
            server_instance_id=owner.server_instance_id if same_instance else None,
        )
        assert {job_id: owner.catalog.get_job(job_id) for job_id in before} == before
        assert observer.jobs.has_active("resident-running")
        assert observer.jobs.has_active("resident-queued")
        observer.close()
        assert {job_id: owner.catalog.get_job(job_id) for job_id in before} == before
        gate.set()
        for job_id in before:
            assert owner.jobs.wait(job_id, timeout=10)["status"] == "succeeded"
    finally:
        gate.set()
        if observer is not None:
            observer.close()
        owner.close()


@pytest.mark.parametrize("transport", ["streamable-http", "stdio"])
def test_server_owner_context_recovers_abandoned_jobs(home: Path, transport: str):
    previous = Catalog(catalog_path(home))
    abandoned = []
    try:
        for status in ("queued", "running"):
            job_id = previous.create_job("process", f"abandoned-{status}")
            previous.update_job(job_id, status=status)
            abandoned.append(job_id)
    finally:
        previous.close()
    owner = build_context(
        home,
        transports=[transport],
        recover_jobs=True,
        provider_secret_store=MemorySecretStore(),
        provider_codex_backend=FakeCodexBackend(),
    )
    try:
        for job_id in abandoned:
            job = owner.catalog.get_job(job_id)
            assert job["status"] == "failed"
            assert "restarted" in job["error"]["message"]
    finally:
        owner.close()
