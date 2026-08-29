import os
import re
from pathlib import Path

import pytest
from narumi.align import build_alignment, build_intervals, run_align
from narumi.bundle import Bundle
from narumi.diarize.layer3 import NameSuggestion
from narumi.errors import NotFoundError, PolicyViolationError
from narumi.generate import (
    INTEGRATE_KEY,
    INTEGRATE_PROMPT_VERSION,
    MINUTES_PROMPT_VERSION,
    PLAIN_PLACEHOLDER,
    SPEAKER_MAP_PATH,
    IntegrateCache,
    generate_minutes,
    integrate,
    run_generate,
    run_integrate,
)
from narumi.generate.asr_speakers import build_asr_turns
from narumi.generate.minutes import chunk_lines, format_jst, split_sections
from narumi.llm import FakeProvider, NoneProvider
from narumi.models import (
    Diarization,
    EngineInfo,
    MeetingConfig,
    MergedTranscript,
    Segment,
    SpeakerMap,
    Transcript,
    Turn,
)

SNAPSHOTS = Path(__file__).parent / "snapshots"
UPDATE = os.environ.get("NARUMI_UPDATE_SNAPSHOTS") == "1"
VOLATILE_ROW = re.compile(r"^\| 生成日時 \| .* \|$", re.MULTILINE)

MIC = [
    (0.0, 4.0, "お疲れさまです。定例を始めます。"),
    (12.0, 16.0, "リリースは金曜日に完了しました。"),
    (24.0, 28.0, "では来月の予定を決めましょう。"),
]
SYSTEM = [
    (6.0, 10.0, "先週のリリース状況を教えてください。"),
    (12.2, 15.8, "リリースは金曜に完了しました、了解です。"),
    (18.0, 22.0, "障害は二件ありましたが復旧済みです。"),
]


def make_transcript(source_id: str, track: str | None, spans, *, time_offset: float = 0.0):
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


def with_asr_speakers(transcript: Transcript, labels: list[str | None]) -> Transcript:
    return transcript.model_copy(
        update={
            "engine": EngineInfo(
                name="openai-api", version="1", params={"model": "gpt-4o-transcribe-diarize"}
            ),
            "segments": [
                segment.model_copy(update={"speaker": label})
                for segment, label in zip(transcript.segments, labels, strict=True)
            ],
        }
    )


def layer1() -> Diarization:
    turns = [Turn(start=s, end=e, speaker="me", layer=1, source_id="own-mic") for s, e, _ in MIC]
    turns += [
        Turn(start=s, end=e, speaker="other", layer=1, source_id="own-system") for s, e, _ in SYSTEM
    ]
    return Diarization(layer=1, engine=EngineInfo(name="tracks", version="1"), turns=turns)


def layer2() -> Diarization:
    return Diarization(
        layer=2,
        engine=EngineInfo(name="fake-diarizer", version="1"),
        turns=[
            Turn(start=5.0, end=11.0, speaker="SPEAKER_00", layer=2),
            Turn(start=17.0, end=23.0, speaker="SPEAKER_01", layer=2),
        ],
    )


def transcripts() -> dict[str, Transcript]:
    mic = make_transcript("own-mic", "mic", MIC)
    system = make_transcript("own-system", "system", SYSTEM)
    return {"own-mic": mic, "own-system": system}


def record_json(bundle: Bundle, key: str, rel: str, model, params=None) -> None:
    bundle.run_stage(
        key,
        inputs={"upstream": "0" * 64},
        params=params or {},
        producer=("fake", "1"),
        output=rel,
        fn=lambda _: bundle.write_json(rel, model),
    )


def prepared_bundle(tmp_path: Path, config: MeetingConfig) -> Bundle:
    bundle = Bundle.create(tmp_path, meeting_name="定例会議", config=config)
    bundle.manifest.recording.started_at = "2026-08-27T03:05:00Z"
    bundle.save()
    for sid, t in transcripts().items():
        record_json(bundle, f"transcripts/{sid}", f"transcripts/{sid}.json", t)
    record_json(bundle, "diarization/layer1", "diarization/layer1-tracks.json", layer1())
    run_align(bundle)
    return bundle


