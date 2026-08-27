"""Key-slide detection: screen frames → pHash dedup → ``preprocess/slides.json``.

Deterministic stage (絶対原則 2): ffmpeg extracts candidate frames on a fixed interval + scene
threshold, consecutive frames whose perceptual distance stays within ``distance_threshold`` are
folded into one key slide, and each kept frame is copied to ``preprocess/slides/<id>.png``.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from narumi.bundle import Bundle, StageResult
from narumi.errors import InvalidArgumentError, NotFoundError
from narumi.preprocess.ffmpeg import extract_frames, probe_duration
from narumi.slides.phash import hamming, phash

SCREEN_TRACK = "screen"
SLIDES_KEY = "preprocess/slides"
"""Manifest artifact key of the key-slide index."""
SLIDES_OUTPUT = "preprocess/slides.json"
"""Bundle-relative path of the key-slide index (the hashed stage artifact)."""
FRAMES_DIR = "preprocess/frames"
SLIDES_DIR = "preprocess/slides"
SLIDE_FRAME_WIDTH = 640
DEFAULT_INTERVAL_SEC = 5.0
DEFAULT_SCENE_THRESHOLD = 0.08
DEFAULT_DISTANCE_THRESHOLD = 10
"""Hamming distance (of 128 bits) above which a frame starts a new key slide."""
SLIDES_VERSION = "1"
PRODUCER_NAME = "slides"

_FRAME_NAME_RE = re.compile(r"^frame_(\d{4})_(\d{8})\.png$")
_STALE_SLIDE_GLOB = "slide-*.png"


class SlideEntry(BaseModel):
    """One key slide. ``frame`` / ``path`` are bundle-relative; times are recording seconds."""

    model_config = ConfigDict(extra="forbid")

    id: str
    frame: str
    path: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    phash: str


class SlideIndex(BaseModel):
    """Schema of ``preprocess/slides.json``."""

    model_config = ConfigDict(extra="forbid")

    slides: list[SlideEntry] = Field(default_factory=list)


def slide_id(index: int) -> str:
    """Key-slide id (1-based): ``slide-0001`` …"""
    return f"slide-{index:04d}"


def frame_time_sec(frame: Path | str) -> float:
    """Recording time in seconds encoded in a ``frame_<idx>_<ms>.png`` file name."""
    name = Path(frame).name
    match = _FRAME_NAME_RE.match(name)
    if match is None:
        raise InvalidArgumentError(f"not a frame file name: {name}", details={"name": name})
    return int(match.group(2)) / 1000.0


def list_frames(bundle: Bundle) -> list[Path]:
    """Extracted candidate frames in ``preprocess/frames`` in chronological order."""
    frames_dir = bundle.abspath(FRAMES_DIR)
    if not frames_dir.is_dir():
        return []
    return sorted(p for p in frames_dir.iterdir() if _FRAME_NAME_RE.match(p.name))


def detect_keyslides(
    frames: Sequence[Path],
    *,
    distance_threshold: int = DEFAULT_DISTANCE_THRESHOLD,
    duration: float | None = None,
) -> list[tuple[Path, str, float, float]]:
    """Fold consecutive similar frames into key slides → ``(frame, phash, start, end)``.

    A frame starts a new key slide when its Hamming distance to the *last kept* slide exceeds
    ``distance_threshold``. ``end`` is the next slide's ``start``; the last slide ends at
    ``duration`` (or the last frame's timestamp when unknown).
    """
    if distance_threshold < 0:
        raise InvalidArgumentError(
            "distance_threshold must be >= 0", details={"value": distance_threshold}
        )
    if not frames:
        return []
    kept: list[tuple[Path, str, float]] = []
    for frame in frames:
        digest = phash(frame)
        if not kept or hamming(digest, kept[-1][1]) > distance_threshold:
            kept.append((frame, digest, frame_time_sec(frame)))
    last_time = frame_time_sec(frames[-1])
    results: list[tuple[Path, str, float, float]] = []
    for index, (frame, digest, start) in enumerate(kept):
        if index + 1 < len(kept):
            end = kept[index + 1][2]
        else:
            end = max(start, last_time, duration or 0.0)
        results.append((frame, digest, round(start, 3), round(end, 3)))
    return results


def load_slides(bundle: Bundle) -> list[SlideEntry]:
    """Key slides recorded by :func:`run_slides` (``NotFoundError`` when the stage never ran)."""
    record = bundle.artifact(SLIDES_KEY)
    if record is None:
        raise NotFoundError("key slides not extracted yet", details={"key": SLIDES_KEY})
    return SlideIndex.model_validate(bundle.read_json(record.path)).slides


def run_slides(
    bundle: Bundle,
    *,
    force: bool = False,
    interval_sec: float = DEFAULT_INTERVAL_SEC,
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
    distance_threshold: int = DEFAULT_DISTANCE_THRESHOLD,
) -> StageResult | None:
    """Extract key slides from the screen track into ``preprocess/slides.json`` + PNG copies.

    Returns ``None`` — skipped, no artifact recorded — when the bundle has no screen track (a
    zero-byte screen file counts: the recorder registered the track but never delivered video),
    or the track was discarded before this stage ever ran. A track discarded *after* a
    successful run keeps its existing artifact (mirrors ``run_preprocess``: 破棄後は
    preprocess/ 以降で再生成). Stage identity: key ``preprocess/slides``, inputs
    ``{"tracks/screen": sha}``, params ``{interval, scene, threshold, version}``.
    """
    track = bundle.manifest.recording.tracks.get(SCREEN_TRACK)
    if track is None:
        return None
    empty = bundle.abspath(track.path)
    if not track.discarded and empty.is_file() and empty.stat().st_size == 0:
        return None
    if track.discarded:
        record = bundle.artifact(SLIDES_KEY)
        if record is not None and bundle.abspath(record.path).exists():
            return StageResult(
                key=SLIDES_KEY, path=bundle.abspath(record.path), record=record, skipped=True
            )
        return None
    if track.sha256 is None:
        raise InvalidArgumentError(
            "screen track has no sha256; finalize the recording before extracting slides",
            details={"track": SCREEN_TRACK, "path": track.path},
        )
    video = bundle.abspath(track.path)
    if not video.is_file():
        raise NotFoundError(
            f"screen track file missing: {track.path}",
            details={"track": SCREEN_TRACK, "path": str(video)},
        )
    if distance_threshold < 0:
        raise InvalidArgumentError(
            "distance_threshold must be >= 0", details={"value": distance_threshold}
        )

    def produce(out: Path) -> None:
        frames = extract_frames(
            video,
            bundle.abspath(FRAMES_DIR),
            interval_sec=interval_sec,
            scene_threshold=scene_threshold,
            width=SLIDE_FRAME_WIDTH,
        )
        if not frames:
            raise InvalidArgumentError(
                "screen track produced no frames; is the video readable?",
                details={"path": track.path},
            )
        keyslides = detect_keyslides(
            frames, distance_threshold=distance_threshold, duration=probe_duration(video)
        )
        slides_dir = bundle.abspath(SLIDES_DIR)
        slides_dir.mkdir(parents=True, exist_ok=True)
        for stale in slides_dir.glob(_STALE_SLIDE_GLOB):
            stale.unlink()
        entries: list[SlideEntry] = []
        for index, (frame, digest, start, end) in enumerate(keyslides, start=1):
            sid = slide_id(index)
            target = slides_dir / f"{sid}.png"
            shutil.copyfile(frame, target)
            entries.append(
                SlideEntry(
                    id=sid,
                    frame=f"{FRAMES_DIR}/{frame.name}",
                    path=f"{SLIDES_DIR}/{sid}.png",
                    start=start,
                    end=end,
                    phash=digest,
                )
            )
        bundle.write_json(SLIDES_OUTPUT, SlideIndex(slides=entries))

    return bundle.run_stage(
        SLIDES_KEY,
        inputs={f"tracks/{SCREEN_TRACK}": track.sha256},
        params={
            "interval": interval_sec,
            "scene": scene_threshold,
            "threshold": distance_threshold,
            "version": SLIDES_VERSION,
        },
        producer=(PRODUCER_NAME, SLIDES_VERSION),
        output=SLIDES_OUTPUT,
        fn=produce,
        force=force,
    )
