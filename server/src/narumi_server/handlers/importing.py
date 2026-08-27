"""``import_recording``: create a session bundle from existing files (Zoom local recording, …).

The dev CLI (``narumi-dev import-recording``) keeps its own library-direct copy of this logic;
this handler is the product surface (contract → server → CLI → app parity).
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from narumi.bundle import Bundle, TrackRecord, new_meeting_id, sha256_file
from narumi.errors import InvalidArgumentError, NotFoundError

from narumi_server.handlers.common import (
    check_config_policy,
    config_from_mapping,
    probe_duration_or_none,
    resolve_profile,
    sync_catalog,
)
from narumi_server.handlers.processing import enqueue_process

if TYPE_CHECKING:
    from narumi_server.context import ServerContext

logger = logging.getLogger(__name__)

TRACK_ARGS: tuple[tuple[str, str], ...] = (
    ("mic", "mic_path"),
    ("system", "system_path"),
    ("screen", "screen_path"),
)


def import_recording(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    sources = _validated_sources(args)
    profile = resolve_profile(ctx, args.get("profile"))
    config = config_from_mapping(profile.config, args.get("config"))
    check_config_policy(config)  # fail fast: no bundle is created for a rejected config
    copy_files = bool(args.get("copy", True))
    started = _started_at(args.get("started_at"), sources)
    durations = {name: probe_duration_or_none(path) for name, path in sources.items()}

    bundle = Bundle.create(
        ctx.meetings_root,
        meeting_name=args["meeting_name"],
        meeting_id=new_meeting_id(started),
        engagement=args["engagement"] if "engagement" in args else profile.engagement,
        scope=args["scope"] if "scope" in args else profile.scope,
        profile=profile.name,
        config=config,
    )
    try:
        tracks_dir = bundle.dir("tracks")
        for name, src in sources.items():
            dest = tracks_dir / (f"{name}{src.suffix}" if src.suffix else name)
            _place_file(src, dest, copy=copy_files)
            bundle.manifest.recording.tracks[name] = TrackRecord(
                path=bundle.relpath(dest),
                sha256=sha256_file(dest),
                bytes=dest.stat().st_size,
                duration_sec=durations[name],
            )
        known = [d for d in durations.values() if d is not None]
        duration = max(known) if known else None
        recording = bundle.manifest.recording
        recording.started_at = _iso_utc(started)
        recording.duration_sec = duration
        recording.stopped_at = _iso_utc(started + timedelta(seconds=duration)) if duration else None
        recording.recorder = {
            "importer": "narumi-server",
            "mode": "copy" if copy_files else "link",
            "sources": {name: str(path) for name, path in sources.items()},
        }
        bundle.manifest.status = "recorded"
        bundle.save()
    except BaseException:
        shutil.rmtree(bundle.path, ignore_errors=True)  # no half-imported bundle left behind
        raise

    sync_catalog(ctx, bundle)
    ctx.catalog.audit(
        ctx.actor,
        "import_recording",
        {
            "meeting_id": bundle.meeting_id,
            "scope": bundle.manifest.scope,
            "profile": profile.name,
            "tracks": sorted(sources),
            "copy": copy_files,
        },
    )
    result: dict[str, Any] = {
        "meeting_id": bundle.meeting_id,
        "bundle_path": str(bundle.path),
        "tracks": {
            name: record.model_dump(mode="json")
            for name, record in bundle.manifest.recording.tracks.items()
        },
    }
    if args.get("auto_process", True):
        result["job_id"] = enqueue_process(ctx, bundle.meeting_id)
    return result


# ---------------------------------------------------------------------------- helpers
def _validated_sources(args: dict[str, Any]) -> dict[str, Path]:
    """Track name → source file. Absolute regular files only; at least mic or system."""
    sources: dict[str, Path] = {}
    for name, key in TRACK_ARGS:
        raw = args.get(key)
        if raw is None:
            continue
        path = Path(raw)
        if not path.is_absolute():  # the contract's pattern already enforces this
            raise InvalidArgumentError(f"{key} must be an absolute path", details={key: str(raw)})
        if not path.exists():
            raise NotFoundError(f"file not found: {path}", details={key: str(path)})
        if not path.is_file():
            raise InvalidArgumentError(
                f"{key} must point to a regular file", details={key: str(path)}
            )
        sources[name] = path
    if "mic" not in sources and "system" not in sources:  # enforced by the contract's anyOf
        raise InvalidArgumentError("at least one of mic_path / system_path is required")
    return sources


def _started_at(raw: Any, sources: dict[str, Path]) -> datetime:
    """Explicit ``started_at``, or the oldest input file's modification time (documented)."""
    if raw:
        started = datetime.fromisoformat(str(raw))
        if started.tzinfo is None:  # the contract's timestamp always carries an offset
            started = started.replace(tzinfo=UTC)
        return started.astimezone(UTC)
    oldest = min(path.stat().st_mtime for path in sources.values())
    return datetime.fromtimestamp(oldest, tz=UTC)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _place_file(src: Path, dest: Path, *, copy: bool) -> None:
    if copy:
        shutil.copy2(src, dest)
        return
    try:
        os.link(src, dest)
    except OSError as exc:
        raise InvalidArgumentError(
            f"cannot hardlink {src} into the bundle ({exc.strerror}); use copy=true",
            details={"source": str(src), "errno": exc.errno},
        ) from exc
