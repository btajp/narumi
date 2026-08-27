"""End-to-end Steps 3-8: slides + brief + external transcript ingestion + incremental re-run.

A synthetic meeting (two sine tracks + a two-scene screen video, fake engines, fake LLM) is
processed into minutes v1 with embedded key slides and a meeting brief. A +7 s-shifted WebVTT
with speaker names is then registered through the real server handler (``register_context``),
and one ``refresh_meeting`` resolves the ``other`` speaker to the VTT name, re-running the LLM
integration only for the intervals the new source touches (Step 8). Finally the markdown export
carries the slide images along.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from narumi.bundle import Bundle
from narumi.models import MeetingConfig, MinutesMeta
from narumi.pipeline import export_meeting, process_meeting, refresh_meeting
from narumi.slides import load_slides
from narumi.transcribe import sidecar_path
from narumi_server.context import build_context
from narumi_server.handlers.contexts import register_context

from .media_fixtures import make_bundle_with_tracks, write_sidecar
from .test_slides import add_screen_track

MIC_SCRIPT = [
    {"start": 0.0, "end": 1.0, "text": "おはようございます、岡村です。"},
    {"start": 3.0, "end": 4.0, "text": "では始めましょう。"},
    {"start": 8.0, "end": 9.0, "text": "以上で私からの共有は終わりです。"},
]
SYSTEM_SCRIPT = [
    {"start": 1.6, "end": 2.4, "text": "おはようございます、よろしくお願いします。"},
    {"start": 4.6, "end": 6.0, "text": "本日の議題は三つあります。"},
    {"start": 9.6, "end": 10.8, "text": "ありがとうございました、失礼します。"},
]

# The same three system-side sentences as a Zoom WebVTT whose clock runs 7 s ahead, with the
# speaker's real name. Alignment must recover the −7 s correction from the text anchors.
SHIFTED_VTT = """WEBVTT

1
00:00:08.600 --> 00:00:09.400
田中 太郎: おはようございます、よろしくお願いします。

2
00:00:11.600 --> 00:00:13.000
田中 太郎: 本日の議題は三つあります。

3
00:00:16.600 --> 00:00:17.800
田中 太郎: ありがとうございました、失礼します。
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    monkeypatch.setenv("NARUMI_HOME", str(root))
    monkeypatch.delenv("NARUMI_GAIA_URL", raising=False)
    return root


def make_meeting(home: Path) -> Bundle:
    bundle = make_bundle_with_tracks(
        home,
        seconds=12.0,
        meeting_name="Steps 3-8 定例",
        config=MeetingConfig(
            transcription_engine="fake",
            diarization_engine="none",
            llm_provider="fake",
            self_name="岡村",
            vocab_hints=["gaia-library"],
        ),
    )
    add_screen_track(bundle)  # two-scene screen video (6 s per scene)
    write_sidecar(sidecar_path(bundle.abspath("preprocess/mic.16k.wav")), MIC_SCRIPT)
    write_sidecar(sidecar_path(bundle.abspath("preprocess/system.16k.wav")), SYSTEM_SCRIPT)
    return bundle


def minutes_text(bundle: Bundle, version: int) -> str:
    return bundle.abspath(f"minutes/v{version}/minutes.md").read_text(encoding="utf-8")