def snapshot_compare(name: str, text: str) -> None:
    path = SNAPSHOTS / name
    normalized = VOLATILE_ROW.sub("| 生成日時 | <generated_at> |", text)
    if UPDATE:
        path.write_text(normalized, encoding="utf-8")
    assert path.exists(), f"snapshot missing: {path} (set NARUMI_UPDATE_SNAPSHOTS=1)"
    assert normalized == path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ integrate
def test_integrate_passthrough_and_speaker_labels():
    ts = transcripts()
    alignment = build_alignment(list(ts.values()))
    config = MeetingConfig(self_name="岡村")
    merged = integrate(alignment, ts, [layer1()], config, None)
    # 5 intervals; the overlapping 12–16 s one keeps both own tracks → 6 segments
    assert [s.id for s in merged.segments] == [f"m-{i:05d}" for i in range(1, 7)]
    assert [s.start for s in merged.segments] == sorted(s.start for s in merged.segments)
    by_text = {s.text: s for s in merged.segments}
    first = by_text["お疲れさまです。定例を始めます。"]
    assert first.speaker_label == "me" and first.speaker_name == "岡村"
    assert first.sources == ["own-mic:0"]
    second = by_text["先週のリリース状況を教えてください。"]
    assert second.speaker_label == "other" and second.speaker_name is None
    assert merged.provider == "none"
    assert merged.params["integration"] == "deterministic"
    assert merged.params["prompt_version"] is None
    assert merged.speaker_map.speakers["me"].name == "岡村"
    assert merged.speaker_map.speakers["me"].confidence == 1.0
    assert merged.speaker_map.speakers["other"].name is None
    assert merged.speaker_map.speakers["other"].confidence == 0.0
    assert merged.speaker_map.speakers["other"].evidence[0].layer == 1


def test_integrate_without_self_name_has_zero_confidence():
    ts = transcripts()
    merged = integrate(build_alignment(list(ts.values())), ts, [layer1()], MeetingConfig(), None)
    assert merged.speaker_map.speakers["me"].name is None
    assert merged.speaker_map.speakers["me"].confidence == 0.0


def test_integrate_falls_back_to_track_when_no_diarization():
    ts = transcripts()
    merged = integrate(build_alignment(list(ts.values())), ts, [], MeetingConfig(), None)
    labels = {s.text: s.speaker_label for s in merged.segments}
    assert labels["お疲れさまです。定例を始めます。"] == "me"
    assert labels["先週のリリース状況を教えてください。"] == "other"
    assert merged.params["diarization_layers"] == []


def test_integrate_deterministic_keeps_every_own_track():
    """mic and system are different people: an overlap must not drop either side's words."""
    ts = transcripts()
    alignment = build_alignment(list(ts.values()))
    multi = [i for i in alignment.intervals if len(i.columns) == 2]
    assert len(multi) == 1
    merged = integrate(alignment, ts, [layer1()], MeetingConfig(), NoneProvider())
    mine = next(s for s in merged.segments if s.sources == ["own-mic:1"])
    theirs = next(s for s in merged.segments if s.sources == ["own-system:1"])
    assert (mine.text, mine.speaker_label, mine.start, mine.end) == (
        "リリースは金曜日に完了しました。",
        "me",
        12.0,
        16.0,
    )
    assert (theirs.text, theirs.speaker_label, theirs.start, theirs.end) == (
        "リリースは金曜に完了しました、了解です。",
        "other",
        12.2,
        15.8,
    )
    assert merged.segments.index(mine) + 1 == merged.segments.index(theirs)
    assert not any(len(s.sources) > 1 for s in merged.segments)
    assert merged.params["integration"] == "deterministic"
    assert merged.provider == "none"


def test_integrate_deterministic_overlap_with_different_texts():
    """The reviewer's scenario: a short reply overlapping the end of my sentence."""
    mic = make_transcript("own-mic", "mic", [(0.0, 4.0, "では来月の予定を決めましょう")])
    system = make_transcript("own-system", "system", [(3.8, 5.0, "はい、お願いします")])
    ts = {"own-mic": mic, "own-system": system}
    merged = integrate(build_alignment(list(ts.values())), ts, [], MeetingConfig(), None)
    assert [(s.text, s.speaker_label, s.start, s.end) for s in merged.segments] == [
        ("では来月の予定を決めましょう", "me", 0.0, 4.0),
        ("はい、お願いします", "other", 3.8, 5.0),
    ]


