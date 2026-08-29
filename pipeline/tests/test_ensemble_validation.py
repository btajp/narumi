"""Strict response and evidence validation for ensemble model calls."""

from __future__ import annotations

import json

import pytest
from narumi.generate.ensemble.canonical import make_question
from narumi.generate.ensemble.public import forward_chars
from narumi.generate.ensemble.response import ResponseStructureError, decode_response
from narumi.generate.ensemble.source import allowed_ranges, snapshot_source
from narumi.generate.ensemble.types import (
    AllowedEvidenceRange,
    EnsembleDocument,
    PreparedPrompt,
    RawQuestion,
    SourceSnapshot,
    ValidationOutcome,
)
from narumi.generate.ensemble.validation import validate_response
from narumi.models import MergedSegment, MergedTranscript


def source() -> SourceSnapshot:
    text = "A🙂e\N{COMBINING ACUTE ACCENT}BCDE"
    merged = MergedTranscript(
        segments=[
            MergedSegment(
                id="m-1",
                start=1.0,
                end=2.0,
                text=text,
                speaker_label="other",
                speaker_name=None,
                sources=["system"],
            )
        ]
    )
    return snapshot_source(merged, "meeting-validation")


def prepared(
    snapshot: SourceSnapshot,
    *,
    stage: str = "draft",
    direct: tuple[str, ...] = (),
    carried=(),
    ranges: tuple[AllowedEvidenceRange, ...] | None = None,
    input_forward_chars: int | None = None,
    output_forward_limit: int | None = None,
) -> PreparedPrompt:
    return PreparedPrompt(
        system="system",
        user="user",
        stage=stage,  # type: ignore[arg-type]
        template_hash="a" * 64,
        response_schema_version="ensemble-response-v1",
        allowed_evidence_ranges=ranges or allowed_ranges(snapshot.evidence),
        direct_claim_ids=direct,
        carried_questions=tuple(carried),
        input_chars=10,
        input_forward_chars=input_forward_chars,
        output_forward_limit=output_forward_limit,
    )


def response_dict(snapshot: SourceSnapshot) -> dict:
    item = snapshot.evidence[0]
    return {
        "schema_version": "ensemble-response-v1",
        "claims": [
            {
                "kind": "decision",
                "text": "採用する",
                "evidence": [
                    {
                        "evidence_id": item.evidence_id,
                        "char_start": 1,
                        "char_end": 4,
                    }
                ],
                "owner": None,
                "due": None,
                "from": [],
            }
        ],
        "questions": [],
        "omissions": [],
    }


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def test_valid_response_uses_absolute_python_codepoint_offsets_and_stable_ids():
    snapshot = source()
    raw = encode(response_dict(snapshot))
    first = validate_response(raw, prepared(snapshot), snapshot)
    second = validate_response(raw, prepared(snapshot), snapshot)

    assert first.outcome is ValidationOutcome.ACCEPTED
    assert first.validated == second.validated
    assert first.validated is not None
    claim = first.validated.document.claims[0]
    assert claim.evidence[0].char_start == 1
    assert claim.evidence[0].char_end == 4
    assert snapshot.evidence[0].text[1:4] == "🙂e\N{COMBINING ACUTE ACCENT}"


@pytest.mark.parametrize(
    "raw",
    [
        "```json\n{}\n```",
        '{"schema_version":"ensemble-response-v1","claims":[],"claims":[],'
        '"questions":[],"omissions":[]}',
        '{"schema_version":"ensemble-response-v1","claims":[],"questions":[],'
        '"omissions":[],"extra":true}',
        '{"schema_version":"ensemble-response-v1","claims":[],"questions":[],'
        '"omissions":[],"n":NaN}',
        '[{"schema_version":"ensemble-response-v1"}]',
        '{"schema_version":"ensemble-response-v1","claims":[],"questions":[],'
        '"omissions":[]} trailing',
    ],
)
def test_fence_duplicate_unknown_nonfinite_nonobject_and_prose_are_rejected(raw: str):
    with pytest.raises(ResponseStructureError):
        decode_response(raw)


def test_bytes_surrogate_depth_and_response_byte_limit_are_rejected():
    with pytest.raises(ResponseStructureError, match="Unicode text"):
        decode_response(b"{}")  # type: ignore[arg-type]
    with pytest.raises(ResponseStructureError, match="invalid Unicode"):
        decode_response('"\ud800"')
    nested = "0"
    for _ in range(13):
        nested = f"[{nested}]"
    deep_object = (
        '{"schema_version":"ensemble-response-v1","claims":[],"questions":[],'
        f'"omissions":[],"unknown":{nested}}}'
    )
    with pytest.raises(ResponseStructureError, match="depth"):
        decode_response(deep_object)
    oversized = " " * 262_145
    with pytest.raises(ResponseStructureError, match="byte limit"):
        decode_response(oversized)


def test_bool_as_offset_and_whitespace_only_text_are_invalid_structure():
    snapshot = source()
    value = response_dict(snapshot)
    value["claims"][0]["evidence"][0]["char_start"] = True
    assert (
        validate_response(encode(value), prepared(snapshot), snapshot).outcome
        is ValidationOutcome.INVALID_STRUCTURE
    )


