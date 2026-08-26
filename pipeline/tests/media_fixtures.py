"""Test helpers: synthetic media via ffmpeg (``lavfi``) and bundles with registered tracks."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from narumi.bundle import Bundle, TrackRecord, sha256_file, utc_now_iso
from narumi.models import MeetingConfig
from narumi.preprocess.ffmpeg import ffmpeg_path, run_tool

TRACK_FREQUENCIES: dict[str, int] = {"mic": 440, "system": 660}


def _ffmpeg(*args: str) -> None:
    run_tool([str(ffmpeg_path()), "-y", "-nostdin", "-hide_banner", "-loglevel", "error", *args])


def make_sine_wav(
    path: Path, seconds: float = 3.0, freq: int = 440, *, sample_rate: int = 16000
) -> Path:
    """Write a mono PCM s16le sine tone of ``seconds`` at ``freq`` Hz to ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={freq}:duration={seconds}",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(path),
    )
    return path


def make_test_video(
    path: Path, seconds: float = 12.0, *, fps: int = 10, size: str = "320x240"
) -> Path:
    """Write a small ``testsrc2`` video (native mpeg4 encoder, no audio) to ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={size}:rate={fps}:duration={seconds}",
        "-c:v",
        "mpeg4",
        "-q:v",
        "5",
        "-pix_fmt",
        "yuv420p",
        str(path),
    )
    return path


def make_bundle_with_tracks(
    tmp_path: Path,
    *,
    tracks: Sequence[str] = ("mic", "system"),
    seconds: float = 3.0,
    config: MeetingConfig | None = None,
    meeting_name: str = "テスト会議",
) -> Bundle:
    """Create a recorded bundle under ``tmp_path/meetings`` with sine-tone audio tracks.

    Each track is written to ``tracks/<track>.wav`` and registered in
    ``manifest.recording.tracks`` with its sha256, byte size and duration.
    """
    bundle = Bundle.create(tmp_path / "meetings", meeting_name=meeting_name, config=config)
    now = utc_now_iso()
    for index, track in enumerate(tracks):
        rel = f"tracks/{track}.wav"
        path = bundle.abspath(rel)
        make_sine_wav(path, seconds, TRACK_FREQUENCIES.get(track, 880 + 110 * index))
        bundle.manifest.recording.tracks[track] = TrackRecord(
            path=rel,
            sha256=sha256_file(path),
            bytes=path.stat().st_size,
            duration_sec=seconds,
        )
    bundle.manifest.recording.started_at = now
    bundle.manifest.recording.stopped_at = now
    bundle.manifest.recording.duration_sec = seconds
    bundle.manifest.status = "recorded"
    bundle.save()
    return bundle


def write_sidecar(path: Path, items: list[dict[str, Any]]) -> Path:
    """Write a JSON list sidecar (fake transcript / fake diarization script)."""
    path = Path(path)
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path
