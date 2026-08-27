"""Job cancellation: ``JobManager.cancel``, the ``cancel_job`` tool and status restoration."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

import pytest
from conftest import PerCallClient, call, make_recorded_bundle, wait_job, write_fake_minutes
from narumi.bundle import Bundle
from narumi.catalog import Catalog
from narumi.errors import BusyError, CancelledError, NotFoundError
from narumi.pipeline import ProcessResult, process_meeting
from narumi_server.context import ServerContext
from narumi_server.jobs import JobManager, JobProgress

MEETING_A = "20260827T010000Z-0000000a"


def rid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def catalog(tmp_path: Path):
    cat = Catalog(tmp_path / "narumi.db")
    yield cat
    cat.close()


# ---------------------------------------------------------------------------- JobManager.cancel
def test_cancel_queued_job_is_immediate(catalog: Catalog):
    manager = JobManager(catalog, max_workers=1)
    gate = threading.Event()
    entered = threading.Event()

    def blocking(progress: JobProgress) -> dict:
        entered.set()
        assert gate.wait(10)
        return {}

    try:
        running = manager.submit("process", "m1", blocking)
        assert entered.wait(10)
        queued = manager.submit("process", "m2", blocking)  # waits behind the single worker
        job = manager.cancel(queued)
        assert job["status"] == "cancelled"
        assert job["error"] == {"code": "cancelled", "message": "cancelled by cancel_job"}
        # cancelling an already-cancelled job is a no-op returning its state
        assert manager.cancel(queued)["status"] == "cancelled"
        # wait() on a job cancelled while queued returns its row instead of raising
        assert manager.wait(queued, timeout=5)["status"] == "cancelled"
        gate.set()
        assert manager.wait(running, timeout=10)["status"] == "succeeded"
        with pytest.raises(BusyError) as exc:  # finished jobs cannot be cancelled any more
            manager.cancel(running)
        assert exc.value.details == {"job_id": running, "status": "succeeded"}
        with pytest.raises(NotFoundError):
            manager.cancel("job-000000000000")
    finally:
        gate.set()
        manager.shutdown()


def test_cancel_running_job_is_cooperative(catalog: Catalog):
    manager = JobManager(catalog, max_workers=1)
    gate = threading.Event()
    entered = threading.Event()

    def work(progress: JobProgress) -> dict:
        progress("align", 0.1)
        entered.set()
        assert gate.wait(10)
        progress("integrate", 0.5)  # raises CancelledError once the flag is set
        raise AssertionError("the job must stop at the progress checkpoint")

    try:
        job_id = manager.submit("regenerate", "m1", work)
        assert entered.wait(10)
        flagged = manager.cancel(job_id)
        assert flagged["status"] == "running"  # cooperative: not cancelled yet
        gate.set()
        job = manager.wait(job_id, timeout=10)
        assert job["status"] == "cancelled"
        assert job["error"]["code"] == "cancelled"
        assert job["error"]["details"] == {"job_id": job_id, "stage": "integrate"}
        assert not manager.has_active("m1")
    finally:
        gate.set()
        manager.shutdown()


def test_progress_reports_cancelled_flag(catalog: Catalog):
    event = threading.Event()
    progress = JobProgress(catalog, "job-0000000000000001", event)
    assert progress.cancelled is False
    event.set()
    assert progress.cancelled is True
    with pytest.raises(CancelledError):
        progress("stage", 0.5)


# ---------------------------------------------------------------------------- pipeline restore
def test_run_steps_restores_status_on_cancellation(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    """``narumi.pipeline`` puts ``manifest.status`` back instead of marking ``failed``."""
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_A)

    def cancelling_preprocess(bundle: Bundle, *, force: bool = False) -> list:
        assert bundle.manifest.status == "processing"  # the run had started
        raise CancelledError("cancelled by cancel_job")

    monkeypatch.setattr("narumi.pipeline.run_preprocess", cancelling_preprocess)
    with pytest.raises(CancelledError):
        process_meeting(bundle)
    assert Bundle.find(ctx.meetings_root, MEETING_A).manifest.status == "recorded"


# ---------------------------------------------------------------------------- cancel_job tool
async def test_cancel_job_tool_restores_manifest_status(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_A)
    write_fake_minutes(bundle)
    ctx.catalog.upsert_meeting(bundle)
    entered = threading.Event()
    gate = threading.Event()

    def blocking_refresh(
        bundle,
        *,
        force=False,
        progress=None,
        reason="regenerate",
        job_id=None,
        gaia_client_factory=None,
    ):
        progress("align", 0.1)
        entered.set()
        assert gate.wait(10)
        progress("integrate", 0.5)  # cancellation checkpoint
        return ProcessResult(meeting_id=bundle.meeting_id, minutes_version=1)

    monkeypatch.setattr("narumi.pipeline.refresh_meeting", blocking_refresh)
    started = await call(client, "regenerate", {"meeting_id": MEETING_A, "request_id": rid()})
    job_id = started["job_id"]
    assert entered.wait(10)
    assert Bundle.find(ctx.meetings_root, MEETING_A).manifest.status == "processing"

    key = rid()
    cancelled = await call(client, "cancel_job", {"job_id": job_id, "request_id": key})
    assert cancelled["job"]["job_id"] == job_id
    assert cancelled["job"]["status"] in {"running", "cancelled"}
    # replaying the same request_id returns the original response without acting again
    assert await call(client, "cancel_job", {"job_id": job_id, "request_id": key}) == cancelled
    gate.set()

    job = await wait_job(ctx, job_id)
    assert job["status"] == "cancelled"
    assert job["error"]["code"] == "cancelled"
    status = await call(client, "get_job_status", {"job_id": job_id})
    assert status["job"]["status"] == "cancelled"

    # the manifest went back to its pre-run status — not failed — in bundle and catalog
    manifest = Bundle.find(ctx.meetings_root, MEETING_A).manifest
    assert manifest.status == "recorded"
    row = ctx.catalog.get_meeting_row(MEETING_A)
    assert row is not None and row["status"] == "recorded"
    # completed outputs stay; the next regenerate is not blocked
    assert manifest.latest_minutes_version == 1
    assert not ctx.jobs.has_active(MEETING_A)

    # a new request_id on the now-cancelled job is a no-op returning its state
    again = await call(client, "cancel_job", {"job_id": job_id, "request_id": rid()})
    assert again["job"]["status"] == "cancelled"


async def test_cancel_job_tool_errors(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    unknown = await call(client, "cancel_job", {"job_id": "job-000000000000", "request_id": rid()})
    assert unknown["error"]["code"] == "not_found"

    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_A)
    write_fake_minutes(bundle)
    ctx.catalog.upsert_meeting(bundle)

    def instant(
        bundle,
        *,
        force=False,
        progress=None,
        reason="regenerate",
        job_id=None,
        gaia_client_factory=None,
    ):
        return ProcessResult(meeting_id=bundle.meeting_id, minutes_version=1)

    monkeypatch.setattr("narumi.pipeline.refresh_meeting", instant)
    regen = await call(client, "regenerate", {"meeting_id": MEETING_A, "request_id": rid()})
    assert (await wait_job(ctx, regen["job_id"]))["status"] == "succeeded"
    finished = await call(client, "cancel_job", {"job_id": regen["job_id"], "request_id": rid()})
    assert finished["error"]["code"] == "busy"
    assert finished["error"]["details"]["status"] == "succeeded"
