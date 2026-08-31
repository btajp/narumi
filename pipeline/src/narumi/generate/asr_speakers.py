"""Anonymous ASR speaker evidence, separate from explicitly selected diarization engines.

The transcription adapter already namespaces labels by track and upload chunk. These turns
exist only during integration: they are not layer-4 names or a saved layer-2 diarization.
"""

from __future__ import annotations

from collections.abc import Collection

from narumi.generate.speakers import overlap
from narumi.models import MergedSegment, Segment, SpeakerEvidence, SpeakerMap, Transcript, Turn

ASR_EVIDENCE_LAYER = 2
ASR_PROVIDER = "openai-api"
ASR_MODEL = "gpt-4o-transcribe-diarize"


def build_asr_turns(transcripts: dict[str, Transcript], offsets: dict[str, float]) -> list[Turn]:
    """Project own API diarize segments onto the aligned clock exactly once.

    Other engines' ``Segment.speaker`` fields are not ASR evidence. Native labels and chunk
    offsets have already been handled by the transcription adapter; only the transcript and
    alignment offsets are added here, using the same projection as alignment's segments.
    """
    turns: list[Turn] = []
    for source_id, transcript in transcripts.items():
        if (
            transcript.kind != "own"
            or transcript.track is None
            or transcript.engine.name != ASR_PROVIDER
            or transcript.engine.params.get("model") != ASR_MODEL
        ):
            continue
        shift = transcript.time_offset + offsets.get(source_id, 0.0)
        for segment in transcript.segments:
            if segment.speaker is None:
                continue
            start = max(0.0, segment.start + shift)
            turns.append(
                Turn(
                    start=start,
                    end=max(start, segment.end + shift),
                    speaker=segment.speaker,
                    confidence=0.0,
                    layer=ASR_EVIDENCE_LAYER,
                    source_id=source_id,
                )
            )
    return sorted(turns, key=lambda turn: (turn.start, turn.end, turn.speaker, turn.source_id))


def matching_asr_turns(
    start: float, end: float, sources: Collection[str], turns: list[Turn]
) -> list[Turn]:
    """Only contributing sources may provide anonymous candidates for a merged segment."""
    return [
        turn
        for turn in turns
        if turn.source_id in sources and overlap(start, end, turn.start, turn.end) > 0
    ]


def append_asr_evidence(
    segments: list[MergedSegment],
    speaker_map: SpeakerMap,
    index: dict[str, tuple[str, Segment]],
    turns: list[Turn],
) -> None:
    """Retain every anonymous candidate, including conflicts and labels not adopted.

    Evidence is rebuilt even when interval rows came from the cache. An anonymous candidate
    cannot set an identity's name or confidence, including the microphone's ``me`` identity.
    """
    candidates: dict[str, set[tuple[str, str]]] = {}
    for segment in segments:
        if segment.speaker_label is None:
            continue
        sources = {index[seg_id][0] for seg_id in segment.sources}
        for turn in matching_asr_turns(segment.start, segment.end, sources, turns):
            assert turn.source_id is not None
            candidates.setdefault(segment.speaker_label, set()).add((turn.source_id, turn.speaker))
    for label, source_labels in sorted(candidates.items()):
        entry = speaker_map.speakers[label]
        for source_id, asr_label in sorted(source_labels):
            namespace = ":".join(asr_label.split(":", 3)[:3])
            entry.evidence.append(
                SpeakerEvidence(
                    layer=ASR_EVIDENCE_LAYER,
                    detail=(
                        f"ASR anonymous speaker; source={source_id}; "
                        f"namespace={namespace}; label={asr_label}"
                    ),
                )
            )
