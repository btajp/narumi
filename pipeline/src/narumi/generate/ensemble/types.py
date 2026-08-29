"""Closed data types for evidence-backed ensemble minutes.

These models are deliberately independent of provider, storage, and run identities.  They
describe only the immutable source projection and the validated document that may flow to a
later model call or the deterministic renderer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, TypeVar

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

T = TypeVar("T")


def _as_tuple(value):
    return tuple(value) if isinstance(value, list) else value


FrozenTuple = Annotated[tuple[T, ...], BeforeValidator(_as_tuple)]

EvidenceId = Annotated[str, StringConstraints(pattern=r"^ev_[0-9a-f]{64}$")]
ClaimId = Annotated[str, StringConstraints(pattern=r"^cl_[0-9a-f]{64}$")]
QuestionId = Annotated[str, StringConstraints(pattern=r"^qu_[0-9a-f]{64}$")]
ArtifactId = Annotated[str, StringConstraints(pattern=r"^artifact-[0-9a-f]{32}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonBlank600 = Annotated[str, StringConstraints(min_length=1, max_length=600, pattern=r"\S")]
NonBlank120 = Annotated[str, StringConstraints(min_length=1, max_length=120, pattern=r"\S")]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True, frozen=True)


class SourceBinding(ClosedModel):
    segment_index: StrictInt = Field(ge=0)
    segment_id: str = Field(min_length=1, max_length=512)
    segment_text_sha256: Sha256
    sources: FrozenTuple[Annotated[str, StringConstraints(min_length=1, max_length=512)]] = Field(
        min_length=0, max_length=256
    )


class EvidenceView(ClosedModel):
    evidence_id: EvidenceId
    start_seconds: StrictFloat = Field(ge=0)
    end_seconds: StrictFloat = Field(ge=0)
    speaker_label: str | None = Field(max_length=512)
    speaker_name: str | None = Field(max_length=512)
    char_start: StrictInt = Field(ge=0)
    char_end: StrictInt = Field(ge=1)
    text: str = Field(min_length=1, max_length=512)
    occurrence_index: StrictInt = Field(ge=0)
    occurrence_count: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def _check_bounds(self) -> EvidenceView:
        import math

        if not math.isfinite(self.start_seconds) or not math.isfinite(self.end_seconds):
            raise ValueError("evidence times must be finite")
        if self.end_seconds < self.start_seconds:
            raise ValueError("evidence end_seconds must not precede start_seconds")
        if self.char_end <= self.char_start:
            raise ValueError("evidence range must be non-empty")
        if self.char_end - self.char_start != len(self.text):
            raise ValueError("evidence range must match the text codepoint length")
        if self.occurrence_index >= self.occurrence_count:
            raise ValueError("evidence occurrence index is outside its occurrence count")
        return self


class Evidence(EvidenceView):
    source_binding: SourceBinding

    def view(self) -> EvidenceView:
        return EvidenceView.model_validate(
            self.model_dump(exclude={"source_binding"}, mode="python")
        )


class EvidenceRef(ClosedModel):
    evidence_id: EvidenceId
    char_start: StrictInt = Field(ge=0)
    char_end: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def _non_empty(self) -> EvidenceRef:
        if self.char_end <= self.char_start:
            raise ValueError("evidence reference must be non-empty")
        return self


class Claim(ClosedModel):
    id: ClaimId
    kind: Literal["agenda", "discussion", "decision", "action"]
    text: NonBlank600
    evidence: FrozenTuple[EvidenceRef] = Field(min_length=1, max_length=8)
    owner: NonBlank120 | None
    due: NonBlank120 | None

    @model_validator(mode="after")
    def _action_fields(self) -> Claim:
        if self.kind != "action" and (self.owner is not None or self.due is not None):
            raise ValueError("owner and due are only valid for action claims")
        return self


class QuestionAlternative(ClosedModel):
    text: NonBlank600
    evidence: FrozenTuple[EvidenceRef] = Field(min_length=1, max_length=8)


class Question(ClosedModel):
    id: QuestionId
    kind: Literal["conflict", "missing_context"]
    text: NonBlank600
    alternatives: FrozenTuple[QuestionAlternative] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def _conflict_has_two_alternatives(self) -> Question:
        if self.kind == "conflict" and len(self.alternatives) < 2:
            raise ValueError("conflict questions require at least two alternatives")
        return self


class SourceIndexDocument(ClosedModel):
    schema_version: Literal["ensemble-source-index-v1"]
    packet_artifact_ids: FrozenTuple[ArtifactId] = Field(min_length=0, max_length=64)

    @field_validator("packet_artifact_ids")
    @classmethod
    def _unique_packet_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("source index packet artifact IDs must be unique")
        return value


class SourceDocument(ClosedModel):
    schema_version: Literal["ensemble-source-v1"]
    evidence: FrozenTuple[Evidence] = Field(min_length=1, max_length=32)

    @field_validator("evidence")
    @classmethod
    def _unique_evidence_ids(cls, value: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
        ids = [item.evidence_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("source evidence IDs must be unique")
        return value


class EnsembleDocument(ClosedModel):
    schema_version: Literal["ensemble-document-v1"]
    claims: FrozenTuple[Claim] = Field(max_length=32)
    questions: FrozenTuple[Question] = Field(max_length=16)

    @model_validator(mode="after")
    def _unique_ids(self) -> EnsembleDocument:
        claim_ids = [item.id for item in self.claims]
        question_ids = [item.id for item in self.questions]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("ensemble claim IDs must be unique")
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("ensemble question IDs must be unique")
        return self


class DraftPart(ClosedModel):
    source_artifact_id: ArtifactId
    document_artifact_id: ArtifactId


class DraftDocument(ClosedModel):
    schema_version: Literal["ensemble-draft-v1"]
    parts: FrozenTuple[DraftPart] = Field(min_length=1, max_length=64)


class RawClaim(ClosedModel):
    kind: Literal["agenda", "discussion", "decision", "action"]
    text: NonBlank600
    evidence: FrozenTuple[EvidenceRef] = Field(min_length=1, max_length=8)
    owner: NonBlank120 | None
    due: NonBlank120 | None
    from_claims: FrozenTuple[ClaimId] = Field(
        max_length=32,
        validation_alias=AliasChoices("from", "from_claims"),
        serialization_alias="from",
    )

    @model_validator(mode="after")
    def _action_fields(self) -> RawClaim:
        if self.kind != "action" and (self.owner is not None or self.due is not None):
            raise ValueError("owner and due are only valid for action claims")
        return self


class RawQuestion(ClosedModel):
    kind: Literal["conflict", "missing_context"]
    text: NonBlank600
    alternatives: FrozenTuple[QuestionAlternative] = Field(min_length=1, max_length=4)
    from_claims: FrozenTuple[ClaimId] = Field(
        max_length=32,
        validation_alias=AliasChoices("from", "from_claims"),
        serialization_alias="from",
    )

    @model_validator(mode="after")
    def _conflict_has_two_alternatives(self) -> RawQuestion:
        if self.kind == "conflict" and len(self.alternatives) < 2:
            raise ValueError("conflict questions require at least two alternatives")
        return self


class RawOmission(ClosedModel):
    from_claim: ClaimId = Field(
        validation_alias=AliasChoices("from", "from_claim"), serialization_alias="from"
    )
    reason: Literal["duplicate", "not_selected"]


class EnsembleResponse(ClosedModel):
    schema_version: Literal["ensemble-response-v1"]
    claims: FrozenTuple[RawClaim] = Field(max_length=32)
    questions: FrozenTuple[RawQuestion] = Field(max_length=16)
    omissions: FrozenTuple[RawOmission] = Field(max_length=32)


@dataclass(frozen=True)
class AllowedEvidenceRange:
    evidence_id: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class DirectOriginBinding:
    content_id: str
    origin_artifact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if re.fullmatch(r"(?:cl|qu)_[0-9a-f]{64}", self.content_id) is None:
            raise ValueError("direct origin content ID is invalid")
        if not self.origin_artifact_ids or len(self.origin_artifact_ids) != len(
            set(self.origin_artifact_ids)
        ):
            raise ValueError("direct origin artifact IDs must be non-empty and unique")
        if any(
            re.fullmatch(r"artifact-[0-9a-f]{32}", value) is None
            for value in self.origin_artifact_ids
        ):
            raise ValueError("direct origin artifact ID is invalid")
        if tuple(sorted(self.origin_artifact_ids)) != self.origin_artifact_ids:
            raise ValueError("direct origin artifact IDs must be canonically sorted")


@dataclass(frozen=True)
class SourcePacket:
    window_index: int
    document: SourceDocument
    content_projection_sha256: str


@dataclass(frozen=True)
class SourceSnapshot:
    meeting_id: str
    evidence: tuple[Evidence, ...]
    segment_texts: tuple[str, ...]

    def evidence_by_id(self) -> dict[str, Evidence]:
        return {item.evidence_id: item for item in self.evidence}


@dataclass(frozen=True)
class PreparedPrompt:
    system: str
    user: str
    stage: Literal["draft", "synthesis", "reduce"]
    template_hash: str
    response_schema_version: str
    allowed_evidence_ranges: tuple[AllowedEvidenceRange, ...]
    direct_claim_ids: tuple[str, ...]
    carried_questions: tuple[Question, ...]
    input_chars: int
    source_coverage: tuple[str, ...] = ()
    direct_origin_bindings: tuple[DirectOriginBinding, ...] = ()
    validation_context_sha256: str | None = None
    input_forward_chars: int | None = None
    output_forward_limit: int | None = None


class ValidationOutcome(StrEnum):
    ACCEPTED = "accepted"
    INVALID_STRUCTURE = "invalid_structure"
    INVALID_EVIDENCE = "invalid_evidence"
    NON_REDUCING = "non_reducing"


@dataclass(frozen=True)
class ValidatedResponse:
    document: EnsembleDocument
    from_claim_ids: tuple[str, ...]
    omissions: tuple[RawOmission, ...]
    content_projection_sha256: str
    forward_chars: int


@dataclass(frozen=True)
class ValidationResult:
    outcome: ValidationOutcome
    validated: ValidatedResponse | None = None
    reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is ValidationOutcome.ACCEPTED
