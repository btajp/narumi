"""Deterministic source snapshotting and fixed-window evidence packetization."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from narumi.models import MergedSegment, MergedTranscript

from .canonical import canonical_bytes, canonical_float, canonical_json, sha256_canonical
from .types import (
    AllowedEvidenceRange,
    Evidence,
    EvidenceRef,
    EvidenceView,
    SourceBinding,
    SourceDocument,
    SourcePacket,
    SourceSnapshot,
)

EVIDENCE_VERSION = "ensemble-evidence-v1"
SOURCE_PROJECTION_VERSION = "ensemble-source-projection-v1"
EVIDENCE_ATOM_CHARS = 512
SOURCE_WINDOW_SECONDS = 600
SOURCE_PACKET_CHARS = 3_000
SOURCE_PACKET_ATOMS = 32
MAX_SOURCE_PACKETS = 64


class EnsembleSourceError(ValueError):
    """Source data cannot be represented without truncation or ambiguity."""


@dataclass(frozen=True)
class ChunkingPolicy:
    atom_chars: int = EVIDENCE_ATOM_CHARS
    window_seconds: int = SOURCE_WINDOW_SECONDS
    packet_chars: int = SOURCE_PACKET_CHARS
    packet_atoms: int = SOURCE_PACKET_ATOMS
    max_packets: int = MAX_SOURCE_PACKETS

    def __post_init__(self) -> None:
        if (
            min(
                self.atom_chars,
                self.window_seconds,
                self.packet_chars,
                self.packet_atoms,
                self.max_packets,
            )
            <= 0
        ):
            raise ValueError("source chunking limits must be positive")
        if self.atom_chars > EVIDENCE_ATOM_CHARS:
            raise ValueError("evidence atoms cannot exceed the public 512-codepoint limit")
        if self.packet_atoms > SOURCE_PACKET_ATOMS:
            raise ValueError("source packets cannot exceed the public 32-atom limit")
        if self.max_packets > MAX_SOURCE_PACKETS:
            raise ValueError("source indexes cannot exceed the public 64-packet limit")


@dataclass(frozen=True)
class _Atom:
    start_seconds: float
    end_seconds: float
    speaker_label: str | None
    speaker_name: str | None
    char_start: int
    char_end: int
    text: str
    source_binding: SourceBinding


def _sha_text(text: str) -> str:
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise EnsembleSourceError("source text contains invalid Unicode") from exc


def _validate_segment(segment: MergedSegment, index: int) -> tuple[float, float]:
    start = float(segment.start)
    end = float(segment.end)
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
        raise EnsembleSourceError(f"segment {index} has an invalid time range")
    for label, value in (
        ("segment ID", segment.id),
        ("speaker label", segment.speaker_label),
        ("speaker name", segment.speaker_name),
    ):
        if value is not None:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise EnsembleSourceError(f"segment {index} {label} is invalid Unicode") from exc
    if not 1 <= len(segment.id) <= 512:
        raise EnsembleSourceError(f"segment {index} ID is outside the public bound")
    if segment.speaker_label is not None and len(segment.speaker_label) > 512:
        raise EnsembleSourceError(f"segment {index} speaker label is too long")
    if segment.speaker_name is not None and len(segment.speaker_name) > 512:
        raise EnsembleSourceError(f"segment {index} speaker name is too long")
    if len(segment.sources) > 256 or any(
        not isinstance(source, str) or not 1 <= len(source) <= 512 for source in segment.sources
    ):
        raise EnsembleSourceError(f"segment {index} source labels are outside the public bound")
    try:
        for source in segment.sources:
            source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EnsembleSourceError(f"segment {index} source label is invalid Unicode") from exc
    if start == 0:
        start = 0.0
    if end == 0:
        end = 0.0
    return start, end


def _atoms(
    segments: Sequence[MergedSegment], atom_chars: int
) -> tuple[list[_Atom], tuple[str, ...]]:
    result: list[_Atom] = []
    texts: list[str] = []
    for index, segment in enumerate(segments):
        start, end = _validate_segment(segment, index)
        text_hash = _sha_text(segment.text)
        texts.append(segment.text)
        binding = SourceBinding(
            segment_index=index,
            segment_id=segment.id,
            segment_text_sha256=text_hash,
            sources=list(segment.sources),
        )
        for char_start in range(0, len(segment.text), atom_chars):
            char_end = min(len(segment.text), char_start + atom_chars)
            result.append(
                _Atom(
                    start_seconds=start,
                    end_seconds=end,
                    speaker_label=segment.speaker_label,
                    speaker_name=segment.speaker_name,
                    char_start=char_start,
                    char_end=char_end,
                    text=segment.text[char_start:char_end],
                    source_binding=binding,
                )
            )
    return result, tuple(texts)


def _identity_without_occurrence(meeting_id: str, atom: _Atom) -> dict[str, object]:
    return {
        "evidence_version": EVIDENCE_VERSION,
        "meeting_id": meeting_id,
        "start_seconds": canonical_float(atom.start_seconds),
        "end_seconds": canonical_float(atom.end_seconds),
        "speaker_label": atom.speaker_label,
        "speaker_name": atom.speaker_name,
        "char_start": atom.char_start,
        "char_end": atom.char_end,
        "text": atom.text,
    }


def snapshot_source(
    merged: MergedTranscript | Sequence[MergedSegment],
    meeting_id: str,
    *,
    policy: ChunkingPolicy | None = None,
) -> SourceSnapshot:
    """Create all evidence occurrences without normalizing or discarding source text."""
    if not isinstance(meeting_id, str) or not meeting_id:
        raise EnsembleSourceError("meeting_id must be a non-empty string")
    try:
        meeting_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EnsembleSourceError("meeting_id contains invalid Unicode") from exc
    selected = policy or ChunkingPolicy()
    segments = merged.segments if isinstance(merged, MergedTranscript) else list(merged)
    atoms, texts = _atoms(segments, selected.atom_chars)
    if not atoms:
        raise EnsembleSourceError("source contains no referenceable text")

    groups: dict[bytes, list[_Atom]] = defaultdict(list)
    identities: dict[bytes, dict[str, object]] = {}
    for atom in atoms:
        identity = _identity_without_occurrence(meeting_id, atom)
        key = canonical_bytes(identity)
        groups[key].append(atom)
        identities[key] = identity

    evidence: list[Evidence] = []
    seen: dict[str, bytes] = {}
    for key in sorted(groups):
        occurrences = sorted(
            groups[key], key=lambda item: canonical_json(item.source_binding).encode("utf-8")
        )
        count = len(occurrences)
        for occurrence_index, atom in enumerate(occurrences):
            identity = {
                **identities[key],
                "occurrence_index": occurrence_index,
                "occurrence_count": count,
            }
            evidence_id = "ev_" + sha256_canonical(identity)
            encoded = canonical_bytes(identity)
            previous = seen.setdefault(evidence_id, encoded)
            if previous != encoded:
                raise EnsembleSourceError("evidence identity collision")
            evidence.append(
                Evidence(
                    evidence_id=evidence_id,
                    start_seconds=atom.start_seconds,
                    end_seconds=atom.end_seconds,
                    speaker_label=atom.speaker_label,
                    speaker_name=atom.speaker_name,
                    char_start=atom.char_start,
                    char_end=atom.char_end,
                    text=atom.text,
                    occurrence_index=occurrence_index,
                    occurrence_count=count,
                    source_binding=atom.source_binding,
                )
            )
    evidence.sort(key=_evidence_sort_key)
    return SourceSnapshot(meeting_id=meeting_id, evidence=tuple(evidence), segment_texts=texts)


def _nullable_sort(value: str | None) -> tuple[int, bytes]:
    return (0, b"") if value is None else (1, value.encode("utf-8"))


def _evidence_sort_key(value: Evidence) -> tuple[object, ...]:
    return (
        value.start_seconds,
        value.end_seconds,
        _nullable_sort(value.speaker_label),
        _nullable_sort(value.speaker_name),
        value.char_start,
        value.char_end,
        value.text.encode("utf-8"),
        value.occurrence_count,
        value.occurrence_index,
        value.evidence_id,
    )


def evidence_view(packet: SourcePacket | SourceDocument) -> tuple[EvidenceView, ...]:
    document = packet.document if isinstance(packet, SourcePacket) else packet
    if isinstance(
        packet, SourcePacket
    ) and packet.content_projection_sha256 != source_projection_sha256(document):
        raise EnsembleSourceError("source packet content changed after projection")
    return tuple(item.view() for item in document.evidence)


def _view_chars(items: Sequence[Evidence]) -> int:
    return len(canonical_json([item.view().model_dump(mode="json") for item in items]))


def build_source_packets(
    snapshot: SourceSnapshot, policy: ChunkingPolicy | None = None
) -> tuple[SourcePacket, ...]:
    """Greedily partition each fixed time window, assigning every atom exactly once."""
    selected = policy or ChunkingPolicy()
    windows: dict[int, list[Evidence]] = defaultdict(list)
    for item in snapshot.evidence:
        window = math.floor(item.start_seconds / selected.window_seconds)
        windows[window].append(item)

    packets: list[SourcePacket] = []
    for window in sorted(windows):
        ordered = sorted(windows[window], key=_evidence_sort_key)
        current: list[Evidence] = []
        for item in ordered:
            candidate = [*current, item]
            if (
                len(candidate) <= selected.packet_atoms
                and _view_chars(candidate) <= selected.packet_chars
            ):
                current = candidate
                continue
            if not current:
                raise EnsembleSourceError("one evidence atom exceeds the source packet limit")
            packets.append(_packet(window, current))
            current = [item]
            if _view_chars(current) > selected.packet_chars:
                raise EnsembleSourceError("one evidence atom exceeds the source packet limit")
        if current:
            packets.append(_packet(window, current))

    if len(packets) > selected.max_packets:
        raise EnsembleSourceError("source requires more than the permitted packet count")
    flattened = [item.evidence_id for packet in packets for item in packet.document.evidence]
    expected = [item.evidence_id for item in snapshot.evidence]
    if len(flattened) != len(expected) or set(flattened) != set(expected):
        raise EnsembleSourceError("source packet partition is incomplete or overlapping")
    return tuple(packets)


def source_projection_sha256(document: SourceDocument) -> str:
    try:
        validated = SourceDocument.model_validate(document.model_dump(mode="python"))
        projection = {
            "projection_version": SOURCE_PROJECTION_VERSION,
            "schema_version": validated.schema_version,
            "evidence": [item.view().model_dump(mode="json") for item in validated.evidence],
        }
        return sha256_canonical(projection)
    except (TypeError, ValueError) as exc:
        raise EnsembleSourceError("source packet projection is invalid") from exc


def _packet(window: int, evidence: list[Evidence]) -> SourcePacket:
    document = SourceDocument(schema_version="ensemble-source-v1", evidence=evidence)
    return SourcePacket(
        window_index=window,
        document=document,
        content_projection_sha256=source_projection_sha256(document),
    )


def allowed_ranges(items: Iterable[EvidenceView]) -> tuple[AllowedEvidenceRange, ...]:
    return tuple(
        AllowedEvidenceRange(item.evidence_id, item.char_start, item.char_end) for item in items
    )


def materialize_ref(snapshot: SourceSnapshot, ref: EvidenceRef) -> str:
    """Resolve a validated absolute codepoint range to its immutable source substring."""
    item = snapshot.evidence_by_id().get(ref.evidence_id)
    if item is None or ref.char_start < item.char_start or ref.char_end > item.char_end:
        raise EnsembleSourceError("evidence reference is outside its source atom")
    segment = snapshot.segment_texts[item.source_binding.segment_index]
    if segment[item.char_start : item.char_end] != item.text:
        raise EnsembleSourceError("source binding no longer matches its immutable snapshot")
    local_start = ref.char_start - item.char_start
    local_end = ref.char_end - item.char_start
    return item.text[local_start:local_end]
