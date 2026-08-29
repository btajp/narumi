"""Deterministic and injection-safe ensemble Markdown rendering."""

from __future__ import annotations

import pytest
from narumi.generate.ensemble.canonical import (
    content_projection_sha256,
    make_claim,
    make_question,
)
from narumi.generate.ensemble.renderer import render_document
from narumi.generate.ensemble.source import snapshot_source
from narumi.generate.ensemble.types import Claim, EnsembleDocument, Question, RawClaim, RawQuestion
from narumi.models import MergedSegment, MergedTranscript


def fixture():
    merged = MergedTranscript(
        segments=[
            MergedSegment(
                id="m-1",
                start=4.125,
                end=5.0,
                text="最初の根拠🙂",
                speaker_name="岡村",
                sources=["mic"],
            ),
            MergedSegment(
                id="m-2",
                start=8.0,
                end=9.0,
                text="別案の根拠",
                speaker_label="other",
                sources=["system"],
            ),
        ]
    )
    return snapshot_source(merged, "meeting-renderer")


def raw_claim(snapshot, *, kind: str, text: str, evidence_index: int = 0, **values):
    evidence = snapshot.evidence[evidence_index]
    return RawClaim.model_validate(
        {
            "kind": kind,
            "text": text,
            "evidence": [
                {
                    "evidence_id": evidence.evidence_id,
                    "char_start": evidence.char_start,
                    "char_end": evidence.char_end,
                }
            ],
            "owner": values.get("owner"),
            "due": values.get("due"),
            "from": [],
        }
    )


def test_empty_valid_document_has_narrow_nonfactual_message():
    document = EnsembleDocument(schema_version="ensemble-document-v1", claims=(), questions=())
    assert render_document(document, fixture()) == "# 統合議事録\n\n抽出項目なし\n"
    assert render_document(document, {}) == "# 統合議事録\n\n抽出項目なし\n"


def test_sections_are_fixed_and_items_use_first_evidence_time_not_model_order():
    snapshot = fixture()
    document = EnsembleDocument(
        schema_version="ensemble-document-v1",
        claims=[
            make_claim(raw_claim(snapshot, kind="action", text="後の行動", evidence_index=1)),
            make_claim(raw_claim(snapshot, kind="decision", text="決定")),
            make_claim(raw_claim(snapshot, kind="agenda", text="議題")),
            make_claim(raw_claim(snapshot, kind="discussion", text="議論")),
        ],
        questions=(),
    )
    rendered = render_document(document, snapshot)

    headings = ["## アジェンダ", "## 議論サマリ", "## 決定事項", "## アクション"]
    assert [rendered.index(value) for value in headings] == sorted(
        rendered.index(value) for value in headings
    )
    assert "担当: 未設定／期限: 未設定" in rendered
    assert "04.125" in rendered and "最初の根拠🙂" in rendered


def test_model_markdown_html_links_and_newlines_are_escaped_but_generated_links_work():
    snapshot = fixture()
    dangerous = "# 見出し\n<script>x</script> [外部](https://example.com) *強調*"
    document = EnsembleDocument(
        schema_version="ensemble-document-v1",
        claims=[make_claim(raw_claim(snapshot, kind="discussion", text=dangerous))],
        questions=(),
    )
    rendered = render_document(document, snapshot)

    assert "<script>" not in rendered
    assert "&lt;script&gt;x&lt;/script&gt;" in rendered
    assert "\\[外部\\]\\(https://example\\.com\\)" in rendered
    assert "\\*強調\\*" in rendered
    assert "<br>" in rendered
    assert "[根拠1](#evidence-" in rendered
    assert '<a id="evidence-' in rendered


