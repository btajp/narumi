"""Overlap-based speaker assignment from diarization turns (local helper, no LLM)."""

from __future__ import annotations

from narumi.models import Turn


def overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_by_overlap(start: float, end: float, turns: list[Turn]) -> str | None:
    """Speaker whose turns overlap ``[start, end]`` the most; ``None`` when nothing overlaps.

    Ties are broken by label order so the result is deterministic.
    """
    totals: dict[str, float] = {}
    for turn in turns:
        amount = overlap(start, end, turn.start, turn.end)
        if amount > 0:
            totals[turn.speaker] = totals.get(turn.speaker, 0.0) + amount
    if not totals:
        return None
    return min(totals, key=lambda label: (-totals[label], label))
