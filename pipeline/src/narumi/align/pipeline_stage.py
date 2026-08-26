"""Stage 1 (deterministic alignment): transcripts → ``merged/alignment.json``."""

from __future__ import annotations

from pathlib import Path

from narumi.align.anchors import estimate_offset, find_anchors
from narumi.align.intervals import build_intervals
from narumi.bundle import Bundle, StageResult
from narumi.errors import InvalidArgumentError, NotFoundError
from narumi.models import Alignment, Anchor, Transcript

ALIGNMENT_KEY = "merged/alignment"
ALIGNMENT_PATH = "merged/alignment.json"
TRANSCRIPT_KEY_PREFIX = "transcripts/"
REFERENCE_SOURCE = "own-system"
PRODUCER = ("align", "1")


def choose_reference(transcripts: list[Transcript], reference: str | None = None) -> str:
    """``reference`` if given, else ``own-system`` when present, else the first source."""
    if not transcripts:
        raise InvalidArgumentError("no transcripts to align")
    ids = [t.source_id for t in transcripts]
    if reference is not None:
        if reference not in ids:
            raise InvalidArgumentError(
                f"reference source not present: {reference}", details={"sources": ids}
            )
        return reference
    return REFERENCE_SOURCE if REFERENCE_SOURCE in ids else ids[0]


def build_alignment(
    transcripts: list[Transcript],
    *,
    reference: str | None = None,
    n: int = 8,
    gap: float = 0.5,
) -> Alignment:
    """Estimate per-source clock corrections against the reference and build intervals.

    ``offsets[source]`` is the correction **added** to that source's times to land on the
    reference clock (``offsets[reference] == 0``). Sources without enough anchors get ``0.0`` and
    are listed in ``params["unaligned"]``.
    """
    ref_id = choose_reference(transcripts, reference)
    by_id = {t.source_id: t for t in transcripts}
    if len(by_id) != len(transcripts):
        raise InvalidArgumentError("duplicate transcript source_id")
    ref = by_id[ref_id]
    offsets: dict[str, float] = {ref_id: 0.0}
    anchors: list[Anchor] = []
    unaligned: list[str] = []
    for transcript in transcripts:
        if transcript.source_id == ref_id:
            continue
        found = find_anchors(ref, transcript, n=n)
        anchors.extend(found)
        estimate = estimate_offset(found)
        if estimate is None:
            offsets[transcript.source_id] = 0.0
            unaligned.append(transcript.source_id)
        else:
            # find_anchors(ref, other) measures other − ref; the correction is the negation.
            offsets[transcript.source_id] = round(-estimate, 3)
    intervals = build_intervals(transcripts, offsets, gap=gap)
    return Alignment(
        intervals=intervals,
        offsets=offsets,
        anchors=anchors,
        params={"n": n, "gap": gap, "reference": ref_id, "unaligned": unaligned},
    )


def transcript_artifact_keys(bundle: Bundle) -> list[str]:
    """Sorted artifact keys of every transcript source (``transcripts/own-*`` / ``ext-*``)."""
    return sorted(k for k in bundle.manifest.artifacts if k.startswith(TRANSCRIPT_KEY_PREFIX))


def load_transcripts(bundle: Bundle) -> dict[str, Transcript]:
    """Read every transcript artifact as ``{artifact_key: Transcript}`` (sorted by key)."""
    result: dict[str, Transcript] = {}
    for key in transcript_artifact_keys(bundle):
        record = bundle.artifact(key)
        assert record is not None
        result[key] = Transcript.model_validate(bundle.read_json(record.path))
    return result


def run_align(bundle: Bundle, *, force: bool = False, n: int = 8, gap: float = 0.5) -> StageResult:
    """Run stage 1 idempotently. Inputs are the hashes of all ``transcripts/*`` artifacts."""
    transcripts = load_transcripts(bundle)
    if not transcripts:
        raise NotFoundError(
            "no transcript artifacts to align", details={"meeting_id": bundle.meeting_id}
        )
    ordered = list(transcripts.values())
    ref_id = choose_reference(ordered)
    inputs = {key: bundle.artifact_hash(key) for key in transcripts}
    params = {"n": n, "gap": gap, "reference": ref_id}

    def produce(out: Path) -> None:
        alignment = build_alignment(ordered, reference=ref_id, n=n, gap=gap)
        bundle.write_json(bundle.relpath(out), alignment)

    return bundle.run_stage(
        ALIGNMENT_KEY,
        inputs=inputs,
        params=params,
        producer=PRODUCER,
        output=ALIGNMENT_PATH,
        fn=produce,
        force=force,
    )


def load_alignment(bundle: Bundle) -> Alignment:
    record = bundle.artifact(ALIGNMENT_KEY)
    if record is None:
        raise NotFoundError("alignment not generated yet", details={"key": ALIGNMENT_KEY})
    return Alignment.model_validate(bundle.read_json(record.path))