def test_required_from_action_whitespace_and_conflict_cardinality_are_strict():
    snapshot = source()
    value = response_dict(snapshot)
    del value["claims"][0]["from"]
    assert (
        validate_response(encode(value), prepared(snapshot), snapshot).outcome
        is ValidationOutcome.INVALID_STRUCTURE
    )

    value = response_dict(snapshot)
    value["claims"][0].update(kind="action", owner="   ")
    assert (
        validate_response(encode(value), prepared(snapshot), snapshot).outcome
        is ValidationOutcome.INVALID_STRUCTURE
    )

    item = snapshot.evidence[0]
    value = {
        "schema_version": "ensemble-response-v1",
        "claims": [],
        "questions": [
            {
                "kind": "conflict",
                "text": "どちらか",
                "alternatives": [
                    {
                        "text": "一案だけ",
                        "evidence": [
                            {
                                "evidence_id": item.evidence_id,
                                "char_start": 0,
                                "char_end": 1,
                            }
                        ],
                    }
                ],
                "from": [],
            }
        ],
        "omissions": [],
    }
    assert (
        validate_response(encode(value), prepared(snapshot), snapshot).outcome
        is ValidationOutcome.INVALID_STRUCTURE
    )
    value = response_dict(snapshot)
    value["claims"][0]["text"] = "   "
    assert (
        validate_response(encode(value), prepared(snapshot), snapshot).outcome
        is ValidationOutcome.INVALID_STRUCTURE
    )


def test_reference_must_exist_and_fit_one_contiguous_allowed_range():
    snapshot = source()
    item = snapshot.evidence[0]
    value = response_dict(snapshot)
    value["claims"][0]["evidence"][0].update(char_start=1, char_end=5)
    gaps = (
        AllowedEvidenceRange(item.evidence_id, 0, 2),
        AllowedEvidenceRange(item.evidence_id, 3, item.char_end),
    )
    result = validate_response(encode(value), prepared(snapshot, ranges=gaps), snapshot)
    assert result.outcome is ValidationOutcome.INVALID_EVIDENCE

    value["claims"][0]["evidence"][0]["evidence_id"] = "ev_" + "f" * 64
    result = validate_response(encode(value), prepared(snapshot), snapshot)
    assert result.outcome is ValidationOutcome.INVALID_EVIDENCE


def test_evidence_diagnostic_precedes_direct_claim_coverage_diagnostic():
    snapshot = source()
    value = response_dict(snapshot)
    value["claims"][0]["evidence"][0]["evidence_id"] = "ev_" + "f" * 64
    direct = "cl_" + "1" * 64
    result = validate_response(encode(value), prepared(snapshot, direct=(direct,)), snapshot)
    assert result.outcome is ValidationOutcome.INVALID_EVIDENCE


def test_direct_claim_coverage_requires_exact_union_without_keep_omit_overlap():
    snapshot = source()
    direct = "cl_" + "1" * 64
    value = response_dict(snapshot)
    result = validate_response(encode(value), prepared(snapshot, direct=(direct,)), snapshot)
    assert result.outcome is ValidationOutcome.INVALID_STRUCTURE

    value["claims"][0]["from"] = [direct]
    value["omissions"] = [{"from": direct, "reason": "not_selected"}]
    result = validate_response(encode(value), prepared(snapshot, direct=(direct,)), snapshot)
    assert result.outcome is ValidationOutcome.INVALID_STRUCTURE

    decoded = decode_response(encode(value)).model_dump(mode="json", by_alias=True)
    assert decoded["claims"][0]["from"] == [direct]
    assert decoded["omissions"][0] == {"from": direct, "reason": "not_selected"}

    value["omissions"] = []
    result = validate_response(encode(value), prepared(snapshot, direct=(direct,)), snapshot)
    assert result.outcome is ValidationOutcome.ACCEPTED


def test_known_questions_are_carried_verbatim_and_cannot_be_deleted_by_the_model():
    snapshot = source()
    item = snapshot.evidence[0]
    carried = make_question(
        RawQuestion.model_validate(
            {
                "kind": "missing_context",
                "text": "期限はいつか",
                "alternatives": [
                    {
                        "text": "原文では不明",
                        "evidence": [
                            {
                                "evidence_id": item.evidence_id,
                                "char_start": 0,
                                "char_end": 1,
                            }
                        ],
                    }
                ],
                "from": [],
            }
        )
    )
    empty = {
        "schema_version": "ensemble-response-v1",
        "claims": [],
        "questions": [],
        "omissions": [],
    }
    result = validate_response(
        encode(empty), prepared(snapshot, stage="synthesis", carried=(carried,)), snapshot
    )
    assert result.outcome is ValidationOutcome.ACCEPTED
    assert result.validated is not None
    assert result.validated.document.questions == (carried,)


def test_reduce_must_shrink_the_same_forward_projection_and_fit_next_receiver():
    snapshot = source()
    raw = encode(response_dict(snapshot))
    accepted = validate_response(raw, prepared(snapshot), snapshot)
    assert accepted.validated is not None
    size = forward_chars(accepted.validated.document, snapshot)

    result = validate_response(
        raw,
        prepared(snapshot, stage="reduce", input_forward_chars=size),
        snapshot,
    )
    assert result.outcome is ValidationOutcome.NON_REDUCING
    result = validate_response(
        raw,
        prepared(
            snapshot,
            stage="reduce",
            input_forward_chars=size + 100,
            output_forward_limit=size - 1,
        ),
        snapshot,
    )
    assert result.outcome is ValidationOutcome.NON_REDUCING


def test_explicit_empty_document_is_valid_but_malformed_json_is_not():
    snapshot = source()
    empty = encode(
        {
            "schema_version": "ensemble-response-v1",
            "claims": [],
            "questions": [],
            "omissions": [],
        }
    )
    result = validate_response(empty, prepared(snapshot), snapshot)
    assert result.outcome is ValidationOutcome.ACCEPTED
    assert result.validated is not None
    assert result.validated.document == EnsembleDocument(
        schema_version="ensemble-document-v1", claims=(), questions=()
    )
    assert (
        validate_response("{", prepared(snapshot), snapshot).outcome
        is ValidationOutcome.INVALID_STRUCTURE
    )
