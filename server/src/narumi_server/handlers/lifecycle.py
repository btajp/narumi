"""``discard_tracks`` / ``delete_meeting``: destructive bundle operations.

Both go through :func:`locked_bundle` (not_found → scope_denied → busy → per-meeting write
lock), so they can never race a running job or an active recording, and both leave an audit
row. Nothing is erased irrecoverably by ``delete_meeting``: the bundle moves to
``<NARUMI_HOME>/trash/<meeting_id>-<timestamp>/``.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from narumi.errors import InvalidArgumentError, NotFoundError

from narumi_server.handlers.common import locked_bundle, sync_catalog

if TYPE_CHECKING:
    from narumi_server.context import ServerContext

logger = logging.getLogger(__name__)

AUDIO_TRACKS: tuple[str, ...] = ("mic", "system")
"""Tracks that need their ``transcripts/own-<track>`` artifact before they may be discarded."""

TRASH_DIR = "trash"


def discard_tracks(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    requested: list[str] = list(args["tracks"])
    with locked_bundle(
        ctx, args["meeting_id"], scope=args.get("scope"), purpose="discard_tracks"
    ) as bundle:
        tracks = bundle.manifest.recording.tracks
        missing = sorted(set(requested) - set(tracks))
        if missing:
            raise NotFoundError(
                f"meeting has no track(s): {', '.join(missing)}",
                details={
                    "meeting_id": bundle.meeting_id,
                    "missing": missing,
                    "tracks": sorted(tracks),
                },
            )
        to_discard = [name for name in requested if not tracks[name].discarded]
        for name in to_discard:  # validate everything before deleting anything
            if name not in AUDIO_TRACKS:
                continue  # screen can be discarded at any time
            key = f"transcripts/own-{name}"
            record = bundle.artifact(key)
            if record is None or not bundle.abspath(record.path).is_file():
                raise InvalidArgumentError(
                    f"track {name!r} can only be discarded after its transcript exists ({key});"
                    " run the process job first",
                    details={"meeting_id": bundle.meeting_id, "track": name, "artifact": key},
                )
        for name in to_discard:
            record = tracks[name]
            path = bundle.abspath(record.path)
            if path.is_file():
                path.unlink()
            record.discarded = True
            record.bytes = None  # the media is gone; sha256 / duration stay as provenance
            logger.info("discarded track %s of %s (%s)", name, bundle.meeting_id, record.path)
        if to_discard:
            bundle.save()
            sync_catalog(ctx, bundle)
        ctx.catalog.audit(
            ctx.actor,
            "discard_tracks",
            {"meeting_id": bundle.meeting_id, "requested": requested, "discarded": to_discard},
        )
        return {
            "meeting_id": bundle.meeting_id,
            "tracks": {name: record.model_dump(mode="json") for name, record in tracks.items()},
        }


def delete_meeting(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    if args.get("confirm") is not True:  # the contract's const already rejects this
        raise InvalidArgumentError("confirm must be literally true")
    meeting_id = args["meeting_id"]
    with locked_bundle(
        ctx, meeting_id, scope=args.get("scope"), purpose="delete_meeting"
    ) as bundle:
        trash_root = ctx.data_root / TRASH_DIR
        trash_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = trash_root / f"{meeting_id}-{stamp}"
        suffix = 1
        while target.exists():  # same meeting id restored and deleted twice in one second
            target = trash_root / f"{meeting_id}-{stamp}-{suffix}"
            suffix += 1
        shutil.move(str(bundle.path), str(target))
        ctx.catalog.delete_meeting(meeting_id)
        ctx.catalog.audit(
            ctx.actor, "delete_meeting", {"meeting_id": meeting_id, "moved_to": str(target)}
        )
        logger.info("meeting %s moved to trash: %s", meeting_id, target)
        return {"meeting_id": meeting_id, "deleted": True, "moved_to": str(target)}
