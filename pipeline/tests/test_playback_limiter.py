"""Synthetic audio checks for limiter timing, gain, and independent track levels."""

from __future__ import annotations

import math
import sys
from array import array
from pathlib import Path

import pytest
from narumi.playback._limiter import LIMITER_FILTER
from narumi.playback._loudness import (
    AUDIO_FORMAT_FILTER,
    compute_normalization,
    measure_loudness,
)
from narumi.playback._media import mix_recording
from narumi.preprocess.ffmpeg import ffmpeg_path, run_tool

from .media_fixtures import _ffmpeg, make_test_video

RATE = 48000


def _pcm(path: Path, filters: str | None = None) -> array:
    args = [
        str(ffmpeg_path()),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-copyts",
        "-i",
        str(path),
        "-map",
        "0:a:0",
    ]
    if filters is not None:
        args.extend(["-af", filters])
    args.extend(["-ar", str(RATE), "-ac", "1", "-f", "f32le", "pipe:1"])
    samples = array("f", run_tool(args).stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _markers(tmp_path: Path, size: int, amplitude: float) -> Path:
    samples = array("f", [0.0] * size)
    samples[0], samples[size // 2], samples[-1] = amplitude, -amplitude, amplitude
    if sys.byteorder != "little":
        samples.byteswap()
    raw = tmp_path / "markers.f32"
    raw.write_bytes(samples.tobytes())
    output = tmp_path / "markers.wav"
    _ffmpeg(
        "-f",
        "f32le",
        "-ar",
        str(RATE),
        "-ac",
        "1",
        "-i",
        str(raw),
        "-c:a",
        "pcm_f32le",
        str(output),
    )
    return output


@pytest.mark.parametrize("size", [96, 960])
@pytest.mark.parametrize(("amplitude", "gain_db"), [(0.025, 10), (0.75, 12)])
def test_limiter_preserves_first_and_last_samples_even_below_attack_duration(
    tmp_path: Path, size, amplitude, gain_db
):
    source = _markers(tmp_path, size, amplitude)
    prefix = f"{AUDIO_FORMAT_FILTER},volume={gain_db}dB,"
    reference = _pcm(source, prefix + "aresample=192000,aresample=48000")
    limited = _pcm(source, prefix + LIMITER_FILTER)

    assert len(limited) == len(reference) == size
    for position in (0, size // 2, size - 1):
        window = range(max(0, position - 3), min(size, position + 4))
        assert max(window, key=lambda index: abs(limited[index])) == position
        assert abs(limited[position]) > 0.04
    if amplitude < 0.1:
        # A limiter that never fires must preserve the requested gain without auto leveling.
        assert max(abs(a - b) for a, b in zip(reference, limited, strict=True)) < 2e-6
    else:
        assert max(map(abs, limited)) < max(map(abs, reference)) * 0.5


@pytest.mark.parametrize("delay", [1.25, 4.25])
def test_limiter_keeps_delayed_audio_aligned_through_eof(tmp_path: Path, delay):
    source = _markers(tmp_path, RATE, 0.025)
    delayed = tmp_path / "delayed.m4a"
    _ffmpeg(
        "-i",
        str(source),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-output_ts_offset",
        str(delay),
        str(delayed),
    )
    prefix = f"{AUDIO_FORMAT_FILTER},volume=6dB,"
    reference = _pcm(delayed, prefix + "aresample=192000,aresample=48000")
    limited = _pcm(delayed, prefix + LIMITER_FILTER)

    assert len(limited) == len(reference)
    assert max(map(abs, reference[:RATE])) < 1e-6
    assert max(map(abs, reference[-RATE // 10 :])) > 0.001
    # Whole-waveform equality catches a 5 ms shift even when total duration is unchanged.
    assert max(abs(a - b) for a, b in zip(reference, limited, strict=True)) < 2e-6


def _amplitude(samples: array, frequency: int, start: float, end: float) -> float:
    window = samples[round(start * RATE) : round(end * RATE)]
    assert len(window) == round((end - start) * RATE)
    angle = 2 * math.pi * frequency / RATE
    real = sum(value * math.cos(angle * index) for index, value in enumerate(window))
    imaginary = sum(value * math.sin(angle * index) for index, value in enumerate(window))
    return 2 * math.hypot(real, imaginary) / len(window)


def test_peak_on_quiet_microphone_does_not_reduce_other_speakers(tmp_path: Path):
    mic_source, mic = tmp_path / "mic.wav", tmp_path / "mic.m4a"
    system = tmp_path / "system.m4a"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "aevalsrc='0.0125*sin(2*PI*440*t)+0.75*eq(n,144000)':s=48000:d=6",
        "-c:a",
        "pcm_f32le",
        str(mic_source),
    )
    _ffmpeg(
        "-i",
        str(mic_source),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-output_ts_offset",
        "1.25",
        str(mic),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=660:duration=8:sample_rate=48000",
        "-af",
        "volume=2",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(system),
    )
    gains, levels = [], []
    for name, source in (("mic", mic), ("system", system)):
        correction = compute_normalization(measure_loudness(source))
        gains.append(correction.gain_db)
        corrected = tmp_path / f"{name}-corrected.wav"
        _ffmpeg(
            "-copyts",
            "-i",
            str(source),
            "-af",
            f"{AUDIO_FORMAT_FILTER},volume={correction.gain_db}dB,{LIMITER_FILTER}",
            "-c:a",
            "pcm_f32le",
            str(corrected),
        )
        levels.append(measure_loudness(corrected))
    assert all(level.integrated_lufs is not None for level in levels)
    assert abs(levels[0].integrated_lufs - levels[1].integrated_lufs) < 1
    assert levels[0].true_peak_dbfs < -1.8

    video = make_test_video(tmp_path / "screen.mp4", seconds=3)
    output = tmp_path / "recording.mp4"
    mix_recording(video, [mic, system], output, gains_db=gains)
    samples = _pcm(output)
    mic_level = _amplitude(samples, 440, 3.8, 4.0)
    system_level = _amplitude(samples, 660, 3.8, 4.0)
    assert mic_level > 0.03
    assert mic_level == pytest.approx(system_level, rel=0.15)
    assert _amplitude(samples, 660, 4.25, 4.30) == pytest.approx(system_level, rel=0.15)
    assert _amplitude(samples, 440, 0.2, 0.5) < 0.001
    assert _amplitude(samples, 660, 7.7, 7.9) == pytest.approx(system_level, rel=0.15)