def test_integrate_deterministic_external_columns_are_redundant():
    """ext-* columns transcribe the same speech: own tracks win, ext-only picks one column."""
    mic = make_transcript("own-mic", "mic", [(0.0, 4.0, "自分の発話")])
    ext_a = make_transcript(
        "ext-a", None, [(0.2, 4.1, "外部Aの文字起こし"), (10.0, 12.0, "A だけ")]
    )
    ext_b = make_transcript("ext-b", None, [(10.1, 12.2, "B だけ")])
    ts = {"own-mic": mic, "ext-a": ext_a, "ext-b": ext_b}
    alignment = build_alignment(list(ts.values()), reference="own-mic")
    merged = integrate(alignment, ts, [], MeetingConfig(), None)
    assert [(s.text, s.speaker_label, s.sources) for s in merged.segments] == [
        ("自分の発話", "me", ["own-mic:0"]),
        ("A だけ", None, ["ext-a:1"]),  # ext-only overlap → sorted first, no track → no label
    ]


def test_integrate_shifts_layer1_turns_by_alignment_offsets():
    """Layer-1 turns live on the raw clock; labels must compare them on the aligned clock."""
    mic = make_transcript("own-mic", "mic", [(13.2, 15.2, "自分の発話")])
    ext = make_transcript("ext-x", None, [(10.0, 12.0, "外部の文字起こし")])
    ts = {"own-mic": mic, "ext-x": ext}
    turns = Diarization(
        layer=1,
        engine=EngineInfo(name="tracks", version="1"),
        turns=[Turn(start=13.2, end=15.2, speaker="me", layer=1, source_id="own-mic")],
    )
    # as estimated by anchors when the mic clock runs 3.2 s late: correction −3.2 s
    offsets = {"ext-x": 0.0, "own-mic": -3.2}
    aligned = build_alignment(list(ts.values()), reference="ext-x").model_copy(
        update={"offsets": offsets, "intervals": build_intervals(list(ts.values()), offsets)}
    )
    assert len(aligned.intervals) == 1
    assert set(aligned.intervals[0].columns) == {"ext-x", "own-mic"}
    merged = integrate(aligned, ts, [turns], MeetingConfig(), None)
    assert [(s.text, s.speaker_label, s.start, s.end) for s in merged.segments] == [
        ("自分の発話", "me", 10.0, 12.0)  # own column wins over ext, placed on the aligned clock
    ]
    # an ext-only interval is labelled through the shifted mic turn …
    only = {"ext-x": ext}
    base = build_alignment([ext], reference="ext-x").model_copy(update={"offsets": offsets})
    merged = integrate(base, only, [turns], MeetingConfig(), None)
    assert [(s.text, s.speaker_label) for s in merged.segments] == [("外部の文字起こし", "me")]
    # … and stays unlabelled when the offset is not applied (turn at 13.2 s vs interval 10–12 s)
    unshifted = integrate(
        build_alignment([ext], reference="ext-x"), only, [turns], MeetingConfig(), None
    )
    assert unshifted.segments[0].speaker_label is None


def test_integrate_with_fake_provider_calls_llm_for_multi_source_only():
    ts = transcripts()
    alignment = build_alignment(list(ts.values()))
    fake = FakeProvider()
    merged = integrate(
        alignment, ts, [layer1()], MeetingConfig(llm_provider="fake", vocab_hints=["narumi"]), fake
    )
    assert len(fake.calls) == 1
    prompt = fake.calls[0].prompt
    assert "<transcript>" in prompt and "[own-system]" in prompt and "[own-mic]" in prompt
    assert prompt.index("[own-system]") < prompt.index("[own-mic]")
    assert "narumi" in prompt and "00:00:12" in prompt
    seg = next(s for s in merged.segments if len(s.sources) == 2)
    assert seg.text.startswith("（fake）")
    assert merged.provider == "fake"
    assert merged.params["integration"] == "llm"
    assert merged.params["prompt_version"] == INTEGRATE_PROMPT_VERSION == "integrate-v1"


