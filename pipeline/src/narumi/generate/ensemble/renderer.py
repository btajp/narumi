"""Deterministic Markdown rendering for validated ensemble minutes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_EVEN, Decimal

from .canonical import canonical_document
from .source import EnsembleSourceError, materialize_ref
from .types import Claim, EnsembleDocument, Evidence, EvidenceRef, Question, SourceSnapshot

RENDERER_VERSION = "ensemble-markdown-v1"
_SECTIONS = (
    ("agenda", "アジェンダ"),
    ("discussion", "議論サマリ"),
    ("decision", "決定事項"),
    ("action", "アクション"),
)
_MARKDOWN = frozenset("\\`*_{}[]()#+-.!|")


def escape_model_text(value: str) -> str:
    """Render model text as inert Markdown text while retaining every codepoint."""
    escaped: list[str] = []
    for char in value:
        if char == "&":
            escaped.append("&amp;")
        elif char == "<":
            escaped.append("&lt;")
        elif char == ">":
            escaped.append("&gt;")
        elif char in {"\r", "\n"}:
            escaped.append("<br>")
        elif char in _MARKDOWN:
            escaped.append("\\" + char)
        else:
            escaped.append(char)
    return "".join(escaped)


def _lookup(
    value: SourceSnapshot | Mapping[str, Evidence],
) -> tuple[dict[str, Evidence], SourceSnapshot | None]:
    if isinstance(value, SourceSnapshot):
        return value.evidence_by_id(), value
    evidence = dict(value)
    if not evidence:
        raise EnsembleSourceError("renderer evidence lookup is empty")
    return evidence, None


def _ref_sort(ref: EvidenceRef, evidence: Mapping[str, Evidence]) -> tuple[object, ...]:
    source = evidence.get(ref.evidence_id)
    if source is None:
        raise EnsembleSourceError("renderer document references unknown evidence")
    return source.start_seconds, ref.char_start, ref.char_end, ref.evidence_id


def _item_sort(refs: Iterable[EvidenceRef], identity: str, evidence: Mapping[str, Evidence]):
    ordered = sorted(refs, key=lambda ref: _ref_sort(ref, evidence))
    if not ordered:
        raise EnsembleSourceError("renderer item has no evidence")
    return (*_ref_sort(ordered[0], evidence), identity)


def _clock(seconds: float) -> str:
    millis = int((Decimal.from_float(seconds) * 1000).to_integral_value(rounding=ROUND_HALF_EVEN))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole, fraction = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d}.{fraction:03d}"


def _anchor(ref: EvidenceRef) -> str:
    return f"evidence-{ref.evidence_id}-{ref.char_start}-{ref.char_end}"


def _unique_refs(refs: Iterable[EvidenceRef]) -> list[EvidenceRef]:
    values = {(ref.evidence_id, ref.char_start, ref.char_end): ref for ref in refs}
    return list(values.values())


def _links(refs: Iterable[EvidenceRef], evidence: Mapping[str, Evidence]) -> str:
    ordered = sorted(_unique_refs(refs), key=lambda ref: _ref_sort(ref, evidence))
    return " ".join(f"[根拠{i}](#{_anchor(ref)})" for i, ref in enumerate(ordered, start=1))


def _claim_lines(claim: Claim, evidence: Mapping[str, Evidence]) -> list[str]:
    text = escape_model_text(claim.text)
    if claim.kind == "action":
        owner = escape_model_text(claim.owner) if claim.owner is not None else "未設定"
        due = escape_model_text(claim.due) if claim.due is not None else "未設定"
        text += f" （担当: {owner}／期限: {due}）"
    return [f"- {text}", f"  - {_links(claim.evidence, evidence)}"]


def _question_lines(question: Question, evidence: Mapping[str, Evidence]) -> list[str]:
    lines = [f"- {escape_model_text(question.text)}"]
    for alternative in question.alternatives:
        lines.append(
            f"  - {escape_model_text(alternative.text)} {_links(alternative.evidence, evidence)}"
        )
    return lines


def _all_refs(document: EnsembleDocument) -> list[EvidenceRef]:
    refs = [ref for claim in document.claims for ref in claim.evidence]
    refs.extend(
        ref
        for question in document.questions
        for alternative in question.alternatives
        for ref in alternative.evidence
    )
    return refs


def _quote(
    ref: EvidenceRef,
    evidence: Mapping[str, Evidence],
    snapshot: SourceSnapshot | None,
) -> str:
    if snapshot is not None:
        return materialize_ref(snapshot, ref)
    source = evidence.get(ref.evidence_id)
    if source is None or ref.char_start < source.char_start or ref.char_end > source.char_end:
        raise EnsembleSourceError("renderer reference is outside its evidence atom")
    return source.text[ref.char_start - source.char_start : ref.char_end - source.char_start]


def render_document(
    document: EnsembleDocument,
    evidence_lookup: SourceSnapshot | Mapping[str, Evidence],
) -> str:
    """Render only a validated document; source excerpts always come from the snapshot."""
    document = canonical_document(document)
    if not document.claims and not document.questions:
        return "# 統合議事録\n\n抽出項目なし\n"
    evidence, snapshot = _lookup(evidence_lookup)

    lines = ["# 統合議事録"]
    for kind, heading in _SECTIONS:
        claims = [claim for claim in document.claims if claim.kind == kind]
        if not claims:
            continue
        lines.extend(("", f"## {heading}"))
        for claim in sorted(claims, key=lambda item: _item_sort(item.evidence, item.id, evidence)):
            lines.extend(_claim_lines(claim, evidence))
    if document.questions:
        lines.extend(("", "## 確認事項"))
        questions = sorted(
            document.questions,
            key=lambda item: _item_sort(
                (ref for alt in item.alternatives for ref in alt.evidence), item.id, evidence
            ),
        )
        for question in questions:
            lines.extend(_question_lines(question, evidence))

    lines.extend(("", "## 根拠"))
    for ref in sorted(
        _unique_refs(_all_refs(document)), key=lambda item: _ref_sort(item, evidence)
    ):
        source = evidence[ref.evidence_id]
        quote = escape_model_text(_quote(ref, evidence, snapshot))
        if source.speaker_name is not None:
            speaker = source.speaker_name
        elif source.speaker_label is not None:
            speaker = source.speaker_label
        else:
            speaker = "話者不明"
        lines.append(
            f'<a id="{_anchor(ref)}"></a>- {_clock(source.start_seconds)} '
            f"{escape_model_text(speaker)}: {quote}"
        )
    return "\n".join(lines) + "\n"
