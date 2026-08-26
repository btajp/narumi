from __future__ import annotations

import re
from pathlib import Path

import pytest
from narumi.bundle import Bundle, TrackRecord, sha256_file
from narumi.errors import EngineUnavailableError, ErrorCode, InvalidArgumentError, NotFoundError
from narumi.preprocess import (
    FfmpegError,
    extract_audio,
    extract_frames,
    ffmpeg_path,
    ffmpeg_version,
    ffprobe_path,
    probe,
    probe_duration,
    run_preprocess,
)

from .media_fixtures import make_bundle_with_tracks, make_sine_wav, make_test_video

FRAME_NAME = re.compile(r"^frame_\d{4}_\d{8}\.png$")


def test_binaries_and_version():
    assert ffmpeg_path().is_file()
    assert ffprobe_path().is_file()
    version = ffmpeg_version()
    assert version and version[0].isdigit()


def test_env_override_must_exist(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("NARUMI_FFMPEG", str(tmp_path / "missing-ffmpeg"))
    with pytest.raises(EngineUnavailableError) as excinfo:
        ffmpeg_path()
    assert "NARUMI_FFMPEG" in str(excinfo.value)
    assert excinfo.value.code == ErrorCode.ENGINE_UNAVAILABLE


def test_env_override_and_ffprobe_sibling(monkeypatch):
    real = ffmpeg_path()
    monkeypatch.setenv("NARUMI_FFMPEG", str(real))
    monkeypatch.delenv("NARUMI_FFPROBE", raising=False)
    assert ffmpeg_path() == real
    assert ffprobe_path() == real.with_name("ffprobe")


def test_missing_binary_raises(monkeypatch):
    monkeypatch.delenv("NARUMI_FFMPEG", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("narumi.preprocess.ffmpeg.FALLBACK_DIRS", ())
    with pytest.raises(EngineUnavailableError):
        ffmpeg_path()


def test_extract_audio_resamples_and_is_deterministic(tmp_path: Path):
    src = make_sine_wav(tmp_path / "src.wav", seconds=3.0, freq=440, sample_rate=44100)
    dst = extract_audio(src, tmp_path / "out" / "a.16k.wav")
    assert dst.exists()
    assert not dst.with_name(dst.name + ".part").exists()
    stream = probe(dst)["streams"][0]
    assert stream["codec_name"] == "pcm_s16le"
    assert stream["sample_rate"] == "16000"
    assert stream["channels"] == 1
    assert abs(probe_duration(dst) - 3.0) < 0.01
    again = extract_audio(src, tmp_path / "b.wav")
    assert sha256_file(again) == sha256_file(dst)


def test_extract_audio_errors(tmp_path: Path):
    with pytest.raises(NotFoundError):
        extract_audio(tmp_path / "nope.wav", tmp_path / "out.wav")
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"this is not audio")
    with pytest.raises(FfmpegError) as excinfo:
        extract_audio(bad, tmp_path / "out.wav")
    assert excinfo.value.code == ErrorCode.INTERNAL
    assert excinfo.value.details["stderr_tail"]
    assert excinfo.value.details["returncode"] != 0
    assert not (tmp_path / "out.wav").exists()
    assert not (tmp_path / "out.wav.part").exists()
    with pytest.raises(InvalidArgumentError):
        extract_audio(bad, tmp_path / "out.wav", sample_rate=0)


def test_probe_duration(tmp_path: Path):
    wav = make_sine_wav(tmp_path / "d.wav", seconds=2.5)
    assert abs(probe_duration(wav) - 2.5) < 0.01
    with pytest.raises(NotFoundError):
        probe_duration(tmp_path / "missing.wav")


def test_extract_frames(tmp_path: Path):
    video = make_test_video(tmp_path / "v.mp4", seconds=12.0)
    out_dir = tmp_path / "frames"
    frames = extract_frames(video, out_dir, interval_sec=5.0)
    names = [frame.name for frame in frames]
    assert len(names) >= 3
    assert all(FRAME_NAME.match(name) for name in names)
    assert names == sorted(names)
    ms = [int(name.split("_")[2].split(".")[0]) for name in names]
    assert ms[0] == 0
    assert ms == sorted(ms)
    assert 5000 in ms and 10000 in ms
    assert probe(frames[0])["streams"][0]["width"] == 640
    # a second run replaces stale frames instead of accumulating them
    again = extract_frames(video, out_dir, interval_sec=5.0)
    assert [frame.name for frame in again] == names
    assert sorted(p.name for p in out_dir.iterdir()) == names
    with pytest.raises(InvalidArgumentError):
        extract_frames(video, out_dir, interval_sec=0)
    with pytest.raises(InvalidArgumentError):
        extract_frames(video, out_dir, scene_threshold=2)
    with pytest.raises(NotFoundError):
        extract_frames(tmp_path / "none.mp4", out_dir)


def test_run_preprocess_creates_then_skips(tmp_path: Path):
    bundle = make_bundle_with_tracks(tmp_path)
    results = run_preprocess(bundle)
    assert [r.key for r in results] == ["preprocess/audio/mic", "preprocess/audio/system"]
    assert all(not r.skipped for r in results)
    assert all(r.path.exists() for r in results)
    mic = bundle.artifact("preprocess/audio/mic")
    assert mic is not None
    assert mic.path == "preprocess/mic.16k.wav"
    assert mic.inputs == {"tracks/mic": bundle.manifest.recording.tracks["mic"].sha256}
    assert mic.params == {"sample_rate": 16000, "channels": 1, "codec": "pcm_s16le"}
    assert mic.producer.name == "ffmpeg"
    assert mic.producer.version == ffmpeg_version()
    assert mic.sha256 == sha256_file(bundle.abspath(mic.path))
    system = bundle.artifact("preprocess/audio/system")
    assert system is not None and system.sha256 != mic.sha256

    reopened = Bundle.open(bundle.path)
    second = run_preprocess(reopened)
    assert all(r.skipped for r in second)
    assert [r.record for r in second] == [r.record for r in results]
    forced = run_preprocess(reopened, force=True)
    assert all(not r.skipped for r in forced)
    assert [r.record.sha256 for r in forced] == [r.record.sha256 for r in results]


def test_run_preprocess_keeps_artifact_of_discarded_track(tmp_path: Path):
    bundle = make_bundle_with_tracks(tmp_path)
    first = run_preprocess(bundle)
    mic = bundle.manifest.recording.tracks["mic"]
    bundle.abspath(mic.path).unlink()
    mic.discarded = True
    bundle.save()
    results = run_preprocess(bundle, force=True)
    assert [r.key for r in results] == ["preprocess/audio/mic", "preprocess/audio/system"]
    assert results[0].skipped and results[0].path.exists()
    assert results[0].record == first[0].record
    assert not results[1].skipped
    # a discarded track whose artifact is gone too simply drops out
    results[0].path.unlink()
    assert [r.key for r in run_preprocess(bundle)] == ["preprocess/audio/system"]


def test_run_preprocess_requires_audio_track(tmp_path: Path):
    bundle = make_bundle_with_tracks(tmp_path, tracks=())
    with pytest.raises(InvalidArgumentError):
        run_preprocess(bundle)
    bundle.manifest.recording.tracks["screen"] = TrackRecord(
        path="tracks/screen.mp4", sha256="0" * 64
    )
    with pytest.raises(InvalidArgumentError):
        run_preprocess(bundle)


def test_run_preprocess_rejects_unhashed_track(tmp_path: Path):
    bundle = make_bundle_with_tracks(tmp_path, tracks=("mic",))
    bundle.manifest.recording.tracks["mic"].sha256 = None
    with pytest.raises(InvalidArgumentError):
        run_preprocess(bundle)


def test_run_preprocess_missing_track_file(tmp_path: Path):
    bundle = make_bundle_with_tracks(tmp_path, tracks=("mic",))
    bundle.abspath("tracks/mic.wav").unlink()
    with pytest.raises(NotFoundError):
        run_preprocess(bundle)


def test_run_preprocess_mic_only(tmp_path: Path):
    bundle = make_bundle_with_tracks(tmp_path, tracks=("mic",))
    results = run_preprocess(bundle)
    assert [r.key for r in results] == ["preprocess/audio/mic"]
    assert bundle.artifact("preprocess/audio/system") is None
