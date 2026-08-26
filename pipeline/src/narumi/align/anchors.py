"""Anchor discovery: unique character n-grams shared by two transcript sources."""

from __future__ import annotations

from statistics import median

from narumi.align.normalize import char_ngrams, normalize_text
from narumi.models import Anchor, Segment, Transcript


def _unique_ngrams(transcript: Transcript, n: int) -> dict[str, Segment]:
    """Map each n-gram that occurs exactly once in the whole source to its segment."""
    counts: dict[str, int] = {}
    first: dict[str, Segment] = {}
    for segment in transcript.segments:
        for gram in char_ngrams(normalize_text(segment.text), n):
            counts[gram] = counts.get(gram, 0) + 1
            first.setdefault(gram, segment)
    return {gram: first[gram] for gram, count in counts.items() if count == 1}


def find_anchors(
    a: Transcript,
    b: Transcript,
    *,
    n: int = 8,
    max_anchors: int = 200,
) -> list[Anchor]:
    """Find n-grams that occur exactly once in ``a`` and exactly once in ``b``.

    ``Anchor.offset`` is ``(b.segment.start + b.time_offset) - (a.segment.start + a.time_offset)``,
    i.e. how much later the same speech appears on ``b``'s clock than on ``a``'s clock.

    One anchor is kept per (segment_a, segment_b) pair (the earliest n-gram of the pair) so that
    long segments do not dominate the offset estimate. Anchors are sorted by ``a`` time and, when
    more than ``max_anchors`` remain, sampled evenly across the recording.
    """
    if max_anchors <= 0:
        raise ValueError("max_anchors must be positive")
    unique_a = _unique_ngrams(a, n)
    unique_b = _unique_ngrams(b, n)
    seen_pairs: set[tuple[str, str]] = set()
    anchors: list[Anchor] = []
    # Iterate in a's positional order so "earliest n-gram per pair" is deterministic.
    for segment_a in a.segments:
        for gram in char_ngrams(normalize_text(segment_a.text), n):
            if gram not in unique_a or gram not in unique_b:
                continue
            segment_b = unique_b[gram]
            pair = (segment_a.id, segment_b.id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            offset = (segment_b.start + b.time_offset) - (segment_a.start + a.time_offset)
            anchors.append(
                Anchor(
                    ngram=gram,
                    source_a=a.source_id,
                    segment_a=segment_a.id,
                    source_b=b.source_id,
                    segment_b=segment_b.id,
                    offset=round(offset, 3),
                )
            )
    anchors.sort(key=lambda x: (_segment_start(a, x.segment_a), x.segment_b, x.ngram))
    if len(anchors) > max_anchors:
        step = len(anchors) / max_anchors
        anchors = [anchors[int(i * step)] for i in range(max_anchors)]
    return anchors


def _segment_start(transcript: Transcript, segment_id: str) -> float:
    for segment in transcript.segments:
        if segment.id == segment_id:
            return segment.start + transcript.time_offset
    return 0.0


def estimate_offset(anchors: list[Anchor]) -> float | None:
    """Median anchor offset, or ``None`` when fewer than 3 anchors support an estimate."""
    if len(anchors) < 3:
        return None
    return round(float(median(anchor.offset for anchor in anchors)), 3)