def test_integrate_layer2_refines_other_labels():
    ts = transcripts()
    alignment = build_alignment(list(ts.values()))
    merged = integrate(alignment, ts, [layer1(), layer2()], MeetingConfig(self_name="岡村"), None)
    labels = {s.text: s.speaker_label for s in merged.segments}
    assert labels["先週のリリース状況を教えてください。"] == "SPEAKER_00"
    assert labels["障害は二件ありましたが復旧済みです。"] == "SPEAKER_01"
    assert labels["お疲れさまです。定例を始めます。"] == "me"
    # the mixed 12–16 s interval keeps both tracks; no layer-2 turn covers the system side there,
    # so that segment stays a bare "other"
    assert labels["リリースは金曜日に完了しました。"] == "me"
    assert labels["リリースは金曜に完了しました、了解です。"] == "other"
    speakers = merged.speaker_map.speakers
    assert list(speakers) == ["me", "other", "SPEAKER_00", "SPEAKER_01"]
    assert speakers["SPEAKER_01"].evidence[0].layer == 2
    assert "fake-diarizer" in speakers["SPEAKER_01"].evidence[0].detail
    assert merged.params["diarization_layers"] == [1, 2]


def test_asr_labels_are_anonymous_and_scoped_to_track_and_chunk():
    mic = with_asr_speakers(
        make_transcript("own-mic", "mic", [(0, 4, "本人の発話")]), ["asr:mic:0:A"]
    )
    system = with_asr_speakers(
        make_transcript("own-system", "system", [(0, 4, "相手の発話"), (600, 604, "次の区間")]),
        ["asr:system:0:A", "asr:system:1:A"],
    )
    ts = {t.source_id: t for t in (mic, system)}
    merged = integrate(
        build_alignment(list(ts.values())), ts, [], MeetingConfig(self_name="本人"), None
    )
    labels = {tuple(s.sources): s.speaker_label for s in merged.segments}
    assert labels == {
        ("own-mic:0",): "me",
        ("own-system:0",): "asr:system:0:A",
        ("own-system:1",): "asr:system:1:A",
    }
    speakers = merged.speaker_map.speakers
    assert speakers["me"].name == "本人" and speakers["me"].confidence == 1
    for label in ("asr:system:0:A", "asr:system:1:A"):
        entry = speakers[label]
        assert entry.name is None and entry.confidence == 0
        assert [e.layer for e in entry.evidence] == [2]
        assert "source=own-system" in entry.evidence[0].detail
        assert f"namespace={label.rsplit(':', 1)[0]}" in entry.evidence[0].detail
    assert "label=asr:mic:0:A" in speakers["me"].evidence[-1].detail
    assert "own-system" not in speakers["me"].evidence[-1].detail
    assert merged.params["diarization_layers"] == [] and merged.params["layer4_sources"] == []


def test_asr_preserves_explicit_diarization_and_real_name_priorities():
    ts = transcripts()
    ts["own-mic"] = with_asr_speakers(ts["own-mic"], ["asr:mic:0:A"] * 3)
    ts["own-system"] = with_asr_speakers(ts["own-system"], ["asr:system:0:A"] * 3)
    ts["ext-names"] = ext_named("ext-names", [(6.1, 9.9, "田中", "確認事項")])
    suggestion = NameSuggestion(name="鈴木", confidence=0.7, evidence="screen highlight")
    merged = integrate(
        build_alignment(list(ts.values())),
        ts,
        [layer1(), layer2()],
        MeetingConfig(self_name="本人"),
        None,
        layer3_names={"SPEAKER_00": suggestion, "SPEAKER_01": suggestion},
    )
    speakers = merged.speaker_map.speakers
    assert speakers["me"].name == "本人" and speakers["me"].confidence == 1
    assert speakers["SPEAKER_00"].name == "田中"
    assert speakers["SPEAKER_01"].name == "鈴木"
    assert [e.layer for e in speakers["SPEAKER_00"].evidence] == [2, 2, 4]
    assert [e.layer for e in speakers["SPEAKER_01"].evidence] == [2, 2, 3]
    assert "fake-diarizer" in speakers["SPEAKER_00"].evidence[0].detail
    assert "ASR anonymous" in speakers["SPEAKER_00"].evidence[1].detail
    fallback = speakers["asr:system:0:A"]
    assert fallback.name is None and fallback.confidence == 0
    assert all("fake-diarizer" not in e.detail for e in fallback.evidence)


