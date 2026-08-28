"""Pipeline orchestrator: process / regenerate / export for one bundle.

Stage modules are owned by their packages; this module only wires them in order and keeps the
"regenerate re-runs from alignment onward" rule. It is catalog-agnostic: the bundle on disk is
the source of truth (AGENTS.md 絶対原則 1) and callers (server handlers, dev CLI) refresh
``narumi.db`` afterwards.

Stage order and artifact keys (``Bundle.run_stage`` keys, one per deterministic output):

1. ``preprocess`` → ``preprocess/audio/{mic,system}``
2. ``brief``      → ``context/brief`` (meeting brief; gaia-library only when ``NARUMI_GAIA_URL``
   is set — its merged vocab_hints feed transcription and the LLM stages)
3. ``transcribe`` → ``transcripts/own-{mic,system}``
4. ``diarize``    → ``diarization/layer1`` (+ ``diarization/layer2`` unless ``none``,
   + ``diarization/layer4`` when ``transcripts/ext-*`` exist)
5. ``slides``     → ``preprocess/slides`` (screen track only; skipped without one)
6. ``align``      → ``merged/alignment`` (all ``transcripts/*``, own and ext)
7. ``layer3``     → ``diarization/layer3`` (screen vision; only a vision-capable provider the
   send policy allows — a disallowed one raises, 絶対原則 4)
8. ``integrate``  → ``merged/merged`` (``merged/speaker_map.json`` is a convenience copy;
   incremental via ``merged/integrate_cache.json``, consumes layer-3/-4 names)
9. ``generate``   → ``minutes/v<N>`` (append-only; embeds key slides + the brief)

``regenerate_meeting`` runs align → integrate → generate only and appends a
:class:`RegenerationRecord`. ``refresh_meeting`` (what the MCP ``regenerate`` tool runs) first
brings every other stage up to date idempotently — they only run when they never ran or their
inputs / params changed (a newly registered external transcript re-runs layer 4 and alignment
automatically) — and then runs align → integrate → generate.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from narumi.align import run_align, transcript_artifact_keys
from narumi.brief import load_brief, run_brief
from narumi.bundle import Bundle, ExportRecord, RegenerationRecord, StageResult, utc_now_iso
from narumi.diarize import run_diarize, run_layer3, run_layer4
from narumi.errors import CancelledError, InvalidArgumentError, NotFoundError
from narumi.export import GaiaExporter, get_exporter
from narumi.gaia import GaiaClient
from narumi.generate import run_generate, run_integrate
from narumi.models import MinutesMeta
from narumi.preprocess import run_preprocess
from narumi.slides import run_slides
from narumi.transcribe import run_transcribe

if TYPE_CHECKING:
    from narumi.providers.generation import MinutesResolver

ProgressFn = Callable[[str, float], None]
StepFn = Callable[[Bundle, bool], StageResult | Sequence[StageResult]]
GaiaClientFactory = Callable[[], GaiaClient | None]

STAGE_PREPROCESS = "preprocess"
STAGE_BRIEF = "brief"
STAGE_TRANSCRIBE = "transcribe"
STAGE_DIARIZE = "diarize"
STAGE_SLIDES = "slides"
STAGE_ALIGN = "align"
STAGE_LAYER3 = "layer3"
STAGE_INTEGRATE = "integrate"
STAGE_GENERATE = "generate"


def _step_brief(
    bundle: Bundle, force: bool, *, client_factory: GaiaClientFactory | None = None
) -> StageResult:
    """Build the meeting brief; gaia-library is consulted only when ``NARUMI_GAIA_URL`` is set.

    ``GaiaClient.from_env()`` returns ``None`` without the env var (任意依存: local-only brief);
    with it set, an unreachable gaia-library raises instead of silently thinning the brief.
    """
    factory = GaiaClient.from_env if client_factory is None else client_factory
    return run_brief(bundle, factory(), force=force)


def _step_transcribe(bundle: Bundle, force: bool) -> Sequence[StageResult]:
    """Transcribe with the brief's merged vocab hints (config + gaia glossary)."""
    brief = load_brief(bundle)
    hints = None if brief is None else list(brief.vocab_hints)
    return run_transcribe(bundle, force=force, vocab_hints=hints)


