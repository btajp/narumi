"""Unit tests for ``JobManager`` / ``JobProgress``."""

from __future__ import annotations

import asyncio
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
        assert job["processing_run_id"] is None
        assert job["result"] == {"minutes_version": 3, "processing_run_id": None}
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

        def nonserializable(progress: JobProgress) -> dict:
            result = {}
            result["cycle"] = result
            return result

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
        invalid = manager.wait(manager.submit("process", None, nonserializable), timeout=10)
        assert invalid["status"] == "failed"
        assert invalid["error"]["details"]["exception"] == "ValueError"
    finally:
        manager.shutdown()


def test_processing_run_is_attached_before_work_and_copied_to_success(catalog: Catalog):
    manager = JobManager(catalog)
    run_id = "run-" + "a" * 32
    try:

        def work(progress: JobProgress) -> dict:
            progress.attach_processing_run(run_id)
            running = catalog.get_job(progress.job_id)
            assert running is not None and running["processing_run_id"] == run_id
            return {"stages": ["minutes_ensemble"]}

        job = manager.wait(manager.submit("regenerate", "m1", work), timeout=10)
        assert job["status"] == "succeeded"
        assert job["processing_run_id"] == run_id
        assert job["result"] == {
            "stages": ["minutes_ensemble"],
            "processing_run_id": run_id,
        }
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


@pytest.mark.parametrize("exception_type", [asyncio.CancelledError, SystemExit])
@pytest.mark.parametrize("kind", ["process", "provider_setup"])
def test_abrupt_worker_exit_is_terminal_before_ownership_is_forgotten(
    catalog: Catalog, caplog, exception_type, kind
):
    manager = JobManager(catalog)
    release = threading.Event()
    secret = "fake-interrupted-job-value-9742"

    def interrupted(progress: JobProgress) -> dict:
        assert release.wait(10)
        raise exception_type(secret)

    job_id = manager.submit(kind, None, interrupted)
    future = manager._futures[job_id]
    try:
        release.set()
        with pytest.raises(exception_type):
            future.result(timeout=10)
        before_shutdown = catalog.get_job(job_id)
        assert before_shutdown["status"] == "failed"
        assert secret not in json.dumps(before_shutdown)
        assert secret not in caplog.text
        manager.shutdown()
        assert catalog.get_job(job_id) == before_shutdown
    finally:
        release.set()
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


@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("recover", [False, True])
def test_startup_recovers_stale_jobs_only_for_the_owner(catalog: Catalog, status, recover):
    stale = catalog.create_job("process", "m1")
    catalog.update_job(stale, status=status)
    before = catalog.get_job(stale)
    manager = JobManager(catalog, recover=recover)
    try:
        job = catalog.get_job(stale)
        if recover:
            assert job is not None and job["status"] == "failed"
            assert "restarted" in job["error"]["message"]
            assert not manager.has_active("m1")
        else:
            assert job == before
            assert manager.has_active("m1")
    finally:
        manager.shutdown()
    assert catalog.get_job(stale) == job


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("wait", [False, True])
def test_shutdown_only_fails_owned_work_dropped_from_the_queue(catalog: Catalog, recover, wait):
    manager = JobManager(catalog, recover=recover)
    entered = threading.Event()
    gate = threading.Event()
    queued_done = threading.Event()
    shutdown_errors: list[Exception] = []

    def blocking(progress: JobProgress) -> dict:
        entered.set()
        assert gate.wait(20)
        return {"completed": True}

    def never_started(progress: JobProgress) -> dict:
        raise AssertionError("shutdown must discard queued work")

    def close() -> None:
        try:
            manager.shutdown(wait=wait)
        except Exception as exc:
            shutdown_errors.append(exc)

    running = manager.submit("process", "owned-running", blocking)
    running_future = manager._futures[running]
    closer = threading.Thread(target=close)
    try:
        assert entered.wait(10)
        queued = manager.submit("process", "owned-queued", never_started)
        queued_future = manager._futures[queued]
        queued_future.add_done_callback(lambda _: queued_done.set())
        foreign = {}
        for status in ("queued", "running", "succeeded"):
            job_id = catalog.create_job("process", f"foreign-{status}")
            catalog.update_job(job_id, status=status)
            foreign[job_id] = catalog.get_job(job_id)

        closer.start()
        assert queued_done.wait(10)
        assert queued_future.cancelled()
        if wait:
            assert closer.is_alive()
        else:
            closer.join(timeout=10)
            assert not closer.is_alive()
            assert catalog.get_job(running)["status"] == "running"
        assert {job_id: catalog.get_job(job_id) for job_id in foreign} == foreign

        gate.set()
        closer.join(timeout=10)
        assert not closer.is_alive()
        assert not shutdown_errors
        completed = manager.wait(running, timeout=10)
        assert completed["status"] == "succeeded"
        assert completed["result"] == {"completed": True, "processing_run_id": None}
        dropped = catalog.get_job(queued)
        assert dropped["status"] == "failed"
        assert "shut down" in dropped["error"]["message"]
        assert {job_id: catalog.get_job(job_id) for job_id in foreign} == foreign
    finally:
        gate.set()
        if closer.ident is not None:
            closer.join(timeout=10)
        running_future.result(timeout=10)
        manager.shutdown()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_cancel_does_not_reconcile_a_job_owned_by_another_manager(catalog: Catalog, status):
    manager = JobManager(catalog, recover=False)
    foreign = catalog.create_job("process", "foreign")
    catalog.update_job(foreign, status=status)
    before = catalog.get_job(foreign)
    try:
        with pytest.raises(BusyError, match="another job manager"):
            manager.cancel(foreign)
        assert catalog.get_job(foreign) == before
    finally:
        manager.shutdown()


def test_unknown_job(catalog: Catalog):
    manager = JobManager(catalog)
    try:
        with pytest.raises(NarumiError):
            manager.wait("job-000000000000", timeout=1)
    finally:
        manager.shutdown()
