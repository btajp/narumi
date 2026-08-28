"""``regenerate`` / ``export_minutes`` / ``get_job_status`` and the job bodies behind them.

``narumi.pipeline`` functions are looked up on the module at call time (never imported by name)
so tests — and a future integration step — can replace ``refresh_meeting`` & co.

Every job body and every manifest-writing tool holds the meeting's write lock (``ctx.locks``)
around its read → modify → ``save()``: a job keeps one in-memory ``Bundle`` for its whole run,
so a concurrent handler save would otherwise be silently reverted by the job's next
``Bundle.save()`` (and vice versa). Handlers answer ``busy`` while a job owns the lock.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from narumi import pipeline as narumi_pipeline
from narumi.bundle import Bundle
from narumi.errors import (
    CancelledError,
    ErrorCode,
    InvalidArgumentError,
    NarumiError,
    NotFoundError,
)
from narumi.export import get_exporter

from narumi_server.handlers.common import (
    check_config_policy,
    check_scope,
    ensure_not_busy,
    find_bundle,
    locked_bundle,
    sync_catalog,
)
from narumi_server.jobs import JobProgress

if TYPE_CHECKING:
    from narumi_server.context import ServerContext

logger = logging.getLogger(__name__)

JOB_LOCK_PURPOSE = "job"

__all__ = [
    "cancel_job",
    "enqueue_process",
    "enqueue_regenerate",
    "ensure_not_busy",
    "export_minutes",
    "get_job_status",
    "perform_export",
    "process_result_payload",
    "regenerate",
    "run_pipeline_job",
    "validate_export_options",
]


# ---------------------------------------------------------------------------- job bodies
def process_result_payload(result: Any) -> dict[str, Any]:
    """Serialize ``narumi.pipeline.ProcessResult`` (attribute access keeps fakes simple)."""
    return {
        "meeting_id": result.meeting_id,
        "minutes_version": result.minutes_version,
        "stages": list(result.stages),
        "skipped": list(result.skipped),
        "unresolved_speakers": list(getattr(result, "unresolved_speakers", [])),
    }


def run_pipeline_job(
    ctx: ServerContext, meeting_id: str, stage: Callable[[Bundle], Any]
) -> dict[str, Any]:
    """Run ``stage(bundle)`` with status bookkeeping ``processing → ready | failed``.

    The meeting lock is held for the whole job. The bundle is read from disk only once the lock
    is ours, so a handler write made before the job started is seen, and no handler can write
    underneath the running stage.

    A :class:`CancelledError` (cooperative ``cancel_job``) restores the pre-run status instead
    of marking the meeting ``failed`` — ``narumi.pipeline`` does the same on disk for real runs,
    but the restore is repeated here from a fresh read so faked stages behave identically and
    the catalog is refreshed. On success the meeting profile's ``export_destinations`` are
    exported automatically; an auto-export failure is reported in the job result, never fatal.
    """
    with ctx.locks.hold(meeting_id, purpose=JOB_LOCK_PURPOSE):
        bundle = find_bundle(ctx, meeting_id)
        previous_status = bundle.manifest.status
        _set_status(ctx, bundle, "processing")
        try:
            result = stage(bundle)
        except CancelledError:
            try:
                fresh = find_bundle(ctx, meeting_id)
                fresh.manifest.status = previous_status
                fresh.save()
                sync_catalog(ctx, fresh)
            except Exception:  # never mask the cancellation with a bookkeeping error
                logger.exception("could not restore status of %s after cancellation", meeting_id)
            raise
        except BaseException:
            try:
                _set_status(ctx, find_bundle(ctx, meeting_id), "failed")
            except Exception:  # never mask the pipeline error with a bookkeeping one
                logger.exception("could not mark %s as failed", meeting_id)
            raise
        fresh = find_bundle(ctx, meeting_id)  # the stage saved artifacts; re-read from disk
        fresh.manifest.status = "ready"
        fresh.save()
        sync_catalog(ctx, fresh)
        ctx.catalog.index_segments(fresh)
        payload = process_result_payload(result)
        exports, export_errors = _auto_export(ctx, meeting_id, fresh)
        if exports:
            payload["exports"] = exports
        if export_errors:
            payload["export_errors"] = export_errors
        return payload


def _auto_export(
    ctx: ServerContext, meeting_id: str, bundle: Bundle
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Export the latest minutes to the meeting profile's ``export_destinations``.

    Runs after a successful process / regenerate job; the caller holds the meeting lock.
    Failures are logged and returned for the job result — they never fail the job. A profile
    that was deleted since the meeting was created simply exports nothing.
    """
    profile = ctx.profiles.peek(bundle.manifest.profile)
    version = bundle.manifest.latest_minutes_version
    if profile is None or not profile.export_destinations or version is None:
        return [], []
    exports: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for destination in profile.export_destinations:
        try:
            exports.append(perform_export(ctx, meeting_id, destination, {}, version, None))
        except NarumiError as exc:
            logger.warning(
                "auto-export of %s to %s failed: %s: %s",
                meeting_id,
                destination,
                exc.code,
                exc.message,
            )
            errors.append({"destination": destination, "error": exc.to_payload()["error"]})
        except Exception as exc:  # noqa: BLE001 - auto-export must never fail the job
            logger.exception("auto-export of %s to %s crashed", meeting_id, destination)
            errors.append(
                {
                    "destination": destination,
                    "error": {
                        "code": str(ErrorCode.INTERNAL),
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                }
            )
    return exports, errors


def _set_status(ctx: ServerContext, bundle: Bundle, status: str) -> None:
    bundle.manifest.status = status  # type: ignore[assignment]
    bundle.save()
    sync_catalog(ctx, bundle)


def enqueue_process(ctx: ServerContext, meeting_id: str, *, force: bool = False) -> str:
    def run(progress: JobProgress) -> dict[str, Any]:
        return run_pipeline_job(
            ctx,
            meeting_id,
            lambda bundle: narumi_pipeline.process_meeting(
                bundle, force=force, progress=progress, gaia_client_factory=ctx.gaia.client
            ),
        )

    return ctx.jobs.submit("process", meeting_id, run)


def enqueue_regenerate(ctx: ServerContext, meeting_id: str, *, force: bool, reason: str) -> str:
    """Enqueue the ``regenerate`` job: ``narumi.pipeline.refresh_meeting``.

    Deterministic stages run idempotently (only when they never ran or their params changed —
    a meeting stopped with ``auto_process=false``, a failed process job, an engine / language /
    vocab_hints change), then alignment → integrate → generate (forced when ``force``).
    """

    def run(progress: JobProgress) -> dict[str, Any]:
        return run_pipeline_job(
            ctx,
            meeting_id,
            lambda bundle: narumi_pipeline.refresh_meeting(
                bundle,
                force=force,
                progress=progress,
                reason=reason,
                job_id=progress.job_id,
                gaia_client_factory=ctx.gaia.client,
            ),
        )

    return ctx.jobs.submit("regenerate", meeting_id, run)


def perform_export(
    ctx: ServerContext,
    meeting_id: str,
    destination: str,
    options: dict[str, Any],
    minutes_version: int,
    request_id: str | None,
) -> dict[str, Any]:
    """Export + manifest / catalog / audit bookkeeping. The caller holds the meeting lock."""
    bundle = find_bundle(ctx, meeting_id)
    result = narumi_pipeline.export_meeting(
        bundle,
        destination,
        options=dict(options),
        minutes_version=minutes_version,
        request_id=request_id,
        gaia_client_factory=ctx.gaia.client,
    )
    payload = {
        "destination": str(result.destination),
        "ref": str(result.ref),
        "minutes_version": int(result.minutes_version),
        "at": str(result.at),
    }
    sync_catalog(ctx, find_bundle(ctx, meeting_id))
    ctx.catalog.record_export(
        meeting_id,
        payload["destination"],
        payload["ref"],
        payload["minutes_version"],
        payload["at"],
    )
    ctx.catalog.audit(ctx.actor, "export_minutes", {"meeting_id": meeting_id, **payload})
    return payload


def validate_export_options(exporter: Any, options: Any) -> dict[str, Any]:
    """``options`` checked against the exporter's ``options_schema`` (``invalid_argument``)."""
    given = dict(options or {})
    schema = getattr(exporter, "options_schema", None)
    if schema is None:
        if given:
            raise InvalidArgumentError(
                f"destination {exporter.name!r} takes no options",
                details={"destination": exporter.name, "options": sorted(given)},
            )
        return given
    errors = sorted(
        Draft202012Validator(schema).iter_errors(given), key=lambda e: (e.json_path, e.message)
    )
    if errors:
        raise InvalidArgumentError(
            f"options rejected by the {exporter.name!r} options_schema: {errors[0].message}",
            details={
                "destination": exporter.name,
                "errors": [{"path": e.json_path, "message": e.message} for e in errors],
            },
        )
    return given


# ---------------------------------------------------------------------------- tools
def regenerate(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    bundle = find_bundle(ctx, args["meeting_id"])
    check_scope(ctx, bundle, args.get("scope"))
    ensure_not_busy(ctx, bundle)
    check_config_policy(bundle.manifest.config)  # fail fast instead of inside the job
    job_id = enqueue_regenerate(
        ctx,
        bundle.meeting_id,
        force=bool(args.get("force", False)),
        reason=args.get("reason") or "regenerate",
    )
    return {"job_id": job_id, "meeting_id": bundle.meeting_id}


def export_minutes(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    meeting_id = args["meeting_id"]
    destination = args["destination"]
    exporter = get_exporter(destination)  # NotFoundError for unknown destinations, before any job
    options = validate_export_options(exporter, args.get("options"))
    request_id = args.get("request_id")
    with locked_bundle(
        ctx, meeting_id, scope=args.get("scope"), purpose="export_minutes", allow_recording=True
    ) as bundle:
        minutes_version = _resolve_minutes_version(bundle, args.get("minutes_version"))
        if args.get("run_async", False):

            def run(progress: JobProgress) -> dict[str, Any]:
                progress("export", 0.0)
                with ctx.locks.hold(meeting_id, purpose=JOB_LOCK_PURPOSE):
                    return perform_export(
                        ctx, meeting_id, destination, options, minutes_version, request_id
                    )

            return {"job_id": ctx.jobs.submit("export", meeting_id, run)}
        return {
            "result": perform_export(
                ctx, meeting_id, destination, options, minutes_version, request_id
            )
        }


def _resolve_minutes_version(bundle: Bundle, requested: Any) -> int:
    versions = sorted(v.version for v in bundle.manifest.minutes_versions)
    if not versions:
        raise NotFoundError(
            "no minutes have been generated for this meeting yet",
            details={"meeting_id": bundle.meeting_id},
        )
    minutes_version = int(requested or versions[-1])
    if minutes_version not in versions:
        raise NotFoundError(
            f"minutes version {minutes_version} does not exist",
            details={"meeting_id": bundle.meeting_id, "available": versions},
        )
    return minutes_version


def get_job_status(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    job = ctx.catalog.get_job(args["job_id"])
    if job is None:
        raise NotFoundError(f"job not found: {args['job_id']}", details={"job_id": args["job_id"]})
    if job["kind"] == "provider_setup":
        ctx.providers.observe_job(job["job_id"], job["status"])
    return {"job": job}


def cancel_job(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    """``JobManager.cancel``: queued → cancelled now, running → cooperative flag."""
    job = ctx.jobs.cancel(args["job_id"])
    if job["kind"] == "provider_setup":
        ctx.providers.observe_job(job["job_id"], job["status"])
    return {"job": job}