def test_question_alternatives_and_exact_source_quotes_survive_to_evidence_section():
    snapshot = fixture()
    first, second = snapshot.evidence
    question = make_question(
        RawQuestion.model_validate(
            {
                "kind": "conflict",
                "text": "どちらを採用するか",
                "alternatives": [
                    {
                        "text": "第一案",
                        "evidence": [
                            {
                                "evidence_id": first.evidence_id,
                                "char_start": first.char_start,
                                "char_end": first.char_end,
                            }
                        ],
                    },
                    {
                        "text": "第二案",
                        "evidence": [
                            {
                                "evidence_id": second.evidence_id,
                                "char_start": second.char_start,
                                "char_end": second.char_end,
                            }
                        ],
                    },
                ],
                "from": [],
            }
        )
    )
    rendered = render_document(
        EnsembleDocument(schema_version="ensemble-document-v1", claims=(), questions=[question]),
        snapshot,
    )

    assert "## 確認事項" in rendered
    assert "第一案" in rendered and "第二案" in rendered
    assert "最初の根拠🙂" in rendered and "別案の根拠" in rendered


def test_extreme_but_finite_source_time_renders_without_float_overflow():
    merged = MergedTranscript(
        segments=[MergedSegment(id="m-extreme", start=1e308, end=1e308, text="遠い時刻")]
    )
    snapshot = snapshot_source(merged, "meeting-extreme-time")
    document = EnsembleDocument(
        schema_version="ensemble-document-v1",
        claims=[make_claim(raw_claim(snapshot, kind="discussion", text="確認"))],
        questions=(),
    )
    rendered = render_document(document, snapshot)
    assert "遠い時刻" in rendered
    assert "evidence-ev_" in rendered


def test_renderer_revalidates_duplicate_ids_even_for_unchecked_internal_models():
    snapshot = fixture()
    claim = make_claim(raw_claim(snapshot, kind="discussion", text="重複"))
    unchecked = EnsembleDocument.model_construct(
        schema_version="ensemble-document-v1",
        claims=(claim, claim),
        questions=(),
    )
    with pytest.raises(ValueError, match="closed bounds"):
        render_document(unchecked, snapshot)


def test_empty_speaker_name_is_not_collapsed_into_label_or_unknown():
    merged = MergedTranscript(
        segments=[
            MergedSegment(
                id="m-empty-speaker",
                start=1,
                end=2,
                text="発言",
                speaker_name="",
                speaker_label="fallback-label",
            )
        ]
    )
    snapshot = snapshot_source(merged, "meeting-empty-speaker")
    document = EnsembleDocument(
        schema_version="ensemble-document-v1",
        claims=[make_claim(raw_claim(snapshot, kind="discussion", text="空名を保持"))],
        questions=(),
    )
    rendered = render_document(document, snapshot)
    assert "fallback-label" not in rendered
    assert "話者不明" not in rendered


def test_nested_reference_and_alternative_order_do_not_change_projection_or_rendering():
    snapshot = fixture()
    first, second = snapshot.evidence
    refs = [
        {
            "evidence_id": item.evidence_id,
            "char_start": item.char_start,
            "char_end": item.char_end,
        }
        for item in (first, second)
    ]
    base_claim = make_claim(
        RawClaim.model_validate(
            {
                "kind": "discussion",
                "text": "二つの根拠",
                "evidence": refs,
                "owner": None,
                "due": None,
                "from": [],
            }
        )
    )
    reversed_claim = Claim(
        id=base_claim.id,
        kind=base_claim.kind,
        text=base_claim.text,
        evidence=tuple(reversed(base_claim.evidence)),
        owner=None,
        due=None,
    )
    base_question = make_question(
        RawQuestion.model_validate(
            {
                "kind": "conflict",
                "text": "どちらか",
                "alternatives": [
                    {"text": "第一", "evidence": [refs[0]]},
                    {"text": "第二", "evidence": [refs[1]]},
                ],
                "from": [],
            }
        )
    )
    reversed_question = Question(
        id=base_question.id,
        kind=base_question.kind,
        text=base_question.text,
        alternatives=tuple(reversed(base_question.alternatives)),
    )
    canonical = EnsembleDocument(
        schema_version="ensemble-document-v1",
        claims=[base_claim],
        questions=[base_question],
    )
    reordered = EnsembleDocument(
        schema_version="ensemble-document-v1",
        claims=[reversed_claim],
        questions=[reversed_question],
    )

    assert content_projection_sha256(canonical) == content_projection_sha256(reordered)
    assert render_document(canonical, snapshot) == render_document(reordered, snapshot)
