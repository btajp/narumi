"""Assign diarization turns to transcript segments by majority temporal overlap."""

from __future__ import annotations

from narumi.errors import InvalidArgumentError
from narumi.models import Segment, Turn

_OVERLAP_PRECISION = 6


def _overlap(segment: Segment, turn: Turn) -> float:
    return max(0.0, min(segment.end, turn.end) - max(segment.start, turn.start))


def assign_speakers(
    segments: list[Segment], turns: list[Turn], *, min_overlap: float = 0.0
) -> list[Segment]:
    """Return copies of ``segments`` with ``speaker`` set to the best-overlapping turn speaker.

    For each segment the overlap with every turn is summed per speaker; the speaker with the
    largest total wins, ties going to the speaker whose earliest overlapping turn starts first.
    A segment keeps its current ``speaker`` when nothing overlaps it or when the best overlap is
    below ``min_overlap`` seconds. Zero-length segments take the earliest turn containing them.
    Inputs are never mutated.
    """
    if min_overlap < 0:
        raise InvalidArgumentError("min_overlap must be >= 0", details={"value": min_overlap})
    ordered = sorted(enumerate(turns), key=lambda item: (item[1].start, item[1].end, item[0]))
    assigned: list[Segment] = []
    for segment in segments:
        if segment.end <= segment.start:
            container = next((t for _, t in ordered if t.start <= segment.start <= t.end), None)
            assigned.append(
                segment.model_copy(update={"speaker": container.speaker})
                if container is not None
                else segment.model_copy()
            )
            continue
        totals: dict[str, float] = {}
        first_rank: dict[str, int] = {}
        for rank, (_, turn) in enumerate(ordered):
            if turn.start >= segment.end:
                break
            amount = _overlap(segment, turn)
            if amount <= 0:
                continue
            totals[turn.speaker] = totals.get(turn.speaker, 0.0) + amount
            first_rank.setdefault(turn.speaker, rank)
        if not totals:
            assigned.append(segment.model_copy())
            continue
        speaker, best = max(
            totals.items(),
            key=lambda item: (round(item[1], _OVERLAP_PRECISION), -first_rank[item[0]]),
        )
        if best < min_overlap:
            assigned.append(segment.model_copy())
            continue
        assigned.append(segment.model_copy(update={"speaker": speaker}))
    return assigned
