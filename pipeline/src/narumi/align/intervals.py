"""Interval construction: cluster segments from all sources on a common clock."""

from __future__ import annotations

from dataclasses import dataclass

from narumi.models import Interval, Transcript


@dataclass(frozen=True)
class _Placed:
    source_id: str
    segment_id: str
    start: float
    end: float


def place_segments(transcripts: list[Transcript], offsets: dict[str, float]) -> list[_Placed]:
    """Project every segment onto the common clock: ``start + time_offset + offsets[source]``."""
    placed: list[_Placed] = []
    for transcript in transcripts:
        shift = transcript.time_offset + offsets.get(transcript.source_id, 0.0)
        for segment in transcript.segments:
            start = max(0.0, segment.start + shift)
            end = max(start, segment.end + shift)
            placed.append(_Placed(transcript.source_id, segment.id, start, end))
    placed.sort(key=lambda p: (p.start, p.end, p.source_id, p.segment_id))
    return placed


def build_intervals(
    transcripts: list[Transcript],
    offsets: dict[str, float],
    *,
    gap: float = 0.5,
) -> list[Interval]:
    """Cluster segments into merged-timeline intervals.

    A segment joins the current cluster when it starts within ``gap`` seconds of the cluster end
    *and* it is within ``gap`` of a segment from a **different** source already in the cluster.
    Segments of the same source therefore never merge with each other unless another source
    bridges them, so a single-source alignment yields one interval per segment while overlapping
    mic / system segments are merged.

    Interval ids are ``iv-00001`` … (1-based); ``columns`` maps source_id → segment ids in
    time order.
    """
    if gap < 0:
        raise ValueError("gap must be non-negative")
    clusters: list[list[_Placed]] = []
    current: list[_Placed] = []
    current_end = 0.0
    for item in place_segments(transcripts, offsets):
        near = item.start <= current_end + gap
        if current and near and _touches_other_source(item, current, gap):
            current.append(item)
            current_end = max(current_end, item.end)
            continue
        if current:
            clusters.append(current)
        current = [item]
        current_end = item.end
    if current:
        clusters.append(current)

    intervals: list[Interval] = []
    for index, cluster in enumerate(clusters, start=1):
        columns: dict[str, list[str]] = {}
        for item in cluster:
            columns.setdefault(item.source_id, []).append(item.segment_id)
        intervals.append(
            Interval(
                id=f"iv-{index:05d}",
                start=round(min(p.start for p in cluster), 3),
                end=round(max(p.end for p in cluster), 3),
                columns=dict(sorted(columns.items())),
            )
        )
    return intervals


def _touches_other_source(item: _Placed, cluster: list[_Placed], gap: float) -> bool:
    return any(
        other.source_id != item.source_id and item.start <= other.end + gap for other in cluster
    )
