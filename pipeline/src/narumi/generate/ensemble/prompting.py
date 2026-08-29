"""Exact, bounded prompt construction for draft, synthesis, and reduce calls."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

from .brief import CommonBrief
from .canonical import canonical_json, sha256_canonical, validation_context_sha256
from .planning import CandidatePlan, combine_documents
from .public import forward_chars, forward_payload
from .source import allowed_ranges, evidence_view
from .types import (
    AllowedEvidenceRange,
    DirectOriginBinding,
    EnsembleDocument,
    EvidenceRef,
    PreparedPrompt,
    Question,
    SourcePacket,
    SourceSnapshot,
)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
RESPONSE_SCHEMA_VERSION = "ensemble-response-v1"
MAX_INPUT_UNITS = 32


class PromptPlanningError(ValueError):
    pass


@dataclass(frozen=True)
class PromptLimits:
    input_chars: int = 12_000

    def __post_init__(self) -> None:
        if self.input_chars <= 0:
            raise ValueError("prompt input limit must be positive")


@cache
def _asset(name: str) -> str:
    path = PROMPTS_DIR / f"ensemble_{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PromptPlanningError(f"ensemble prompt asset is unavailable: {name}") from exc


def _render(name: str, payload: dict[str, object]) -> tuple[str, str, str]:
    system = _asset("system")
    template = _asset(name)
    marker = "{{payload}}"
    if template.count(marker) != 1:
        raise PromptPlanningError(f"ensemble prompt asset has invalid placeholders: {name}")
    user = template.replace(marker, canonical_json(payload))
    template_hash = hashlib.sha256((system + "\0" + template).encode("utf-8")).hexdigest()
    return system, user, template_hash


def _prepared(
    *,
    name: str,
    payload: dict[str, object],
    stage: Literal["draft", "synthesis", "reduce"],
    ranges: tuple[AllowedEvidenceRange, ...],
    direct_claim_ids: tuple[str, ...],
    carried_questions: tuple[Question, ...],
    limits: PromptLimits,
    source_coverage: tuple[str, ...],
    direct_origin_bindings: tuple[DirectOriginBinding, ...] = (),
    input_forward_chars: int | None = None,
    output_forward_limit: int | None = None,
) -> PreparedPrompt:
    system, user, template_hash = _render(name, payload)
    input_chars = len(system) + len(user)
    if input_chars > limits.input_chars:
        raise PromptPlanningError("ensemble prompt exceeds the exact input character limit")
    return PreparedPrompt(
        system=system,
        user=user,
        stage=stage,
        template_hash=template_hash,
        response_schema_version=RESPONSE_SCHEMA_VERSION,
        allowed_evidence_ranges=ranges,
        direct_claim_ids=direct_claim_ids,
        carried_questions=carried_questions,
        input_chars=input_chars,
        source_coverage=source_coverage,
        direct_origin_bindings=direct_origin_bindings,
        validation_context_sha256=validation_context_sha256(
            ranges, direct_claim_ids, carried_questions
        ),
        input_forward_chars=input_forward_chars,
        output_forward_limit=output_forward_limit,
    )


def _verify_common_brief(common_brief: CommonBrief) -> None:
    try:
        observed = sha256_canonical(common_brief.payload)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PromptPlanningError("common brief payload is invalid") from exc
    if observed != common_brief.content_sha256:
        raise PromptPlanningError("common brief payload changed after selection")


def prepare_draft(
    packet: SourcePacket,
    common_brief: CommonBrief,
    limits: PromptLimits | None = None,
) -> PreparedPrompt:
    selected = limits or PromptLimits()
    _verify_common_brief(common_brief)
    views = evidence_view(packet)
    payload: dict[str, object] = {
        "stage": "draft",
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "common_brief": common_brief.payload,
        "evidence": [item.model_dump(mode="json") for item in views],
    }
    return _prepared(
        name="draft",
        payload=payload,
        stage="draft",
        ranges=allowed_ranges(views),
        direct_claim_ids=(),
        carried_questions=(),
        limits=selected,
        source_coverage=tuple(item.evidence_id for item in views),
    )


def prepare_synthesis(
    packet: SourcePacket,
    candidates: CandidatePlan,
    common_brief: CommonBrief,
    limits: PromptLimits | None = None,
) -> PreparedPrompt:
    selected = limits or PromptLimits()
    _verify_common_brief(common_brief)
    views = evidence_view(packet)
    documents = [item.document for item in candidates.wire_items]
    _require_unit_limit(documents, "synthesis")
    _require_refs_in_ranges(
        [ref for document in documents for ref in _document_refs(document)],
        allowed_ranges(views),
    )
    combined = combine_documents(documents)
    origin_bindings = _candidate_origin_bindings(candidates)
    payload: dict[str, object] = {
        "stage": "synthesis",
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "common_brief": common_brief.payload,
        "evidence": [item.model_dump(mode="json") for item in views],
        "candidates": candidates.wire(),
    }
    return _prepared(
        name="synthesis",
        payload=payload,
        stage="synthesis",
        ranges=allowed_ranges(views),
        direct_claim_ids=tuple(sorted(claim.id for claim in combined.claims)),
        carried_questions=tuple(combined.questions),
        limits=selected,
        source_coverage=tuple(item.evidence_id for item in views),
        direct_origin_bindings=origin_bindings,
    )


def _ranges_from_forward(payload: dict[str, object]) -> tuple[AllowedEvidenceRange, ...]:
    evidence = payload["evidence"]
    if not isinstance(evidence, list):
        raise PromptPlanningError("forward evidence projection is invalid")
    return tuple(
        AllowedEvidenceRange(
            evidence_id=item["evidence_id"],
            char_start=item["char_start"],
            char_end=item["char_end"],
        )
        for item in evidence
        if isinstance(item, dict)
    )


def _document_refs(document: EnsembleDocument) -> tuple[EvidenceRef, ...]:
    refs = [ref for claim in document.claims for ref in claim.evidence]
    refs.extend(
        ref
        for question in document.questions
        for alternative in question.alternatives
        for ref in alternative.evidence
    )
    return tuple(refs)


def _require_unit_limit(documents: list[EnsembleDocument], stage: str) -> None:
    unit_count = sum(len(document.claims) + len(document.questions) for document in documents)
    if unit_count > MAX_INPUT_UNITS:
        raise PromptPlanningError(f"{stage} input exceeds the direct candidate unit limit")


def _candidate_origin_bindings(
    candidates: CandidatePlan,
) -> tuple[DirectOriginBinding, ...]:
    by_content: dict[str, set[str]] = {}
    for item, artifact_ids in zip(
        candidates.wire_items, candidates.origin_artifact_ids, strict=True
    ):
        for content_id in [
            *(claim.id for claim in item.document.claims),
            *(question.id for question in item.document.questions),
        ]:
            by_content.setdefault(content_id, set()).update(artifact_ids)
    return tuple(
        DirectOriginBinding(content_id, tuple(sorted(by_content[content_id])))
        for content_id in sorted(by_content)
    )


def _validate_reduce_provenance(
    document: EnsembleDocument,
    snapshot: SourceSnapshot,
    source_coverage: tuple[str, ...],
    direct_origin_bindings: tuple[DirectOriginBinding, ...],
) -> tuple[tuple[str, ...], tuple[DirectOriginBinding, ...]]:
    if len(source_coverage) != len(set(source_coverage)):
        raise PromptPlanningError("reduce source coverage contains duplicate evidence IDs")
    known_evidence = snapshot.evidence_by_id()
    if any(value not in known_evidence for value in source_coverage):
        raise PromptPlanningError("reduce source coverage contains unknown evidence")
    referenced = {value.evidence_id for value in _document_refs(document)}
    if not referenced <= set(source_coverage):
        raise PromptPlanningError("reduce source coverage does not include every cited atom")

    by_content: dict[str, DirectOriginBinding] = {}
    for binding in direct_origin_bindings:
        if binding.content_id in by_content:
            raise PromptPlanningError("reduce direct origin content IDs are duplicated")
        by_content[binding.content_id] = binding
    expected_content = {
        *(claim.id for claim in document.claims),
        *(question.id for question in document.questions),
    }
    if set(by_content) != expected_content:
        raise PromptPlanningError("reduce direct origins do not cover every direct content unit")
    return tuple(sorted(source_coverage)), tuple(
        by_content[content_id] for content_id in sorted(by_content)
    )


def _require_refs_in_ranges(
    refs: list[EvidenceRef], ranges: tuple[AllowedEvidenceRange, ...]
) -> None:
    by_id: dict[str, list[tuple[int, int]]] = {}
    for allowed in ranges:
        by_id.setdefault(allowed.evidence_id, []).append((allowed.char_start, allowed.char_end))
    for ref in refs:
        if not any(
            start <= ref.char_start and ref.char_end <= end
            for start, end in by_id.get(ref.evidence_id, ())
        ):
            raise PromptPlanningError("candidate evidence is outside the synthesis source packet")


def prepare_reduce(
    documents: list[EnsembleDocument],
    snapshot: SourceSnapshot,
    common_brief: CommonBrief,
    *,
    target_chars: int,
    source_coverage: tuple[str, ...],
    direct_origin_bindings: tuple[DirectOriginBinding, ...],
    limits: PromptLimits | None = None,
) -> PreparedPrompt:
    if not documents:
        raise PromptPlanningError("reduce requires at least one document")
    if target_chars <= 0:
        raise ValueError("reduce target must be positive")
    selected = limits or PromptLimits()
    _verify_common_brief(common_brief)
    combined = combine_documents(documents)
    if not combined.claims and not combined.questions:
        raise PromptPlanningError("empty documents require deterministic pass-through")
    _require_unit_limit([combined], "reduce")
    source_coverage, direct_origin_bindings = _validate_reduce_provenance(
        combined,
        snapshot,
        source_coverage,
        direct_origin_bindings,
    )
    forward = forward_payload(combined, snapshot)
    payload: dict[str, object] = {
        "stage": "reduce",
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "common_brief": common_brief.payload,
        "input_document": combined.model_dump(mode="json"),
        "evidence": forward["evidence"],
        "target_forward_chars": target_chars,
    }
    return _prepared(
        name="reduce",
        payload=payload,
        stage="reduce",
        ranges=_ranges_from_forward(forward),
        direct_claim_ids=tuple(sorted(claim.id for claim in combined.claims)),
        carried_questions=tuple(combined.questions),
        limits=selected,
        source_coverage=source_coverage,
        direct_origin_bindings=direct_origin_bindings,
        input_forward_chars=forward_chars(combined, snapshot),
        output_forward_limit=target_chars,
    )
