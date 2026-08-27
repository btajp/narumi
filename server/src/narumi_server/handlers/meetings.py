"""``list_meetings`` / ``get_meeting`` / ``get_transcript`` / ``get_minutes`` /
``search_transcripts`` / ``set_meeting_config``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from narumi.bundle import Bundle
from narumi.catalog import row_to_summary
from narumi.errors import NotFoundError
from narumi.models import MergedTranscript, MinutesMeta, SpeakerMap, Transcript

from narumi_server.handlers.common import (
    CONFIG_KEYS,
    check_config_policy,
    config_from_mapping,
    find_bundle,
    locked_bundle,
    meeting_summary,
    sync_catalog,
)

if TYPE_CHECKING:
    from narumi_server.context import ServerContext

MERGED_SOURCE = "merged"
OWN_SOURCES: tuple[str, ...] = ("own-mic", "own-system")
EXT_PREFIX = "ext-"
SPEAKER_MAP_KEY = "merged/speaker_map"
SPEAKER_MAP_PATH = "merged/speaker_map.json"


# ---------------------------------------------------------------------------- list / get
def list_meetings(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    time_range = args.get("range") or {}
    rows = ctx.catalog.list_meetings(
        query=args.get("query"),
        since=time_range.get("from"),
        until=time_range.get("to"),
        scope=args.get("scope"),
        limit=int(args.get("limit", 50)),
        actor=ctx.actor,
    )
    return {"meetings": [row_to_summary(row) for row in rows]}


def get_meeting(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    bundle = find_bundle(ctx, args["meeting_id"])
    manifest = bundle.manifest
    ctx.catalog.check_scope(
        manifest.scope, args.get("scope"), actor=ctx.actor, meeting_id=bundle.meeting_id
    )
    latest: dict[str, Any] | None = None
    if args.get("include_minutes", True) and manifest.minutes_versions:
        record = max(manifest.minutes_versions, key=lambda v: v.version)
        path = bundle.abspath(record.path)
        if not path.is_file():
            raise NotFoundError(
                f"minutes file missing for version {record.version}: {record.path}",
                details={"meeting_id": bundle.meeting_id, "path": record.path},
            )
        latest = {"version": record.version, "markdown": path.read_text(encoding="utf-8")}
    recording = manifest.recording
    return {
        "meeting": meeting_summary(manifest),
        "bundle_path": str(bundle.path),
        "config": manifest.config.model_dump(mode="json"),
        "recording": {
            "started_at": recording.started_at,
            "stopped_at": recording.stopped_at,
            "duration_sec": recording.duration_sec,
            "tracks": {name: t.model_dump(mode="json") for name, t in recording.tracks.items()},
        },
        "contexts": [
            {
                "context_id": c.context_id,
                "source_type": c.source_type,
                "status": c.status,
                "registered_at": c.registered_at,
                "label": c.label,
            }
            for c in manifest.contexts
        ],
        "minutes_versions": [
            {
                "version": v.version,
                "generated_at": v.generated_at,
                "provider": v.provider,
                "path": v.path,
            }
            for v in sorted(manifest.minutes_versions, key=lambda v: v.version)
        ],
        "latest_minutes": latest,
        "exports": [
            {
                "destination": e.destination,
                "ref": e.ref,
                "minutes_version": e.minutes_version,
                "at": e.at,
            }
            for e in manifest.exports
        ],
        "artifacts": sorted(manifest.artifacts),
    }


# ---------------------------------------------------------------------------- minutes / search
def get_minutes(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    """One minutes version: ``minutes/v<N>/minutes.md`` + its ``meta.json`` (source of truth)."""
    bundle = find_bundle(ctx, args["meeting_id"])
    ctx.catalog.check_scope(
        bundle.manifest.scope, args.get("scope"), actor=ctx.actor, meeting_id=bundle.meeting_id
    )
    records = sorted(bundle.manifest.minutes_versions, key=lambda v: v.version)
    if not records:
        raise NotFoundError(
            "no minutes have been generated for this meeting yet",
            details={"meeting_id": bundle.meeting_id, "status": bundle.manifest.status},
        )
    available = [record.version for record in records]
    version = int(args.get("version") or available[-1])
    record = next((r for r in records if r.version == version), None)
    if record is None:
        raise NotFoundError(
            f"minutes version {version} does not exist",
            details={"meeting_id": bundle.meeting_id, "available": available},
        )
    path = bundle.abspath(record.path)
    if not path.is_file():
        raise NotFoundError(
            f"minutes file missing for version {version}: {record.path}",
            details={"meeting_id": bundle.meeting_id, "path": record.path},
        )
    meta = MinutesMeta.model_validate(bundle.read_json(f"minutes/v{version}/meta.json"))
    return {
        "meeting_id": bundle.meeting_id,
        "version": version,
        "markdown": path.read_text(encoding="utf-8"),
        "generated_at": record.generated_at,
        "provider": record.provider,
        "unresolved_speakers": list(dict.fromkeys(meta.unresolved_speakers)),
        "available_versions": available,
    }


def search_transcripts(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    hits = ctx.catalog.search_segments(
        args["query"],
        scope=args.get("scope"),
        limit=int(args.get("limit", 20)),
        actor=ctx.actor,
    )
    return {"hits": hits}


# ---------------------------------------------------------------------------- transcript
def transcript_path(bundle: Bundle, source: str) -> Path | None:
    """File of a transcript source (artifact record first, conventional path second)."""
    if source == MERGED_SOURCE:
        key, default = "merged/merged", "merged/merged.json"
    else:
        key, default = f"transcripts/{source}", f"transcripts/{source}.json"
    record = bundle.artifact(key)
    path = bundle.abspath(record.path if record is not None else default)
    return path if path.is_file() else None


def available_sources(bundle: Bundle) -> list[str]:
    candidates: list[str] = [MERGED_SOURCE, *OWN_SOURCES]
    for context in bundle.manifest.contexts:
        candidates.append(f"{EXT_PREFIX}{context.context_id}")
    for key in sorted(bundle.manifest.artifacts):
        if key.startswith(f"transcripts/{EXT_PREFIX}"):
            candidates.append(key.removeprefix("transcripts/"))
    seen: list[str] = []
    for source in candidates:
        if source not in seen and transcript_path(bundle, source) is not None:
            seen.append(source)
    return seen


def get_transcript(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    bundle = find_bundle(ctx, args["meeting_id"])
    ctx.catalog.check_scope(
        bundle.manifest.scope, args.get("scope"), actor=ctx.actor, meeting_id=bundle.meeting_id
    )
    source = args.get("source", MERGED_SOURCE)
    sources = available_sources(bundle)
    path = transcript_path(bundle, source)
    if path is None:
        raise NotFoundError(
            f"transcript source {source!r} has not been produced for this meeting",
            details={
                "meeting_id": bundle.meeting_id,
                "source": source,
                "available_sources": sources,
            },
        )
    text = path.read_text(encoding="utf-8")
    if source == MERGED_SOURCE:
        merged = MergedTranscript.model_validate_json(text)
        segments: list[dict[str, Any]] = []
        for seg in merged.segments:
            segments.append(
                {
                    "id": seg.id,
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text,
                    "speaker": seg.speaker_label,
                    "speaker_name": seg.speaker_name,
                }
            )
        speaker_map = _speaker_map_payload(merged.speaker_map)
    else:
        transcript = Transcript.model_validate_json(text)
        segments = []
        for seg in transcript.segments:
            item: dict[str, Any] = {
                "id": seg.id,
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "speaker": seg.speaker,
            }
            if seg.confidence is not None:
                item["confidence"] = seg.confidence
            segments.append(item)
        speaker_map = _speaker_map_payload(_load_speaker_map(bundle))
    return {
        "meeting_id": bundle.meeting_id,
        "source": source,
        "segments": segments,
        "speaker_map": speaker_map,
        "available_sources": sources,
    }


def _load_speaker_map(bundle: Bundle) -> SpeakerMap:
    record = bundle.artifact(SPEAKER_MAP_KEY)
    path = bundle.abspath(record.path if record is not None else SPEAKER_MAP_PATH)
    if not path.is_file():
        return SpeakerMap()
    return SpeakerMap.model_validate_json(path.read_text(encoding="utf-8"))


def _speaker_map_payload(speaker_map: SpeakerMap) -> dict[str, dict[str, Any]]:
    return {
        label: {"name": entry.name, "confidence": min(1.0, max(0.0, float(entry.confidence)))}
        for label, entry in speaker_map.speakers.items()
    }


# ---------------------------------------------------------------------------- config
def set_meeting_config(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    """Update ``manifest.config`` / ``manifest.scope`` under the meeting's write lock.

    ``scope`` is the read selector (must cover the meeting's *current* scope, default deny);
    ``new_scope`` is the value to store. A scope change is audit-logged with old and new value.
    """
    updates = {key: args[key] for key in CONFIG_KEYS if key in args}
    with locked_bundle(
        ctx,
        args["meeting_id"],
        scope=args.get("scope"),
        purpose="set_meeting_config",
        allow_recording=True,
    ) as bundle:
        config = config_from_mapping(bundle.manifest.config, updates)
        check_config_policy(config)
        previous_scope = bundle.manifest.scope
        bundle.manifest.config = config
        scope_changed = "new_scope" in args
        if scope_changed:
            bundle.manifest.scope = args["new_scope"]
        bundle.save()
        sync_catalog(ctx, bundle)
        detail: dict[str, Any] = {
            "meeting_id": bundle.meeting_id,
            "updated": sorted(updates),
            "scope_changed": scope_changed,
        }
        if scope_changed:
            detail["scope_from"] = previous_scope
            detail["scope_to"] = bundle.manifest.scope
        ctx.catalog.audit(ctx.actor, "set_meeting_config", detail)
        return {
            "meeting_id": bundle.meeting_id,
            "config": config.model_dump(mode="json"),
            "scope": bundle.manifest.scope,
        }