def test_asr_retains_screen_name_for_other():
    system = with_asr_speakers(
        make_transcript("own-system", "system", [(0, 4, "発話")]), ["asr:system:0:A"]
    )
    alignment = build_alignment([system])
    cache = IntegrateCache()
    integrate(alignment, {system.source_id: system}, [], MeetingConfig(), None, cache=cache)
    merged = integrate(
        alignment,
        {system.source_id: system},
        [],
        MeetingConfig(),
        None,
        cache=cache,
        layer3_names={"other": NameSuggestion(name="田中", confidence=0.7, evidence="screen")},
    )
    assert merged.params["reused"] == 1 and merged.params["recomputed"] == 0
    assert merged.segments[0].speaker_name == "田中"
    entry = merged.speaker_map.speakers["asr:system:0:A"]
    assert entry.confidence == 0.7 and [e.layer for e in entry.evidence] == [2, 3]


@pytest.mark.parametrize("second", ["asr:system:0:A", "asr:system:0:B", "asr:system:1:A"])
def test_asr_overlapping_candidates_do_not_use_majority_vote(second):
    system = with_asr_speakers(
        make_transcript("own-system", "system", [(0, 10, "長い発話"), (8, 9, "短い発話")]),
        ["asr:system:0:A", second],
    )
    ext = make_transcript("ext-bridge", None, [(0, 10, "同じ区間の文字起こし")])
    ts = {t.source_id: t for t in (system, ext)}
    merged = integrate(build_alignment(list(ts.values())), ts, [], MeetingConfig(), None)
    assert len(merged.segments) == 1
    expected = "asr:system:0:A" if second == "asr:system:0:A" else "other"
    assert merged.segments[0].speaker_label == expected
    entry = merged.speaker_map.speakers[expected]
    assert entry.name is None and entry.confidence == 0
    details = [e.detail for e in entry.evidence if e.layer == 2]
    assert len(details) == len({"asr:system:0:A", second})
    assert all(
        any(f"label={label}" in detail for detail in details)
        for label in {"asr:system:0:A", second}
    )


@pytest.mark.parametrize("offset, expected", [(-9.0, (6.0, 8.0)), (-16.0, (0.0, 1.0))])
def test_asr_applies_transcript_and_alignment_offsets_once(offset, expected):
    system = with_asr_speakers(
        make_transcript("own-system", "system", [(10, 12, "発話")], time_offset=5),
        ["asr:system:0:A"],
    )
    ts, offsets = {system.source_id: system}, {system.source_id: offset}
    alignment = build_alignment([system]).model_copy(
        update={"offsets": offsets, "intervals": build_intervals([system], offsets)}
    )
    turns = build_asr_turns(ts, offsets)
    assert [(turn.start, turn.end) for turn in turns] == [expected]
    assert turns[0].source_id == "own-system" and turns[0].layer == 2
    merged = integrate(alignment, ts, [], MeetingConfig(), None)
    segment = merged.segments[0]
    assert (segment.start, segment.end) == expected
    assert segment.speaker_label == "asr:system:0:A"
    assert (system.segments[0].start, system.segments[0].end) == (10, 12)


@pytest.mark.parametrize(
    "engine, model, label",
    [
        ("fake", "gpt-4o-transcribe-diarize", "asr:system:0:A"),
        ("openai-api", "whisper-1", "asr:system:0:A"),
        ("openai-api", None, "asr:system:0:A"),
        ("openai-api", "gpt-4o-transcribe-diarize", None),
    ],
)
def test_only_api_diarize_speakers_supply_asr_evidence(engine, model, label):
    system = with_asr_speakers(make_transcript("own-system", "system", [(0, 4, "発話")]), [label])
    system.engine = EngineInfo(name=engine, version="1", params={"model": model})
    merged = integrate(
        build_alignment([system]), {system.source_id: system}, [], MeetingConfig(), None
    )
    assert merged.segments[0].speaker_label == "other"
    assert [e.layer for e in merged.speaker_map.speakers["other"].evidence] == [1]


@pytest.mark.parametrize("kind, track", [("external", None), ("own", None)])
def test_asr_turns_require_an_own_track(kind, track):
    transcript = with_asr_speakers(
        make_transcript("own-system", "system", [(0, 4, "発話")]), ["asr:system:0:A"]
    ).model_copy(update={"kind": kind, "track": track})
    assert build_asr_turns({transcript.source_id: transcript}, {}) == []


# ------------------------------------------------------------------ minutes
def merged_fixture(provider=None) -> MergedTranscript:
    ts = transcripts()
    return integrate(
        build_alignment(list(ts.values())),
        ts,
        [layer1()],
        MeetingConfig(self_name="岡村"),
        provider,
    )


