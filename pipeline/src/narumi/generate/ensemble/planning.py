"""Canonical, identity-free planning helpers for synthesis and reduce inputs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .canonical import canonical_document, canonical_json, content_projection_sha256
from .types import Claim, EnsembleDocument, Question

_ARTIFACT_ID = re.compile(r"^artifact-[0-9a-f]{32}$")


class EnsemblePlanningError(ValueError):
    pass


@dataclass(frozen=True)
class CandidateArtifact:
    artifact_id: str
    document: EnsembleDocument

    def __post_init__(self) -> None:
        if _ARTIFACT_ID.fullmatch(self.artifact_id) is None:
            raise ValueError("candidate artifact ID is invalid")


@dataclass(frozen=True)
class CandidateWireItem:
    content_projection_sha256: str
    duplicate_ordinal: int
    document: EnsembleDocument
    projection_document: EnsembleDocument | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.content_projection_sha256) is None:
            raise ValueError("candidate content projection is invalid")
        if self.duplicate_ordinal < 0:
            raise ValueError("candidate duplicate ordinal must be non-negative")
        if self.projection_document is None:
            object.__setattr__(self, "projection_document", self.document)

    def validate_integrity(self) -> None:
        assert self.projection_document is not None
        try:
            observed_projection = content_projection_sha256(self.projection_document)
        except (TypeError, ValueError) as exc:
            raise EnsemblePlanningError("candidate content changed after projection") from exc
        if observed_projection != self.content_projection_sha256:
            raise EnsemblePlanningError("candidate content changed after projection")
        claims = {value.id: value for value in self.projection_document.claims}
        questions = {value.id: value for value in self.projection_document.questions}
        if any(claims.get(value.id) != value for value in self.document.claims) or any(
            questions.get(value.id) != value for value in self.document.questions
        ):
            raise EnsemblePlanningError("candidate partition is not a subset of its projection")

    def wire(self) -> dict[str, object]:
        self.validate_integrity()
        return {
            "content_projection_sha256": self.content_projection_sha256,
            "duplicate_ordinal": self.duplicate_ordinal,
            "document": canonical_document(self.document).model_dump(mode="json"),
        }


@dataclass(frozen=True)
class CandidatePlan:
    wire_items: tuple[CandidateWireItem, ...]
    origin_artifact_ids: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if len(self.wire_items) != len(self.origin_artifact_ids):
            raise ValueError("candidate wire and provenance cardinalities differ")

    def wire(self) -> list[dict[str, object]]:
        return [item.wire() for item in self.wire_items]


def canonical_candidate_documents(values: list[CandidateArtifact]) -> CandidatePlan:
    """Remove shared artifacts and order remaining bodies without putting IDs on the wire."""
    by_artifact: dict[str, EnsembleDocument] = {}
    for value in values:
        canonical = canonical_document(value.document)
        previous = by_artifact.setdefault(value.artifact_id, canonical)
        if previous != canonical:
            raise EnsemblePlanningError("one artifact ID resolves to different candidate content")

    by_projection: dict[str, list[tuple[str, EnsembleDocument]]] = {}
    projection_bodies: dict[str, str] = {}
    for artifact_id, document in by_artifact.items():
        projection = content_projection_sha256(document)
        body = canonical_json(document)
        previous_body = projection_bodies.setdefault(projection, body)
        if previous_body != body:
            raise EnsemblePlanningError("candidate content projection collision")
        by_projection.setdefault(projection, []).append((artifact_id, document))

    wire_items: list[CandidateWireItem] = []
    origins: list[tuple[str, ...]] = []
    for projection in sorted(by_projection):
        # Artifact IDs affect provenance assignment only.  Every wire item in this group is
        # byte-identical except for its content-derived, consecutive duplicate ordinal.
        group = sorted(by_projection[projection], key=lambda value: value[0])
        for ordinal, (artifact_id, document) in enumerate(group):
            wire_items.append(CandidateWireItem(projection, ordinal, document))
            origins.append((artifact_id,))
    return CandidatePlan(tuple(wire_items), tuple(origins))


def combine_documents(values: list[EnsembleDocument]) -> EnsembleDocument:
    """Create the same single-document shape used on both sides of reduce metrics."""
    claims: dict[str, Claim] = {}
    questions: dict[str, Question] = {}
    for document in values:
        for claim in document.claims:
            previous = claims.setdefault(claim.id, claim)
            if previous != claim:
                raise EnsemblePlanningError("claim identity collision")
        for question in document.questions:
            previous = questions.setdefault(question.id, question)
            if previous != question:
                raise EnsemblePlanningError("question identity collision")
    try:
        return canonical_document(
            EnsembleDocument(
                schema_version="ensemble-document-v1",
                claims=list(claims.values()),
                questions=list(questions.values()),
            )
        )
    except ValueError as exc:
        raise EnsemblePlanningError("combined candidate content exceeds document bounds") from exc


def partition_candidate_items(
    plan: CandidatePlan, *, max_units: int = 32, max_chars: int | None = None
) -> tuple[CandidatePlan, ...]:
    """Partition every claim/question exactly once, splitting only at unit boundaries."""
    if max_units <= 0 or max_chars is not None and max_chars <= 0:
        raise ValueError("candidate partition limits must be positive")
    if not plan.wire_items:
        return (plan,)

    units: list[tuple[int, str, Claim | Question | None]] = []
    for item_index, item in enumerate(plan.wire_items):
        if not item.document.claims and not item.document.questions:
            units.append((item_index, "empty", None))
        units.extend((item_index, "claim", claim) for claim in item.document.claims)
        units.extend((item_index, "question", question) for question in item.document.questions)

    def materialize(batch: list[tuple[int, str, Claim | Question | None]]) -> CandidatePlan:
        grouped: dict[int, tuple[list[Claim], list[Question]]] = {}
        for item_index, kind, value in batch:
            claims, questions = grouped.setdefault(item_index, ([], []))
            if kind == "claim":
                assert isinstance(value, Claim)
                claims.append(value)
            elif kind == "question":
                assert isinstance(value, Question)
                questions.append(value)
        items: list[CandidateWireItem] = []
        origins: list[tuple[str, ...]] = []
        for item_index in sorted(grouped):
            source = plan.wire_items[item_index]
            claims, questions = grouped[item_index]
            items.append(
                CandidateWireItem(
                    source.content_projection_sha256,
                    source.duplicate_ordinal,
                    EnsembleDocument(
                        schema_version="ensemble-document-v1",
                        claims=claims,
                        questions=questions,
                    ),
                    projection_document=source.projection_document,
                )
            )
            origins.append(plan.origin_artifact_ids[item_index])
        return CandidatePlan(tuple(items), tuple(origins))

    batches: list[CandidatePlan] = []
    current: list[tuple[int, str, Claim | Question | None]] = []
    for unit in units:
        candidate = [*current, unit]
        candidate_plan = materialize(candidate)
        candidate_chars = len(canonical_json(candidate_plan.wire()))
        direct_units = sum(kind != "empty" for _, kind, _ in candidate)
        if direct_units <= max_units and (max_chars is None or candidate_chars <= max_chars):
            current = candidate
            continue
        if not current:
            raise EnsemblePlanningError("one candidate unit exceeds the synthesis input limit")
        batches.append(materialize(current))
        current = [unit]
        if max_chars is not None and len(canonical_json(materialize(current).wire())) > max_chars:
            raise EnsemblePlanningError("one candidate unit exceeds the synthesis input limit")
    if current:
        batches.append(materialize(current))
    observed = [
        (item.content_projection_sha256, item.duplicate_ordinal, kind, content_id)
        for batch in batches
        for item in batch.wire_items
        for kind, content_id in (
            [("empty", None)]
            if not item.document.claims and not item.document.questions
            else [
                *(("claim", value.id) for value in item.document.claims),
                *(("question", value.id) for value in item.document.questions),
            ]
        )
    ]
    expected = [
        (
            plan.wire_items[index].content_projection_sha256,
            plan.wire_items[index].duplicate_ordinal,
            kind,
            value.id if value is not None else None,
        )
        for index, kind, value in units
    ]
    if observed != expected:
        raise EnsemblePlanningError("candidate partition is incomplete or overlapping")
    return tuple(batches)
