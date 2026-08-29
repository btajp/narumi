"""Closed public ensemble types preserve required-field semantics."""

from __future__ import annotations

from copy import deepcopy

import pytest
from narumi.generate.ensemble.canonical import make_claim, make_question
from narumi.generate.ensemble.types import (
    DraftDocument,
    DraftPart,
    EnsembleDocument,
    EnsembleResponse,
    Evidence,
    EvidenceRef,
    EvidenceView,
    QuestionAlternative,
    RawClaim,
    RawOmission,
    RawQuestion,
    SourceBinding,
    SourceDocument,
    SourceIndexDocument,
)
from pydantic import BaseModel, ValidationError


def public_examples() -> tuple[BaseModel, ...]:
    evidence = Evidence(
        evidence_id="ev_" + "1" * 64,
        start_seconds=1.0,
        end_seconds=2.0,
        speaker_label=None,
        speaker_name="",
        char_start=0,
        char_end=2,
        text="発言",
        occurrence_index=0,
        occurrence_count=1,
        source_binding=SourceBinding(
            segment_index=0,
            segment_id="m-1",
            segment_text_sha256="2" * 64,
            sources=("mic",),
        ),
    )
    reference = EvidenceRef(evidence_id=evidence.evidence_id, char_start=0, char_end=2)
    alternative = QuestionAlternative(text="原文では不明", evidence=(reference,))
    raw_claim = RawClaim.model_validate(
        {
            "kind": "decision",
            "text": "採用する",
            "evidence": [reference.model_dump(mode="python")],
            "owner": None,
            "due": None,
            "from": [],
        }
    )
    raw_question = RawQuestion.model_validate(
        {
            "kind": "missing_context",
            "text": "期限はいつか",
            "alternatives": [alternative.model_dump(mode="python")],
            "from": [],
        }
    )
    claim = make_claim(raw_claim)
    question = make_question(raw_question)
    source_part = DraftPart(
        source_artifact_id="artifact-" + "3" * 32,
        document_artifact_id="artifact-" + "4" * 32,
    )
    return (
        evidence.source_binding,
        evidence.view(),
        evidence,
        reference,
        claim,
        alternative,
        question,
        SourceIndexDocument(schema_version="ensemble-source-index-v1", packet_artifact_ids=()),
        SourceDocument(schema_version="ensemble-source-v1", evidence=(evidence,)),
        EnsembleDocument(
            schema_version="ensemble-document-v1",
            claims=(claim,),
            questions=(question,),
        ),
        source_part,
        DraftDocument(schema_version="ensemble-draft-v1", parts=(source_part,)),
        raw_claim,
        raw_question,
        RawOmission(from_claim=claim.id, reason="duplicate"),
        EnsembleResponse(
            schema_version="ensemble-response-v1",
            claims=(raw_claim,),
            questions=(raw_question,),
            omissions=(),
        ),
    )


def test_every_declared_public_and_response_field_is_required():
    for example in public_examples():
        model = type(example)
        payload = example.model_dump(mode="python", by_alias=True)
        for field_name, field in model.model_fields.items():
            assert field.is_required(), f"{model.__name__}.{field_name} must be required"
            wire_name = field.serialization_alias or field_name
            missing = deepcopy(payload)
            del missing[wire_name]
            with pytest.raises(ValidationError, match="Field required"):
                model.model_validate(missing)


def test_nullable_speaker_fields_distinguish_null_empty_and_omitted():
    payload = next(
        item.model_dump(mode="python")
        for item in public_examples()
        if isinstance(item, EvidenceView) and not isinstance(item, Evidence)
    )
    null_value = EvidenceView.model_validate({**payload, "speaker_name": None})
    empty_value = EvidenceView.model_validate({**payload, "speaker_name": ""})

    assert null_value.speaker_name is None
    assert empty_value.speaker_name == ""
    del payload["speaker_name"]
    with pytest.raises(ValidationError, match="Field required"):
        EvidenceView.model_validate(payload)
