"""End-to-end pipeline test: CLI import → process → regenerate → export → catalog rebuild.

Real ffmpeg (lavfi sine tracks), the ``fake`` transcription / diarization engines scripted through
sidecar files, and the ``none`` / ``fake`` LLM providers. No network, no models, no server.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from narumi.bundle import Bundle
from narumi.catalog import Catalog
from narumi.cli import cli
from narumi.errors import NotFoundError, PolicyViolationError
from narumi.generate import PLAIN_PLACEHOLDER
from narumi.models import MinutesMeta
from narumi.pipeline import (
    STAGE_ORDER,
    export_meeting,
    process_meeting,
    refresh_meeting,
    regenerate_meeting,
)
from narumi.transcribe import sidecar_path

from .media_fixtures import make_sine_wav, write_sidecar

MIC_SCRIPT = [
    {"start": 0.0, "end": 1.0, "text": "おはようございます、岡村です。"},
    {"start": 3.0, "end": 4.0, "text": "では始めましょう。"},
]
SYSTEM_SCRIPT = [
    {"start": 1.6, "end": 2.4, "text": "おはようございます。"},
    {"start": 4.6, "end": 6.0, "text": "本日の議題は三つあります。"},
]
FULL_RUN_KEYS = [
    "preprocess/audio/mic",
    "preprocess/audio/system",
    "context/brief",
    "transcripts/own-mic",
    "transcripts/own-system",
    "diarization/layer1",
    "diarization/layer2",
    "merged/alignment",
    "merged/merged",
    "minutes/v1",
]
REGENERATE_KEYS = ["merged/alignment", "merged/merged"]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    monkeypatch.setenv("NARUMI_HOME", str(root))
    return root


def run_cli(home: Path, *args: str) -> str:
    result = CliRunner().invoke(cli, ["--data-root", str(home), *args])
    assert result.exit_code == 0, result.output
    return result.stdout


def import_meeting(home: Path, tmp_path: Path) -> Bundle:
    mic = make_sine_wav(tmp_path / "src" / "mic.wav", 4.0, 440)
    system = make_sine_wav(tmp_path / "src" / "system.wav", 6.0, 660)
    meeting_id = run_cli(
        home,
        "import-recording",
        "--name",
        "E2E 定例",
        "--mic",
        str(mic),
        "--system",
        str(system),
        "--started-at",
        "2026-08-27T12:05:00+09:00",
    ).strip()
    run_cli(
        home,
        "config",
        meeting_id,
        "--transcription-engine",
        "fake",
        "--diarization-engine",
        "fake",
        "--llm-provider",
        "none",
        "--self-name",
        "岡村",
    )
    bundle = Bundle.find(home / "meetings", meeting_id)
    # Script the fake engine through the sidecars it reads next to the preprocessed wavs.
    write_sidecar(sidecar_path(bundle.abspath("preprocess/mic.16k.wav")), MIC_SCRIPT)
    write_sidecar(sidecar_path(bundle.abspath("preprocess/system.16k.wav")), SYSTEM_SCRIPT)
    return bundle


def reopen(bundle: Bundle) -> Bundle:
    return Bundle.open(bundle.path)


def minutes_text(bundle: Bundle, version: int) -> str:
    return bundle.abspath(f"minutes/v{version}/minutes.md").read_text(encoding="utf-8")


def test_full_pipeline_end_to_end(home: Path, tmp_path: Path) -> None:
    bundle = import_meeting(home, tmp_path)
    assert bundle.manifest.status == "recorded"
    assert bundle.manifest.recording.duration_sec == pytest.approx(6.0, abs=0.05)

    # ---------------------------------------------------------------- process (plain minutes)
    progress: list[tuple[str, float]] = []
    result = process_meeting(bundle, progress=lambda stage, f: progress.append((stage, f)))
    assert result.meeting_id == bundle.meeting_id
    assert result.minutes_version == 1
    assert result.stages == FULL_RUN_KEYS
    assert result.skipped == []
    assert result.unresolved_speakers == ["SPEAKER_00"]  # me → 岡村, system speaker unresolved
    assert [stage for stage, _ in progress] == list(STAGE_ORDER)
    assert [f for _, f in progress] == sorted(f for _, f in progress) and progress[-1][1] == 1.0

    bundle = reopen(bundle)
    assert bundle.manifest.status == "ready"
    assert bundle.manifest.latest_minutes_version == 1
    assert sorted(bundle.manifest.artifacts) == sorted(FULL_RUN_KEYS)
    assert bundle.abspath("merged/speaker_map.json").is_file()  # convenience copy, not an artifact

    text = minutes_text(bundle, 1)
    assert "## 文字起こし（全文）" in text
    assert "**岡村**: おはようございます、岡村です。" in text  # me-derived (mic track)
    assert "**SPEAKER_00（未特定）**: 本日の議題は三つあります。" in text  # other-derived
    assert PLAIN_PLACEHOLDER in text and "（fake）" not in text
    meta = MinutesMeta.model_validate(bundle.read_json("minutes/v1/meta.json"))
    assert meta.provider == "none" and meta.unresolved_speakers == ["SPEAKER_00"]

    # ---------------------------------------------------------------- process again: all skipped
    again = process_meeting(bundle)
    assert again.stages == []
    assert again.skipped == FULL_RUN_KEYS
    assert again.minutes_version == 1
    assert [v.version for v in reopen(bundle).manifest.minutes_versions] == [1]

    # ---------------------------------------------------------------- regenerate --force → v2
    regen = regenerate_meeting(
        bundle, force=True, reason="e2e force", job_id="job-0123456789ab", progress=None
    )
    assert regen.minutes_version == 2
    assert regen.stages == [*REGENERATE_KEYS, "minutes/v2"]
    assert not any(key.startswith(("preprocess/", "transcripts/")) for key in regen.stages)
    bundle = reopen(bundle)
    assert [v.version for v in bundle.manifest.minutes_versions] == [1, 2]
    assert len(bundle.manifest.regenerations) == 1
    record = bundle.manifest.regenerations[0]
    assert (record.job_id, record.reason, record.minutes_version) == (
        "job-0123456789ab",
        "e2e force",
        2,
    )
    assert minutes_text(bundle, 1) == minutes_text(bundle, 2).replace(
        "| 議事録バージョン | v2 |", "| 議事録バージョン | v1 |"
    ).replace(
        next(line for line in minutes_text(bundle, 2).splitlines() if "生成日時" in line),
        next(line for line in minutes_text(bundle, 1).splitlines() if "生成日時" in line),
    )

    # ---------------------------------------------------------------- fake LLM → v3
    run_cli(home, "config", bundle.meeting_id, "--llm-provider", "fake")
    bundle = reopen(bundle)
    with_llm = regenerate_meeting(bundle, reason="switch to fake llm")
    assert with_llm.minutes_version == 3
    assert with_llm.stages == ["merged/merged", "minutes/v3"]  # alignment unchanged
    assert with_llm.skipped == ["merged/alignment"]
    bundle = reopen(bundle)
    assert bundle.manifest.minutes_versions[-1].provider == "fake"
    assert len(bundle.manifest.regenerations) == 2
    fake_text = minutes_text(bundle, 3)
    assert "（fake）" in fake_text and PLAIN_PLACEHOLDER not in fake_text
    assert "| LLM プロバイダ | fake |" in fake_text
    assert "**岡村**: おはようございます、岡村です。" in fake_text

    # ---------------------------------------------------------------- export markdown + html
    md_path = tmp_path / "out" / "minutes.md"
    md = export_meeting(
        bundle, "markdown", options={"output_path": str(md_path)}, request_id="req-1"
    )
    assert md.destination == "markdown" and md.minutes_version == 3
    assert Path(md.ref) == md_path.resolve() and md_path.read_text(encoding="utf-8") == fake_text
    html = export_meeting(bundle, "html", minutes_version=1)
    assert html.destination == "html" and html.minutes_version == 1
    assert Path(html.ref).is_file() and Path(html.ref).is_relative_to(home / "exports")
    assert "<html" in Path(html.ref).read_text(encoding="utf-8")
    bundle = reopen(bundle)
    assert [(e.destination, e.minutes_version, e.request_id) for e in bundle.manifest.exports] == [
        ("markdown", 3, "req-1"),
        ("html", 1, None),
    ]
    with pytest.raises(NotFoundError):
        export_meeting(bundle, "markdown", minutes_version=9)
    with pytest.raises(NotFoundError):
        export_meeting(bundle, "nope")

    # ---------------------------------------------------------------- catalog rebuild from disk
    with Catalog(home / "narumi.db") as catalog:
        stats = catalog.rebuild(home / "meetings")
        assert (stats.meetings, stats.segments, stats.errors) == (1, 4, [])
        row = catalog.get_meeting_row(bundle.meeting_id)
        assert row is not None
        assert row["status"] == "ready" and row["latest_minutes_version"] == 3
        assert row["started_at"] == "2026-08-27T03:05:00Z"
        assert [e["destination"] for e in catalog.list_exports(bundle.meeting_id)] == [
            "markdown",
            "html",
        ]
        hits = catalog.search_segments("議題は三つ")
        assert [h["meeting_id"] for h in hits] == [bundle.meeting_id]
        assert hits[0]["speaker"] == "SPEAKER_00"

    # ---------------------------------------------------------------- policy violation → failed
    run_cli(
        home,
        "config",
        bundle.meeting_id,
        "--llm-provider",
        "anthropic-api",
        "--external-send-policy",
        "local_only",
    )
    bundle = reopen(bundle)
    with pytest.raises(PolicyViolationError) as excinfo:
        process_meeting(bundle)
    assert excinfo.value.details["provider"] == "anthropic-api"
    bundle = reopen(bundle)
    assert bundle.manifest.status == "failed"
    assert bundle.manifest.latest_minutes_version == 3  # nothing was generated
    assert json.loads(bundle.manifest_path.read_text(encoding="utf-8"))["status"] == "failed"


def test_regenerate_needs_transcripts(home: Path, tmp_path: Path) -> None:
    bundle = import_meeting(home, tmp_path)
    with pytest.raises(NotFoundError):
        regenerate_meeting(bundle)
    assert reopen(bundle).manifest.status == "recorded"  # untouched: nothing ran


def test_refresh_runs_missing_and_changed_deterministic_stages(home: Path, tmp_path: Path) -> None:
    """``refresh_meeting`` = the MCP regenerate tool: process what never ran, redo what changed."""
    bundle = import_meeting(home, tmp_path)

    # a meeting that was never processed (auto_process=false): the full run happens
    first = refresh_meeting(bundle, reason="stopped with auto_process=false", job_id="job-1")
    assert first.stages == FULL_RUN_KEYS and first.skipped == []
    assert first.minutes_version == 1
    bundle = reopen(bundle)
    assert bundle.manifest.status == "ready"
    assert [(r.job_id, r.minutes_version) for r in bundle.manifest.regenerations] == [("job-1", 1)]

    # nothing changed: everything is skipped, no new version, but the regeneration is recorded
    same = refresh_meeting(bundle, reason="noop")
    assert same.stages == [] and same.skipped == FULL_RUN_KEYS and same.minutes_version == 1

    # force re-runs alignment onward only (never preprocess / brief / transcribe / diarize)
    forced = refresh_meeting(bundle, force=True, reason="force")
    assert forced.stages == ["merged/alignment", "merged/merged", "minutes/v2"]
    assert forced.skipped == FULL_RUN_KEYS[:7]

    # a diarization change made through the config is picked up: layer 2 is dropped, the
    # integration re-runs without it, a new version appears
    run_cli(home, "config", bundle.meeting_id, "--diarization-engine", "none")
    bundle = reopen(bundle)
    changed = refresh_meeting(bundle, reason="diarization off")
    assert "diarization/layer2" not in reopen(bundle).manifest.artifacts
    assert changed.stages == ["merged/merged", "minutes/v3"]
    assert "transcripts/own-mic" in changed.skipped and "merged/alignment" in changed.skipped
    assert changed.unresolved_speakers == ["other"]  # SPEAKER_00 came from the dropped layer 2
    assert "| 話者分離エンジン | tracks 1 |" in minutes_text(reopen(bundle), 3)

    # vocab_hints reach the brief, transcription and integration: the transcripts are redone
    run_cli(home, "config", bundle.meeting_id, "--vocab-hint", "gaia-library")
    bundle = reopen(bundle)
    rehinted = refresh_meeting(bundle, reason="vocab")
    assert rehinted.stages[:3] == [
        "context/brief",
        "transcripts/own-mic",
        "transcripts/own-system",
    ]
    # the fake engine ignores hints, so the transcript hashes (→ layer 1, alignment) are
    # unchanged; integration re-runs because vocab_hints are part of its params
    assert rehinted.skipped == [
        "preprocess/audio/mic",
        "preprocess/audio/system",
        "diarization/layer1",
        "merged/alignment",
    ]
    assert rehinted.stages[3:] == ["merged/merged", "minutes/v4"]
    assert len(reopen(bundle).manifest.regenerations) == 5

    # a failed run leaves status failed and the exception propagates unchanged
    run_cli(home, "config", bundle.meeting_id, "--llm-provider", "anthropic-api")
    bundle = reopen(bundle)
    with pytest.raises(PolicyViolationError):
        refresh_meeting(bundle, reason="policy")
    assert reopen(bundle).manifest.status == "failed"
    # … and refresh is also how a failed process job is retried
    run_cli(home, "config", bundle.meeting_id, "--llm-provider", "none")
    retried = refresh_meeting(reopen(bundle), reason="retry")
    assert retried.minutes_version is not None and reopen(bundle).manifest.status == "ready"