def test_steps38_end_to_end(home: Path, tmp_path: Path) -> None:
    bundle = make_meeting(home)

    # ---------------------------------------------------------------- process → minutes v1
    result = process_meeting(bundle)
    assert result.minutes_version == 1
    assert result.stages == [
        "preprocess/audio/mic",
        "preprocess/audio/system",
        "context/brief",
        "transcripts/own-mic",
        "transcripts/own-system",
        "diarization/layer1",
        "preprocess/slides",
        "merged/alignment",
        "merged/merged",
        "minutes/v1",
    ]  # no layer2 (engine none), no layer4 (no ext transcript), no layer3 (fake has no vision)

    # Step 4+5: the key slides are embedded — image refs in the markdown, files under slides/
    slides = load_slides(bundle)
    assert len(slides) >= 2
    v1 = minutes_text(bundle, 1)
    assert "![slide-0001" in v1 and "](slides/slide-0001.png)" in v1
    for slide in slides:
        assert bundle.abspath(f"minutes/v1/slides/{slide.id}.png").is_file()
    meta1 = MinutesMeta.model_validate(bundle.read_json("minutes/v1/meta.json"))
    assert meta1.params["slides"] == len(slides) and meta1.params["brief"] is True

    # the brief exists and its merged vocab hints reached the transcription engine
    assert bundle.artifact("context/brief") is not None
    brief = json.loads(bundle.abspath("context/brief.json").read_text(encoding="utf-8"))
    assert brief["vocab_hints"] == ["gaia-library"]
    assert [p["name"] for p in brief["participants"]] == ["岡村"]
    assert bundle.manifest.artifacts["transcripts/own-mic"].params["vocab_hints"] == [
        "gaia-library"
    ]

    # v1 speakers: me resolved by self_name, the system side still anonymous
    assert result.unresolved_speakers == ["other"]
    assert "- **other**: 未特定" in v1

    merged1 = json.loads(bundle.abspath("merged/merged.json").read_text(encoding="utf-8"))
    assert merged1["params"]["recomputed"] == 6 and merged1["params"]["reused"] == 0

    # ---------------------------------------------------------------- Step 3: register the VTT
    ctx = build_context(home)
    try:
        registered = register_context(
            ctx,
            {
                "meeting_id": bundle.meeting_id,
                "source_type": "zoom_transcript",
                "content": SHIFTED_VTT,
                "label": "Zoom 字幕",
                "request_id": str(uuid.uuid4()),
            },
        )
    finally:
        ctx.close()
    assert registered["status"] == "parsed"
    ext_source = f"ext-{registered['context_id']}"
    bundle = Bundle.open(bundle.path)
    assert f"transcripts/{ext_source}" in bundle.manifest.artifacts

    # ---------------------------------------------------------------- refresh → minutes v2
    refreshed = refresh_meeting(bundle, reason="external transcript")
    assert refreshed.minutes_version == 2
    assert refreshed.stages == [
        "context/brief",  # its inputs list every context source file
        "diarization/layer4",
        "merged/alignment",
        "merged/merged",
        "minutes/v2",
    ]
    assert "transcripts/own-mic" in refreshed.skipped  # never re-transcribed
    assert "preprocess/slides" in refreshed.skipped

    # the +7 s clock shift was recovered from the text anchors
    alignment = json.loads(bundle.abspath("merged/alignment.json").read_text(encoding="utf-8"))
    assert alignment["offsets"][ext_source] == pytest.approx(-7.0, abs=0.05)

    # Step 8: only the three intervals the VTT touches were recomputed, the rest reused
    merged2 = json.loads(bundle.abspath("merged/merged.json").read_text(encoding="utf-8"))
    assert merged2["params"]["reused"] == 3 and merged2["params"]["recomputed"] == 3
    assert merged2["params"]["layer4_sources"] == [ext_source]

    # the anonymous "other" speaker is now the VTT's real name (layer-4 evidence)
    other = merged2["speaker_map"]["speakers"]["other"]
    assert other["name"] == "田中 太郎"
    assert any(e["layer"] == 4 for e in other["evidence"])
    assert refreshed.unresolved_speakers == []
    v2 = minutes_text(bundle, 2)
    assert "- **other**: 田中 太郎（外部トランスクリプトより）" in v2
    assert "**田中 太郎**:" in v2
    assert "![slide-0001" in v2
    assert bundle.abspath("minutes/v2/slides/slide-0001.png").is_file()

    # ---------------------------------------------------------------- export markdown + slides
    out = tmp_path / "out" / "minutes.md"
    exported = export_meeting(bundle, "markdown", options={"output_path": str(out)})
    assert exported.minutes_version == 2 and out.is_file()
    exported_text = out.read_text(encoding="utf-8")
    assert "](minutes-slides/slide-0001.png)" in exported_text
    assert (tmp_path / "out" / "minutes-slides" / "slide-0001.png").is_file()
