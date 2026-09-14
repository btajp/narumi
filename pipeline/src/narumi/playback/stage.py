"""Prepare a playback artifact while keeping the separate recording originals."""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from narumi.bundle import ArtifactRecord, Bundle, StageResult, sha256_params, utc_now_iso
from narumi.bundle.manifest import Producer
from narumi.errors import InvalidArgumentError, NotFoundError
from narumi.playback._limiter import ensure_limiter_supported
from narumi.playback._loudness import (
    NORMALIZATION_SETTINGS,
    compute_normalization,
    measure_loudness,
    valid_normalization_records,
)
from narumi.playback._media import MediaStream, inspect_media, mix_recording, validate_output
from narumi.playback._process import CancelCheck, check_cancelled
from narumi.preprocess.ffmpeg import ffmpeg_version

ARTIFACT_KEY = "recording/playback"
OUTPUT_PATH = "playback/recording.mp4"
RECIPE_VERSION = 2


def _local_path(bundle: Bundle, relative: str) -> Path:
    path = bundle.abspath(relative).resolve()
    if not path.is_relative_to(bundle.path.resolve()):
        raise InvalidArgumentError("recording path must stay within its meeting bundle")
    return path


def _output_path(bundle: Bundle) -> Path:
    expected = bundle.path.resolve() / OUTPUT_PATH
    try:
        if expected.is_symlink() or expected.resolve() != expected:
            raise InvalidArgumentError("playback output must use its dedicated bundle path")
    except (OSError, RuntimeError) as exc:
        raise InvalidArgumentError("playback output path cannot be resolved") from exc
    return expected


def _sources(bundle: Bundle) -> dict[str, Path]:
    tracks = bundle.manifest.recording.tracks
    screen = tracks.get("screen")
    if screen is None or screen.discarded:
        raise InvalidArgumentError("no retained screen recording is available")
    sources = {"screen": _local_path(bundle, screen.path)}
    for name in ("mic", "system"):
        record = tracks.get(name)
        if record is not None and not record.discarded:
            sources[name] = _local_path(bundle, record.path)
    if len(sources) == 1:
        raise InvalidArgumentError("no retained microphone or system audio is available")
    for name, path in sources.items():
        if not path.is_file():
            raise NotFoundError(
                f"recording track file missing: {name}", details={"track": name, "path": str(path)}
            )
        if path.stat().st_size == 0:
            raise InvalidArgumentError(f"recording track is empty: {name}")
    return sources


def _file_hash(path: Path, should_cancel: CancelCheck) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1 << 20):
                check_cancelled(should_cancel)
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise NotFoundError(
            "recording media disappeared during playback preparation", details={"path": str(path)}
        ) from exc
    return digest.hexdigest()


def playback_info(bundle: Bundle) -> dict[str, Any] | None:
    """Cheap display metadata; full input/output integrity is checked by run_playback."""
    record = bundle.artifact(ARTIFACT_KEY)
    if record is None or record.path != OUTPUT_PATH:
        return None
    tracks = bundle.manifest.recording.tracks
    if any(
        tracks.get(key.removeprefix("tracks/")) is None
        or tracks[key.removeprefix("tracks/")].discarded
        for key in record.inputs
    ):
        return None
    try:
        path = _output_path(bundle)
        if not path.is_file() or path.stat().st_size == 0:
            return None
        if any(path == _local_path(bundle, track.path) for track in tracks.values()):
            return None
        return {"path": str(path), "sha256": record.sha256, "bytes": path.stat().st_size}
    except (OSError, InvalidArgumentError):
        return None


def run_playback(bundle: Bundle, *, should_cancel: CancelCheck = None) -> StageResult:
    """Reuse only verified artifacts; publish a new MP4 only after successful validation."""
    check_cancelled(should_cancel)
    if bundle.manifest.status == "recording":
        raise InvalidArgumentError("stop the recording before preparing playback")
    sources = _sources(bundle)
    output = _output_path(bundle)
    if output in sources.values():
        raise InvalidArgumentError("playback output must not replace an original recording track")
    inputs = {f"tracks/{name}": _file_hash(path, should_cancel) for name, path in sources.items()}
    streams: dict[str, MediaStream] = {}
    for name, path in sources.items():
        kind = "video" if name == "screen" else "audio"
        stream = inspect_media(path, should_cancel=should_cancel).get(kind)
        if stream is None:
            raise InvalidArgumentError(f"recording track {name} contains no {kind} stream")
        streams[name] = stream
    params = {
        "recipe_version": RECIPE_VERSION,
        "video_codec": "copy",
        "audio_codec": "aac",
        "audio_bitrate": 192000,
        "sample_rate": 48000,
        "channels": 2,
        "mix": "fixed_average",
        "timeline": "copyts_aresample_first_pts_zero",
        "duration": "longest",
        "verify_video_decode": True,
        "normalization": dict(NORMALIZATION_SETTINGS),
        "streams": {name: asdict(info) for name, info in streams.items()},
    }
    audio_names = [name for name in sources if name != "screen"]
    producer = Producer(name="ffmpeg", version=ffmpeg_version())
    ensure_limiter_supported(producer.version)
    existing = bundle.artifact(ARTIFACT_KEY)
    if (
        existing is not None
        and existing.path == OUTPUT_PATH
        and existing.inputs == inputs
        and {key: value for key, value in existing.params.items() if key != "normalization_tracks"}
        == params
        and valid_normalization_records(existing.params.get("normalization_tracks"), audio_names)
        and existing.params_hash == sha256_params(existing.params)
        and existing.producer == producer
        and output.is_file()
        and _file_hash(output, should_cancel) == existing.sha256
    ):
        return StageResult(key=ARTIFACT_KEY, path=output, record=existing, skipped=True)
    normalizations = {
        name: compute_normalization(measure_loudness(sources[name], should_cancel=should_cancel))
        for name in audio_names
    }
    params["normalization_tracks"] = {
        name: normalization.to_record() for name, normalization in normalizations.items()
    }
    params_hash = sha256_params(params)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".recording-", dir=output.parent) as work:
        candidate = Path(work) / "recording.mp4"
        mix_recording(
            sources["screen"],
            [sources[name] for name in audio_names],
            candidate,
            gains_db=[normalizations[name].gain_db for name in audio_names],
            should_cancel=should_cancel,
        )
        if not candidate.is_file() or candidate.stat().st_size == 0:
            raise InvalidArgumentError("playback preparation produced no media")
        validate_output(
            candidate,
            streams["screen"],
            [streams[name] for name in audio_names],
            should_cancel=should_cancel,
        )
        output_hash = _file_hash(candidate, should_cancel)
        for name, source in sources.items():
            if _file_hash(source, should_cancel) != inputs[f"tracks/{name}"]:
                raise InvalidArgumentError("recording source changed during playback preparation")
        check_cancelled(should_cancel)
        candidate.replace(output)
    record = ArtifactRecord(
        path=OUTPUT_PATH,
        sha256=output_hash,
        inputs=inputs,
        params=params,
        params_hash=params_hash,
        producer=producer,
        created_at=utc_now_iso(),
    )
    bundle.manifest.artifacts[ARTIFACT_KEY] = record
    bundle.save()
    return StageResult(key=ARTIFACT_KEY, path=output, record=record, skipped=False)