def _step_diarize(bundle: Bundle, force: bool) -> Sequence[StageResult]:
    """Layers 1-2 from the own tracks, layer 4 from the parsed external transcripts."""
    results = list(run_diarize(bundle, force=force))
    layer4 = run_layer4(bundle, force=force)
    if layer4 is not None:
        results.append(layer4)
    return results


def _step_slides(bundle: Bundle, force: bool) -> Sequence[StageResult]:
    result = run_slides(bundle, force=force)
    return [] if result is None else [result]


def _step_layer3(bundle: Bundle, force: bool) -> Sequence[StageResult]:
    result = run_layer3(bundle, force=force)
    return [] if result is None else [result]


PROCESS_STEPS: tuple[tuple[str, StepFn], ...] = (
    (STAGE_PREPROCESS, lambda bundle, force: run_preprocess(bundle, force=force)),
    (STAGE_BRIEF, _step_brief),
    (STAGE_TRANSCRIBE, _step_transcribe),
    (STAGE_DIARIZE, _step_diarize),
    (STAGE_SLIDES, _step_slides),
    (STAGE_ALIGN, lambda bundle, force: run_align(bundle, force=force)),
    (STAGE_LAYER3, _step_layer3),
    (STAGE_INTEGRATE, lambda bundle, force: run_integrate(bundle, force=force)),
    (STAGE_GENERATE, lambda bundle, force: run_generate(bundle, force=force)),
)
"""Full run in order. Every step is idempotent through ``Bundle.run_stage``; ``slides`` and
``layer3`` may also skip with no artifact (no screen track / no vision provider)."""

_REGENERATE_NAMES = (STAGE_ALIGN, STAGE_INTEGRATE, STAGE_GENERATE)

REGENERATE_STEPS: tuple[tuple[str, StepFn], ...] = tuple(
    step for step in PROCESS_STEPS if step[0] in _REGENERATE_NAMES
)
"""``align`` → ``integrate`` → ``generate``: never preprocess / brief / transcribe / diarize /
slides / layer3 again (their outputs are reused as-is; ``refresh_meeting`` brings them up to
date first when needed)."""

STAGE_ORDER: tuple[str, ...] = tuple(name for name, _ in PROCESS_STEPS)


