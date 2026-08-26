"""``run_preprocess``: one ``preprocess/audio/<track>`` artifact per available audio track."""

from __future__ import annotations

from pathlib import Path

from narumi.bundle import Bundle, StageResult
from narumi.errors import InvalidArgumentError, NotFoundError
from narumi.preprocess.ffmpeg import extract_audio, ffmpeg_version

AUDIO_TRACKS: tuple[str, ...] = ("mic", "system")
SAMPLE_RATE = 16000
CHANNELS = 1
CODEC = "pcm_s16le"
PRODUCER_NAME = "ffmpeg"


def audio_artifact_key(track: str) -> str:
    """Manifest artifact key of the preprocessed wav (``preprocess/audio/<track>``)."""
    return f"preprocess/audio/{track}"


def audio_output_path(track: str) -> str:
    """Bundle-relative path of the preprocessed wav (``preprocess/<track>.16k.wav``)."""
    return f"preprocess/{track}.16k.wav"


def _kept_artifact(bundle: Bundle, key: str) -> StageResult | None:
    record = bundle.artifact(key)
    if record is None:
        return None
    path = bundle.abspath(record.path)
    if not path.exists():
        return None
    return StageResult(key=key, path=path, record=record, skipped=True)


def _extract(src: Path, out: Path) -> None:
    extract_audio(src, out, sample_rate=SAMPLE_RATE, channels=CHANNELS)


def run_preprocess(bundle: Bundle, *, force: bool = False) -> list[StageResult]:
    """Extract ``preprocess/<track>.16k.wav`` for every ``mic`` / ``system`` track.

    Stage inputs are ``{"tracks/<track>": <track sha256>}``; params are the fixed audio format.
    A discarded track cannot be re-extracted, so its existing artifact is kept as-is (also under
    ``force``). Raises ``InvalidArgumentError`` when the bundle ends up with no audio artifact,
    or when a live track has not been hashed yet.
    """
    results: list[StageResult] = []
    tracks = bundle.manifest.recording.tracks
    for track in AUDIO_TRACKS:
        record = tracks.get(track)
        if record is None:
            continue
        key = audio_artifact_key(track)
        if record.discarded:
            kept = _kept_artifact(bundle, key)
            if kept is not None:
                results.append(kept)
            continue
        if record.sha256 is None:
            raise InvalidArgumentError(
                f"track {track!r} has no sha256; finalize the recording before preprocessing",
                details={"track": track, "path": record.path},
            )
        src = bundle.abspath(record.path)
        if not src.is_file():
            raise NotFoundError(
                f"track file missing: {record.path}", details={"track": track, "path": str(src)}
            )
        results.append(
            bundle.run_stage(
                key,
                inputs={f"tracks/{track}": record.sha256},
                params={"sample_rate": SAMPLE_RATE, "channels": CHANNELS, "codec": CODEC},
                producer=(PRODUCER_NAME, ffmpeg_version()),
                output=audio_output_path(track),
                fn=lambda out, src=src: _extract(src, out),
                force=force,
            )
        )
    if not results:
        raise InvalidArgumentError(
            "no audio track (mic / system) available in this bundle",
            details={"tracks": sorted(tracks)},
        )
    return results
