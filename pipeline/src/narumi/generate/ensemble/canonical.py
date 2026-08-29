"""Versioned canonical encodings and content-derived ensemble identifiers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel

from .types import (
    AllowedEvidenceRange,
    Claim,
    EnsembleDocument,
    EvidenceRef,
    Question,
    QuestionAlternative,
    RawClaim,
    RawQuestion,
)

CANONICAL_VERSION = "ensemble-canonical-v1"
CONTENT_PROJECTION_VERSION = "ensemble-content-projection-v1"


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", by_alias=True)
    return value


def canonical_float(value: float) -> str:
    """Return the locale-independent binary64 spelling used by identity hashes."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("canonical float requires a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite values have no canonical representation")
    if result == 0:
        return "0x0.0p+0"
    return result.hex()


def canonical_bytes(value: Any) -> bytes:
    """Encode JSON-like values with explicit type and length boundaries.

    This encoding is for hashes, not transport.  In particular, binary64 values use
    ``float.hex`` so locale, decimal formatting, and negative zero cannot change an ID.
    """

    chunks: list[bytes] = [CANONICAL_VERSION.encode("ascii"), b"\0"]

    def append(item: Any) -> None:
        item = _plain(item)
        if item is None:
            chunks.append(b"n;")
        elif isinstance(item, bool):
            chunks.append(b"b1;" if item else b"b0;")
        elif isinstance(item, int):
            chunks.append(f"i{item};".encode("ascii"))
        elif isinstance(item, float):
            chunks.append(b"f" + canonical_float(item).encode("ascii") + b";")
        elif isinstance(item, str):
            raw = item.encode("utf-8")
            chunks.extend((f"s{len(raw)}:".encode("ascii"), raw, b";"))
        elif isinstance(item, (list, tuple)):
            chunks.append(f"l{len(item)}:[".encode("ascii"))
            for child in item:
                append(child)
            chunks.append(b"]")
        elif isinstance(item, Mapping):
            if not all(isinstance(key, str) for key in item):
                raise TypeError("canonical mappings require string keys")
            ordered = sorted(item.items(), key=lambda pair: pair[0].encode("utf-8"))
            chunks.append(f"d{len(ordered)}:{{".encode("ascii"))
            for key, child in ordered:
                append(key)
                append(child)
            chunks.append(b"}")
        else:
            raise TypeError(f"unsupported canonical value: {type(item).__name__}")

    append(value)
    return b"".join(chunks)


def canonical_json(value: Any) -> str:
    """Return the exact compact JSON representation used on model wires."""
    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_canonical(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_wire(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_evidence_refs(refs: Iterable[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    unique = {(ref.evidence_id, ref.char_start, ref.char_end): ref for ref in refs}
    return tuple(unique[key] for key in sorted(unique))


def make_claim(raw: RawClaim) -> Claim:
    refs = canonical_evidence_refs(raw.evidence)
    content = _claim_content(raw, refs)
    return Claim(id="cl_" + sha256_canonical(content), **content)


def _claim_content(
    value: RawClaim | Claim, refs: Iterable[EvidenceRef] | None = None
) -> dict[str, Any]:
    selected = tuple(refs) if refs is not None else canonical_evidence_refs(value.evidence)
    return {
        "kind": value.kind,
        "text": value.text,
        "evidence": [ref.model_dump(mode="json") for ref in selected],
        "owner": value.owner,
        "due": value.due,
    }


def _canonical_alternative(value: QuestionAlternative) -> QuestionAlternative:
    payload = value.model_dump(mode="python")
    payload["evidence"] = canonical_evidence_refs(value.evidence)
    return QuestionAlternative.model_validate(payload)


def make_question(raw: RawQuestion) -> Question:
    content = _question_content(raw)
    return Question(id="qu_" + sha256_canonical(content), **content)


def _question_content(value: RawQuestion | Question) -> dict[str, Any]:
    alternatives = [_canonical_alternative(item) for item in value.alternatives]
    alternatives.sort(key=lambda item: canonical_bytes(item))
    return {
        "kind": value.kind,
        "text": value.text,
        "alternatives": [item.model_dump(mode="json") for item in alternatives],
    }


def validate_document_identities(document: EnsembleDocument) -> None:
    for claim in document.claims:
        if claim.id != "cl_" + sha256_canonical(_claim_content(claim)):
            raise ValueError("claim ID does not match its canonical content")
    for question in document.questions:
        if question.id != "qu_" + sha256_canonical(_question_content(question)):
            raise ValueError("question ID does not match its canonical content")


def canonical_document(document: EnsembleDocument) -> EnsembleDocument:
    try:
        validated = EnsembleDocument.model_validate(document.model_dump(mode="python"))
    except ValueError as exc:
        raise ValueError("ensemble document violates its closed bounds") from exc
    validate_document_identities(validated)
    claims = [Claim(id=claim.id, **_claim_content(claim)) for claim in validated.claims]
    questions = [
        Question(id=question.id, **_question_content(question)) for question in validated.questions
    ]
    claims.sort(key=lambda claim: canonical_bytes(claim))
    questions.sort(key=lambda question: canonical_bytes(question))
    return EnsembleDocument(
        schema_version="ensemble-document-v1", claims=claims, questions=questions
    )


def content_projection(document: EnsembleDocument) -> dict[str, Any]:
    canonical = canonical_document(document)
    return {
        "projection_version": CONTENT_PROJECTION_VERSION,
        "schema_version": canonical.schema_version,
        "claims": [item.model_dump(mode="json") for item in canonical.claims],
        "questions": [item.model_dump(mode="json") for item in canonical.questions],
    }


def content_projection_sha256(document: EnsembleDocument) -> str:
    return sha256_canonical(content_projection(document))


def validation_context_sha256(
    ranges: Iterable[AllowedEvidenceRange],
    direct_claim_ids: Iterable[str],
    carried_questions: Iterable[Question],
) -> str:
    payload = {
        "validation_context_version": "ensemble-validation-context-v1",
        "allowed_evidence_ranges": [
            {
                "evidence_id": value.evidence_id,
                "char_start": value.char_start,
                "char_end": value.char_end,
            }
            for value in ranges
        ],
        "direct_claim_ids": list(direct_claim_ids),
        "carried_questions": [value.model_dump(mode="json") for value in carried_questions],
    }
    return sha256_canonical(payload)
