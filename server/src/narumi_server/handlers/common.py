"""Helpers shared by the tool handlers (bundle lookup, config validation, JSON hygiene)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from narumi import diarize, llm, transcribe
from narumi.bundle import Bundle, Manifest
from narumi.errors import (
    AuthenticationRequiredError,
    BusyError,
    ConfigurationConflictError,
    EngineUnavailableError,
    InvalidArgumentError,
    NarumiError,
    NotFoundError,
)
from narumi.models import MeetingConfig
from narumi.preprocess import probe_duration
from narumi.profiles import Profile
from pydantic import ValidationError

from narumi_server.locks import HANDLER_WAIT_SECONDS

if TYPE_CHECKING:
    from narumi_server.context import ServerContext

logger = logging.getLogger(__name__)

Handler = Callable[["ServerContext", dict[str, Any]], dict[str, Any]]
"""A tool handler: ``(ctx, validated_args) -> result`` (the tool's structured content)."""

CONFIG_KEYS: tuple[str, ...] = tuple(MeetingConfig.model_fields)


def jsonable(value: Any) -> Any:
    """Round-trip through JSON so paths / enums / dataclasses become plain data."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def find_bundle(ctx: ServerContext, meeting_id: str) -> Bundle:
    """``Bundle.find`` (``not_found`` / ``invalid_argument`` are raised by the bundle layer)."""
    return Bundle.find(ctx.meetings_root, meeting_id)


def sync_catalog(ctx: ServerContext, bundle: Bundle) -> None:
    ctx.catalog.upsert_meeting(bundle)


def check_scope(ctx: ServerContext, bundle: Bundle, requested: Any) -> None:
    """``scope_denied`` unless the request's scope selector covers the meeting (default deny)."""
    ctx.catalog.check_scope(
        bundle.manifest.scope, requested, actor=ctx.actor, meeting_id=bundle.meeting_id
    )


def ensure_not_busy(ctx: ServerContext, bundle: Bundle, *, allow_recording: bool = False) -> None:
    """``busy`` when the meeting has a queued / running job (or, unless allowed, is recording)."""
    meeting_id = bundle.meeting_id
    if not allow_recording and ctx.recorder.active_meeting_id == meeting_id:
        raise BusyError(
            "meeting is still recording; call stop_recording first",
            details={"meeting_id": meeting_id},
        )
    if ctx.jobs.has_active(meeting_id):
        raise BusyError(
            "a job is already running for this meeting",
            details={"meeting_id": meeting_id, "jobs": ctx.jobs.active_jobs(meeting_id)},
        )


@contextmanager
def locked_bundle(
    ctx: ServerContext,
    meeting_id: str,
    *,
    scope: Any,
    purpose: str,
    allow_recording: bool = False,
) -> Iterator[Bundle]:
    """Open a meeting for a manifest write: ``not_found`` → ``scope_denied`` → ``busy`` → lock.

    Yields a *fresh* :class:`Bundle` read under the meeting's write lock, so the read → modify →
    ``save()`` inside the block can neither revert nor be reverted by a job or another handler.
    A job that owns the lock makes this ``busy`` after a short wait.
    """
    bundle = find_bundle(ctx, meeting_id)
    check_scope(ctx, bundle, scope)
    ensure_not_busy(ctx, bundle, allow_recording=allow_recording)
    with ctx.locks.hold(meeting_id, purpose=purpose, timeout=HANDLER_WAIT_SECONDS):
        fresh = find_bundle(ctx, meeting_id)
        # A preceding writer can change scope or enqueue a job while this caller waits.
        # Its pre-lock checks must not authorize a later write over the accepted job config.
        check_scope(ctx, fresh, scope)
        ensure_not_busy(ctx, fresh, allow_recording=allow_recording)
        yield fresh


def meeting_summary(manifest: Manifest) -> dict[str, Any]:
    """``meeting_summary`` (contract def) straight from the manifest, the source of truth."""
    return {
        "meeting_id": manifest.meeting_id,
        "meeting_name": manifest.meeting_name,
        "engagement": manifest.engagement,
        "scope": manifest.scope,
        "status": manifest.status,
        "started_at": manifest.recording.started_at or manifest.created_at,
        "stopped_at": manifest.recording.stopped_at,
        "latest_minutes_version": manifest.latest_minutes_version,
    }


def default_meeting_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).astimezone()
    return f"会議 {stamp:%Y-%m-%d %H:%M}"


def resolve_profile(ctx: ServerContext, name: Any) -> Profile:
    """The saved profile ``name`` names, or the default profile when ``name`` is ``None``.

    ``start_recording`` / ``import_recording`` treat an unknown profile as ``invalid_argument``
    (the profile-management tools use ``not_found`` instead — see ``get_profile``).
    """
    if name is None:
        return ctx.profiles.default()
    try:
        return ctx.profiles.get(str(name))
    except NotFoundError as exc:
        known = ctx.profiles.names()
        raise InvalidArgumentError(
            f"unknown profile {name!r}; known: {', '.join(known)}",
            details={"profile": name, "known": known},
        ) from exc


def probe_duration_or_none(path: Path) -> float | None:
    """``ffprobe`` duration for track metadata; ``None`` (logged) when the probe fails.

    Metadata only — this is not an engine fallback: preprocessing fails loudly later when
    ffmpeg is genuinely unusable.
    """
    try:
        return probe_duration(path)
    except NarumiError as exc:
        logger.warning("ffprobe failed for %s: %s", path, exc.message)
        return None


