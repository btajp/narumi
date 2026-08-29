"""Deterministic, lossless ensemble evidence source planning."""

from __future__ import annotations

import math
import re

import pytest
from narumi.generate.ensemble.canonical import canonical_bytes, canonical_float, canonical_json
from narumi.generate.ensemble.source import (
    ChunkingPolicy,
    EnsembleSourceError,
    build_source_packets,
    evidence_view,
    snapshot_source,
)
from narumi.generate.ensemble.types import SourceBinding
from narumi.models import MergedSegment, MergedTranscript
from pydantic import ValidationError


def segment(
    segment_id: str,
    start: float,
    text: str,
    *,
    name: str | None = "岡村",
    sources: list[str] | None = None,
) -> MergedSegment:
    return MergedSegment(
        id=segment_id,
        start=start,
        end=start + 1,
        text=text,
        speaker_label="me",
        speaker_name=name,
        sources=sources or ["mic"],
    )


def merged(*segments: MergedSegment) -> MergedTranscript:
    return MergedTranscript(segments=list(segments))


def views_by_window(value: MergedTranscript) -> dict[int, tuple[object, ...]]:
    snapshot = snapshot_source(value, "meeting-stable")
    return {packet.window_index: evidence_view(packet) for packet in build_source_packets(snapshot)}


def test_unicode_codepoint_ranges_preserve_nfc_nfd_emoji_and_all_513_characters():
    nfd = "e\N{COMBINING ACUTE ACCENT}"
    text = (nfd + "🙂") * 171  # 513 Python Unicode codepoints.
    snapshot = snapshot_source(merged(segment("m-1", 0, text)), "meeting-unicode")

    assert len(snapshot.evidence) == 2
    assert [(item.char_start, item.char_end) for item in snapshot.evidence] == [
        (0, 512),
        (512, 513),
    ]
    assert "".join(item.text for item in snapshot.evidence) == text
    assert snapshot.evidence[0].text[-1] == nfd[-1]

    nfc = "é" + text[1:]
    other = snapshot_source(merged(segment("m-1", 0, nfc)), "meeting-unicode")
    assert other.evidence[0].evidence_id != snapshot.evidence[0].evidence_id
    assert other.evidence[0].text == nfc[:512]


def test_identical_occurrences_are_distinct_and_reordering_does_not_change_model_view():
    first = segment("first", 12, "同じ発言", sources=["mic-a"])
    second = segment("second", 12, "同じ発言", sources=["mic-b"])
    before = snapshot_source(merged(first, second), "meeting-duplicate")
    after = snapshot_source(merged(second, first), "meeting-duplicate")

    assert len({item.evidence_id for item in before.evidence}) == 2
    assert [(item.occurrence_index, item.occurrence_count) for item in before.evidence] == [
        (0, 2),
        (1, 2),
    ]
    assert [item.view() for item in before.evidence] == [item.view() for item in after.evidence]
    before_ids = {item.source_binding.segment_id for item in before.evidence}
    after_ids = {item.source_binding.segment_id for item in after.evidence}
    assert before_ids == after_ids
    assert all(re.fullmatch(r"segment-[0-9a-f]{64}", value) for value in before_ids)


def test_duplicate_occurrences_bind_in_canonical_json_order_not_length_prefix_order():
    snapshot = snapshot_source(
        merged(
            segment("z", 12, "同じ発言", sources=["mic-z"]),
            segment("aa", 12, "同じ発言", sources=["mic-aa"]),
        ),
        "meeting-binding-order",
    )

    assert [item.occurrence_index for item in snapshot.evidence] == [0, 1]
    bindings = [canonical_json(item.source_binding) for item in snapshot.evidence]
    assert bindings == sorted(bindings, key=lambda value: value.encode("utf-8"))


def test_private_source_identifiers_are_replaced_by_stable_opaque_public_labels():
    segment_path = "/Users/example/Library/Application Support/narumi/merged.json"
    private_source = "../tracks/system-secret.wav"
    credential_like_source = "api_key_example_value"
    value = merged(
        segment(
            segment_path,
            0,
            "公開可能な発言",
            sources=[private_source, "own-system:42", credential_like_source],
        )
    )

    first = snapshot_source(value, "meeting-private-source")
    repeated = snapshot_source(value, "meeting-private-source")
    other_meeting = snapshot_source(value, "meeting-private-source-other")
    binding = first.evidence[0].source_binding
    serialized = canonical_json(first.evidence[0])

    assert first == repeated
    assert binding.segment_id.startswith("segment-")
    assert binding.sources[1] == "own-system"
    assert binding.sources[0].startswith("source-")
    assert binding.sources[2].startswith("source-")
    assert binding.segment_id != other_meeting.evidence[0].source_binding.segment_id
    for private in (segment_path, private_source, credential_like_source):
        assert private not in serialized


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("segment_id", "/tmp/private/merged.json"),
        ("segment_id", "../private/merged.json"),
        ("segment_id", "api_key_example_value"),
        ("sources", "/tmp/private/system.wav"),
        ("sources", "system-secret"),
    ],
)
def test_source_binding_rejects_non_public_paths_and_sensitive_labels(field: str, value: str):
    payload = {
        "segment_index": 0,
        "segment_id": "segment-" + "a" * 64,
        "segment_text_sha256": "1" * 64,
        "sources": ["own-system"],
    }
    payload[field] = [value] if field == "sources" else value

    with pytest.raises(ValidationError, match="public|sensitive"):
        SourceBinding.model_validate(payload)


