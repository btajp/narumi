from __future__ import annotations

import re
from pathlib import Path

import pytest
from narumi.bundle import Bundle, TrackRecord, sha256_file
from narumi.errors import InvalidArgumentError, NotFoundError
from narumi.models import MergedSegment
from narumi.preprocess.ffmpeg import ffmpeg_path, run_tool
from narumi.slides import (
    DEFAULT_DISTANCE_THRESHOLD,
    SLIDES_KEY,
    SLIDES_OUTPUT,
    SlideEntry,
    copy_slides_to_minutes,
    detect_keyslides,
    frame_time_sec,
    hamming,
    list_frames,
    load_slides,
    phash,
    phash_image,
    run_slides,
    select_slides_for_minutes,
)
from PIL import Image

from .media_fixtures import make_bundle_with_tracks

HASH_RE = re.compile(r"^[0-9a-f]{32}$")
SCENE_SECONDS = 6.0


# ----------------------------------------------------------------------------- fixtures
def make_screen_video(path: Path, *, seconds_per_scene: float = SCENE_SECONDS) -> Path:
    """Two-scene screen recording: a flat dark-blue slide, then animated ``testsrc2``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    run_tool(
        [
            str(ffmpeg_path()),
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x1f4e79:size=320x240:rate=10:duration={seconds_per_scene:g}",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x240:rate=10:duration={seconds_per_scene:g}",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    return path


def add_screen_track(bundle: Bundle, *, seconds_per_scene: float = SCENE_SECONDS) -> Path:
    rel = "tracks/screen.mp4"
    video = make_screen_video(bundle.abspath(rel), seconds_per_scene=seconds_per_scene)
    bundle.manifest.recording.tracks["screen"] = TrackRecord(
        path=rel,
        sha256=sha256_file(video),
        bytes=video.stat().st_size,
        duration_sec=seconds_per_scene * 2,
    )
    bundle.save()
    return video


def screen_bundle(tmp_path: Path) -> Bundle:
    bundle = make_bundle_with_tracks(tmp_path, seconds=12.0)
    add_screen_track(bundle)
    return bundle


def solid(gray: int) -> Image.Image:
    return Image.new("L", (64, 64), gray)


def gradient() -> Image.Image:
    image = Image.new("L", (64, 64))
    image.putdata([x * 4 for _ in range(64) for x in range(64)])
    return image


def checkerboard(block: int = 8) -> Image.Image:
    image = Image.new("L", (64, 64))
    image.putdata(
        [255 if (x // block + y // block) % 2 else 0 for y in range(64) for x in range(64)]
    )
    return image


# ----------------------------------------------------------------------------- phash
def test_phash_format_and_determinism(tmp_path: Path):
    digest = phash_image(checkerboard())
    assert HASH_RE.match(digest)
    assert phash_image(checkerboard()) == digest
    saved = tmp_path / "board.png"
    checkerboard().save(saved)
    assert phash(saved) == digest


def test_phash_identical_and_near_identical_are_close():
    assert hamming(phash_image(gradient()), phash_image(gradient())) == 0
    # flat frames with a tiny luminance change stay on the same side of every threshold
    assert hamming(phash_image(solid(100)), phash_image(solid(105))) == 0


def test_phash_distinguishes_different_content():
    threshold = DEFAULT_DISTANCE_THRESHOLD
    # flat black vs flat white: the DCT half is blind here, the fixed-threshold half is not
    assert hamming(phash_image(solid(0)), phash_image(solid(255))) > threshold
    assert hamming(phash_image(gradient()), phash_image(checkerboard())) > threshold


def test_phash_missing_file(tmp_path: Path):
    with pytest.raises(NotFoundError):
        phash(tmp_path / "missing.png")


def test_hamming_validates_inputs():
    with pytest.raises(InvalidArgumentError):
        hamming("ab", "abcd")
    with pytest.raises(InvalidArgumentError):
        hamming("", "")
    with pytest.raises(InvalidArgumentError):
        hamming("zz", "ab")
    assert hamming("00", "01") == 1


def test_frame_time_sec():
    assert frame_time_sec("frame_0002_00005000.png") == 5.0
    with pytest.raises(InvalidArgumentError):
        frame_time_sec("nope.png")


# ----------------------------------------------------------------------------- detection
def test_run_slides_extracts_keyslides(tmp_path: Path):
    bundle = screen_bundle(tmp_path)
    result = run_slides(bundle)
    assert result is not None and not result.skipped
    assert result.key == SLIDES_KEY

    record = bundle.artifact(SLIDES_KEY)
    assert record is not None
    assert record.path == SLIDES_OUTPUT
    assert record.inputs == {"tracks/screen": bundle.manifest.recording.tracks["screen"].sha256}
    assert record.params == {
        "interval": 5.0,
        "scene": 0.08,
        "threshold": DEFAULT_DISTANCE_THRESHOLD,
        "version": "1",
    }
    assert record.producer.name == "slides"

    slides = load_slides(bundle)
    assert len(slides) >= 2
    assert slides[0].id == "slide-0001"
    assert slides[0].start == 0.0
    starts = [s.start for s in slides]
    assert starts == sorted(starts)
    for current, following in zip(slides, slides[1:], strict=False):
        assert current.end == following.start
    assert slides[-1].end >= slides[-1].start
    for slide in slides:
        assert HASH_RE.match(slide.phash)
        assert slide.path == f"preprocess/slides/{slide.id}.png"
        assert bundle.abspath(slide.path).is_file()
        assert bundle.abspath(slide.frame).is_file()
    # the scene change (flat slide → testsrc2) is a distinct key slide near t=6
    assert hamming(slides[0].phash, slides[1].phash) > DEFAULT_DISTANCE_THRESHOLD
    assert any(abs(s.start - SCENE_SECONDS) < 1.5 for s in slides[1:])
    assert list_frames(bundle), "candidate frames stay in preprocess/frames"


def test_run_slides_skips_then_forces(tmp_path: Path):
    bundle = screen_bundle(tmp_path)
    first = run_slides(bundle)
    assert first is not None
    again = run_slides(Bundle.open(bundle.path))
    assert again is not None and again.skipped
    assert again.record == first.record
    forced = run_slides(bundle, force=True)
    assert forced is not None and not forced.skipped
    assert forced.record.sha256 == first.record.sha256  # same inputs → same bytes


def test_run_slides_without_screen_track(tmp_path: Path):
    bundle = make_bundle_with_tracks(tmp_path)
    assert run_slides(bundle) is None
    assert bundle.artifact(SLIDES_KEY) is None


def test_run_slides_discarded_track(tmp_path: Path):
    bundle = screen_bundle(tmp_path)
    first = run_slides(bundle)
    assert first is not None
    track = bundle.manifest.recording.tracks["screen"]
    bundle.abspath(track.path).unlink()
    track.discarded = True
    bundle.save()
    kept = run_slides(bundle, force=True)
    assert kept is not None and kept.skipped
    assert kept.record == first.record
    # discarded and the artifact gone too → plain skip with no artifact
    bundle.abspath(SLIDES_OUTPUT).unlink()
    assert run_slides(bundle) is None


def test_run_slides_discarded_before_first_run(tmp_path: Path):
    bundle = screen_bundle(tmp_path)
    track = bundle.manifest.recording.tracks["screen"]
    bundle.abspath(track.path).unlink()
    track.discarded = True
    bundle.save()
    assert run_slides(bundle) is None
    assert bundle.artifact(SLIDES_KEY) is None


def test_run_slides_input_errors(tmp_path: Path):
    bundle = screen_bundle(tmp_path)
    with pytest.raises(InvalidArgumentError):
        run_slides(bundle, distance_threshold=-1)
    bundle.manifest.recording.tracks["screen"].sha256 = None
    with pytest.raises(InvalidArgumentError):
        run_slides(bundle)
    other = screen_bundle(tmp_path / "second")
    other.abspath("tracks/screen.mp4").unlink()
    with pytest.raises(NotFoundError):
        run_slides(other)


def test_detect_keyslides_pure(tmp_path: Path):
    a = tmp_path / "frame_0000_00000000.png"
    b = tmp_path / "frame_0001_00005000.png"
    c = tmp_path / "frame_0002_00010000.png"
    solid(0).save(a)
    solid(3).save(b)  # visually the flat slide again
    solid(255).save(c)
    keyslides = detect_keyslides([a, b, c], distance_threshold=10, duration=14.0)
    assert [(frame.name, start, end) for frame, _, start, end in keyslides] == [
        ("frame_0000_00000000.png", 0.0, 10.0),
        ("frame_0002_00010000.png", 10.0, 14.0),
    ]
    assert detect_keyslides([], distance_threshold=10) == []
    with pytest.raises(InvalidArgumentError):
        detect_keyslides([a], distance_threshold=-1)


# ----------------------------------------------------------------------------- embedding
def _slide(sid: str, start: float, end: float) -> SlideEntry:
    return SlideEntry(
        id=sid,
        frame=f"preprocess/frames/frame_0000_{int(start * 1000):08d}.png",
        path=f"preprocess/slides/{sid}.png",
        start=start,
        end=end,
        phash="0" * 32,
    )


def _segment(sid: str, start: float, end: float) -> MergedSegment:
    return MergedSegment(id=sid, start=start, end=end, text="…")


def test_select_slides_for_minutes():
    slides = [_slide("slide-0002", 6.0, 20.0), _slide("slide-0001", 0.0, 6.0)]
    segments = [
        _segment("m-00001", 3.0, 5.0),
        _segment("m-00002", 5.5, 9.0),
        _segment("m-00003", 12.0, 15.0),
    ]
    anchored = select_slides_for_minutes(slides, segments)
    assert [(slide.id, anchor) for slide, anchor in anchored] == [
        ("slide-0001", None),  # before every segment
        ("slide-0002", "m-00002"),  # last segment that started at or before 6.0
    ]
    late = _slide("slide-0003", 30.0, 40.0)
    assert select_slides_for_minutes([late], segments)[0][1] == "m-00003"
    assert select_slides_for_minutes([], segments) == []
    assert select_slides_for_minutes(slides, []) == [
        (slides[1], None),
        (slides[0], None),
    ]


def test_copy_slides_to_minutes(tmp_path: Path):
    bundle = screen_bundle(tmp_path)
    run_slides(bundle)
    slides = load_slides(bundle)
    refs = copy_slides_to_minutes(bundle, 1)
    assert refs == {s.id: f"slides/{s.id}.png" for s in slides}
    for slide in slides:
        copied = bundle.path / "minutes" / "v1" / "slides" / f"{slide.id}.png"
        assert copied.is_file()
        assert copied.read_bytes() == bundle.abspath(slide.path).read_bytes()
    with pytest.raises(InvalidArgumentError):
        copy_slides_to_minutes(bundle, 0)
    bundle.abspath(slides[0].path).unlink()
    with pytest.raises(NotFoundError):
        copy_slides_to_minutes(bundle, 2)


def test_copy_slides_requires_stage(tmp_path: Path):
    bundle = make_bundle_with_tracks(tmp_path)
    with pytest.raises(NotFoundError):
        copy_slides_to_minutes(bundle, 1)
