"""Stage 2 (interval integration): alignment + transcripts + diarization → ``merged/merged.json``.

Single-source intervals pass through deterministically. Multi-source intervals are integrated by
the configured LLM provider with the fixed ``integrate_interval`` prompt. Without a provider the
merge is deterministic and lossless: the own tracks are *complementary* speakers (mic = ``me``,
system = the others), so each own column of an interval becomes its own segment; external
columns (another tool's transcript of the same speech) are redundant with them and only stand in
when no own column exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from narumi.align.pipeline_stage import (
    ALIGNMENT_KEY,
    REFERENCE_SOURCE,
    load_alignment,
    load_transcripts,
)
from narumi.bundle import Bundle, StageResult
from narumi.errors import InvalidArgumentError, NotFoundError
from narumi.generate.prompts import render_prompt
from narumi.generate.speakers import assign_by_overlap
from narumi.llm.base import LLMProvider
from narumi.llm.policy import check_policy
from narumi.llm.registry import get_provider, provider_profile
from narumi.models import (
    SPEAKER_ME,
    SPEAKER_OTHER,
    Alignment,
    Diarization,
    Interval,
    MeetingConfig,
    MergedSegment,
    MergedTranscript,
    Segment,
    SpeakerEntry,
    SpeakerEvidence,
    SpeakerMap,
    Transcript,
    Turn,
)

INTEGRATE_KEY = "merged/merged"
INTEGRATE_PATH = "merged/merged.json"
SPEAKER_MAP_PATH = "merged/speaker_map.json"
DIARIZATION_KEY_PREFIX = "diarization/"
INTEGRATE_PROMPT_VERSION = "integrate-v1"
INTEGRATE_PROMPT_NAME = "integrate_interval"
INTEGRATE_SYSTEM_PROMPT = (
    "あなたは会議の文字起こしを正確に扱う編集者です。指示された形式でのみ出力してください。"
)
PRODUCER = ("integrate", "1")
NO_SPACE_LANGUAGES = ("ja", "zh")


def uses_llm(provider: LLMProvider | None) -> bool:
    return provider is not None and provider.name != "none"


def join_texts(texts: list[str], language: str) -> str:
    """Concatenate segment texts; Japanese / Chinese need no separator, others a space."""
    parts = [t.strip() for t in texts if t and t.strip()]
    separator = "" if language.lower().startswith(NO_SPACE_LANGUAGES) else " "
    return separator.join(parts)


def preferred_source(candidates: list[str], reference: str) -> str:
    """Deterministic column preference: reference → own-system → own-mic → ext-* (sorted)."""
    for wanted in (reference, REFERENCE_SOURCE, "own-mic"):
        if wanted in candidates:
            return wanted
    return sorted(candidates)[0]


@dataclass(frozen=True)
class _Draft:
    """One merged segment before labelling: its span, text and the columns it was built from."""

    start: float
    end: float
    text: str
    columns: tuple[str, ...]


def integrate(
    alignment: Alignment,
    transcripts: dict[str, Transcript],
    diarizations: list[Diarization],
    config: MeetingConfig,
    provider: LLMProvider | None,
) -> MergedTranscript:
    """Build the integrated transcript. ``transcripts`` is keyed by ``source_id``.

    Every interval yields one segment, except deterministic multi-source intervals in which two
    or more *own* tracks speak: those yield one segment per own track so that neither speaker's
    words are dropped (the tracks are different people, not redundant transcriptions).
    Speaker turns are compared on the aligned clock: layer-1 turns carry their ``source_id`` and
    are shifted by ``alignment.offsets`` like the segments they came from.
    """
    index = _segment_index(transcripts)
    reference = str(alignment.params.get("reference") or REFERENCE_SOURCE)
    llm = uses_llm(provider)
    shifts = {
        sid: t.time_offset + alignment.offsets.get(sid, 0.0) for sid, t in transcripts.items()
    }
    layer1 = [
        _shift_turn(turn, alignment.offsets)
        for d in diarizations
        if d.layer == 1
        for turn in d.turns
    ]
    layer2 = [turn for d in diarizations if d.layer == 2 for turn in d.turns]
    tracks = {sid: t.track for sid, t in transcripts.items()}

    segments: list[MergedSegment] = []
    labels_seen: list[str] = []
    for interval in alignment.intervals:
        contributing = [sid for sid, ids in interval.columns.items() if ids]
        if not contributing:
            continue
        drafts = _interval_drafts(
            interval, contributing, index, reference, config, provider, llm, tracks, shifts
        )
        for draft in sorted(drafts, key=lambda d: (d.start, d.end, d.columns)):
            if not draft.text:
                continue
            label = _draft_label(draft, tracks, layer1, layer2)
            if label is not None and label not in labels_seen:
                labels_seen.append(label)
            sources = [
                seg_id
                for seg_id, _ in sorted(
                    (
                        (seg_id, index[seg_id][1])
                        for sid in draft.columns
                        for seg_id in interval.columns[sid]
                    ),
                    key=lambda pair: (pair[1].start + shifts[index[pair[0]][0]], pair[0]),
                )
            ]
            segments.append(
                MergedSegment(
                    id=f"m-{len(segments) + 1:05d}",
                    start=draft.start,
                    end=draft.end,
                    text=draft.text,
                    speaker_label=label,
                    sources=sources,
                )
            )

    speaker_map = build_speaker_map(labels_seen, config, diarizations)
    for segment in segments:
        segment.speaker_name = speaker_map.name_for(segment.speaker_label)

    return MergedTranscript(
        segments=segments,
        speaker_map=speaker_map,
        provider=provider.name if provider is not None else "none",
        params={
            "integration": "llm" if llm else "deterministic",
            "prompt_version": INTEGRATE_PROMPT_VERSION if llm else None,
            "reference": reference,
            "language": config.language,
            "self_name": config.self_name,
            "vocab_hints": list(config.vocab_hints),
            "diarization_layers": sorted({d.layer for d in diarizations}),
        },
    )


def build_speaker_map(
    labels: list[str], config: MeetingConfig, diarizations: list[Diarization]
) -> SpeakerMap:
    """``me`` → self_name (confidence 1.0 when known); every other label stays unresolved."""
    layer2_engines = [d.engine for d in diarizations if d.layer == 2]
    speakers: dict[str, SpeakerEntry] = {}
    for label in sorted(labels, key=_label_order):
        if label == SPEAKER_ME:
            speakers[label] = SpeakerEntry(
                name=config.self_name,
                confidence=1.0 if config.self_name else 0.0,
                evidence=[SpeakerEvidence(layer=1, detail="mic track (own-mic)")],
            )
        elif label == SPEAKER_OTHER:
            speakers[label] = SpeakerEntry(
                name=None,
                confidence=0.0,
                evidence=[SpeakerEvidence(layer=1, detail="system track (own-system)")],
            )
        else:
            detail = ", ".join(f"{e.name} {e.version}" for e in layer2_engines) or "layer2"
            speakers[label] = SpeakerEntry(
                name=None,
                confidence=0.0,
                evidence=[SpeakerEvidence(layer=2, detail=detail)],
            )
    return SpeakerMap(speakers=speakers)


def _label_order(label: str) -> tuple[int, str]:
    if label == SPEAKER_ME:
        return (0, label)
    if label == SPEAKER_OTHER:
        return (1, label)
    return (2, label)


def _segment_index(transcripts: dict[str, Transcript]) -> dict[str, tuple[str, Segment]]:
    index: dict[str, tuple[str, Segment]] = {}
    for source_id, transcript in transcripts.items():
        if transcript.source_id != source_id:
            raise InvalidArgumentError(
                f"transcripts must be keyed by source_id ({source_id!r} != "
                f"{transcript.source_id!r})"
            )
        for segment in transcript.segments:
            index[segment.id] = (source_id, segment)
    return index


def _column_texts(
    interval: Interval, source_id: str, index: dict[str, tuple[str, Segment]]
) -> list[str]:
    texts: list[str] = []
    for seg_id in interval.columns[source_id]:
        if seg_id not in index:
            raise InvalidArgumentError(
                f"alignment refers to unknown segment {seg_id}",
                details={"interval": interval.id, "source": source_id},
            )
        texts.append(index[seg_id][1].text)
    return texts


def _column_span(
    interval: Interval,
    source_id: str,
    index: dict[str, tuple[str, Segment]],
    shifts: dict[str, float],
) -> tuple[float, float]:
    """Aligned-clock span of one column (same projection as ``align.intervals.place_segments``)."""
    shift = shifts.get(source_id, 0.0)
    starts: list[float] = []
    ends: list[float] = []
    for seg_id in interval.columns[source_id]:
        segment = index[seg_id][1]
        start = max(0.0, segment.start + shift)
        starts.append(start)
        ends.append(max(start, segment.end + shift))
    return round(min(starts), 3), round(max(ends), 3)


def _interval_drafts(
    interval: Interval,
    contributing: list[str],
    index: dict[str, tuple[str, Segment]],
    reference: str,
    config: MeetingConfig,
    provider: LLMProvider | None,
    llm: bool,
    tracks: dict[str, str | None],
    shifts: dict[str, float],
) -> list[_Draft]:
    """Segments an interval contributes: one, or one per own track in deterministic mode."""
    language = config.language
    if len(contributing) == 1:
        text = join_texts(_column_texts(interval, contributing[0], index), language)
        return [_Draft(interval.start, interval.end, text, (contributing[0],))]
    if not llm:
        own = [sid for sid in contributing if tracks.get(sid)]
        if not own:  # external transcripts of the same speech: pick one deterministically
            chosen = preferred_source(contributing, reference)
            text = join_texts(_column_texts(interval, chosen, index), language)
            return [_Draft(interval.start, interval.end, text, (chosen,))]
        drafts: list[_Draft] = []
        for sid in own:  # complementary speakers: keep every own track's words
            start, end = _column_span(interval, sid, index, shifts)
            text = join_texts(_column_texts(interval, sid, index), language)
            drafts.append(_Draft(start, end, text, (sid,)))
        return drafts
    assert provider is not None
    columns = "\n\n".join(
        f"[{sid}]\n{join_texts(_column_texts(interval, sid, index), language)}"
        for sid in sorted(contributing, key=lambda s: (s != reference, s))
    )
    prompt = render_prompt(
        INTEGRATE_PROMPT_NAME,
        start=_clock(interval.start),
        end=_clock(interval.end),
        reference=reference,
        vocab_hints=", ".join(config.vocab_hints) if config.vocab_hints else "なし",
        columns=columns,
    )
    text = provider.complete(prompt, system=INTEGRATE_SYSTEM_PROMPT).strip()
    return [_Draft(interval.start, interval.end, text, tuple(contributing))]


def _shift_turn(turn: Turn, offsets: dict[str, float]) -> Turn:
    """Project a layer-1 turn onto the aligned clock (``+ offsets[source_id]``)."""
    offset = offsets.get(turn.source_id, 0.0) if turn.source_id else 0.0
    if offset == 0.0:
        return turn
    start = max(0.0, turn.start + offset)
    return turn.model_copy(update={"start": start, "end": max(start, turn.end + offset)})


def _draft_label(
    draft: _Draft,
    tracks: dict[str, str | None],
    layer1: list[Turn],
    layer2: list[Turn],
) -> str | None:
    own_tracks = {tracks.get(sid) for sid in draft.columns if tracks.get(sid)}
    if len(draft.columns) == 1 and own_tracks:
        # A single own column is that track's speaker by construction (layer 1 is derived from
        # the same segments); no overlap vote needed.
        label = SPEAKER_ME if own_tracks == {"mic"} else SPEAKER_OTHER
    else:
        label = assign_by_overlap(draft.start, draft.end, layer1)
        if label is None:
            if own_tracks == {"mic"}:
                label = SPEAKER_ME
            elif own_tracks == {"system"}:
                label = SPEAKER_OTHER
    if label == SPEAKER_OTHER and layer2:
        refined = assign_by_overlap(draft.start, draft.end, layer2)
        if refined is not None:
            label = refined
    return label


def _clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


# ---------------------------------------------------------------------------- bundle stage
def diarization_artifact_keys(bundle: Bundle) -> list[str]:
    return sorted(k for k in bundle.manifest.artifacts if k.startswith(DIARIZATION_KEY_PREFIX))


def load_diarizations(bundle: Bundle) -> dict[str, Diarization]:
    result: dict[str, Diarization] = {}
    for key in diarization_artifact_keys(bundle):
        record = bundle.artifact(key)
        assert record is not None
        result[key] = Diarization.model_validate(bundle.read_json(record.path))
    return result


def load_merged(bundle: Bundle) -> MergedTranscript:
    record = bundle.artifact(INTEGRATE_KEY)
    if record is None:
        raise NotFoundError("merged transcript not generated yet", details={"key": INTEGRATE_KEY})
    return MergedTranscript.model_validate(bundle.read_json(record.path))


def run_integrate(bundle: Bundle, *, force: bool = False) -> StageResult:
    """Run stage 2 idempotently; the send policy is checked before anything is instantiated."""
    config = bundle.manifest.config
    alignment = load_alignment(bundle)
    transcripts_by_key = load_transcripts(bundle)
    diarizations_by_key = load_diarizations(bundle)
    inputs = {ALIGNMENT_KEY: bundle.artifact_hash(ALIGNMENT_KEY)}
    inputs.update({key: bundle.artifact_hash(key) for key in transcripts_by_key})
    inputs.update({key: bundle.artifact_hash(key) for key in diarizations_by_key})
    name = config.llm_provider
    check_policy(provider_profile(name), config.external_send_policy, provider=name)
    params = {
        "provider": name,
        "prompt_version": INTEGRATE_PROMPT_VERSION,
        "self_name": config.self_name,
        "language": config.language,
        # Part of the LLM prompt, hence of the idempotency key (unconditional so the key shape is
        # the same in deterministic mode, like ``run_transcribe``).
        "vocab_hints": list(config.vocab_hints),
    }

    def produce(out: Path) -> None:
        provider = get_provider(name)
        merged = integrate(
            alignment,
            {t.source_id: t for t in transcripts_by_key.values()},
            list(diarizations_by_key.values()),
            config,
            provider,
        )
        bundle.write_json(bundle.relpath(out), merged)
        bundle.write_json(SPEAKER_MAP_PATH, merged.speaker_map)

    return bundle.run_stage(
        INTEGRATE_KEY,
        inputs=inputs,
        params=params,
        producer=PRODUCER,
        output=INTEGRATE_PATH,
        fn=produce,
        force=force,
    )