def test_front_insertion_and_renumbering_do_not_invalidate_an_unmodified_time_window():
    base = merged(
        segment("m-00001", 10, "最初の窓"),
        segment("m-00002", 610, "別の窓は不変"),
    )
    changed = merged(
        segment("m-00001", 2, "前方に追加"),
        segment("m-00002", 10, "最初の窓"),
        segment("m-00003", 610, "別の窓は不変"),
    )

    assert views_by_window(base)[1] == views_by_window(changed)[1]
    assert views_by_window(base)[0] != views_by_window(changed)[0]


def test_packetization_is_window_local_exclusive_and_lossless():
    source = merged(*(segment(f"m-{index}", index * 0.1, "発言" * 100) for index in range(6)))
    snapshot = snapshot_source(source, "meeting-packets")
    packets = build_source_packets(
        snapshot,
        ChunkingPolicy(packet_chars=900, packet_atoms=2),
    )
    observed = [item.evidence_id for packet in packets for item in packet.document.evidence]

    assert len(observed) == len(set(observed)) == len(snapshot.evidence)
    assert set(observed) == {item.evidence_id for item in snapshot.evidence}
    assert all(len(packet.document.evidence) <= 2 for packet in packets)


@pytest.mark.parametrize(
    "start,end",
    [
        (math.nan, 1.0),
        (math.inf, math.inf),
        (0.0, math.inf),
        (2.0, 1.0),
    ],
)
def test_nonfinite_or_reversed_times_stop_before_packet_planning(start: float, end: float):
    # Source validation must defend persisted/legacy input even when it bypassed model parsing.
    bad = MergedSegment.model_construct(
        id="m-1",
        start=start,
        end=end,
        text="発言",
        speaker_label=None,
        speaker_name=None,
        sources=[],
    )
    with pytest.raises(EnsembleSourceError, match="invalid time range"):
        snapshot_source(merged(bad), "meeting-invalid")


def test_empty_source_and_oversized_public_metadata_are_not_silently_truncated():
    with pytest.raises(EnsembleSourceError, match="no referenceable text"):
        snapshot_source(merged(segment("m-1", 0, "")), "meeting-empty")
    with pytest.raises(EnsembleSourceError, match="speaker name is too long"):
        snapshot_source(merged(segment("m-1", 0, "発言", name="名" * 513)), "meeting-long")
    with pytest.raises(EnsembleSourceError, match="source label is invalid Unicode"):
        snapshot_source(
            merged(segment("m-1", 0, "発言", sources=["bad\ud800"])),
            "meeting-invalid-source",
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"atom_chars": 513},
        {"packet_atoms": 33},
        {"max_packets": 65},
    ],
)
def test_chunking_overrides_cannot_exceed_public_contract_bounds(changes):
    with pytest.raises(ValueError, match="public"):
        ChunkingPolicy(**changes)


def test_canonical_encoding_distinguishes_types_and_normalizes_only_negative_zero():
    assert canonical_float(-0.0) == canonical_float(0.0)
    assert canonical_bytes(-0.0) == canonical_bytes(0.0)
    assert canonical_bytes(True) != canonical_bytes(1)
    assert canonical_bytes("é") != canonical_bytes("e\N{COMBINING ACUTE ACCENT}")


def test_source_packet_mutation_cannot_reuse_its_old_content_projection():
    snapshot = snapshot_source(merged(segment("m-1", 0, "変更前")), "meeting-mutation")
    packet = build_source_packets(snapshot)[0]
    with pytest.raises(ValidationError, match="frozen"):
        packet.document.evidence[0].text = "改変済"
    assert isinstance(packet.document.evidence, tuple)
    assert isinstance(packet.document.evidence[0].source_binding.sources, tuple)
    object.__setattr__(packet.document.evidence[0], "text", "改変済")
    with pytest.raises(EnsembleSourceError, match="changed after projection"):
        evidence_view(packet)
