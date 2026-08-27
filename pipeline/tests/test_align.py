from pathlib import Path

import pytest
from narumi.align import (
    ALIGNMENT_KEY,
    build_alignment,
    build_intervals,
    char_ngrams,
    estimate_offset,
    find_anchors,
    normalize_text,
    run_align,
)
from narumi.bundle import Bundle
from narumi.errors import InvalidArgumentError, NotFoundError
from narumi.models import Alignment, EngineInfo, Segment, Transcript

SENTENCES = [
    "本日の定例会議を始めます。よろしくお願いします。",
    "まず先週のリリース状況について共有してください。",
    "リリースは予定通り金曜日に完了しました。",
    "障害報告は二件ありましたが、いずれも復旧済みです。",
    "次に来月のロードマップについて相談したいです。",
    "認証基盤の刷新を優先したいと考えています。",
    "予算の承認は経営会議で来週諮る予定です。",
    "以上で本日の議題は終わりです。ありがとうございました。",
]


def make_transcript(
    source_id: str,
    track: str | None,
    spans: list[tuple[float, float, str]],
    *,
    time_offset: float = 0.0,
) -> Transcript:
    return Transcript(
        source_id=source_id,
        kind="own" if source_id.startswith("own-") else "external",
        track=track,  # type: ignore[arg-type]
        engine=EngineInfo(name="fake", version="1"),
        time_offset=time_offset,
        segments=[
            Segment(id=f"{source_id}:{i}", start=s, end=e, text=t)
            for i, (s, e, t) in enumerate(spans)
        ],
    )


def sentence_spans(shift: float = 0.0, texts: list[str] | None = None):
    texts = texts or SENTENCES
    return [(i * 6.0 + shift, i * 6.0 + 4.0 + shift, t) for i, t in enumerate(texts)]


def record_transcript(bundle: Bundle, transcript: Transcript) -> None:
    rel = f"transcripts/{transcript.source_id}.json"
    bundle.run_stage(
        f"transcripts/{transcript.source_id}",
        inputs={"preprocess/audio": "0" * 64},
        params={"engine": "fake"},
        producer=("fake", "1"),
        output=rel,
        fn=lambda _: bundle.write_json(rel, transcript),
        force=True,
    )


# ------------------------------------------------------------------ normalize
def test_normalize_text_nfkc_case_and_punctuation():
    assert normalize_text("Ｈｅｌｌｏ、 World！　ＡＢＣ") == "helloworldabc"
    assert normalize_text("  ") == ""


def test_char_ngrams():
    assert char_ngrams("abcd", 2) == ["ab", "bc", "cd"]
    assert char_ngrams("ab", 3) == []
    with pytest.raises(ValueError):
        char_ngrams("abc", 0)


# ------------------------------------------------------------------ anchors
def test_find_anchors_estimates_shift():
    a = make_transcript("own-system", "system", sentence_spans())
    changed = list(SENTENCES)
    changed[2] = "リリースは予定どおり金曜に完了しました。"
    changed[5] = "認証基盤の刷新を最優先したいと考えています。"
    b = make_transcript("own-mic", "mic", sentence_spans(shift=3.2, texts=changed))
    anchors = find_anchors(a, b)
    assert anchors
    assert {x.source_a for x in anchors} == {"own-system"}
    assert {x.source_b for x in anchors} == {"own-mic"}
    assert all(x.segment_a.split(":")[1] == x.segment_b.split(":")[1] for x in anchors)
    offset = estimate_offset(anchors)
    assert offset is not None and abs(offset - 3.2) < 0.1


def test_find_anchors_respects_time_offset():
    a = make_transcript("own-system", "system", sentence_spans())
    b = make_transcript("own-mic", "mic", sentence_spans(shift=3.2), time_offset=-3.2)
    assert estimate_offset(find_anchors(a, b)) == pytest.approx(0.0, abs=0.01)


def test_estimate_offset_needs_three_anchors():
    a = make_transcript("own-system", "system", sentence_spans()[:2])
    b = make_transcript("own-mic", "mic", sentence_spans(shift=1.0)[:2])
    assert estimate_offset(find_anchors(a, b)) is None


def test_find_anchors_ignores_repeated_ngrams():
    repeated = ["同じ文章を繰り返します。"] * 3 + ["これは一度しか出ません。"]
    a = make_transcript("own-system", "system", sentence_spans(texts=repeated))
    b = make_transcript("own-mic", "mic", sentence_spans(shift=1.0, texts=repeated))
    anchors = find_anchors(a, b)
    assert {x.segment_a for x in anchors} == {"own-system:3"}


def test_find_anchors_caps_and_samples():
    a = make_transcript("own-system", "system", sentence_spans())
    b = make_transcript("own-mic", "mic", sentence_spans(shift=2.0))
    anchors = find_anchors(a, b, max_anchors=3)
    assert len(anchors) == 3
    assert anchors[0].segment_a == "own-system:0"


