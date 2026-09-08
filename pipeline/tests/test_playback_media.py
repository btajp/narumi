"""Playback integration tests using synthetic media on a shared recording timeline."""

from __future__ import annotations

import json
import math
import sys
from array import array
from pathlib import Path

import pytest
from narumi.bundle import TrackRecord, sha256_file
from narumi.playback import ARTIFACT_KEY, OUTPUT_PATH, run_playback
from narumi.playback._media import inspect_media, mix_recording
from narumi.preprocess.ffmpeg import FfmpegError, ffmpeg_path, ffprobe_path, run_tool

from .media_fixtures import _ffmpeg, make_bundle_with_tracks, make_sine_wav, make_test_video

SAMPLE_RATE = 48000


def _aac_tone(path: Path, *, frequency: int, seconds: float, delay: float = 0) -> Path:
    source = make_sine_wav(
        path.with_suffix(".wav"), seconds=seconds, freq=frequency, sample_rate=SAMPLE_RATE
    )
    # Independent recorder writers retain an offset from their common session start.
    _ffmpeg("-i", str(source), "-c:a", "aac", "-output_ts_offset", str(delay), str(path))
    return path


def _mono_samples(path: Path) -> array:
    result = run_tool(
        [
            str(ffmpeg_path()),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            "pipe:1",
        ]
    )
    samples = array("f", result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _amplitude(samples: array, frequency: int, start: float, end: float) -> float:
    window = samples[round(start * SAMPLE_RATE) : round(end * SAMPLE_RATE)]
    assert len(window) == round((end - start) * SAMPLE_RATE)
    angle = 2 * math.pi * frequency / SAMPLE_RATE
    real = sum(value * math.cos(angle * index) for index, value in enumerate(window))
    imaginary = sum(value * math.sin(angle * index) for index, value in enumerate(window))
    return 2 * math.hypot(real, imaginary) / len(window)


def test_playback_preserves_audio_delay_tail_and_reproducible_output(tmp_path: Path):
    bundle = make_bundle_with_tracks(tmp_path, tracks=())
    video = make_test_video(bundle.abspath("tracks/screen.mp4"), seconds=3)
    mic = _aac_tone(bundle.abspath("tracks/mic.m4a"), frequency=440, seconds=2, delay=1.25)
    system = _aac_tone(bundle.abspath("tracks/system.m4a"), frequency=660, seconds=5)
    for name, path in {"screen": video, "mic": mic, "system": system}.items():
        bundle.manifest.recording.tracks[name] = TrackRecord(
            path=str(path.relative_to(bundle.path)), sha256=sha256_file(path)
        )
    bundle.save()
    assert inspect_media(mic)["audio"].start == pytest.approx(1.25, abs=0.03)

    result = run_playback(bundle)
    assert not result.skipped
    streams = inspect_media(result.path)
    assert set(streams) == {"video", "audio"}
    assert streams["video"].end == pytest.approx(3, abs=0.03)
    assert streams["audio"].end == pytest.approx(5, abs=0.03)
    samples = _mono_samples(result.path)
    assert len(samples) / SAMPLE_RATE == pytest.approx(5, abs=0.03)
    assert _amplitude(samples, 440, 0.2, 0.5) < 0.001
    mic_level = _amplitude(samples, 440, 1.4, 1.7)
    system_level = _amplitude(samples, 660, 1.4, 1.7)
    assert 0.05 < mic_level < 0.08
    assert mic_level == pytest.approx(system_level, rel=0.15)
    assert _amplitude(samples, 660, 4.5, 4.8) == pytest.approx(system_level, rel=0.15)
    assert _amplitude(samples, 440, 3.5, 3.8) < 0.001

    first_hash = sha256_file(result.path)
    result.path.unlink()
    rebuilt = run_playback(bundle)
    assert not rebuilt.skipped
    assert sha256_file(rebuilt.path) == first_hash


def test_mix_recording_preserves_delayed_video_start(tmp_path: Path):
    source = make_test_video(tmp_path / "source.mp4", seconds=3)
    video = tmp_path / "screen.mp4"
    _ffmpeg("-i", str(source), "-c", "copy", "-output_ts_offset", "0.4", str(video))
    audio = _aac_tone(tmp_path / "mic.m4a", frequency=440, seconds=4)
    output = tmp_path / "playback.mp4"

    mix_recording(video, [audio], output)

    original = inspect_media(video)["video"]
    rendered = inspect_media(output)
    assert original.start == pytest.approx(0.4, abs=0.01)
    assert rendered["video"].start == pytest.approx(original.start, abs=0.01)
    assert rendered["video"].end == pytest.approx(original.end, abs=0.01)
    assert rendered["audio"].start == pytest.approx(0, abs=0.03)
    assert _amplitude(_mono_samples(output), 440, 0.1, 0.3) > 0.1


def test_mix_recording_keeps_video_after_short_audio_ends(tmp_path: Path):
    video = make_test_video(tmp_path / "screen.mp4", seconds=5)
    audio = _aac_tone(tmp_path / "mic.m4a", frequency=440, seconds=1)
    output = tmp_path / "playback.mp4"

    mix_recording(video, [audio], output)

    streams = inspect_media(output)
    assert streams["video"].end == pytest.approx(5, abs=0.03)
    assert streams["audio"].end == pytest.approx(1, abs=0.03)


def test_mix_recording_preserves_single_audio_delay_and_level(tmp_path: Path):
    video = make_test_video(tmp_path / "screen.mp4", seconds=3)
    audio = _aac_tone(tmp_path / "mic.m4a", frequency=440, seconds=2, delay=1.25)
    output = tmp_path / "playback.mp4"

    mix_recording(video, [audio], output)

    streams = inspect_media(output)
    assert streams["audio"].start == pytest.approx(0, abs=0.03)
    assert streams["audio"].end == pytest.approx(3.25, abs=0.03)
    samples = _mono_samples(output)
    assert _amplitude(samples, 440, 0.2, 0.5) < 0.001
    assert _amplitude(samples, 440, 1.4, 1.7) == pytest.approx(0.125, rel=0.15)


@pytest.mark.parametrize("damaged_track", ["screen", "mic"])
def test_corrupted_original_is_typed_and_never_published(tmp_path: Path, damaged_track):
    bundle = make_bundle_with_tracks(tmp_path, tracks=("mic",), seconds=1)
    video = make_test_video(bundle.abspath("tracks/screen.mp4"), seconds=1)
    bundle.manifest.recording.tracks["screen"] = TrackRecord(
        path=bundle.relpath(video), sha256=sha256_file(video)
    )
    record = bundle.manifest.recording.tracks[damaged_track]
    bundle.abspath(record.path).write_bytes(b"corrupted original")
    bundle.save()

    with pytest.raises(FfmpegError):
        run_playback(bundle)

    assert bundle.artifact(ARTIFACT_KEY) is None
    assert not bundle.abspath(OUTPUT_PATH).exists()
    assert bundle.abspath(record.path).read_bytes() == b"corrupted original"


def test_corrupted_video_payload_is_rejected_before_publication(tmp_path: Path):
    bundle = make_bundle_with_tracks(tmp_path, tracks=("mic",), seconds=3)
    video = make_test_video(bundle.abspath("tracks/screen.mp4"), seconds=3)
    metadata = inspect_media(video)
    packets = json.loads(
        run_tool(
            [
                str(ffprobe_path()),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_packets",
                "-show_entries",
                "packet=pos,size",
                "-of",
                "json",
                str(video),
            ]
        ).stdout
    )["packets"]
    packet = packets[10]
    # Retain the MP4 tables and frame header: stream-copy and metadata checks alone pass.
    with video.open("r+b") as stream:
        stream.seek(int(packet["pos"]) + 16)
        stream.write(b"\xff" * (int(packet["size"]) - 16))
    damaged_hash = sha256_file(video)
    assert inspect_media(video) == metadata
    bundle.manifest.recording.tracks["screen"] = TrackRecord(
        path=bundle.relpath(video), sha256=damaged_hash
    )
    bundle.save()

    with pytest.raises(FfmpegError):
        run_playback(bundle)

    assert bundle.artifact(ARTIFACT_KEY) is None
    assert not bundle.abspath(OUTPUT_PATH).exists()
    assert not list(bundle.abspath("playback").glob(".recording-*"))
    assert sha256_file(video) == damaged_hash
