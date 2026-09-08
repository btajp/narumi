"""Local recording playback jobs, independent of transcription and minutes state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from narumi import playback as narumi_playback
from narumi.bundle import Bundle
from narumi.errors import InvalidArgumentError

from narumi_server.handlers.common import find_bundle, locked_bundle, sync_catalog
from narumi_server.jobs import JobProgress

if TYPE_CHECKING:
    from narumi_server.context import ServerContext

PLAYBACK_KEY = "recording/playback"


def has_screen_track(bundle: Bundle) -> bool:
    screen = bundle.manifest.recording.tracks.get("screen")
    return screen is not None and not screen.discarded


def prepare_recording(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    with locked_bundle(
        ctx, args["meeting_id"], scope=args.get("scope"), purpose="prepare_recording"
    ) as bundle:
        tracks = bundle.manifest.recording.tracks
        if not has_screen_track(bundle) or not any(
            name in tracks and not tracks[name].discarded for name in ("mic", "system")
        ):
            raise InvalidArgumentError("playback requires retained screen and audio tracks")
        job_id = enqueue_playback(ctx, bundle.meeting_id)
        ctx.catalog.audit(ctx.actor, "prepare_recording", {"meeting_id": bundle.meeting_id})
        return {"meeting_id": bundle.meeting_id, "job_id": job_id}


def prepare_playback(ctx: ServerContext, bundle: Bundle, progress: JobProgress) -> dict[str, Any]:
    """The caller holds the meeting lock; save playback before any later minutes failure."""
    progress("recording/playback", 0.0)
    stage = narumi_playback.run_playback(bundle, should_cancel=lambda: progress.cancelled)
    bundle.save()
    sync_catalog(ctx, bundle)
    progress("recording/playback", 1.0)
    return {
        "meeting_id": bundle.meeting_id,
        "playback": narumi_playback.playback_info(bundle),
        "stages": [] if stage.skipped else [stage.key],
        "skipped": [stage.key] if stage.skipped else [],
    }


def enqueue_playback(ctx: ServerContext, meeting_id: str) -> str:
    def run(progress: JobProgress) -> dict[str, Any]:
        with ctx.locks.hold(meeting_id, purpose="job"):
            bundle = find_bundle(ctx, meeting_id)
            return prepare_playback(ctx, bundle, progress)

    return ctx.jobs.submit("playback", meeting_id, run)


def discard_playback(bundle: Bundle) -> None:
    """Discard media derived from any removed recording track under the same write lock."""
    record = bundle.artifact(PLAYBACK_KEY)
    if record is not None:
        bundle.abspath(record.path).unlink(missing_ok=True)
        del bundle.manifest.artifacts[PLAYBACK_KEY]
