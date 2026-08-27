from pathlib import Path

import pytest
from narumi.context_sources import (
    FORMAT_PLAIN,
    FORMAT_SRT,
    FORMAT_VTT,
    FORMAT_ZOOM_TXT,
    INDEX_SPACING_SEC,
    PARSER_VERSION,
    TRANSCRIPT_SOURCE_TYPES,
    detect_format,
    parse_context,
)
from narumi.diarize.layer4 import LAYER_EXTERNAL, build_layer4
from narumi.errors import InvalidArgumentError
from narumi.models import EngineInfo, Segment, Transcript

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ detect_format
def test_detect_format_fixtures():
    assert detect_format(fixture("zoom_meeting.vtt")) == FORMAT_VTT
    assert detect_format(fixture("teams_meeting.vtt")) == FORMAT_VTT
    assert detect_format(fixture("meeting.srt")) == FORMAT_SRT
    assert detect_format(fixture("zoom_transcript.txt")) == FORMAT_ZOOM_TXT
    assert detect_format(fixture("notion_ai_minutes.txt")) == FORMAT_PLAIN


def test_detect_format_edge_cases():
    assert detect_format("") is None
    assert detect_format("   \n\n  ") is None
    assert detect_format("﻿WEBVTT\n") == FORMAT_VTT
    # a headerless cue with dot decimals reads as vtt, comma decimals as srt
    assert detect_format("00:00:01.000 --> 00:00:02.000\nこんにちは\n") == FORMAT_VTT
    assert detect_format("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n") == FORMAT_SRT
    assert detect_format("ただのメモ書きです。\n続きの行。") == FORMAT_PLAIN


# ------------------------------------------------------------------ vtt
def test_parse_zoom_style_vtt():
    transcript = parse_context(
        "zoom_transcript", fixture("zoom_meeting.vtt"), context_id="ctx-aaa111"
    )
    assert isinstance(transcript, Transcript)
    assert transcript.source_id == "ext-ctx-aaa111"
    assert transcript.kind == "external" and transcript.track is None
    assert transcript.engine.name == "parser-vtt"
    assert transcript.engine.version == PARSER_VERSION
    assert transcript.time_offset == 0.0
    assert [s.id for s in transcript.segments] == [f"ext-ctx-aaa111:{i}" for i in range(4)]
    first = transcript.segments[0]
    assert (first.start, first.end) == (3.19, 6.85)
    assert first.speaker == "岡村 慎太郎"
    assert first.text == "では定例を始めます。よろしくお願いします。"
    assert [s.speaker for s in transcript.segments] == [
        "岡村 慎太郎",
        "田中 太郎",
        "田中 太郎",
        "鈴木 花子",
    ]


def test_parse_teams_style_vtt_voice_tags():
    transcript = parse_context(
        "teams_transcript", fixture("teams_meeting.vtt"), context_id="ctx-bbb222"
    )
    assert transcript is not None
    assert [(s.speaker, s.text) for s in transcript.segments] == [
        ("岡村 慎太郎", "では定例を始めます。よろしくお願いします。"),
        ("田中 太郎", "先週のリリース状況について共有します。"),
        ("田中 太郎", "リリースは予定通り金曜日に完了しました。"),
    ]  # the NOTE block is skipped
    assert transcript.segments[1].start == 7.2


def test_vtt_clock_prefix_is_not_a_speaker():
    text = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n13:00 に締め切りです。\n"
    transcript = parse_context("meet_transcript", text, context_id="ctx-ccc333")
    assert transcript is not None
    assert transcript.segments[0].speaker is None
    assert transcript.segments[0].text == "13:00 に締め切りです。"


def test_vtt_malformed_cues_are_skipped():
    text = "WEBVTT\n\nこの塊にはタイムスタンプがない\n\nnot --> a time\nゴミ\n"
    assert parse_context("zoom_transcript", text, context_id="ctx-ddd444") is None


# ------------------------------------------------------------------ srt
def test_parse_srt():
    transcript = parse_context("meet_transcript", fixture("meeting.srt"), context_id="ctx-eee555")
    assert transcript is not None
    assert transcript.engine.name == "parser-srt"
    assert (transcript.segments[0].start, transcript.segments[0].end) == (3.19, 6.85)
    assert transcript.segments[0].speaker == "岡村 慎太郎"
    assert transcript.segments[2].speaker is None  # no prefix on the third cue
    assert transcript.segments[2].text == "リリースは予定通り金曜日に完了しました。"


# ------------------------------------------------------------------ zoom txt
def test_parse_zoom_txt():
    transcript = parse_context(
        "zoom_transcript", fixture("zoom_transcript.txt"), context_id="ctx-fff666"
    )
    assert transcript is not None
    assert transcript.engine.name == "parser-zoom_txt"
    starts = [s.start for s in transcript.segments]
    ends = [s.end for s in transcript.segments]
    assert starts == [3.0, 7.0, 12.0, 16.0]
    assert ends == [7.0, 12.0, 16.0, 21.0]  # each ends at the next start; the tail gets +5 s
    assert transcript.segments[3].speaker == "鈴木 花子"


