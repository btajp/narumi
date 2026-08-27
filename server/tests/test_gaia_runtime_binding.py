"""Each server context passes its own Gaia connection factory to jobs and exports."""

import pytest
from conftest import fake_process_meeting, make_recorded_bundle, write_fake_minutes
from narumi.pipeline import ExportResult
from narumi_server.handlers import processing

MEETING_ID = "20260827T000000Z-12345678"


@pytest.mark.parametrize("operation", ["process", "regenerate"])
def test_jobs_use_the_context_connection_factory(ctx, monkeypatch, operation):
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_ID)
    client = object()
    seen = []
    monkeypatch.setattr(ctx.gaia, "client", lambda: client)

    def run(bundle, *, gaia_client_factory, **kwargs):
        seen.append(gaia_client_factory())
        return fake_process_meeting(bundle, progress=kwargs.get("progress"))

    target = "process_meeting" if operation == "process" else "refresh_meeting"
    monkeypatch.setattr(processing.narumi_pipeline, target, run)
    if operation == "process":
        job_id = processing.enqueue_process(ctx, bundle.meeting_id)
    else:
        job_id = processing.enqueue_regenerate(ctx, bundle.meeting_id, force=False, reason="test")
    assert ctx.jobs.wait(job_id, 5)["status"] == "succeeded"
    assert seen == [client]


def test_export_uses_the_context_connection_factory(ctx, monkeypatch):
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_ID)
    write_fake_minutes(bundle)
    client = object()
    seen = []
    monkeypatch.setattr(ctx.gaia, "client", lambda: client)

    def export(bundle, destination, *, gaia_client_factory, **kwargs):
        seen.append(gaia_client_factory())
        return ExportResult(
            destination=destination,
            ref="gaia://proposal/7",
            minutes_version=1,
            at="2026-08-27T00:00:00Z",
        )

    monkeypatch.setattr(processing.narumi_pipeline, "export_meeting", export)
    result = processing.perform_export(ctx, bundle.meeting_id, "gaia-library", {}, 1, "test-id")
    assert result["ref"] == "gaia://proposal/7"
    assert seen == [client]