def _process_steps(
    client_factory: GaiaClientFactory | None,
    minutes_resolver: MinutesResolver | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Sequence[tuple[str, StepFn]]:
    """Bind the caller's connection settings without changing the shared stage registry."""
    if client_factory is None:
        return _minutes_steps(PROCESS_STEPS, minutes_resolver, should_cancel)
    steps = tuple(
        (
            name,
            (lambda bundle, force: _step_brief(bundle, force, client_factory=client_factory))
            if name == STAGE_BRIEF and client_factory is not None
            else step,
        )
        for name, step in PROCESS_STEPS
    )
    return _minutes_steps(steps, minutes_resolver, should_cancel)


def _minutes_steps(
    steps: Sequence[tuple[str, StepFn]],
    resolver: MinutesResolver | None,
    should_cancel: Callable[[], bool] | None,
) -> Sequence[tuple[str, StepFn]]:
    if resolver is None and should_cancel is None:
        return steps
    return tuple(
        (
            name,
            (
                lambda bundle, force: run_generate(
                    bundle, force=force, minutes_resolver=resolver, should_cancel=should_cancel
                )
            )
            if name == STAGE_GENERATE
            else step,
        )
        for name, step in steps
    )


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
    gaia_client_factory: GaiaClientFactory | None = None,
    minutes_resolver: MinutesResolver | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ProcessResult:
    """Full run: preprocess → brief → transcribe → diarize → slides → align → layer3 →
    integrate → generate.

    Sets ``manifest.status`` to ``processing`` first, ``ready`` on success and ``failed`` when
    any stage raises (the exception is re-raised unchanged; nothing is swallowed).
    """
    _check_minutes_force(bundle, force)
    return _run_steps(
        bundle,
        _process_steps(gaia_client_factory, minutes_resolver, should_cancel),
        force=force,
        progress=progress,
    )


def regenerate_meeting(
    bundle: Bundle,
    *,
    force: bool = False,
    progress: ProgressFn | None = None,
    reason: str = "regenerate",
    job_id: str | None = None,
    minutes_resolver: MinutesResolver | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ProcessResult:
    """Re-run alignment → integrate → generate only (never preprocess / transcribe / diarize).

    A :class:`RegenerationRecord` is appended to ``manifest.regenerations`` on success, even when
    every stage was skipped (the record then points at the unchanged latest version).
    """
    _check_minutes_force(bundle, force)
    if not transcript_artifact_keys(bundle):
        raise NotFoundError(
            "no transcripts to regenerate from; run process_meeting first",
            details={"meeting_id": bundle.meeting_id, "status": bundle.manifest.status},
        )
    result = _run_steps(
        bundle,
        _minutes_steps(REGENERATE_STEPS, minutes_resolver, should_cancel),
        force=force,
        progress=progress,
    )
    _record_regeneration(bundle, result, reason=reason, job_id=job_id)
    return result


def refresh_meeting(
    bundle: Bundle,
    *,
    force: bool = False,
    progress: ProgressFn | None = None,
    reason: str = "regenerate",
    job_id: str | None = None,
    gaia_client_factory: GaiaClientFactory | None = None,
    minutes_resolver: MinutesResolver | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ProcessResult:
    """Bring the minutes up to date with the bundle and its config (the MCP ``regenerate`` tool).

    The upstream stages (preprocess / brief / transcribe / diarize incl. layer 4 / slides /
    layer 3) run through the same idempotent ``run_stage`` check as :func:`process_meeting`:
    they only do work when they never ran (a meeting stopped with ``auto_process=false``, a
    process job that failed), when their params changed (``set_meeting_config``:
    transcription_engine / diarization_engine / language / vocab_hints) or when their inputs
    changed (``register_context``: a newly parsed ``transcripts/ext-*`` re-runs layer 4 and
    alignment picks it up). They are never forced. Alignment → integrate → generate then run,
    forced when ``force`` is true. A :class:`RegenerationRecord` is appended like in
    :func:`regenerate_meeting`.
    """
    _check_minutes_force(bundle, force)
    forced = {name for name, _ in REGENERATE_STEPS} if force else set()
    result = _run_steps(
        bundle,
        _process_steps(gaia_client_factory, minutes_resolver, should_cancel),
        force=forced,
        progress=progress,
    )
    _record_regeneration(bundle, result, reason=reason, job_id=job_id)
    return result


def _check_minutes_force(bundle: Bundle, force: bool) -> None:
    if force and bundle.manifest.config.minutes_model is not None:
        raise InvalidArgumentError(
            "Codex minutes cannot use force; start a new cache epoch instead"
        )


def export_meeting(
    bundle: Bundle,
    destination: str,
    *,
    options: dict[str, Any] | None = None,
    minutes_version: int | None = None,
    request_id: str | None = None,
    gaia_client_factory: GaiaClientFactory | None = None,
) -> ExportResult:
    """Export a minutes version through a registered exporter and record it in the manifest.

    ``minutes_version`` defaults to the latest version; ``NotFoundError`` when the meeting has no
    minutes yet, the version does not exist or ``destination`` is not a registered exporter.
    """
    exporter = get_exporter(destination)
    if gaia_client_factory is not None and isinstance(exporter, GaiaExporter):
        exporter = GaiaExporter(client_factory=gaia_client_factory)
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
