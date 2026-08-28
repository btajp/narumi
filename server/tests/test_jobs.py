"""Unit tests for ``JobManager`` / ``JobProgress``."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from narumi.catalog import Catalog
from narumi.errors import BusyError, CancelledError, NarumiError, PolicyViolationError
from narumi_server.jobs import JobManager, JobProgress


@pytest.fixture
def catalog(tmp_path: Path):
    cat = Catalog(tmp_path / "narumi.db")
    yield cat
    cat.close()


def test_success_and_progress(catalog: Catalog):
    manager = JobManager(catalog)
    try:
        seen: dict[str, str] = {}

        def work(progress: JobProgress) -> dict:
            seen["job_id"] = progress.job_id
            progress("align", 0.25)
            progress("generate", 1.5)  # clamped
            return {"minutes_version": 3}

        job_id = manager.submit("regenerate", "m1", work)
        job = manager.wait(job_id, timeout=10)
        assert job["status"] == "succeeded"
        assert job["result"] == {"minutes_version": 3}
        assert job["progress"] == {"stage": "generate", "fraction": 1.0}
        assert job["kind"] == "regenerate" and job["meeting_id"] == "m1"
        assert seen["job_id"] == job_id
        assert manager.active_jobs() == []
        assert not manager.has_active("m1")
    finally:
        manager.shutdown()


def test_failures_are_persisted(catalog: Catalog):
    manager = JobManager(catalog)
    try:

        def policy(progress: JobProgress) -> dict:
            raise PolicyViolationError("nope", details={"provider": "x"})

        def crash(progress: JobProgress) -> dict:
            raise ValueError("boom")

        def wrong_type(progress: JobProgress) -> dict:
            return ["not", "a", "dict"]  # type: ignore[return-value]

        failed = manager.wait(manager.submit("export", "m1", policy), timeout=10)
        assert failed["status"] == "failed"
        assert failed["error"] == {
            "code": "policy_violation",
            "message": "nope",
            "details": {"provider": "x"},
        }
        crashed = manager.wait(manager.submit("process", None, crash), timeout=10)
        assert crashed["error"]["code"] == "internal"
        assert crashed["error"]["details"]["exception"] == "ValueError"
        assert "boom" in crashed["error"]["message"]
        bad = manager.wait(manager.submit("process", None, wrong_type), timeout=10)
        assert (
            bad["error"]["code"] == "internal"
            and "expected a JSON object" in bad["error"]["message"]
        )
    finally:
        manager.shutdown()


@pytest.mark.parametrize("exception_type", [ValueError, PolicyViolationError, CancelledError])
def test_provider_setup_never_persists_or_logs_upstream_exception_values(
    catalog: Catalog, caplog, exception_type
):
    secret = "fake-job-credential-843910"
    manager = JobManager(catalog)
    try:

        def fail(progress: JobProgress) -> dict:
            raise exception_type(secret)

        job = manager.wait(manager.submit("provider_setup", None, fail), timeout=10)
        assert job["status"] in {"failed", "cancelled"}
        assert secret not in json.dumps(job)
        assert secret not in caplog.text
    finally:
        manager.shutdown()


def test_has_active_and_shutdown_marks_unfinished(catalog: Catalog):
    manager = JobManager(catalog, max_workers=1)
    gate = threading.Event()

    def blocking(progress: JobProgress) -> dict:
        gate.wait(10)
        return {}

    first = manager.submit("process", "m1", blocking)
    second = manager.submit("process", "m2", blocking)  # queued behind the single worker
    assert manager.has_active("m1") and manager.has_active("m2")
    assert not manager.has_active("m3")
    assert set(manager.active_jobs()) == {first, second}
    # one meeting, one job: the guard lives in submit (under the lock), not only in handlers
    with pytest.raises(BusyError) as excinfo:
        manager.submit("regenerate", "m1", blocking)
    assert excinfo.value.details == {"meeting_id": "m1", "jobs": [first]}
    with pytest.raises(BusyError):
        manager.submit("export", "m2", blocking)  # queued counts as active too
    assert manager.submit("process", None, blocking)  # meeting-less jobs are never blocked
    with pytest.raises(TimeoutError):
        manager.wait(first, timeout=0.2)
    gate.set()
    assert manager.wait(first, timeout=10)["status"] == "succeeded"
    manager.shutdown()
    with pytest.raises(NarumiError):
        manager.submit("process", None, blocking)


def test_startup_marks_stale_jobs_failed(catalog: Catalog):
    stale = catalog.create_job("process", "m1")
    catalog.update_job(stale, status="running")
    manager = JobManager(catalog)
    try:
        job = catalog.get_job(stale)
        assert job is not None and job["status"] == "failed"
        assert "restarted" in job["error"]["message"]
        assert not manager.has_active("m1")
    finally:
        manager.shutdown()


def test_unknown_job(catalog: Catalog):
    manager = JobManager(catalog)
    try:
        with pytest.raises(NarumiError):
            manager.wait("job-000000000000", timeout=1)
    finally:
        manager.shutdown()