def manifest_fixture(tmp_path: Path, config: MeetingConfig):
    bundle = Bundle.create(
        tmp_path, meeting_name="定例会議", meeting_id="20260827T030500Z-a1b2c3d4", config=config
    )
    bundle.manifest.recording.started_at = "2026-08-27T03:05:00Z"
    return bundle.manifest


def test_plain_minutes_snapshot(tmp_path: Path):
    config = MeetingConfig(self_name="岡村")
    text, meta = generate_minutes(
        merged_fixture(), manifest_fixture(tmp_path, config), config, None, version=1
    )
    snapshot_compare("minutes_plain.md", text)
    assert text.count(PLAIN_PLACEHOLDER) == 4
    assert "| 日時 | 2026-08-27 12:05 JST |" in text
    assert "- **other**: 未特定" in text
    assert "**other（未特定）**:" in text
    assert meta.version == 1 and meta.provider == "none" and meta.prompt_version is None
    assert meta.unresolved_speakers == ["other"]
    assert meta.params["mode"] == "plain"


def test_llm_minutes_snapshot(tmp_path: Path):
    config = MeetingConfig(llm_provider="fake", self_name="岡村")
    fake = FakeProvider()
    text, meta = generate_minutes(
        merged_fixture(), manifest_fixture(tmp_path, config), config, fake, version=2
    )
    snapshot_compare("minutes_fake.md", text)
    assert len(fake.calls) == 2  # one chunk + final integration
    assert "<transcript>" in fake.calls[0].prompt and "<summaries>" in fake.calls[1].prompt
    assert PLAIN_PLACEHOLDER not in text
    assert meta.prompt_version == MINUTES_PROMPT_VERSION == "minutes-v2"
    assert meta.params["chunks"] == 1 and meta.params["mode"] == "llm"


def test_minutes_missing_sections_are_explicit(tmp_path: Path):
    class Terse:
        name = "terse"
        profile = FakeProvider().profile

        def complete(self, prompt, *, system=None, images=None, max_tokens=None):
            return "## 決定事項\n- 決まった\n"

    config = MeetingConfig(llm_provider="terse")
    text, _ = generate_minutes(
        merged_fixture(), manifest_fixture(tmp_path, config), config, Terse(), version=1
    )
    assert "- 決まった" in text
    assert "「アジェンダ」セクションが含まれていなかった" in text


def test_chunk_lines_and_helpers():
    lines = ["a" * 10, "b" * 10, "c" * 10]
    assert chunk_lines(lines, 25) == [["a" * 10, "b" * 10], ["c" * 10]]
    assert chunk_lines(lines, 5) == [[line] for line in lines]
    assert chunk_lines([], 5) == []
    assert format_jst("2026-08-27T03:05:00Z") == "2026-08-27 12:05 JST"
    assert format_jst(None) == "不明"
    assert split_sections("前置き\n## A\n- 1\n## B\n") == {"A": "- 1", "B": ""}


def test_empty_merged_minutes(tmp_path: Path):
    config = MeetingConfig()
    text, meta = generate_minutes(
        MergedTranscript(speaker_map=SpeakerMap()),
        manifest_fixture(tmp_path, config),
        config,
        None,
        version=1,
    )
    assert "- （話者情報なし）" in text and "- （発話なし）" in text
    assert meta.unresolved_speakers == []