# ------------------------------------------------------------------ intervals
def test_build_intervals_merges_overlapping_sources():
    mic = make_transcript("own-mic", "mic", [(0.0, 5.0, "a"), (6.0, 10.0, "b")])
    system = make_transcript("own-system", "system", [(0.2, 4.8, "c"), (6.1, 9.9, "d")])
    intervals = build_intervals([mic, system], {"own-mic": 0.0, "own-system": 0.0})
    assert [i.id for i in intervals] == ["iv-00001", "iv-00002"]
    assert intervals[0].columns == {"own-mic": ["own-mic:0"], "own-system": ["own-system:0"]}
    assert intervals[1].columns == {"own-mic": ["own-mic:1"], "own-system": ["own-system:1"]}
    assert intervals[0].start == 0.0 and intervals[0].end == 5.0


def test_build_intervals_applies_offsets():
    mic = make_transcript("own-mic", "mic", [(3.2, 8.2, "a")])
    system = make_transcript("own-system", "system", [(0.0, 5.0, "b")])
    merged = build_intervals([mic, system], {"own-mic": -3.2, "own-system": 0.0})
    assert len(merged) == 1 and set(merged[0].columns) == {"own-mic", "own-system"}
    separate = build_intervals([mic, system], {"own-mic": 0.0, "own-system": 0.0})
    assert len(separate) == 1  # 3.2 <= 5.0 + gap → still overlaps without correction
    far = build_intervals([mic, system], {"own-mic": 3.0, "own-system": 0.0})
    assert len(far) == 2


def test_build_intervals_single_source_one_per_segment():
    mic = make_transcript("own-mic", "mic", [(0.0, 5.0, "a"), (5.0, 10.0, "b"), (10.0, 15.0, "c")])
    intervals = build_intervals([mic], {"own-mic": 0.0})
    assert len(intervals) == 3
    assert [i.columns for i in intervals] == [
        {"own-mic": ["own-mic:0"]},
        {"own-mic": ["own-mic:1"]},
        {"own-mic": ["own-mic:2"]},
    ]


def test_build_intervals_bridged_by_other_source():
    mic = make_transcript("own-mic", "mic", [(0.0, 5.0, "a"), (5.0, 10.0, "b")])
    system = make_transcript("own-system", "system", [(0.0, 10.0, "c")])
    intervals = build_intervals([mic, system], {"own-mic": 0.0, "own-system": 0.0})
    assert len(intervals) == 1
    assert intervals[0].columns["own-mic"] == ["own-mic:0", "own-mic:1"]


def test_build_intervals_clamps_negative_start():
    mic = make_transcript("own-mic", "mic", [(0.0, 2.0, "a")])
    intervals = build_intervals([mic], {"own-mic": -1.0})
    assert intervals[0].start == 0.0 and intervals[0].end == 1.0


# ------------------------------------------------------------------ alignment
def test_build_alignment_prefers_system_reference_and_corrects_offset():
    system = make_transcript("own-system", "system", sentence_spans())
    mic = make_transcript("own-mic", "mic", sentence_spans(shift=3.2))
    alignment = build_alignment([mic, system])
    assert alignment.params["reference"] == "own-system"
    assert alignment.offsets["own-system"] == 0.0
    assert alignment.offsets["own-mic"] == pytest.approx(-3.2, abs=0.1)
    assert alignment.params["unaligned"] == []
    assert len(alignment.intervals) == len(SENTENCES)
    assert all(set(i.columns) == {"own-mic", "own-system"} for i in alignment.intervals)


def test_build_alignment_unaligned_source_gets_zero():
    system = make_transcript("own-system", "system", sentence_spans())
    other = ["全く別の話をしています。"] * 2
    ext = make_transcript("ext-ctx1", None, sentence_spans(shift=1.0, texts=other)[:2])
    alignment = build_alignment([system, ext])
    assert alignment.offsets["ext-ctx1"] == 0.0
    assert alignment.params["unaligned"] == ["ext-ctx1"]


def test_build_alignment_single_source():
    mic = make_transcript("own-mic", "mic", sentence_spans())
    alignment = build_alignment([mic])
    assert alignment.params["reference"] == "own-mic"
    assert len(alignment.intervals) == len(SENTENCES)
    assert alignment.anchors == []


def test_build_alignment_rejects_bad_input():
    mic = make_transcript("own-mic", "mic", sentence_spans())
    with pytest.raises(InvalidArgumentError):
        build_alignment([])
    with pytest.raises(InvalidArgumentError):
        build_alignment([mic], reference="own-system")
    with pytest.raises(InvalidArgumentError):
        build_alignment([mic, mic])


