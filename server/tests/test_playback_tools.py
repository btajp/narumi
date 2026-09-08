"""Recording playback and display selection through the public MCP tools."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import (
    PerCallClient,
    call,
    fake_process_meeting,
    make_recorded_bundle,
    wait_job,
    write_fake_minutes,
)
from narumi.bundle import Bundle, StageResult, TrackRecord, sha256_file
from narumi.errors import CancelledError, EngineUnavailableError
from narumi_server.context import ServerContext

MEETING_ID = "20260827T010000Z-0000000a"
PLAYBACK_KEY = "recording/playback"
PLAYBACK_PATH = "playback/recording.mp4"


def rid() -> str:
    return str(uuid.uuid4())


def recorded_bundle(ctx: ServerContext, *, scope: str | None = None) -> Bundle:
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_ID, scope=scope)
    for name, suffix in (("screen", "mp4"), ("mic", "wav"), ("system", "wav")):
        path = bundle.dir("tracks") / f"{name}.{suffix}"
        path.write_bytes(f"original {name}".encode())
        bundle.manifest.recording.tracks[name] = TrackRecord(
            path=bundle.relpath(path),
            sha256=sha256_file(path),
            bytes=path.stat().st_size,
            duration_sec=1,
        )
    bundle.save()
    ctx.catalog.upsert_meeting(bundle)
    return bundle


def fake_playback(
    bundle: Bundle, *, should_cancel: Callable[[], bool] | None = None
) -> StageResult:
    if should_cancel is not None and should_cancel():
        raise CancelledError("playback cancelled")
    return bundle.run_stage(
        PLAYBACK_KEY,
        inputs={
            f"tracks/{name}": sha256_file(bundle.abspath(track.path))
            for name, track in bundle.manifest.recording.tracks.items()
            if not track.discarded
        },
        params={"fake": True},
        producer=("fake-playback", "1"),
        output=PLAYBACK_PATH,
        fn=lambda output: output.write_bytes(b"fake playback with mixed audio"),
    )


@pytest.mark.parametrize("status", ["recorded", "ready", "failed"])
async def test_prepare_recording_preserves_meeting_status_and_minutes(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch, status: str
):
    bundle = recorded_bundle(ctx)
    write_fake_minutes(bundle)
    bundle.manifest.status = status
    bundle.save()
    ctx.catalog.upsert_meeting(bundle)
    monkeypatch.setattr("narumi.playback.run_playback", fake_playback)
    before = await call(client, "get_meeting", {"meeting_id": MEETING_ID})
    assert before["playback"] is None
    args = {"meeting_id": MEETING_ID, "request_id": rid()}
    prepared = await call(client, "prepare_recording", args)
    job = await wait_job(ctx, prepared["job_id"])
    assert job["status"] == "succeeded"
    assert job["kind"] == "playback"
    assert await call(client, "prepare_recording", args) == prepared
    assert len(ctx.catalog.list_jobs(meeting_id=MEETING_ID)) == 1
    after = await call(client, "get_meeting", {"meeting_id": MEETING_ID})
    path = bundle.abspath(PLAYBACK_PATH)
    assert after["playback"] == {
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }
    assert after["meeting"]["status"] == status
    assert after["latest_minutes"] == before["latest_minutes"]
    assert after["minutes_versions"] == before["minutes_versions"]
    assert ctx.catalog.get_meeting_row(MEETING_ID)["status"] == status


async def test_prepare_recording_checks_scope_and_busy_before_submitting(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    recorded_bundle(ctx, scope="private")
    entered, gate = threading.Event(), threading.Event()

    def blocked(bundle, *, should_cancel=None):
        entered.set()
        assert gate.wait(10)
        return fake_playback(bundle, should_cancel=should_cancel)

    monkeypatch.setattr("narumi.playback.run_playback", blocked)
    args = {"meeting_id": MEETING_ID, "request_id": rid()}
    denied = await call(client, "prepare_recording", args)
    assert denied["error"]["code"] == "scope_denied"
    assert ctx.catalog.list_jobs(meeting_id=MEETING_ID) == []
    prepared = await call(client, "prepare_recording", {**args, "scope": "private"})
    try:
        assert entered.wait(10)
        busy = await call(
            client, "prepare_recording", {**args, "scope": "private", "request_id": rid()}
        )
        assert busy["error"]["code"] == "busy"
        assert len(ctx.catalog.list_jobs(meeting_id=MEETING_ID)) == 1
    finally:
        gate.set()
    assert (await wait_job(ctx, prepared["job_id"]))["status"] == "succeeded"


async def test_prepare_recording_cancellation_reaches_playback_and_preserves_originals(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    bundle = recorded_bundle(ctx)
    entered, gate = threading.Event(), threading.Event()

    def blocked(bundle, *, should_cancel=None):
        assert should_cancel is not None
        assert not should_cancel()
        entered.set()
        assert gate.wait(10)
        assert should_cancel()
        raise CancelledError("playback cancelled")

    monkeypatch.setattr("narumi.playback.run_playback", blocked)
    prepared = await call(
        client, "prepare_recording", {"meeting_id": MEETING_ID, "request_id": rid()}
    )
    try:
        assert entered.wait(10)
        await call(client, "cancel_job", {"job_id": prepared["job_id"], "request_id": rid()})
    finally:
        gate.set()
    assert (await wait_job(ctx, prepared["job_id"]))["status"] == "cancelled"
    fresh = Bundle.find(ctx.meetings_root, MEETING_ID)
    assert fresh.manifest.status == "recorded"
    assert fresh.manifest.recording.tracks == bundle.manifest.recording.tracks
    assert all(fresh.abspath(t.path).is_file() for t in fresh.manifest.recording.tracks.values())
    assert not fresh.abspath(PLAYBACK_PATH).exists()


async def test_prepare_failure_keeps_originals_and_existing_meeting_status(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    bundle = recorded_bundle(ctx)
    bundle.manifest.status = "ready"
    bundle.save()

    def fail(*args, **kwargs):
        raise EngineUnavailableError("ffmpeg unavailable")

    monkeypatch.setattr("narumi.playback.run_playback", fail)
    prepared = await call(
        client, "prepare_recording", {"meeting_id": MEETING_ID, "request_id": rid()}
    )
    job = await wait_job(ctx, prepared["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "engine_unavailable"
    fresh = Bundle.find(ctx.meetings_root, MEETING_ID)
    assert fresh.manifest.status == "ready"
    assert fresh.manifest.recording.tracks == bundle.manifest.recording.tracks
    assert all(fresh.abspath(t.path).is_file() for t in fresh.manifest.recording.tracks.values())
    assert fresh.artifact(PLAYBACK_KEY) is None


@pytest.mark.parametrize("auto_process", [True, False])
async def test_stop_prepares_playback_before_optional_minutes(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch, auto_process: bool
):
    calls = []

    def playback(bundle, *, should_cancel=None):
        calls.append("playback")
        assert all(
            bundle.abspath(t.path).is_file() for t in bundle.manifest.recording.tracks.values()
        )
        return fake_playback(bundle, should_cancel=should_cancel)

    def process(bundle, **kwargs):
        calls.append("minutes")
        assert bundle.abspath(PLAYBACK_PATH).is_file()
        return fake_process_meeting(bundle, **kwargs)

    monkeypatch.setattr("narumi.playback.run_playback", playback)
    monkeypatch.setattr("narumi.pipeline.process_meeting", process)
    started = await call(client, "start_recording", {"request_id": rid()})
    args = {"auto_process": auto_process, "request_id": rid()}
    stopped = await call(client, "stop_recording", args)
    job = await wait_job(ctx, stopped["job_id"])
    assert job["status"] == "succeeded"
    assert job["kind"] == ("process" if auto_process else "playback")
    assert calls == (["playback", "minutes"] if auto_process else ["playback"])
    assert await call(client, "stop_recording", args) == stopped
    assert len(ctx.catalog.list_jobs(meeting_id=started["meeting_id"])) == 1
    detail = await call(client, "get_meeting", {"meeting_id": started["meeting_id"]})
    assert detail["playback"] is not None
    assert bool(detail["minutes_versions"]) is auto_process


async def test_minutes_failure_keeps_successful_playback(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("narumi.playback.run_playback", fake_playback)

    def fail(bundle, **kwargs):
        assert bundle.abspath(PLAYBACK_PATH).is_file()
        raise EngineUnavailableError("transcription unavailable")

    monkeypatch.setattr("narumi.pipeline.process_meeting", fail)
    started = await call(client, "start_recording", {"request_id": rid()})
    stopped = await call(client, "stop_recording", {"request_id": rid()})
    job = await wait_job(ctx, stopped["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "engine_unavailable"
    detail = await call(client, "get_meeting", {"meeting_id": started["meeting_id"]})
    assert detail["meeting"]["status"] == "failed"
    assert Path(detail["playback"]["path"]).is_file()
    assert PLAYBACK_KEY in detail["artifacts"]


async def test_stop_playback_failure_keeps_finalized_tracks_and_skips_minutes(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    def fail(*args, **kwargs):
        raise EngineUnavailableError("ffmpeg unavailable")

    def unexpected(*args, **kwargs):
        raise AssertionError("minutes must wait for successful playback")

    monkeypatch.setattr("narumi.playback.run_playback", fail)
    monkeypatch.setattr("narumi.pipeline.process_meeting", unexpected)
    started = await call(client, "start_recording", {"request_id": rid()})
    stopped = await call(client, "stop_recording", {"request_id": rid()})
    job = await wait_job(ctx, stopped["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "engine_unavailable"
    bundle = Bundle.find(ctx.meetings_root, started["meeting_id"])
    assert bundle.manifest.recording.stopped_at is not None
    assert all(
        sha256_file(bundle.abspath(track.path)) == track.sha256
        for track in bundle.manifest.recording.tracks.values()
    )
    assert bundle.artifact(PLAYBACK_KEY) is None
    assert bundle.manifest.minutes_versions == []


async def test_stop_without_retained_screen_does_not_prepare_playback(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    def unexpected(*args, **kwargs):
        raise AssertionError("discarded video must not be prepared")

    monkeypatch.setattr("narumi.playback.run_playback", unexpected)
    started = await call(client, "start_recording", {"request_id": rid()})
    stopped = await call(
        client,
        "stop_recording",
        {"request_id": rid(), "auto_process": False, "discard_video": True},
    )
    assert "job_id" not in stopped
    assert ctx.catalog.list_jobs(meeting_id=started["meeting_id"]) == []
    detail = await call(client, "get_meeting", {"meeting_id": started["meeting_id"]})
    assert detail["playback"] is None


@pytest.mark.parametrize("track", ["screen", "mic", "system"])
async def test_discard_original_removes_derived_playback(
    client: PerCallClient, ctx: ServerContext, track: str
):
    bundle = recorded_bundle(ctx)
    if track != "screen":
        bundle.run_stage(
            f"transcripts/own-{track}",
            inputs={},
            params={},
            producer=("fake", "1"),
            output=f"transcripts/own-{track}.json",
            fn=lambda output: output.write_text("{}"),
        )
    fake_playback(bundle)
    discarded = await call(
        client,
        "discard_tracks",
        {"meeting_id": MEETING_ID, "tracks": [track], "request_id": rid()},
    )
    assert discarded["tracks"][track]["discarded"] is True
    fresh = Bundle.find(ctx.meetings_root, MEETING_ID)
    assert not fresh.abspath(PLAYBACK_PATH).exists()
    assert fresh.artifact(PLAYBACK_KEY) is None
    detail = await call(client, "get_meeting", {"meeting_id": MEETING_ID})
    assert detail["playback"] is None


async def test_shutdown_finalizes_originals_without_new_playback_job(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    started = await call(client, "start_recording", {"request_id": rid()})
    submitted = []

    def unexpected(*args, **kwargs):
        submitted.append(args)
        raise AssertionError("shutdown must defer new work")

    monkeypatch.setattr(ctx.jobs, "submit", unexpected)
    ctx.close()
    assert submitted == []
    bundle = Bundle.find(ctx.meetings_root, started["meeting_id"])
    assert bundle.manifest.status == "recorded"
    assert bundle.manifest.recording.stopped_at is not None
    assert all(bundle.abspath(t.path).is_file() for t in bundle.manifest.recording.tracks.values())


async def test_finalized_recorder_error_remains_visible_with_playback(client, ctx, monkeypatch):
    monkeypatch.setattr("narumi.playback.run_playback", fake_playback)
    monkeypatch.setenv("FAKE_RECORDER_ERROR_AFTER_STOP", "capture_failed")
    await call(client, "start_recording", {"request_id": rid()})
    stopped = await call(client, "stop_recording", {"request_id": rid(), "auto_process": False})
    assert stopped["recorder_error"]["code"] == "recorder_unavailable"
    assert (await wait_job(ctx, stopped["job_id"]))["status"] == "succeeded"
    detail = await call(client, "get_meeting", {"meeting_id": stopped["meeting_id"]})
    assert detail["recording"]["recorder_error"] == stopped["recorder_error"]
    assert detail["playback"] is not None


@pytest.mark.parametrize("display_id", [None, 2])
async def test_selected_display_is_reported_by_start_status_and_meeting(
    client: PerCallClient,
    ctx: ServerContext,
    monkeypatch: pytest.MonkeyPatch,
    display_id: int | None,
):
    monkeypatch.setattr("narumi.playback.run_playback", fake_playback)
    listed = await call(client, "list_recording_displays")
    assert listed["displays"][0]["is_main"] is False
    selected = next(
        display
        for display in listed["displays"]
        if display["id"] == display_id or (display_id is None and display["is_main"])
    )
    assert await call(client, "get_recording_status") == {"active": False}
    args = {"request_id": rid()}
    if display_id is not None:
        args["display_id"] = display_id
    started = await call(client, "start_recording", args)
    assert started["display"] == selected
    status = await call(client, "get_recording_status")
    assert status["display"] == selected
    detail = await call(client, "get_meeting", {"meeting_id": started["meeting_id"]})
    assert detail["recording"]["display"] == selected
    busy = await call(
        client, "prepare_recording", {"meeting_id": started["meeting_id"], "request_id": rid()}
    )
    assert busy["error"]["code"] == "busy"
    stopped = await call(client, "stop_recording", {"request_id": rid(), "auto_process": False})
    assert (await wait_job(ctx, stopped["job_id"]))["status"] == "succeeded"
    detail = await call(client, "get_meeting", {"meeting_id": started["meeting_id"]})
    assert detail["recording"]["display"] == selected


@pytest.mark.parametrize("missing", ["screen", "audio"])
async def test_prepare_without_required_tracks_returns_invalid_argument(
    client: PerCallClient, ctx: ServerContext, missing: str
):
    bundle = recorded_bundle(ctx)
    for name in ["screen"] if missing == "screen" else ["mic", "system"]:
        bundle.manifest.recording.tracks[name].discarded = True
    bundle.save()
    result = await call(
        client, "prepare_recording", {"meeting_id": MEETING_ID, "request_id": rid()}
    )
    assert result["error"]["code"] == "invalid_argument"
    assert ctx.catalog.list_jobs(meeting_id=MEETING_ID) == []


async def test_app_quit_defers_playback_before_server_shutdown(client, ctx, monkeypatch):
    started = await call(client, "start_recording", {"request_id": rid()})

    def unexpected(*args, **kwargs):
        raise AssertionError("app quit must not enqueue a conversion before shutdown")

    monkeypatch.setattr(ctx.jobs, "submit", unexpected)
    result = await call(
        client,
        "stop_recording",
        {
            "request_id": rid(),
            "auto_process": False,
            "prepare_playback": False,
        },
    )
    assert "job_id" not in result and "error" not in result
    bundle = Bundle.find(ctx.meetings_root, started["meeting_id"])
    assert bundle.manifest.status == "recorded"
    assert bundle.manifest.recording.stopped_at is not None
    assert all(bundle.abspath(t.path).is_file() for t in bundle.manifest.recording.tracks.values())