def config_from_mapping(base: MeetingConfig, updates: Mapping[str, Any] | None) -> MeetingConfig:
    """Merge ``updates`` onto ``base`` and validate as :class:`MeetingConfig`."""
    merged = base.model_dump(mode="json")
    unknown = sorted(set(updates or {}) - set(CONFIG_KEYS))
    if unknown:
        raise InvalidArgumentError(
            f"unknown config keys: {', '.join(unknown)}", details={"unknown": unknown}
        )
    merged.update(dict(updates or {}))
    try:
        return MeetingConfig.model_validate(merged)
    except ValidationError as exc:
        raise InvalidArgumentError(
            "invalid meeting config",
            details={"errors": json.loads(exc.json(include_url=False))},
        ) from exc


def check_cache_epoch_monotonic(previous: MeetingConfig, updated: MeetingConfig) -> None:
    """Reject moving an unchanged model selection back to an earlier send epoch."""
    for field in ("minutes_model", "transcription_model"):
        old = getattr(previous, field)
        new = getattr(updated, field)
        if old is None or new is None:
            continue
        old_identity = (old.provider, old.connection_id, old.model_id, old.parameters)
        new_identity = (new.provider, new.connection_id, new.model_id, new.parameters)
        if old_identity == new_identity and new.cache_epoch < old.cache_epoch:
            raise InvalidArgumentError(
                f"{field}.cache_epoch cannot decrease for the same model selection",
                details={
                    "reason": "cache_epoch_regression",
                    "field": f"{field}.cache_epoch",
                    "current": old.cache_epoch,
                    "requested": new.cache_epoch,
                },
            )


@contextmanager
def validated_config(ctx: ServerContext, config: MeetingConfig) -> Iterator[None]:
    """Validate a config and protect its model reference until the caller saves it.

    Meeting writers take their meeting lock before entering this scope. The provider
    deletion callback only reads atomically replaced files and never acquires a meeting
    or profile lock, so the provider transaction cannot invert that lock order.
    """
    check_config_policy(config)
    if config.minutes_model is None and config.transcription_model is None:
        yield
        return
    if "streamable-http" not in ctx.transports or ctx.providers is None:
        raise AuthenticationRequiredError(
            "Model selections require the authenticated resident server"
        )
    from narumi.providers.generation import MinutesResolver
    from narumi.providers.transcription import TranscriptionResolver

    # Both selections must remain valid through the same atomic save. Provider-store
    # transactions cannot be nested, including when the connection is shared.
    with ctx.providers.store.transaction() as document:
        if config.minutes_model is not None:
            MinutesResolver(ctx.providers).validate_in_transaction(config, document)
        if config.transcription_model is not None:
            TranscriptionResolver(ctx.providers).validate_in_transaction(config, document)
        yield


def check_expected_config(
    config: MeetingConfig, args: Mapping[str, Any], *, generation: bool = True
) -> None:
    """Reject a generation request made for a stale displayed meeting configuration."""
    if "expected_config" not in args:
        if generation and (
            config.minutes_model is not None or config.transcription_model is not None
        ):
            raise ConfigurationConflictError("Reload the meeting configuration before generating")
        return
    expected = MeetingConfig.model_validate(args["expected_config"])
    if expected != config:
        raise ConfigurationConflictError("The meeting configuration changed; reload it")


def check_config_policy(config: MeetingConfig) -> None:
    """Reject engine / provider names that are unknown or violate ``external_send_policy``.

    絶対原則 4: a forbidden combination is a ``policy_violation`` error, never a silent downgrade.
    Unknown registry names are ``engine_unavailable`` (contract wording: not registered here).
    """
    policy = config.external_send_policy

    providers = llm.provider_names()
    if config.llm_provider not in providers:
        raise EngineUnavailableError(
            f"unknown llm_provider {config.llm_provider!r}; registered: {', '.join(providers)}",
            details={"llm_provider": config.llm_provider, "registered": providers},
        )
    llm.check_policy(
        llm.provider_profile(config.llm_provider), policy, provider=config.llm_provider
    )

    engine = config.transcription_engine
    known_engines = [transcribe.AUTO, *transcribe.ENGINE_FACTORIES]
    if engine not in known_engines:
        raise EngineUnavailableError(
            f"unknown transcription_engine {engine!r}; registered: {', '.join(known_engines)}",
            details={"transcription_engine": engine, "registered": known_engines},
        )
    if engine != transcribe.AUTO:  # auto only ever picks local Whisper engines
        transcribe.check_send_policy(
            policy,
            _engine_profile(transcribe.ENGINE_FACTORIES[engine]),
            subject=f"transcription engine {engine!r}",
        )

    diarizer = config.diarization_engine
    known_diarizers = list(diarize.ENGINE_FACTORIES)
    if diarizer not in known_diarizers:
        raise EngineUnavailableError(
            f"unknown diarization_engine {diarizer!r}; registered: {', '.join(known_diarizers)}",
            details={"diarization_engine": diarizer, "registered": known_diarizers},
        )
    transcribe.check_send_policy(
        policy,
        _engine_profile(diarize.ENGINE_FACTORIES[diarizer]),
        subject=f"diarization engine {diarizer!r}",
    )


def _engine_profile(factory: Any) -> Any:
    """Static ``profile`` of an engine class; instantiate only when it is not a class attribute.

    Constructors may probe installed packages / tokens (``engine_unavailable``), which a pure
    policy check should not trigger for an engine that is merely being configured.
    """
    profile = getattr(factory, "profile", None)
    return profile if profile is not None else factory().profile