# ------------------------------------------------------------------ bundle stage
def test_run_align_idempotent(tmp_path: Path):
    bundle = Bundle.create(tmp_path, meeting_name="定例")
    with pytest.raises(NotFoundError):
        run_align(bundle)
    record_transcript(bundle, make_transcript("own-system", "system", sentence_spans()))
    record_transcript(bundle, make_transcript("own-mic", "mic", sentence_spans(shift=3.2)))

    first = run_align(bundle)
    assert not first.skipped and first.key == ALIGNMENT_KEY
    assert first.record.path == "merged/alignment.json"
    assert set(first.record.inputs) == {"transcripts/own-mic", "transcripts/own-system"}
    assert first.record.params == {"n": 8, "gap": 0.5, "reference": "own-system"}
    alignment = Alignment.model_validate_json(first.path.read_text(encoding="utf-8"))
    assert alignment.offsets["own-mic"] == pytest.approx(-3.2, abs=0.1)

    second = run_align(bundle)
    assert second.skipped and second.record.sha256 == first.record.sha256

    forced = run_align(bundle, force=True)
    assert not forced.skipped and forced.record.sha256 == first.record.sha256

    reopened = Bundle.open(bundle.path)
    assert reopened.artifact(ALIGNMENT_KEY) is not None

    # a changed transcript hash invalidates the stage
    record_transcript(bundle, make_transcript("own-mic", "mic", sentence_spans(shift=2.0)))
    assert not run_align(bundle).skipped


# ------------------------------------------------------------------ external sources (Step 3)
def vtt_clock(seconds: float) -> str:
    total = int(seconds)
    millis = round((seconds - total) * 1000)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}.{millis:03d}"


def render_vtt(spans: list[tuple[float, float, str]], speaker: str | None = None) -> str:
    blocks = ["WEBVTT"]
    for start, end, text in spans:
        prefix = f"{speaker}: " if speaker else ""
        blocks.append(f"{vtt_clock(start)} --> {vtt_clock(end)}\n{prefix}{text}")
    return "\n\n".join(blocks) + "\n"


def test_ext_vtt_shifted_by_seven_seconds_recovers_offset(tmp_path: Path):
    from narumi.context_sources import parse_context

    bundle = Bundle.create(tmp_path, meeting_name="定例")
    record_transcript(bundle, make_transcript("own-system", "system", sentence_spans()))
    record_transcript(bundle, make_transcript("own-mic", "mic", sentence_spans()))

    vtt = render_vtt(sentence_spans(shift=7.0), speaker="田中 太郎")
    ext = parse_context("zoom_transcript", vtt, context_id="ctx-shifted")
    assert ext is not None and ext.source_id == "ext-ctx-shifted"
    record_transcript(bundle, ext)

    result = run_align(bundle)
    assert set(result.record.inputs) == {
        "transcripts/ext-ctx-shifted",
        "transcripts/own-mic",
        "transcripts/own-system",
    }
    alignment = Alignment.model_validate_json(result.path.read_text(encoding="utf-8"))
    assert alignment.params["reference"] == "own-system"
    assert alignment.params["unaligned"] == []
    assert alignment.offsets["ext-ctx-shifted"] == pytest.approx(-7.0, abs=0.1)
    # every interval carries the ext column next to the own columns after the correction
    assert len(alignment.intervals) == len(SENTENCES)
    for i, interval in enumerate(alignment.intervals):
        assert set(interval.columns) == {"ext-ctx-shifted", "own-mic", "own-system"}
        assert interval.columns["ext-ctx-shifted"] == [f"ext-ctx-shifted:{i}"]
    # the parsed speaker names survive the round trip through the bundle
    from narumi.align import load_transcripts

    stored = load_transcripts(bundle)["transcripts/ext-ctx-shifted"]
    assert {s.speaker for s in stored.segments} == {"田中 太郎"}


def test_build_alignment_ext_only_interval_is_kept(tmp_path: Path):
    from narumi.context_sources import parse_context

    spans = sentence_spans(shift=7.0)
    extra = (len(SENTENCES) * 6.0 + 7.0, len(SENTENCES) * 6.0 + 11.0, "こちらだけの追加発言です。")
    vtt = render_vtt([*spans, extra], speaker="鈴木 花子")
    ext = parse_context("zoom_transcript", vtt, context_id="ctx-extra")
    assert ext is not None
    system = make_transcript("own-system", "system", sentence_spans())
    alignment = build_alignment([system, ext])
    assert alignment.offsets["ext-ctx-extra"] == pytest.approx(-7.0, abs=0.1)
    last = alignment.intervals[-1]
    assert set(last.columns) == {"ext-ctx-extra"}  # speech only the external tool heard
    assert last.columns["ext-ctx-extra"] == [f"ext-ctx-extra:{len(SENTENCES)}"]
