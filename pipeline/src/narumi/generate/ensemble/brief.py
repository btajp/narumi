"""Model-independent, whole-item selection for the common ensemble brief."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from narumi.brief import Brief

from .canonical import canonical_bytes, canonical_json, sha256_canonical

COMMON_BRIEF_CHARS = 1_000
BRIEF_VERSION = "ensemble-common-brief-v1"
BriefKind = Literal["vocabulary", "participant", "previous_point", "background"]


@dataclass(frozen=True)
class BriefItem:
    kind: BriefKind
    value: str | dict[str, Any]

    def wire(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class OmittedBriefItem:
    item: BriefItem
    reason: Literal["duplicate", "budget_exceeded"]


@dataclass(frozen=True)
class CommonBrief:
    payload: dict[str, Any]
    selected: tuple[BriefItem, ...]
    omitted: tuple[OmittedBriefItem, ...]
    content_sha256: str


def _items(brief: Brief) -> list[BriefItem]:
    values: list[BriefItem] = [BriefItem("vocabulary", value) for value in brief.vocab_hints]
    values.extend(
        BriefItem(
            "participant",
            {
                "name": participant.name,
                "aliases": list(participant.aliases),
                "note": participant.note,
            },
        )
        for participant in brief.participants
    )
    values.extend(BriefItem("previous_point", value) for value in brief.previous_points)
    values.extend(BriefItem("background", value) for value in brief.background)
    return values


def select_common_brief(brief: Brief | None, *, max_chars: int = COMMON_BRIEF_CHARS) -> CommonBrief:
    """Select whole items in priority order and record every exclusion explicitly."""
    if max_chars <= 0:
        raise ValueError("common brief limit must be positive")
    candidates = _items(brief) if brief is not None else []
    selected: list[BriefItem] = []
    omitted: list[OmittedBriefItem] = []
    seen: set[bytes] = set()
    budget_blocked = False
    for item in candidates:
        identity = canonical_bytes(item.wire())
        if identity in seen:
            omitted.append(OmittedBriefItem(item, "duplicate"))
            continue
        seen.add(identity)
        if budget_blocked:
            omitted.append(OmittedBriefItem(item, "budget_exceeded"))
            continue
        candidate = {
            "schema_version": BRIEF_VERSION,
            "items": [value.wire() for value in [*selected, item]],
        }
        if len(canonical_json(candidate)) > max_chars:
            budget_blocked = True
            omitted.append(OmittedBriefItem(item, "budget_exceeded"))
            continue
        selected.append(item)
    payload = {
        "schema_version": BRIEF_VERSION,
        "items": [item.wire() for item in selected],
    }
    if len(canonical_json(payload)) > max_chars:
        raise ValueError("common brief envelope exceeds its configured limit")
    return CommonBrief(
        payload=payload,
        selected=tuple(selected),
        omitted=tuple(omitted),
        content_sha256=sha256_canonical(payload),
    )
