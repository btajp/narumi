"""Unit tests for ``MeetingLocks`` (per-meeting manifest write lock)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from conftest import fake_process_meeting, make_recorded_bundle
from narumi.bundle import Bundle
from narumi.errors import BusyError
from narumi.models import MeetingConfig
from narumi_server.app import dispatch
from narumi_server.handlers import common, importing, processing, recording
from narumi_server.locks import MeetingLocks
from test_surface_tools import write_silence_wav


def test_hold_is_exclusive_per_meeting_and_reports_the_holder():
    locks = MeetingLocks()
    released = threading.Event()
    entered = threading.Event()

    def job() -> None:
        with locks.hold("m1", purpose="job"):
            entered.set()
            released.wait(5)

    worker = threading.Thread(target=job)
    worker.start()
    assert entered.wait(5)
    assert locks.holder("m1") == "job" and locks.holder("m2") is None
    with pytest.raises(BusyError) as excinfo:
        with locks.hold("m1", purpose="handler", timeout=0.05):
            pass
    assert excinfo.value.details == {"meeting_id": "m1", "holder": "job"}
    with locks.hold("m2", purpose="handler", timeout=0.05):  # another meeting is independent
        assert locks.holder("m2") == "handler"
    released.set()
    worker.join(5)
    assert locks.holder("m1") is None
    with locks.hold("m1", purpose="handler", timeout=0.05):
        pass


def test_hold_waits_for_a_short_holder_and_releases_on_error():
    locks = MeetingLocks()
    done = threading.Event()

    def short() -> None:
        with locks.hold("m1", purpose="short"):
            done.wait(0.2)

    worker = threading.Thread(target=short)
    worker.start()
    with locks.hold("m1", purpose="waiter", timeout=5.0):  # waits ≤ 0.2 s instead of failing
        pass
    worker.join(5)
    with pytest.raises(RuntimeError):
        with locks.hold("m1", purpose="boom"):
            raise RuntimeError("inside")
    assert locks.holder("m1") is None  # released although the body raised


@pytest.mark.parametrize("change", ["scope", "queued_job"])
def test_waiting_config_writer_rechecks_scope_and_jobs_under_lock(ctx, monkeypatch, change):
    bundle = make_recorded_bundle(ctx, meeting_id="20260829T000000Z-000010cc")
    prechecked, release_job = threading.Event(), threading.Event()
    original = common.ensure_not_busy
    jobs = []

    def checked(*args, **kwargs):
        original(*args, **kwargs)
        prechecked.set()

    def occupy_worker(_progress):
        assert release_job.wait(5)
        return {}

    monkeypatch.setattr(common, "ensure_not_busy", checked)
    monkeypatch.setattr(
        processing.narumi_pipeline,
        "refresh_meeting",
        lambda bundle, **kwargs: fake_process_meeting(bundle, progress=kwargs.get("progress")),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with ctx.locks.hold(bundle.meeting_id, purpose="preceding_writer"):
                waiting = pool.submit(
                    dispatch,
                    ctx,
                    "set_meeting_config",
                    {
                        "meeting_id": bundle.meeting_id,
                        "self_name": "must not be saved",
                        "request_id": str(uuid4()),
                    },
                )
                assert prechecked.wait(5)
                if change == "scope":
                    bundle.manifest.scope = "private"
                    bundle.save()
                else:
                    jobs.append(ctx.jobs.submit("process", None, occupy_worker))
                    jobs.append(
                        processing.enqueue_regenerate(
                            ctx, bundle.meeting_id, force=False, reason="accepted generation"
                        )
                    )
            rejected = waiting.result(timeout=5)
            expected = "scope_denied" if change == "scope" else "busy"
            assert rejected.is_error and rejected.payload["error"]["code"] == expected
            assert (
                Bundle.find(ctx.meetings_root, bundle.meeting_id).manifest.config.self_name is None
            )
        finally:
            release_job.set()
            for job_id in jobs:
                assert ctx.jobs.wait(job_id, timeout=5)["status"] == "succeeded"


@pytest.mark.parametrize("operation", ["process", "regenerate"])
@pytest.mark.parametrize("change", ["clear", "model", "policy", "add"])
def test_queued_model_job_rejects_a_changed_config_before_pipeline_calls(
    ctx, monkeypatch, operation, change
):
    bundle = make_recorded_bundle(ctx, meeting_id="20260829T000000Z-000010cc")
    selected = MeetingConfig.model_validate(
        {
            "external_send_policy": "subscription_ok",
            "minutes_model": {
                "provider": "codex-app-server",
                "connection_id": "conn-123456789abc",
                "connection_revision": 1,
                "model_id": "fixture-model",
            },
        }
    )
    bundle.manifest.config = MeetingConfig() if change == "add" else selected
    bundle.save()
    release = threading.Event()
    calls = []

    def occupy_worker(_progress):
        assert release.wait(5)
        return {}

    def unexpected(*args, **kwargs):
        calls.append(True)
        raise AssertionError("A changed config must not enter the pipeline")

    monkeypatch.setattr(processing.narumi_pipeline, "process_meeting", unexpected)
    monkeypatch.setattr(processing.narumi_pipeline, "refresh_meeting", unexpected)
    blocker = ctx.jobs.submit("process", None, occupy_worker)
    try:
        job_id = (
            processing.enqueue_process(ctx, bundle.meeting_id)
            if operation == "process"
            else processing.enqueue_regenerate(ctx, bundle.meeting_id, force=False, reason="test")
        )
        if change == "clear":
            bundle.manifest.config.minutes_model = None
        elif change == "add":
            bundle.manifest.config = selected
        elif change == "model":
            bundle.manifest.config.minutes_model.model_id = "another-fixture-model"
        else:
            bundle.manifest.config = MeetingConfig.model_validate(
                {
                    **selected.model_dump(mode="json"),
                    "external_send_policy": "api_ok",
                }
            )
        bundle.save()
    finally:
        release.set()
    assert ctx.jobs.wait(blocker, timeout=5)["status"] == "succeeded"
    job = ctx.jobs.wait(job_id, timeout=5)
    assert job["status"] == "failed" and job["error"]["code"] == "configuration_conflict"
    assert calls == []


@pytest.mark.parametrize("operation", ["start", "import"])
def test_initial_bundle_write_excludes_concurrent_config_updates(
    ctx, monkeypatch, tmp_path, operation
):
    exposed, finish_creation, finish_job = (threading.Event() for _ in range(3))
    seen = {}

    def occupy_worker(_progress):
        assert finish_job.wait(5)
        return {}

    blocker = ctx.jobs.submit("process", None, occupy_worker)
    if operation == "start":
        original = ctx.recorder.start

        def start(bundle):
            event = original(bundle)
            seen["meeting_id"] = bundle.meeting_id
            exposed.set()
            assert finish_creation.wait(5)
            return event

        monkeypatch.setattr(ctx.recorder, "start", start)
        tool, args = "start_recording", {}
    else:
        original = importing.sync_catalog

        def indexed(context, bundle):
            original(context, bundle)
            seen["meeting_id"] = bundle.meeting_id
            exposed.set()
            assert finish_creation.wait(5)

        monkeypatch.setattr(importing, "sync_catalog", indexed)
        monkeypatch.setattr(processing.narumi_pipeline, "process_meeting", fake_process_meeting)
        tool, args = (
            "import_recording",
            {
                "meeting_name": "synthetic",
                "mic_path": str(write_silence_wav(tmp_path / "mic.wav")),
            },
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(dispatch, ctx, tool, {**args, "request_id": str(uuid4())})
        created = None
        try:
            assert exposed.wait(5)
            updated = pool.submit(
                dispatch,
                ctx,
                "set_meeting_config",
                {
                    "meeting_id": seen["meeting_id"],
                    "self_name": "preserved",
                    "request_id": str(uuid4()),
                },
            )
            assert not updated.done()
            assert ctx.locks.holder(seen["meeting_id"]) == f"{operation}_recording"
            finish_creation.set()
            created = creating.result(timeout=5)
            assert not created.is_error, created.payload
            update = updated.result(timeout=5)
            if operation == "start":
                assert not update.is_error, update.payload
                assert (
                    Bundle.find(ctx.meetings_root, seen["meeting_id"]).manifest.config.self_name
                    == "preserved"
                )
                recording.stop_recording(ctx, {"auto_process": False})
            else:
                assert update.is_error and update.payload["error"]["code"] == "busy"
        finally:
            finish_creation.set()
            finish_job.set()
            assert ctx.jobs.wait(blocker, timeout=5)["status"] == "succeeded"
            if operation == "import" and created is not None and "job_id" in created.payload:
                assert ctx.jobs.wait(created.payload["job_id"], timeout=5)["status"] == "succeeded"
