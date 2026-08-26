import os
import re
from pathlib import Path

import pytest
from narumi.align import build_alignment, build_intervals, run_align
from narumi.bundle import Bundle
from narumi.errors import NotFoundError, PolicyViolationError
from narumi.generate import (
    INTEGRATE_KEY,
    INTEGRATE_PROMPT_VERSION,
    MINUTES_PROMPT_VERSION,
    PLAIN_PLACEHOLDER,
    SPEAKER_MAP_PATH,
    generate_minutes,
    integrate,
    run_generate,
    run_integrate,
)
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
    assert meta.prompt_version == MINUTES_PROMPT_VERSION == "minutes-v1"
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
