"""``ServerContext.close``: recording finalization first, then jobs; safe to call twice."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from conftest import TRANSPORT
from narumi.bundle import Bundle
from narumi.catalog import Catalog
from narumi.config import catalog_path
from narumi_server.context import build_context
from narumi_server.jobs import JobProgress


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