# ------------------------------------------------------------------ plain
def test_parse_plain_with_timestamps():
    text = "[00:00:10] 最初の議題です。\n[00:20] 次の議題です。\n[00:00:45] 最後です。\n"
    transcript = parse_context("notion_ai_minutes", text, context_id="ctx-abc123")
    assert transcript is not None
    assert transcript.engine.name == "parser-plain"
    assert transcript.engine.params["timestamps"] == "explicit"
    assert [(s.start, s.end) for s in transcript.segments] == [
        (10.0, 20.0),
        (20.0, 45.0),
        (45.0, 45.0 + INDEX_SPACING_SEC),
    ]
    assert all(s.speaker is None for s in transcript.segments)


def test_parse_plain_without_timestamps_is_low_confidence():
    transcript = parse_context(
        "notion_ai_minutes", fixture("notion_ai_minutes.txt"), context_id="ctx-abc456"
    )
    assert transcript is not None
    assert transcript.engine.params == {
        "format": "plain",
        "timestamps": "none",
        "confidence": "low",
    }
    assert transcript.time_offset == 0.0
    assert [(s.start, s.end) for s in transcript.segments] == [
        (i * INDEX_SPACING_SEC, (i + 1) * INDEX_SPACING_SEC) for i in range(4)
    ]
    # plain treatment never extracts speakers, even from "名前:" looking prose
    assert all(s.speaker is None for s in transcript.segments)
    assert transcript.segments[1].text == "岡村: では定例を始めます。"


# ------------------------------------------------------------------ parse_context routing
def test_parse_context_only_transcript_source_types():
    vtt = fixture("zoom_meeting.vtt")
    for source_type in ("document", "chat_log", "text", "url", "file"):
        assert parse_context(source_type, vtt, context_id="ctx-xyz") is None
    for source_type in TRANSCRIPT_SOURCE_TYPES:
        assert parse_context(source_type, vtt, context_id="ctx-xyz") is not None


def test_parse_context_unparseable_returns_none():
    assert parse_context("zoom_transcript", "", context_id="ctx-1") is None
    assert parse_context("zoom_transcript", "   \n ", context_id="ctx-1") is None


def test_parse_context_requires_context_id():
    with pytest.raises(InvalidArgumentError):
        parse_context("zoom_transcript", "text", context_id="")


# ------------------------------------------------------------------ layer 4
def ext_transcript(source_id: str, spans, *, time_offset: float = 0.0) -> Transcript:
    return Transcript(
        source_id=source_id,
        kind="external",
        engine=EngineInfo(name="parser-vtt", version="1"),
        time_offset=time_offset,
        segments=[
            Segment(id=f"{source_id}:{i}", start=s, end=e, text=t, speaker=sp)
            for i, (s, e, sp, t) in enumerate(spans)
        ],
    )


def test_build_layer4_turns_from_named_segments():
    ext = ext_transcript(
        "ext-a",
        [
            (0.0, 4.0, "岡村", "こんにちは"),
            (5.0, 8.0, None, "話者不明の発話"),
            (9.0, 12.0, "田中", "よろしくお願いします"),
        ],
        time_offset=1.0,
    )
    diarization = build_layer4([ext])
    assert diarization.layer == LAYER_EXTERNAL
    assert [(t.start, t.end, t.speaker, t.layer, t.source_id) for t in diarization.turns] == [
        (1.0, 5.0, "岡村", 4, "ext-a"),
        (10.0, 13.0, "田中", 4, "ext-a"),
    ]  # the unnamed segment produces no turn; time_offset is applied


def test_build_layer4_sorts_across_sources():
    a = ext_transcript("ext-b", [(6.0, 8.0, "鈴木", "b")])
    b = ext_transcript("ext-a", [(1.0, 3.0, "田中", "a")])
    turns = build_layer4([a, b]).turns
    assert [(t.start, t.speaker) for t in turns] == [(1.0, "田中"), (6.0, "鈴木")]


def test_build_layer4_from_parsed_fixture():
    transcript = parse_context("zoom_transcript", fixture("zoom_meeting.vtt"), context_id="ctx-l4")
    assert transcript is not None
    turns = build_layer4([transcript]).turns
    assert len(turns) == 4
    assert {t.speaker for t in turns} == {"岡村 慎太郎", "田中 太郎", "鈴木 花子"}
    assert all(t.source_id == "ext-ctx-l4" for t in turns)


def test_build_layer4_rejects_own_transcripts():
    own = Transcript(
        source_id="own-mic",
        kind="own",
        track="mic",
        engine=EngineInfo(name="fake", version="1"),
        segments=[],
    )
    with pytest.raises(InvalidArgumentError):
        build_layer4([own])
