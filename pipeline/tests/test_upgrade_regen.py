"""Step 8: affected-interval re-run — adding one source re-runs stage 2 only where it overlaps."""

import importlib
import json
from pathlib import Path

from narumi.align import build_alignment, run_align
from narumi.bundle import Bundle
from narumi.generate import CACHE_PATH, IntegrateCache, integrate, run_integrate
from narumi.generate.integrate import INTEGRATE_PROMPT_VERSION
from narumi.llm import FakeProvider
from narumi.models import (
    Diarization,
    EngineInfo,
    MeetingConfig,
    MergedTranscript,
    Segment,
    Transcript,
    Turn,
)

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
EXT_SPANS = [(24.4, 27.6, "田中", "では来月の予定を決めましょう、という話でした。")]
"""Overlaps only the 24-28 s mic interval (the speech the ext tool also transcribed)."""


def make_transcript(source_id: str, track: str | None, spans, *, named: bool = False):
    return Transcript(
        source_id=source_id,
        kind="own" if source_id.startswith("own-") else "external",
        track=track,  # type: ignore[arg-type]
        engine=EngineInfo(name="fake", version="1"),
        segments=[
            Segment(
                id=f"{source_id}:{i}",
                start=span[0],
                end=span[1],
                speaker=span[2] if named else None,
                text=span[-1],
            )
            for i, span in enumerate(spans)
        ],
    )


def layer1() -> Diarization:
    turns = [Turn(start=s, end=e, speaker="me", layer=1, source_id="own-mic") for s, e, _ in MIC]
    turns += [
        Turn(start=s, end=e, speaker="other", layer=1, source_id="own-system") for s, e, _ in SYSTEM
    ]
    return Diarization(layer=1, engine=EngineInfo(name="tracks", version="1"), turns=turns)


def own_transcripts() -> dict[str, Transcript]:
    return {
        "own-mic": make_transcript("own-mic", "mic", MIC),
        "own-system": make_transcript("own-system", "system", SYSTEM),
    }


def ext_transcript() -> Transcript:
    return make_transcript("ext-ctx1", None, EXT_SPANS, named=True)


# ------------------------------------------------------------------ direct (no bundle)
def test_cache_reuses_unchanged_intervals_and_recomputes_affected_only():
    ts = own_transcripts()
    config = MeetingConfig(llm_provider="fake", self_name="岡村")
    cache = IntegrateCache()

    first = FakeProvider()
    alignment = build_alignment(list(ts.values()))
    merged = integrate(alignment, ts, [layer1()], config, first, cache=cache)
    # 5 intervals; only the 12-16 s mic+system overlap needs the LLM
    assert len(first.calls) == 1
    assert merged.params["reused"] == 0 and merged.params["recomputed"] == 5

    # identical inputs → everything reused, zero LLM calls, identical segments
    second = FakeProvider()
    again = integrate(alignment, ts, [layer1()], config, second, cache=cache)
    assert second.calls == []
    assert again.params["reused"] == 5 and again.params["recomputed"] == 0
    assert [s.model_dump() for s in again.segments] == [s.model_dump() for s in merged.segments]

    # one ext column added (Step 8): only the interval it overlaps goes back to the LLM
    with_ext = {**ts, "ext-ctx1": ext_transcript()}
    third = FakeProvider()
    upgraded = integrate(
        build_alignment(list(with_ext.values())), with_ext, [layer1()], config, third, cache=cache
    )
    assert len(third.calls) == 1
    assert "[ext-ctx1]" in third.calls[0].prompt
    assert upgraded.params["reused"] == 4 and upgraded.params["recomputed"] == 1
    # the untouched 12-16 s integration is byte-identical to the first run's
    texts = {tuple(s.sources): s.text for s in upgraded.segments}
    old = {tuple(s.sources): s.text for s in merged.segments}
    key = ("own-mic:1", "own-system:1")
    assert texts[key] == old[key]


def test_cache_never_serves_stale_speaker_names():
    """Names are resolved fresh on every run: a cached interval still follows self_name."""
    ts = own_transcripts()
    cache = IntegrateCache()
    alignment = build_alignment(list(ts.values()))
    named = MeetingConfig(self_name="岡村")
    merged = integrate(alignment, ts, [layer1()], named, None, cache=cache)
    assert merged.segments[0].speaker_name == "岡村"
    renamed = integrate(
        alignment, ts, [layer1()], MeetingConfig(self_name="鳴海"), None, cache=cache
    )
    assert renamed.params["reused"] == 5
    assert renamed.segments[0].speaker_name == "鳴海"


# ------------------------------------------------------------------ bundle stage
def record_json(bundle: Bundle, key: str, rel: str, model) -> None:
    bundle.run_stage(
        key,
        inputs={"upstream": "0" * 64},
        params={},
        producer=("fake", "1"),
        output=rel,
        fn=lambda _: bundle.write_json(rel, model),
        force=True,
    )


