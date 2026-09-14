"""Measure track loudness, then apply a bounded constant gain without changing timing."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from narumi.playback._process import CancelCheck, run_media_tool
from narumi.preprocess.ffmpeg import FfmpegError, ffmpeg_path

AUDIO_FORMAT_FILTER = (
    "aresample=48000:async=1:first_pts=0,aformat=sample_fmts=fltp:channel_layouts=stereo"
)
TARGET_LUFS = -18.0
MAX_BOOST_DB = 24.0
PEAK_CEILING_DBFS = -2.0
MIN_LOUDNESS_LUFS = -55.0
NORMALIZATION_SETTINGS = {
    "method": "ebu_r128_bounded_static_gain",
    "target_lufs": TARGET_LUFS,
    "max_boost_db": MAX_BOOST_DB,
    "peak_ceiling_dbfs": PEAK_CEILING_DBFS,
    "minimum_loudness_lufs": MIN_LOUDNESS_LUFS,
    "measurement_filter": AUDIO_FORMAT_FILTER,
}


@dataclass(frozen=True)
class LoudnessMeasurement:
    integrated_lufs: float | None
    true_peak_dbfs: float | None


@dataclass(frozen=True)
class TrackNormalization:
    measurement: LoudnessMeasurement
    gain_db: float
    reason: str

    def to_record(self) -> dict[str, Any]:
        return {
            "integrated_lufs": self.measurement.integrated_lufs,
            "true_peak_dbfs": self.measurement.true_peak_dbfs,
            "gain_db": self.gain_db,
            "reason": self.reason,
        }


def _measurement_number(value: Any) -> float | None:
    if value == "-inf":
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise FfmpegError("loudness measurement contains a non-numeric value")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise FfmpegError("loudness measurement contains a non-numeric value") from exc
    if not math.isfinite(number):
        raise FfmpegError("loudness measurement contains an invalid non-finite value")
    return number


def parse_loudness(stderr: bytes) -> LoudnessMeasurement:
    """Read loudnorm's input measurements, allowing surrounding ffmpeg log messages."""
    text = stderr.decode("utf-8", errors="replace")
    decoder = json.JSONDecoder()
    candidates = []
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and ("input_i" in value or "input_tp" in value):
            candidates.append(value)
    if len(candidates) != 1 or not {"input_i", "input_tp"} <= candidates[0].keys():
        raise FfmpegError("ffmpeg returned no unambiguous loudness measurement")
    data = candidates[0]
    result = LoudnessMeasurement(
        integrated_lufs=_measurement_number(data["input_i"]),
        true_peak_dbfs=_measurement_number(data["input_tp"]),
    )
    _validate_measurement(result)
    return result


def _validate_measurement(measurement: LoudnessMeasurement) -> None:
    for value in (measurement.integrated_lufs, measurement.true_peak_dbfs):
        if value is not None and not _finite_number(value):
            raise FfmpegError("invalid loudness measurement")
    if measurement.integrated_lufs is not None and measurement.true_peak_dbfs is None:
        raise FfmpegError("finite loudness measurement has no measurable peak")


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def compute_normalization(measurement: LoudnessMeasurement) -> TrackNormalization:
    _validate_measurement(measurement)
    loudness, peak = measurement.integrated_lufs, measurement.true_peak_dbfs
    if peak is None:
        return TrackNormalization(measurement, 0.0, "silence")
    peak_gain = PEAK_CEILING_DBFS - peak
    if loudness is None or loudness <= MIN_LOUDNESS_LUFS:
        # Below the EBU measurement gate, including short clips, never increase noise.
        gain = min(0.0, peak_gain)
        reason = "below_measurement_gate" if loudness is None else "below_floor"
        return TrackNormalization(
            measurement, round(gain, 6), "peak_limited" if gain < 0 else reason
        )
    target_gain = TARGET_LUFS - loudness
    gain = min(target_gain, MAX_BOOST_DB, peak_gain)
    if peak_gain < target_gain and peak_gain <= MAX_BOOST_DB:
        reason = "peak_limited"
    elif MAX_BOOST_DB < target_gain:
        reason = "boost_limited"
    else:
        reason = "matched_target"
    return TrackNormalization(measurement, round(gain, 6), reason)


def measure_loudness(path: Path, *, should_cancel: CancelCheck = None) -> LoudnessMeasurement:
    stderr = run_media_tool(
        [
            str(ffmpeg_path()),
            "-nostdin",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "info",
            "-xerror",
            "-copyts",
            "-err_detect",
            "explode",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-af",
            f"{AUDIO_FORMAT_FILTER},loudnorm=I={TARGET_LUFS}:TP={PEAK_CEILING_DBFS}:"
            "LRA=50:print_format=json",
            "-f",
            "null",
            "-",
        ],
        should_cancel=should_cancel,
        capture_stderr=True,
    )
    return parse_loudness(stderr)


def valid_normalization_records(records: Any, tracks: list[str]) -> bool:
    """Validate cached provenance without rescanning unchanged media."""
    if not isinstance(records, dict) or set(records) != set(tracks):
        return False
    for record in records.values():
        if not isinstance(record, dict) or not _finite_number(record.get("gain_db")):
            return False
        try:
            measurement = LoudnessMeasurement(record["integrated_lufs"], record["true_peak_dbfs"])
            if compute_normalization(measurement).to_record() != record:
                return False
        except (KeyError, FfmpegError):
            return False
    return True
