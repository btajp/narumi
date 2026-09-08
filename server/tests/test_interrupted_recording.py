"""Capture subprocess exit remains visible until the public stop call finalizes it."""

from __future__ import annotations

import json
import uuid

import anyio
import pytest
from conftest import PerCallClient, call
from narumi.bundle import Bundle
from narumi_server.context import ServerContext


@pytest.mark.parametrize("recorder_error", [None, "capture_failed"])
async def test_exited_recorder_status_waits_for_public_stop(
    client: PerCallClient,
    ctx: ServerContext,
    monkeypatch: pytest.MonkeyPatch,
    recorder_error: str | None,
):
    monkeypatch.setenv("FAKE_RECORDER_AUTO_STOP_AFTER", "0.1")
    if recorder_error:
        monkeypatch.setenv("FAKE_RECORDER_ERROR_AFTER_STOP", recorder_error)
    started = await call(client, "start_recording", {"request_id": str(uuid.uuid4())})
    meeting_id = started["meeting_id"]
    with anyio.fail_after(5):
        while ctx.recorder.process_alive:
            await anyio.sleep(0.01)

    for _ in range(2):
        status = await call(client, "get_recording_status")
        assert status["active"] is True
        assert status["recorder_alive"] is False
        assert "elapsed_sec" not in status
        assert status["meeting_id"] == meeting_id
        assert status["started_at"] == started["started_at"]
        bundle = Bundle.find(ctx.meetings_root, meeting_id)
        assert bundle.manifest.status == "recording"
        assert bundle.manifest.recording.stopped_at is None
        assert ctx.catalog.list_jobs(meeting_id=meeting_id) == []
    busy = await call(client, "start_recording", {"request_id": str(uuid.uuid4())})
    assert busy["error"]["code"] == "busy"
    captured = json.loads(bundle.abspath("tracks/recorder.json").read_text())

    jobs: list[str] = []
    job_id = "job-" + uuid.uuid4().hex

    def enqueue_saved_meeting(context: ServerContext, identifier: str) -> str:
        assert context is ctx
        saved = Bundle.find(context.meetings_root, identifier)
        assert saved.manifest.status == "recorded"
        assert saved.manifest.recording.stopped_at is not None
        jobs.append(identifier)
        return job_id

    monkeypatch.setattr("narumi_server.handlers.recording.enqueue_process", enqueue_saved_meeting)
    stopped = await call(client, "stop_recording", {"request_id": str(uuid.uuid4())})
    assert stopped["meeting_id"] == meeting_id
    assert stopped["duration_sec"] == captured["duration_sec"]
    assert stopped["job_id"] == job_id
    assert jobs == [meeting_id]
    assert stopped["tracks"]["mic"]["bytes"] > 0
    assert stopped["tracks"]["system"]["bytes"] > 0
    if recorder_error:
        assert stopped["recorder_error"]["details"]["recorder_code"] == recorder_error
    else:
        assert "recorder_error" not in stopped
    assert await call(client, "get_recording_status") == {"active": False}


async def test_running_recorder_status_includes_elapsed_time(client: PerCallClient):
    await call(client, "start_recording", {"request_id": str(uuid.uuid4())})
    status = await call(client, "get_recording_status")
    assert status["active"] is True
    assert status["recorder_alive"] is True
    assert status["elapsed_sec"] >= 0
    await call(
        client,
        "stop_recording",
        {"request_id": str(uuid.uuid4()), "auto_process": False, "prepare_playback": False},
    )