def test_run_integrate_rerun_after_new_ext_source(tmp_path: Path, monkeypatch):
    shared = FakeProvider()
    integrate_module = importlib.import_module("narumi.generate.integrate")
    monkeypatch.setattr(integrate_module, "get_provider", lambda name: shared)
    bundle = Bundle.create(
        tmp_path, meeting_name="定例", config=MeetingConfig(llm_provider="fake", self_name="岡村")
    )
    for sid, transcript in own_transcripts().items():
        record_json(bundle, f"transcripts/{sid}", f"transcripts/{sid}.json", transcript)
    record_json(bundle, "diarization/layer1", "diarization/layer1-tracks.json", layer1())
    run_align(bundle)

    first = run_integrate(bundle)
    assert not first.skipped and len(shared.calls) == 1
    cache_path = bundle.abspath(CACHE_PATH)
    assert cache_path.exists()
    cache_doc = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache_doc["version"] == 1 and cache_doc["provider"] == "fake"
    assert cache_doc["prompt_version"] == INTEGRATE_PROMPT_VERSION
    assert len(cache_doc["entries"]) == 5
    assert all(
        set(row) == {"start", "end", "text", "speaker_label", "sources"}
        for rows in cache_doc["entries"].values()
        for row in rows
    )

    # unchanged inputs: run_stage skips outright, the cache is not even consulted
    assert run_integrate(bundle).skipped and len(shared.calls) == 1

    # register a new ext transcript → align + integrate inputs change → only 1 new LLM call
    record_json(bundle, "transcripts/ext-ctx1", "transcripts/ext-ctx1.json", ext_transcript())
    assert not run_align(bundle).skipped
    upgraded = run_integrate(bundle)
    assert not upgraded.skipped
    assert len(shared.calls) == 2
    assert "[ext-ctx1]" in shared.calls[-1].prompt
    merged = MergedTranscript.model_validate_json(upgraded.path.read_text(encoding="utf-8"))
    assert merged.params["reused"] == 4 and merged.params["recomputed"] == 1
    assert merged.params["layer4_sources"] == ["ext-ctx1"]
    # the affected interval merged mic + ext into one LLM segment; the mic turn labels it me
    tail = [s for s in merged.segments if s.start >= 24.0]
    assert len(tail) == 1 and sorted(tail[0].sources) == ["ext-ctx1:0", "own-mic:2"]
    assert tail[0].speaker_label == "me" and tail[0].speaker_name == "岡村"

    # force bypasses cache reads: every interval recomputes (both LLM intervals call again)
    forced = run_integrate(bundle, force=True)
    assert not forced.skipped and len(shared.calls) == 4
    forced_merged = MergedTranscript.model_validate_json(forced.path.read_text(encoding="utf-8"))
    assert forced_merged.params["reused"] == 0 and forced_merged.params["recomputed"] == 5


def test_cache_survives_corruption(tmp_path: Path):
    path = tmp_path / "integrate_cache.json"
    path.write_text("{not json", encoding="utf-8")
    assert len(IntegrateCache.load(path)) == 0
    path.write_text(json.dumps({"version": 99, "entries": {"x": []}}), encoding="utf-8")
    assert len(IntegrateCache.load(path)) == 0
    path.write_text(json.dumps({"version": 1, "entries": {"x": [{"bad": 1}]}}), encoding="utf-8")
    assert len(IntegrateCache.load(path)) == 0


def test_asr_speaker_only_change_recomputes_only_overlapping_interval(tmp_path: Path, monkeypatch):
    shared = FakeProvider()
    integrate_module = importlib.import_module("narumi.generate.integrate")
    monkeypatch.setattr(integrate_module, "get_provider", lambda name: shared)
    ts = own_transcripts()
    system = ts["own-system"]
    system.engine = EngineInfo(
        name="openai-api", version="1", params={"model": "gpt-4o-transcribe-diarize"}
    )
    for index, segment in enumerate(system.segments):
        segment.speaker = f"asr:system:0:{index}"
    ts["ext-system"] = make_transcript("ext-system", None, [(6.1, 9.9, "確認の発話")])
    bundle = Bundle.create(tmp_path, meeting_name="定例", config=MeetingConfig(llm_provider="fake"))
    for sid, transcript in ts.items():
        record_json(bundle, f"transcripts/{sid}", f"transcripts/{sid}.json", transcript)
    record_json(bundle, "diarization/layer1", "diarization/layer1-tracks.json", layer1())
    run_align(bundle)
    first = run_integrate(bundle)
    original = MergedTranscript.model_validate_json(first.path.read_text(encoding="utf-8"))
    assert len(shared.calls) == 2 and original.params["recomputed"] == 5
    assert run_integrate(bundle).skipped
    alignment_hash = bundle.artifact_hash("merged/alignment")

    # The text, timing, and explicit diarization stay byte-identical; only ASR's label changes.
    system.segments[0].speaker = "asr:system:0:changed"
    record_json(bundle, "transcripts/own-system", "transcripts/own-system.json", system)
    run_align(bundle)
    assert bundle.artifact_hash("merged/alignment") == alignment_hash
    updated = run_integrate(bundle)
    assert not updated.skipped and len(shared.calls) == 3
    merged = MergedTranscript.model_validate_json(updated.path.read_text(encoding="utf-8"))
    assert merged.params["reused"] == 4 and merged.params["recomputed"] == 1
    changed = next(s for s in merged.segments if "own-system:0" in s.sources)
    assert changed.speaker_label == "asr:system:0:changed"
    before = {tuple(s.sources): s.model_dump() for s in original.segments}
    for segment in merged.segments:
        if segment is not changed:
            assert segment.model_dump() == before[tuple(segment.sources)]
    entry = merged.speaker_map.speakers["asr:system:0:changed"]
    assert entry.name is None and entry.confidence == 0
    assert "label=asr:system:0:changed" in entry.evidence[0].detail
    assert "asr:system:0:0" not in merged.speaker_map.speakers
    assert {key for key in bundle.manifest.artifacts if key.startswith("diarization/")} == {
        "diarization/layer1"
    }
