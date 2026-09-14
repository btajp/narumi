"""Fixed ffmpeg mixing recipe preserving the common recording timeline."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from narumi.errors import InvalidArgumentError
from narumi.playback._loudness import AUDIO_FORMAT_FILTER
from narumi.playback._process import CancelCheck, run_media_tool
from narumi.preprocess.ffmpeg import FfmpegError, ffmpeg_path, ffprobe_path

SAMPLE_RATE = 48000


@dataclass(frozen=True)
class MediaStream:
    codec: str
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


def _seconds(value: Any) -> float | None:
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def inspect_media(path: Path, *, should_cancel: CancelCheck = None) -> dict[str, MediaStream]:
    raw = run_media_tool(
        [
            str(ffprobe_path()),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        should_cancel=should_cancel,
        timeout=30,
    )
    try:
        data = json.loads(raw)
        streams = data["streams"]
        format_end = _seconds(data.get("format", {}).get("duration"))
        result: dict[str, MediaStream] = {}
        for stream in streams:
            kind = stream.get("codec_type")
            if kind not in {"video", "audio"}:
                continue
            if kind in result:
                raise ValueError(f"multiple {kind} streams")
            start = _seconds(stream.get("start_time", 0))
            duration = _seconds(stream.get("duration"))
            if duration is None and format_end is not None and start is not None:
                duration = format_end - start
            codec = stream.get("codec_name")
            if start is None or duration is None or duration <= 0 or not codec:
                raise ValueError("stream has no finite positive duration or codec")
            result[kind] = MediaStream(codec=codec, start=start, duration=duration)
        if not result:
            raise ValueError("no audio or video stream")
        return result
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise FfmpegError(
            f"invalid recording media: {path.name}", details={"path": str(path)}
        ) from exc


def mix_recording(
    video: Path,
    audio: list[Path],
    output: Path,
    *,
    gains_db: list[float] | None = None,
    should_cancel: CancelCheck = None,
) -> None:
    gains = [0.0] * len(audio) if gains_db is None else gains_db
    if not audio or len(gains) != len(audio) or not all(math.isfinite(gain) for gain in gains):
        raise InvalidArgumentError("playback needs one finite gain per audio track")
    args = [
        str(ffmpeg_path()),
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-copyts",
        "-err_detect",
        "explode",
        "-i",
        str(video),
    ]
    for path in audio:
        args.extend(["-err_detect", "explode", "-i", str(path)])
    filters = [
        f"[{index}:a:0]{AUDIO_FORMAT_FILTER},volume={gain:.6f}dB[a{index}]"
        for index, gain in enumerate(gains, start=1)
    ]
    labels = "".join(f"[a{index}]" for index in range(1, len(audio) + 1))
    # Fixed averaging avoids clipping and the gain jump when a shorter source ends.
    filters.append(
        f"{labels}amix=inputs={len(audio)}:duration=longest:"
        f"dropout_transition=0:normalize=0,volume={1 / len(audio):.8f}[mixed]"
    )
    args.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "0:v:0",
            "-map",
            "[mixed]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(output),
            "-map",
            "0:v:0",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    # No -shortest: either media source may outlast the other.
    # Decode a second video output to null so corrupt packets cannot pass through stream copy.
    run_media_tool(args, should_cancel=should_cancel)


def validate_output(
    output: Path, video: MediaStream, audio: list[MediaStream], *, should_cancel: CancelCheck
) -> None:
    streams = inspect_media(output, should_cancel=should_cancel)
    if set(streams) != {"video", "audio"}:
        raise FfmpegError("playback must contain one video and one mixed audio stream")
    rendered_video, rendered_audio = streams["video"], streams["audio"]
    if rendered_video.codec != video.codec or rendered_audio.codec != "aac":
        raise FfmpegError("playback codecs do not match the recording recipe")
    if abs(rendered_video.start - max(0, video.start)) > 0.15:
        raise FfmpegError("playback video start time changed")
    if rendered_video.end < video.end - 0.15:
        raise FfmpegError("playback truncated the screen recording")
    if rendered_audio.end < max(stream.end for stream in audio) - 0.1:
        raise FfmpegError("playback truncated the audio recording")