# ------------------------------------------------------------------ bundle stages
def test_run_integrate_and_generate_versions(tmp_path: Path):
    bundle = prepared_bundle(tmp_path, MeetingConfig(self_name="岡村"))
    with pytest.raises(NotFoundError):
        run_generate(bundle)

    integrated = run_integrate(bundle)
    assert not integrated.skipped and integrated.key == INTEGRATE_KEY
    assert set(integrated.record.inputs) == {
        "merged/alignment",
        "transcripts/own-mic",
        "transcripts/own-system",
        "diarization/layer1",
    }
    assert integrated.record.params["provider"] == "none"
    assert integrated.record.params["prompt_version"] == "integrate-v1"
    assert integrated.record.params["vocab_hints"] == []
    assert (bundle.path / SPEAKER_MAP_PATH).exists()
    assert run_integrate(bundle).skipped
    # vocab_hints feed the integration prompt, so they are part of the idempotency key
    bundle.manifest.config = MeetingConfig(self_name="岡村", vocab_hints=["gaia-library"])
    bundle.save()
    rehinted = run_integrate(bundle)
    assert not rehinted.skipped and rehinted.record.params["vocab_hints"] == ["gaia-library"]
    bundle.manifest.config = MeetingConfig(self_name="岡村")
    bundle.save()
    assert not run_integrate(bundle).skipped

    v1 = run_generate(bundle)
    assert not v1.skipped and v1.key == "minutes/v1"
    assert v1.record.path == "minutes/v1/minutes.md"
    assert (bundle.path / "minutes/v1/meta.json").exists()
    assert bundle.manifest.latest_minutes_version == 1
    assert bundle.manifest.minutes_versions[0].provider == "none"
    v1_text = v1.path.read_text(encoding="utf-8")

    again = run_generate(bundle)
    assert again.skipped and again.key == "minutes/v1"
    assert bundle.manifest.latest_minutes_version == 1

    v2 = run_generate(bundle, force=True)
    assert not v2.skipped and v2.key == "minutes/v2"
    assert bundle.manifest.latest_minutes_version == 2
    assert v1.path.read_text(encoding="utf-8") == v1_text

    # changed upstream input (self_name → merged.json) → new version
    bundle.manifest.config = MeetingConfig(self_name="鳴海")
    bundle.save()
    assert not run_integrate(bundle).skipped
    v3 = run_generate(bundle)
    assert not v3.skipped and v3.key == "minutes/v3"
    assert "鳴海" in v3.path.read_text(encoding="utf-8")
    reopened = Bundle.open(bundle.path)
    assert [m.version for m in reopened.manifest.minutes_versions] == [1, 2, 3]
    assert reopened.artifact("minutes/v3").inputs == {
        INTEGRATE_KEY: bundle.artifact_hash(INTEGRATE_KEY)
    }


def test_run_integrate_with_fake_provider(tmp_path: Path):
    bundle = prepared_bundle(tmp_path, MeetingConfig(llm_provider="fake"))
    result = run_integrate(bundle)
    merged = MergedTranscript.model_validate_json(result.path.read_text(encoding="utf-8"))
    assert merged.provider == "fake" and merged.params["integration"] == "llm"
    generated = run_generate(bundle)
    assert bundle.manifest.minutes_versions[0].provider == "fake"
    assert "（fake）" in generated.path.read_text(encoding="utf-8")


