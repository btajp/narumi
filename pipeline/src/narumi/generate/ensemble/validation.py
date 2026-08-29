"""Local validation of closed ensemble responses and their evidence references."""

from __future__ import annotations

from collections import defaultdict

from .canonical import (
    canonical_document,
    make_claim,
    make_question,
    validation_context_sha256,
)
from .public import forward_chars
from .response import ResponseStructureError, decode_response
from .source import EnsembleSourceError, materialize_ref
from .types import (
    Claim,
    EnsembleDocument,
    EvidenceRef,
    PreparedPrompt,
    Question,
    SourceSnapshot,
    ValidatedResponse,
    ValidationOutcome,
    ValidationResult,
)


class EvidenceValidationError(ValueError):
    pass


def _validate_ref(
    ref: EvidenceRef,
    prepared: PreparedPrompt,
    snapshot: SourceSnapshot,
) -> None:
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for allowed in prepared.allowed_evidence_ranges:
        ranges[allowed.evidence_id].append((allowed.char_start, allowed.char_end))
    if not any(
        start <= ref.char_start and ref.char_end <= end
        for start, end in ranges.get(ref.evidence_id, ())
    ):
        raise EvidenceValidationError("reference is outside this call's evidence allowlist")
    try:
        materialize_ref(snapshot, ref)
    except EnsembleSourceError as exc:
        raise EvidenceValidationError("reference does not resolve to immutable source") from exc


def _all_refs(response) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    for claim in response.claims:
        refs.extend(claim.evidence)
    for question in response.questions:
        for alternative in question.alternatives:
            refs.extend(alternative.evidence)
    return refs


def _validate_coverage(response, prepared: PreparedPrompt) -> tuple[str, ...]:
    expected = set(prepared.direct_claim_ids)
    if len(expected) != len(prepared.direct_claim_ids):
        raise ResponseStructureError("prepared direct claim IDs are not unique")
    kept_values = [
        value for item in [*response.claims, *response.questions] for value in item.from_claims
    ]
    omitted_values = [item.from_claim for item in response.omissions]
    kept = set(kept_values)
    omitted = set(omitted_values)
    if len(omitted_values) != len(omitted):
        raise ResponseStructureError("one direct claim is omitted more than once")
    if not kept <= expected or not omitted <= expected:
        raise ResponseStructureError("response references an unknown direct claim")
    if kept & omitted:
        raise ResponseStructureError("a direct claim cannot be kept and omitted")
    if kept | omitted != expected:
        raise ResponseStructureError("response does not cover every direct claim")
    return tuple(sorted(kept))


def _canonical_questions(values: list[Question]) -> list[Question]:
    by_id: dict[str, Question] = {}
    for question in values:
        previous = by_id.setdefault(question.id, question)
        if previous != question:
            raise ResponseStructureError("question identity collision")
    return list(by_id.values())


def _canonical_claims(values: list[Claim]) -> list[Claim]:
    by_id: dict[str, Claim] = {}
    for claim in values:
        previous = by_id.setdefault(claim.id, claim)
        if previous != claim:
            raise ResponseStructureError("claim identity collision")
    return list(by_id.values())


def validate_response(
    raw_text: str,
    prepared: PreparedPrompt,
    snapshot: SourceSnapshot,
) -> ValidationResult:
    """Classify a received response without triggering or requesting another model call."""
    try:
        observed_context = validation_context_sha256(
            prepared.allowed_evidence_ranges,
            prepared.direct_claim_ids,
            prepared.carried_questions,
        )
    except (TypeError, ValueError, UnicodeError):
        observed_context = None
    if (
        prepared.validation_context_sha256 is not None
        and prepared.validation_context_sha256 != observed_context
    ):
        return ValidationResult(
            ValidationOutcome.INVALID_STRUCTURE,
            reason="prepared validation context changed after prompt construction",
        )
    try:
        response = decode_response(raw_text)
    except ResponseStructureError as exc:
        return ValidationResult(ValidationOutcome.INVALID_STRUCTURE, reason=str(exc))

    try:
        for ref in _all_refs(response):
            _validate_ref(ref, prepared, snapshot)
        for question in prepared.carried_questions:
            for alternative in question.alternatives:
                for ref in alternative.evidence:
                    _validate_ref(ref, prepared, snapshot)
    except EvidenceValidationError as exc:
        return ValidationResult(ValidationOutcome.INVALID_EVIDENCE, reason=str(exc))

    try:
        kept = _validate_coverage(response, prepared)
    except ResponseStructureError as exc:
        return ValidationResult(ValidationOutcome.INVALID_STRUCTURE, reason=str(exc))

    try:
        claims = _canonical_claims([make_claim(value) for value in response.claims])
        questions = [make_question(value) for value in response.questions]
        questions = _canonical_questions([*questions, *prepared.carried_questions])
        document = canonical_document(
            EnsembleDocument(
                schema_version="ensemble-document-v1",
                claims=claims,
                questions=questions,
            )
        )
        current_forward_chars = forward_chars(document, snapshot)
    except (ValueError, ResponseStructureError) as exc:
        return ValidationResult(ValidationOutcome.INVALID_STRUCTURE, reason=str(exc))

    if prepared.stage == "reduce":
        if prepared.input_forward_chars is None:
            return ValidationResult(
                ValidationOutcome.INVALID_STRUCTURE,
                reason="reduce validation is missing the input forward size",
            )
        if current_forward_chars >= prepared.input_forward_chars:
            return ValidationResult(
                ValidationOutcome.NON_REDUCING,
                reason="validated response does not reduce the forward payload",
            )
        if (
            prepared.output_forward_limit is not None
            and current_forward_chars > prepared.output_forward_limit
        ):
            return ValidationResult(
                ValidationOutcome.NON_REDUCING,
                reason="validated response does not fit the next receiver",
            )

    from .canonical import content_projection_sha256

    return ValidationResult(
        ValidationOutcome.ACCEPTED,
        validated=ValidatedResponse(
            document=document,
            from_claim_ids=kept,
            omissions=tuple(response.omissions),
            content_projection_sha256=content_projection_sha256(document),
            forward_chars=current_forward_chars,
        ),
    )
