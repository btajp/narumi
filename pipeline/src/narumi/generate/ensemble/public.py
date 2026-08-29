"""Stable public and downstream projections of validated ensemble content."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .canonical import canonical_document, canonical_json, content_projection_sha256
from .source import materialize_ref
from .types import EnsembleDocument, EvidenceRef, SourceSnapshot


def _merged_ranges(refs: Iterable[EvidenceRef]) -> list[tuple[str, int, int]]:
    by_id: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for ref in refs:
        by_id[ref.evidence_id].append((ref.char_start, ref.char_end))
    result: list[tuple[str, int, int]] = []
    for evidence_id in sorted(by_id):
        merged: list[list[int]] = []
        for start, end in sorted(set(by_id[evidence_id])):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        result.extend((evidence_id, start, end) for start, end in merged)
    return result


def document_refs(document: EnsembleDocument) -> tuple[EvidenceRef, ...]:
    refs: list[EvidenceRef] = []
    for claim in document.claims:
        refs.extend(claim.evidence)
    for question in document.questions:
        for alternative in question.alternatives:
            refs.extend(alternative.evidence)
    return tuple(refs)


def forward_payload(document: EnsembleDocument, snapshot: SourceSnapshot) -> dict[str, Any]:
    """Project a document with exactly the source ranges a later model may receive."""
    canonical = canonical_document(document)
    evidence = snapshot.evidence_by_id()
    fragments: list[dict[str, Any]] = []
    for evidence_id, char_start, char_end in _merged_ranges(document_refs(canonical)):
        source = evidence.get(evidence_id)
        ref = EvidenceRef(
            evidence_id=evidence_id,
            char_start=char_start,
            char_end=char_end,
        )
        if source is None:
            raise ValueError("document references unknown evidence")
        fragments.append(
            {
                "evidence_id": evidence_id,
                "start_seconds": source.start_seconds,
                "end_seconds": source.end_seconds,
                "speaker_label": source.speaker_label,
                "speaker_name": source.speaker_name,
                "char_start": char_start,
                "char_end": char_end,
                "text": materialize_ref(snapshot, ref),
                "occurrence_index": source.occurrence_index,
                "occurrence_count": source.occurrence_count,
            }
        )
    return {
        "schema_version": "ensemble-forward-v1",
        "document": canonical.model_dump(mode="json"),
        "evidence": fragments,
    }


def forward_chars(document: EnsembleDocument, snapshot: SourceSnapshot) -> int:
    return len(canonical_json(forward_payload(document, snapshot)))


def stable_document_projection(document: EnsembleDocument) -> tuple[str, dict[str, Any]]:
    canonical = canonical_document(document)
    payload = canonical.model_dump(mode="json")
    return content_projection_sha256(canonical), payload
