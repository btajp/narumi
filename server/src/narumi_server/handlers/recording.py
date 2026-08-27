"""``start_recording`` / ``stop_recording`` / ``get_recording_status``."""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from narumi.bundle import Bundle, TrackRecord, sha256_file
from narumi.errors import (
    BusyError,
    NarumiError,
    NotFoundError,
    RecorderUnavailableError,
)

from narumi_server.handlers.common import (
    check_config_policy,
    config_from_mapping,
    default_meeting_name,
    find_bundle,
    jsonable,
    probe_duration_or_none,
    resolve_profile,
    sync_catalog,
)
from narumi_server.handlers.processing import enqueue_process
from narumi_server.locks import HANDLER_WAIT_SECONDS
from narumi_server.recording import StoppedEvent

if TYPE_CHECKING:
    from narumi_server.context import ServerContext

logger = logging.getLogger(__name__)

TRACKS_SUBDIR = "tracks"
SCREEN_TRACK = "screen"


def start_recording(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.recorder.is_active:
        raise BusyError(
            "a recording is already running",
            details={"meeting_id": ctx.recorder.active_meeting_id},
        )
    recorder_path = ctx.recorder.require_available()  # recorder_unavailable before any bundle
    permissions = ctx.recorder.permissions()
    if permissions is not None and permissions.get("microphone") == "denied":
        # Definitive: macOS never prompts again once microphone access was denied. (Screen
        # recording reads as "denied" until first granted, so the recorder is left to trigger
        # that prompt itself and to report permission_denied when it really is.)
        raise RecorderUnavailableError(
            "microphone access is denied for narumi-recorder; grant it in System Settings › "
            "Privacy & Security › Microphone and start again",
            details={"recorder_code": "permission_denied", "permissions": permissions},
        )
    profile = resolve_profile(ctx, args.get("profile"))
    config = config_from_mapping(profile.config, args.get("config"))
    check_config_policy(config)

    bundle = Bundle.create(
        ctx.meetings_root,
        meeting_name=args.get("meeting_name") or default_meeting_name(),
        engagement=args["engagement"] if "engagement" in args else profile.engagement,
        scope=args["scope"] if "scope" in args else profile.scope,
        profile=profile.name,
        config=config,
    )
    try:
        started = ctx.recorder.start(bundle)
    except BaseException:
        shutil.rmtree(bundle.path, ignore_errors=True)  # no orphan bundle for a failed start
        raise

    recording = bundle.manifest.recording
    recording.started_at = started.started_at
    recording.tracks = {
        name: TrackRecord(path=track_relpath(bundle, file_name))
        for name, file_name in started.tracks.items()
    }
    recording.recorder = {"binary": str(recorder_path), "started": jsonable(started.raw)}
    bundle.manifest.status = "recording"
    bundle.save()
    sync_catalog(ctx, bundle)
    ctx.catalog.audit(
        ctx.actor,
        "start_recording",
        {"meeting_id": bundle.meeting_id, "scope": bundle.manifest.scope},
    )
    return {
        "meeting_id": bundle.meeting_id,
        "started_at": started.started_at,
        "bundle_path": str(bundle.path),
        "tracks": {name: record.path for name, record in recording.tracks.items()},
    }


def stop_recording(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    meeting_id = ctx.recorder.active_meeting_id
    if meeting_id is None:
        raise NotFoundError("no recording is running")
    discard_video = bool(args.get("discard_video"))
    with ctx.locks.hold(meeting_id, purpose="stop_recording", timeout=HANDLER_WAIT_SECONDS):
        bundle = find_bundle(ctx, meeting_id)
        try:
            stopped = ctx.recorder.stop()
        except NarumiError as exc:
            _mark_failed(ctx, bundle, exc)
            raise
        try:
            tracks = finalize_tracks(bundle, stopped, discard_video=discard_video)
        except NarumiError as exc:
            _mark_failed(ctx, bundle, exc, discard_video=discard_video)
            raise

        recording = bundle.manifest.recording
        recording.stopped_at = stopped.stopped_at
        recording.duration_sec = stopped.duration_sec
        recording.tracks = tracks
        recording.recorder = {**recording.recorder, "stopped": jsonable(stopped.raw)}
        if stopped.error is not None:
            # capture failed mid-meeting but the audio tracks were finalized: the meeting is
            # usable (status recorded); the failure stays visible as provenance
            recording.recorder["error"] = stopped.error
            logger.warning(
                "recording %s ended with recorder error %s: %s",
                meeting_id,
                stopped.error.get("code"),
                stopped.error.get("message"),
            )
        bundle.manifest.status = "recorded"
        bundle.save()
        sync_catalog(ctx, bundle)
        ctx.catalog.audit(
            ctx.actor,
            "stop_recording",
            {
                "meeting_id": meeting_id,
                "discard_video": discard_video,
                "recorder_error": stopped.error,
            },
        )

        result: dict[str, Any] = {
            "meeting_id": meeting_id,
            "stopped_at": stopped.stopped_at,
            "duration_sec": stopped.duration_sec,
            "tracks": {name: record.model_dump(mode="json") for name, record in tracks.items()},
        }
        if args.get("auto_process", True):
            result["job_id"] = enqueue_process(ctx, meeting_id)
    return result


def get_recording_status(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    """Recorder state + the recording meeting's manifest; ``{"active": false}`` when idle."""
    meeting_id = ctx.recorder.active_meeting_id
    if meeting_id is None:
        return {"active": False}
    manifest = find_bundle(ctx, meeting_id).manifest
    recording = manifest.recording
    result: dict[str, Any] = {
        "active": True,
        "meeting_id": meeting_id,
        "meeting_name": manifest.meeting_name,
        "tracks": {name: track.path for name, track in recording.tracks.items()},
    }
    if recording.started_at:
        result["started_at"] = recording.started_at
        started = datetime.fromisoformat(recording.started_at)
        if started.tzinfo is None:  # recorder timestamps are RFC3339 UTC; be defensive
            started = started.replace(tzinfo=UTC)
        result["elapsed_sec"] = max(0.0, (datetime.now(UTC) - started).total_seconds())
    return result


# ---------------------------------------------------------------------------- helpers
def track_relpath(bundle: Bundle, value: str) -> str:
    """Bundle-relative path (``tracks/<file>``) for a file name reported by the recorder."""
    path = Path(value)
    if path.is_absolute():
        try:
            return bundle.relpath(path)
        except ValueError as exc:
            raise RecorderUnavailableError(
                "recorder wrote a track outside the bundle",
                details={"path": value, "bundle": str(bundle.path)},
            ) from exc
    if path.parts and path.parts[0] == TRACKS_SUBDIR:
        return path.as_posix()
    return (Path(TRACKS_SUBDIR) / path).as_posix()


def finalize_tracks(
    bundle: Bundle, stopped: StoppedEvent, *, discard_video: bool
) -> dict[str, TrackRecord]:
    """Hash / size / duration every track file; optionally delete the screen video.

    The screen file is unlinked only after every other track has been validated and hashed, so
    a failure in between never leaves a manifest that still describes a deleted file.
    """
    existing = bundle.manifest.recording.tracks
    names = list(existing) + [name for name in stopped.tracks if name not in existing]
    tracks: dict[str, TrackRecord] = {}
    to_discard: tuple[Path, str] | None = None
    for name in names:
        summary = stopped.tracks.get(name)
        if summary is not None:
            rel = track_relpath(bundle, summary.path)
        else:
            rel = existing[name].path
        path = bundle.abspath(rel)
        if not path.is_file():
            if summary is not None and summary.bytes == 0:
                # narumi-recorder reports a track that never received a sample (typically a
                # screen track without a single frame) as bytes 0 with no file on disk.
                logger.warning("recorder captured nothing for track %r (%s); dropped", name, rel)
                continue
            raise RecorderUnavailableError(
                f"recorder did not produce track {name!r} ({rel})",
                details={"track": name, "path": rel},
            )
        if discard_video and name == SCREEN_TRACK:
            to_discard = (path, rel)
            tracks[name] = TrackRecord(path=rel, discarded=True)
            continue
        duration = summary.duration_sec if summary is not None else None
        if duration is None:
            duration = probe_duration_or_none(path)
        tracks[name] = TrackRecord(
            path=rel,
            sha256=sha256_file(path),
            bytes=path.stat().st_size,
            duration_sec=duration,
        )
    if to_discard is not None:
        path, rel = to_discard
        path.unlink()
        logger.info("discarded screen track %s", rel)
    return tracks


def _mark_failed(
    ctx: ServerContext, bundle: Bundle, exc: NarumiError, *, discard_video: bool = False
) -> None:
    bundle.manifest.status = "failed"
    bundle.manifest.recording.recorder = {
        **bundle.manifest.recording.recorder,
        "error": exc.to_payload()["error"],
    }
    bundle.save()
    sync_catalog(ctx, bundle)
    ctx.catalog.audit(
        ctx.actor,
        "stop_recording",
        {
            "meeting_id": bundle.meeting_id,
            "discard_video": discard_video,
            "failed": exc.to_payload()["error"],
        },
    )