def test_run_stages_enforce_policy_before_instantiation(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bundle = prepared_bundle(
        tmp_path, MeetingConfig(llm_provider="anthropic-api", external_send_policy="local_only")
    )
    with pytest.raises(PolicyViolationError):
        run_integrate(bundle)
    assert bundle.artifact(INTEGRATE_KEY) is None
    bundle.manifest.config = MeetingConfig(llm_provider="none")
    bundle.save()
    run_integrate(bundle)
    bundle.manifest.config = MeetingConfig(
        llm_provider="anthropic-api", external_send_policy="local_only"
    )
    bundle.save()
    with pytest.raises(PolicyViolationError):
        run_generate(bundle)
    assert bundle.manifest.minutes_versions == []


# ------------------------------------------------------------------ layer 4 (external names)
def ext_named(source_id: str, spans):
    """External transcript with (start, end, speaker, text) spans."""
    return Transcript(
        source_id=source_id,
        kind="external",
        engine=EngineInfo(name="parser-vtt", version="1"),
        segments=[
            Segment(id=f"{source_id}:{i}", start=s, end=e, text=t, speaker=sp)
            for i, (s, e, sp, t) in enumerate(spans)
        ],
    )


def test_integrate_layer4_resolves_speaker_labels():
    """SPEAKER_xx labels overlapped by exactly one external name get that name (evidence 4)."""
    ts = transcripts()
    ext = ext_named(
        "ext-ctx1",
        [
            (6.1, 9.9, "田中", "先週のリリース状況を教えてください"),
            (18.1, 21.9, "鈴木", "障害は二件ありましたが復旧済みです"),
        ],
    )
    ts["ext-ctx1"] = ext
    alignment = build_alignment(list(ts.values()))
    merged = integrate(alignment, ts, [layer1(), layer2()], MeetingConfig(self_name="岡村"), None)
    speakers = merged.speaker_map.speakers
    assert speakers["SPEAKER_00"].name == "田中"
    assert speakers["SPEAKER_01"].name == "鈴木"
    assert [e.layer for e in speakers["SPEAKER_00"].evidence] == [2, 4]
    assert "ext-ctx1" in speakers["SPEAKER_00"].evidence[1].detail
    assert 0 < speakers["SPEAKER_00"].confidence < 1
    named = {s.text: s.speaker_name for s in merged.segments}
    assert named["先週のリリース状況を教えてください。"] == "田中"
    assert named["障害は二件ありましたが復旧済みです。"] == "鈴木"
    assert named["お疲れさまです。定例を始めます。"] == "岡村"  # me stays self_name
    assert merged.params["layer4_sources"] == ["ext-ctx1"]


def test_integrate_layer4_ambiguous_label_fills_per_segment_only():
    """One 'other' label over two external names: the map stays unresolved, segments resolve."""
    ts = transcripts()
    ts["ext-ctx2"] = ext_named(
        "ext-ctx2",
        [
            (6.1, 9.9, "田中", "先週のリリース状況を教えてください"),
            (18.1, 21.9, "鈴木", "障害は二件ありましたが復旧済みです"),
        ],
    )
    alignment = build_alignment(list(ts.values()))
    merged = integrate(alignment, ts, [layer1()], MeetingConfig(self_name="岡村"), None)
    other = merged.speaker_map.speakers["other"]
    assert other.name is None  # two candidates → no label-level resolution
    assert all(e.layer != 4 for e in other.evidence)
    named = {s.text: s.speaker_name for s in merged.segments}
    assert named["先週のリリース状況を教えてください。"] == "田中"
    assert named["障害は二件ありましたが復旧済みです。"] == "鈴木"
    # the 12-16 s overlap has no layer-4 turn → stays unresolved
    assert named["リリースは金曜に完了しました、了解です。"] is None


def test_integrate_layer4_ext_only_segment_gets_its_name():
    mic = make_transcript("own-mic", "mic", [(0.0, 4.0, "自分の発話")])
    ext = ext_named("ext-c", [(10.0, 13.0, "佐藤", "外部だけが聞き取った発話")])
    ts = {"own-mic": mic, "ext-c": ext}
    alignment = build_alignment(list(ts.values()), reference="own-mic")
    merged = integrate(alignment, ts, [], MeetingConfig(), None)
    tail = merged.segments[-1]
    assert tail.sources == ["ext-c:0"]
    assert tail.speaker_label is None and tail.speaker_name == "佐藤"


def test_integrate_prefers_recorded_layer4_artifact():
    """A diarization/layer4 artifact wins over deriving turns from the ext transcripts."""
    ts = transcripts()
    ts["ext-ctx3"] = ext_named("ext-ctx3", [(6.1, 9.9, "田中", "先週のリリース状況")])
    recorded = Diarization(
        layer=4,
        engine=EngineInfo(name="external-transcripts", version="1"),
        turns=[Turn(start=6.1, end=9.9, speaker="上書希子", layer=4, source_id="ext-ctx3")],
    )
    alignment = build_alignment(list(ts.values()))
    merged = integrate(alignment, ts, [layer1(), recorded], MeetingConfig(), None)
    named = {s.text: s.speaker_name for s in merged.segments}
    assert named["先週のリリース状況を教えてください。"] == "上書希子"
    assert merged.params["diarization_layers"] == [1, 4]


def test_minutes_note_layer4_names(tmp_path: Path):
    ts = transcripts()
    ts["ext-ctx4"] = ext_named(
        "ext-ctx4",
        [
            (6.1, 9.9, "田中", "先週のリリース状況を教えてください"),
            (18.1, 21.9, "田中", "障害は二件ありましたが復旧済みです"),
        ],
    )
    alignment = build_alignment(list(ts.values()))
    config = MeetingConfig(self_name="岡村")
    merged = integrate(alignment, ts, [layer1()], config, None)
    assert merged.speaker_map.speakers["other"].name == "田中"
    text, meta = generate_minutes(
        merged, manifest_fixture(tmp_path, config), config, None, version=1
    )
    assert "- **other**: 田中（外部トランスクリプトより）" in text
    assert "- **me**: 岡村\n" in text  # self_name carries no layer-4 note
    assert "**田中**: 先週のリリース状況を教えてください。" in text
    assert "other" not in meta.unresolved_speakers
