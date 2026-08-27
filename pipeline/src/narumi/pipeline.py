"""Pipeline orchestrator: process / regenerate / export for one bundle.

Stage modules are owned by their packages; this module only wires them in order and keeps the
"regenerate re-runs from alignment onward" rule. It is catalog-agnostic: the bundle on disk is
the source of truth (AGENTS.md 絶対原則 1) and callers (server handlers, dev CLI) refresh
``narumi.db`` afterwards.

Stage order and artifact keys (``Bundle.run_stage`` keys, one per deterministic output):

1. ``preprocess`` → ``preprocess/audio/{mic,system}``
2. ``transcribe`` → ``transcripts/own-{mic,system}``
3. ``diarize``    → ``diarization/layer1`` (+ ``diarization/layer2`` unless ``none``)
4. ``align``      → ``merged/alignment``
5. ``integrate``  → ``merged/merged`` (``merged/speaker_map.json`` is a convenience copy)
6. ``generate``   → ``minutes/v<N>`` (append-only; skipped when nothing changed)

``regenerate_meeting`` runs steps 4-6 only and appends a :class:`RegenerationRecord`.
``refresh_meeting`` (what the MCP ``regenerate`` tool runs) first brings steps 1-3 up to date
idempotently — they only run when they never ran or their params changed — and then runs 4-6.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from narumi.align import run_align, transcript_artifact_keys
from narumi.bundle import Bundle, ExportRecord, RegenerationRecord, StageResult, utc_now_iso
from narumi.diarize import run_diarize
from narumi.errors import CancelledError, NotFoundError
from narumi.export import get_exporter
from narumi.generate import run_generate, run_integrate
from narumi.models import MinutesMeta
from narumi.preprocess import run_preprocess
from narumi.transcribe import run_transcribe

ProgressFn = Callable[[str, float], None]
StepFn = Callable[[Bundle, bool], StageResult | Sequence[StageResult]]

STAGE_PREPROCESS = "preprocess"
STAGE_TRANSCRIBE = "transcribe"
STAGE_DIARIZE = "diarize"
STAGE_ALIGN = "align"
STAGE_INTEGRATE = "integrate"
STAGE_GENERATE = "generate"

PROCESS_STEPS: tuple[tuple[str, StepFn], ...] = (
    (STAGE_PREPROCESS, lambda bundle, force: run_preprocess(bundle, force=force)),
    (STAGE_TRANSCRIBE, lambda bundle, force: run_transcribe(bundle, force=force)),
    (STAGE_DIARIZE, lambda bundle, force: run_diarize(bundle, force=force)),
    (STAGE_ALIGN, lambda bundle, force: run_align(bundle, force=force)),
    (STAGE_INTEGRATE, lambda bundle, force: run_integrate(bundle, force=force)),
    (STAGE_GENERATE, lambda bundle, force: run_generate(bundle, force=force)),
)
"""Full run in order. Every step is idempotent through ``Bundle.run_stage``."""

REGENERATE_STEPS: tuple[tuple[str, StepFn], ...] = PROCESS_STEPS[3:]
"""``align`` → ``integrate`` → ``generate``: never preprocess / transcribe / diarize again."""

STAGE_ORDER: tuple[str, ...] = tuple(name for name, _ in PROCESS_STEPS)


@dataclass
class ProcessResult:
    meeting_id: str
    minutes_version: int | None
    stages: list[str] = field(default_factory=list)
    """Artifact keys that were (re)computed in this run, in execution order."""
    skipped: list[str] = field(default_factory=list)
    """Artifact keys whose inputs / params were unchanged (existing output reused)."""
    unresolved_speakers: list[str] = field(default_factory=list)


@dataclass
class ExportResult:
    destination: str
    ref: str
    minutes_version: int
    at: str
    details: dict[str, Any] = field(default_factory=dict)


def process_meeting(
    bundle: Bundle,
    *,
    force: bool = False,
    progress: ProgressFn | None = None,
) -> ProcessResult:
    """Full run: preprocess → transcribe → diarize → align → integrate → generate.

    Sets ``manifest.status`` to ``processing`` first, ``ready`` on success and ``failed`` when
    any stage raises (the exception is re-raised unchanged; nothing is swallowed).
    """
    return _run_steps(bundle, PROCESS_STEPS, force=force, progress=progress)


def regenerate_meeting(
    bundle: Bundle,
    *,
    force: bool = False,
    progress: ProgressFn | None = None,
    reason: str = "regenerate",
    job_id: str | None = None,
) -> ProcessResult:
    """Re-run alignment → integrate → generate only (never preprocess / transcribe / diarize).

    A :class:`RegenerationRecord` is appended to ``manifest.regenerations`` on success, even when
    every stage was skipped (the record then points at the unchanged latest version).
    """
    if not transcript_artifact_keys(bundle):
        raise NotFoundError(
            "no transcripts to regenerate from; run process_meeting first",
            details={"meeting_id": bundle.meeting_id, "status": bundle.manifest.status},
        )
    result = _run_steps(bundle, REGENERATE_STEPS, force=force, progress=progress)
    _record_regeneration(bundle, result, reason=reason, job_id=job_id)
    return result


def refresh_meeting(
    bundle: Bundle,
    *,
    force: bool = False,
    progress: ProgressFn | None = None,
    reason: str = "regenerate",
    job_id: str | None = None,
) -> ProcessResult:
    """Bring the minutes up to date with the bundle and its config (the MCP ``regenerate`` tool).

    The deterministic stages (preprocess / transcribe / diarize) run through the same idempotent
    ``run_stage`` check as :func:`process_meeting`: they only do work when they never ran (a
    meeting stopped with ``auto_process=false``, a process job that failed) or when their params
    changed (``set_meeting_config``: transcription_engine / diarization_engine / language /
    vocab_hints). They are never forced. Alignment → integrate → generate then run, forced when
    ``force`` is true. A :class:`RegenerationRecord` is appended like in
    :func:`regenerate_meeting`.
    """
    forced = {name for name, _ in REGENERATE_STEPS} if force else set()
    result = _run_steps(bundle, PROCESS_STEPS, force=forced, progress=progress)
    _record_regeneration(bundle, result, reason=reason, job_id=job_id)
    return result


def export_meeting(
    bundle: Bundle,
    destination: str,
    *,
    options: dict[str, Any] | None = None,
    minutes_version: int | None = None,
    request_id: str | None = None,
) -> ExportResult:
    """Export a minutes version through a registered exporter and record it in the manifest.

    ``minutes_version`` defaults to the latest version; ``NotFoundError`` when the meeting has no
    minutes yet, the version does not exist or ``destination`` is not a registered exporter.
    """
    exporter = get_exporter(destination)
    versions = sorted(v.version for v in bundle.manifest.minutes_versions)
    if not versions:
        raise NotFoundError(
            "no minutes have been generated for this meeting yet",
            details={"meeting_id": bundle.meeting_id},
        )
    version = versions[-1] if minutes_version is None else int(minutes_version)
    if version not in versions:
        raise NotFoundError(
            f"minutes version {version} does not exist",
            details={"meeting_id": bundle.meeting_id, "available": versions},
        )
    outcome = exporter.export(bundle, minutes_version=version, options=dict(options or {}))
    bundle.manifest.exports.append(
        ExportRecord(
            destination=outcome.destination,
            ref=outcome.ref,
            minutes_version=outcome.minutes_version,
            at=outcome.at,
            request_id=request_id,
        )
    )
    bundle.save()
    return ExportResult(
        destination=outcome.destination,
        ref=outcome.ref,
        minutes_version=outcome.minutes_version,
        at=outcome.at,
        details=dict(outcome.details),
    )


# ---------------------------------------------------------------------------- internals
def _record_regeneration(
    bundle: Bundle, result: ProcessResult, *, reason: str, job_id: str | None
) -> None:
    bundle.manifest.regenerations.append(
        RegenerationRecord(
            job_id=job_id,
            at=utc_now_iso(),
            reason=reason,
            minutes_version=result.minutes_version,
        )
    )
    bundle.save()


def _run_steps(
    bundle: Bundle,
    steps: Sequence[tuple[str, StepFn]],
    *,
    force: bool | Collection[str],
    progress: ProgressFn | None,
) -> ProcessResult:
    """Run ``steps`` in order; ``force`` is a bool for all of them or the names to force.

    A :class:`CancelledError` raised by a step or the ``progress`` hook (cooperative job
    cancellation) restores ``manifest.status`` to what it was before this run — the meeting is
    not ``failed``, and completed stage outputs stay for the next run. Any other exception
    marks the meeting ``failed``; both are re-raised unchanged.
    """
    previous_status = bundle.manifest.status
    _set_status(bundle, "processing")
    result = ProcessResult(meeting_id=bundle.meeting_id, minutes_version=None)
    total = len(steps)
    try:
        for index, (name, step) in enumerate(steps, start=1):
            outcome = step(bundle, force if isinstance(force, bool) else name in force)
            for stage in [outcome] if isinstance(outcome, StageResult) else outcome:
                (result.skipped if stage.skipped else result.stages).append(stage.key)
            if progress is not None:
                progress(name, index / total)
    except CancelledError:
        _set_status(bundle, previous_status)
        raise
    except Exception:
        _set_status(bundle, "failed")
        raise
    result.minutes_version = bundle.manifest.latest_minutes_version
    result.unresolved_speakers = _unresolved_speakers(bundle, result.minutes_version)
    _set_status(bundle, "ready")
    return result


def _set_status(bundle: Bundle, status: str) -> None:
    bundle.manifest.status = status  # type: ignore[assignment]  # MeetingStatus literal
    bundle.save()


def _unresolved_speakers(bundle: Bundle, version: int | None) -> list[str]:
    """``unresolved_speakers`` from ``minutes/v<N>/meta.json`` (empty when there is no version)."""
    if version is None:
        return []
    meta = MinutesMeta.model_validate(bundle.read_json(f"minutes/v{version}/meta.json"))
    return list(meta.unresolved_speakers)
