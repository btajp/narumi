"""Bounded loudness correction, measurement failures, and cached provenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from narumi.errors import CancelledError
from narumi.playback import _loudness
from narumi.playback._loudness import (
    LoudnessMeasurement,
    compute_normalization,
    parse_loudness,
    valid_normalization_records,
)
from narumi.preprocess.ffmpeg import FfmpegError


def _stderr(integrated, peak) -> bytes:
    report = json.dumps({"input_i": integrated, "input_tp": peak})
    return f"Input #0: audio\n[Parsed_loudnorm_0]\n{report}\n[out#0] muxing overhead\n".encode()


@pytest.mark.parametrize(
    ("integrated", "peak", "gain", "reason"),
    [
        (-30, -20, 12, "matched_target"),
        (-14, -5, -4, "matched_target"),
        (-18, -10, 0, "matched_target"),
        (-50, -40, 24, "boost_limited"),
        (-40, -12, 10, "peak_limited"),
        (-50, -26, 24, "peak_limited"),
        (-42, -40, 24, "matched_target"),
        (-26, -10, 8, "matched_target"),
        (-18, -1, -1, "peak_limited"),
        (-55, -70, 0, "below_floor"),
        (-54.999, -50, 24, "boost_limited"),
        (-60, -1, -1, "peak_limited"),
        (None, None, 0, "silence"),
        (None, -95, 0, "below_measurement_gate"),
        (None, -12.04, 0, "below_measurement_gate"),
        (None, -1, -1, "peak_limited"),
        (-30.123456789, -40, 12.123457, "matched_target"),
    ],
)
def test_normalization_respects_target_noise_floor_and_peak(integrated, peak, gain, reason):
    measured = LoudnessMeasurement(integrated, peak)

    result = compute_normalization(measured)

    assert result.measurement == measured
    assert result.gain_db == gain
    assert result.reason == reason
    if peak is not None:
        assert peak + result.gain_db <= -2 + 1e-6
    assert result.gain_db <= 24


@pytest.mark.parametrize(
    ("integrated", "peak", "expected"),
    [
        ("-18.25", "-3.50", LoudnessMeasurement(-18.25, -3.5)),
        (-18.25, -3.5, LoudnessMeasurement(-18.25, -3.5)),
        ("-inf", "-inf", LoudnessMeasurement(None, None)),
        ("-inf", "-95.00", LoudnessMeasurement(None, -95)),
        ("-inf", "-12.04", LoudnessMeasurement(None, -12.04)),
    ],
)
def test_parse_loudness_accepts_measurements_surrounded_by_logs(integrated, peak, expected):
    assert parse_loudness(_stderr(integrated, peak)) == expected


@pytest.mark.parametrize(
    "bad_value",
    ["nan", "inf", "-Infinity", "1e309", "", "quiet", None, True, [], float("nan")],
)
@pytest.mark.parametrize("field", ["input_i", "input_tp"])
def test_parse_loudness_rejects_invalid_numbers(field, bad_value):
    values = {"input_i": "-18", "input_tp": "-3"}
    values[field] = bad_value

    with pytest.raises(FfmpegError):
        parse_loudness(json.dumps(values).encode())


@pytest.mark.parametrize(
    "stderr",
    [
        b"no statistics produced",
        b'{"input_i": "-18"}',
        b'{"input_tp": "-3"}',
        b'{"input_i": "-18", "input_tp":',
        b'{"input_i": "-18", "input_tp": "-inf"}',
        _stderr("-18", "-3") + _stderr("-19", "-4"),
    ],
)
def test_parse_loudness_rejects_missing_inconsistent_or_ambiguous_report(stderr):
    with pytest.raises(FfmpegError):
        parse_loudness(stderr)


@pytest.mark.parametrize(
    "measurement",
    [
        LoudnessMeasurement(-18, None),
        LoudnessMeasurement(float("nan"), -3),
        LoudnessMeasurement(-18, float("inf")),
        LoudnessMeasurement(True, -3),
        LoudnessMeasurement(-18, False),
    ],
)
def test_compute_normalization_rejects_invalid_measurement(measurement):
    with pytest.raises(FfmpegError):
        compute_normalization(measurement)


def test_measure_loudness_reads_stderr_and_forwards_cancellation(monkeypatch, tmp_path: Path):
    def cancelled():
        return False

    def fake_tool(args, *, should_cancel=None, capture_stderr=False):
        assert str(tmp_path / "mic.m4a") in args
        assert should_cancel is cancelled
        assert capture_stderr
        return _stderr("-35.75", "-35.05")

    monkeypatch.setattr(_loudness, "ffmpeg_path", lambda: Path("ffmpeg"))
    monkeypatch.setattr(_loudness, "run_media_tool", fake_tool)

    result = _loudness.measure_loudness(tmp_path / "mic.m4a", should_cancel=cancelled)

    assert result == LoudnessMeasurement(-35.75, -35.05)


@pytest.mark.parametrize("stderr", [b"", _stderr("nan", "-3"), _stderr("-18", "-inf")])
def test_measurement_failure_never_falls_back_to_uncorrected_audio(monkeypatch, stderr):
    monkeypatch.setattr(_loudness, "ffmpeg_path", lambda: Path("ffmpeg"))
    monkeypatch.setattr(_loudness, "run_media_tool", lambda *args, **kwargs: stderr)

    with pytest.raises(FfmpegError):
        _loudness.measure_loudness(Path("mic.m4a"))


@pytest.mark.parametrize("error", [CancelledError("cancelled"), FfmpegError("decoder failed")])
def test_measure_loudness_propagates_cancellation_and_tool_failure(monkeypatch, error):
    def fake_tool(*args, **kwargs):
        raise error

    monkeypatch.setattr(_loudness, "ffmpeg_path", lambda: Path("ffmpeg"))
    monkeypatch.setattr(_loudness, "run_media_tool", fake_tool)

    with pytest.raises(type(error)) as caught:
        _loudness.measure_loudness(Path("mic.m4a"))

    assert caught.value is error


def test_normalization_record_requires_the_same_tracks_and_consistent_gain():
    record = {
        "integrated_lufs": -19,
        "true_peak_dbfs": -20,
        "gain_db": 1.0,
        "reason": "matched_target",
    }
    assert valid_normalization_records({"mic": record}, ["mic"])
    assert not valid_normalization_records({"mic": record}, ["mic", "system"])
    assert not valid_normalization_records({"mic": record, "system": record}, ["mic"])
    for changes in [
        {"gain_db": 24},
        {"gain_db": True},
        {"gain_db": float("nan")},
        {"reason": "boost_limited"},
        {"integrated_lufs": None, "true_peak_dbfs": None},
        {"true_peak_dbfs": None},
    ]:
        assert not valid_normalization_records({"mic": record | changes}, ["mic"])


def test_silence_and_below_gate_records_are_serializable_without_nonfinite_numbers():
    for measurement in [LoudnessMeasurement(None, None), LoudnessMeasurement(None, -95)]:
        record = compute_normalization(measurement).to_record()
        serialized = json.dumps(record, allow_nan=False)
        assert valid_normalization_records({"mic": json.loads(serialized)}, ["mic"])
