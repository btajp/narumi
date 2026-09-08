from __future__ import annotations

import pytest
from narumi.bundle import Bundle, TrackRecord, sha256_file
from narumi.errors import CancelledError, InvalidArgumentError, NotFoundError
from narumi.playback import ARTIFACT_KEY, OUTPUT_PATH, playback_info, run_playback
from narumi.playback._media import MediaStream
from narumi.preprocess.ffmpeg import FfmpegError


@pytest.fixture
def recorded_bundle(tmp_path):
    bundle = Bundle.create(tmp_path, meeting_name="Playback tests")
    for name, suffix in (("screen", "mp4"), ("mic", "m4a"), ("system", "m4a")):
        path = bundle.abspath(f"tracks/{name}.{suffix}")
        path.write_bytes(f"original {name}".encode())
        bundle.manifest.recording.tracks[name] = TrackRecord(
            path=bundle.relpath(path), sha256=sha256_file(path), bytes=path.stat().st_size
        )
    bundle.manifest.status = "recorded"
    bundle.save()
    return bundle


@pytest.fixture
def fake_media(monkeypatch):
    calls = []

    def inspect(path, **kwargs):
        if path.suffix == ".m4a":
            return {"audio": MediaStream("aac", 0, 3)}
        return {"video": MediaStream("h264", 0, 3)}

    def mix(video, audio, output, **kwargs):
        calls.append((video, audio, output))
        output.write_bytes(video.read_bytes() + b"|" + b"|".join(p.read_bytes() for p in audio))

    monkeypatch.setattr("narumi.playback.stage.inspect_media", inspect)
    monkeypatch.setattr("narumi.playback.stage.mix_recording", mix)
    monkeypatch.setattr("narumi.playback.stage.validate_output", lambda *args, **kwargs: None)
    monkeypatch.setattr("narumi.playback.stage.ffmpeg_version", lambda: "9.0")
    return calls


def test_creates_atomic_artifact_and_reuses_verified_content(recorded_bundle, fake_media):
    bundle = recorded_bundle
    originals = {
        name: sha256_file(bundle.abspath(r.path))
        for name, r in bundle.manifest.recording.tracks.items()
    }
    result = run_playback(bundle)
    assert result.key == ARTIFACT_KEY
    assert result.path.is_absolute() and result.path == bundle.abspath(OUTPUT_PATH)
    assert result.record.inputs == {f"tracks/{name}": digest for name, digest in originals.items()}
    assert result.record.producer.name == "ffmpeg"
    assert result.record.producer.version == "9.0"
    assert result.record.params["mix"] == "fixed_average"
    assert result.record.sha256 == sha256_file(result.path)
    assert not result.skipped
    assert playback_info(bundle) == {
        "path": str(result.path),
        "sha256": result.record.sha256,
        "bytes": result.path.stat().st_size,
    }
    again = run_playback(Bundle.open(bundle.path))
    assert again.skipped and again.record == result.record and len(fake_media) == 1
    assert list(result.path.parent.iterdir()) == [result.path]
    assert originals == {
        name: sha256_file(bundle.abspath(r.path))
        for name, r in bundle.manifest.recording.tracks.items()
    }


def test_uses_actual_input_bytes_instead_of_stale_manifest_hash(recorded_bundle, fake_media):
    first = run_playback(recorded_bundle)
    mic = recorded_bundle.abspath("tracks/mic.m4a")
    mic.write_bytes(b"changed original")
    second = run_playback(recorded_bundle)
    assert not second.skipped and len(fake_media) == 2
    assert second.record.inputs["tracks/mic"] == sha256_file(mic)
    assert second.record.inputs != first.record.inputs


def test_repairs_same_size_output_corruption(recorded_bundle, fake_media):
    first = run_playback(recorded_bundle)
    first.path.write_bytes(b"x" * first.path.stat().st_size)
    second = run_playback(recorded_bundle)
    assert not second.skipped and len(fake_media) == 2
    assert second.record.sha256 == first.record.sha256


def test_producer_and_recipe_changes_invalidate_cache(recorded_bundle, fake_media, monkeypatch):
    run_playback(recorded_bundle)
    monkeypatch.setattr("narumi.playback.stage.ffmpeg_version", lambda: "9.1")
    assert not run_playback(recorded_bundle).skipped
    monkeypatch.setattr("narumi.playback.stage.RECIPE_VERSION", 2)
    assert not run_playback(recorded_bundle).skipped
    assert len(fake_media) == 3


@pytest.mark.parametrize("failure", [FfmpegError("broken media"), CancelledError("cancelled")])
def test_failure_keeps_previous_output_and_cleans_temporary_file(
    recorded_bundle, fake_media, monkeypatch, failure
):
    previous = run_playback(recorded_bundle)
    old_bytes = previous.path.read_bytes()
    recorded_bundle.abspath("tracks/mic.m4a").write_bytes(b"new input")

    def failed_mix(video, audio, output, **kwargs):
        output.write_bytes(b"partial output")
        raise failure

    monkeypatch.setattr("narumi.playback.stage.mix_recording", failed_mix)
    with pytest.raises(type(failure)):
        run_playback(recorded_bundle)
    assert previous.path.read_bytes() == old_bytes
    assert recorded_bundle.artifact(ARTIFACT_KEY) == previous.record
    assert list(previous.path.parent.iterdir()) == [previous.path]


def test_rejects_source_change_while_ffmpeg_runs(recorded_bundle, fake_media, monkeypatch):
    def changed_mix(video, audio, output, **kwargs):
        output.write_bytes(b"completed output")
        audio[0].write_bytes(b"changed while reading")

    monkeypatch.setattr("narumi.playback.stage.mix_recording", changed_mix)
    with pytest.raises(InvalidArgumentError, match="changed during"):
        run_playback(recorded_bundle)
    assert recorded_bundle.artifact(ARTIFACT_KEY) is None
    assert not recorded_bundle.abspath(OUTPUT_PATH).exists()
    assert not list(recorded_bundle.abspath("playback").iterdir())


def test_rejects_invalid_final_output_before_publishing(recorded_bundle, fake_media, monkeypatch):
    def invalid_output(*args, **kwargs):
        raise FfmpegError("output has no audio")

    monkeypatch.setattr("narumi.playback.stage.validate_output", invalid_output)
    with pytest.raises(FfmpegError):
        run_playback(recorded_bundle)
    assert recorded_bundle.artifact(ARTIFACT_KEY) is None
    assert not recorded_bundle.abspath(OUTPUT_PATH).exists()


@pytest.mark.parametrize("problem", ["absent_screen", "discarded_screen", "no_audio", "recording"])
def test_unavailable_tracks_fail_before_media_execution(recorded_bundle, fake_media, problem):
    tracks = recorded_bundle.manifest.recording.tracks
    if problem == "absent_screen":
        del tracks["screen"]
    elif problem == "discarded_screen":
        tracks["screen"].discarded = True
    elif problem == "no_audio":
        tracks["mic"].discarded = True
        del tracks["system"]
    else:
        recorded_bundle.manifest.status = "recording"
    with pytest.raises(InvalidArgumentError):
        run_playback(recorded_bundle)
    assert not fake_media


def test_missing_file_is_typed_and_single_audio_is_supported(recorded_bundle, fake_media):
    recorded_bundle.abspath("tracks/mic.m4a").unlink()
    with pytest.raises(NotFoundError):
        run_playback(recorded_bundle)
    recorded_bundle.manifest.recording.tracks["mic"].discarded = True
    result = run_playback(recorded_bundle)
    assert set(result.record.inputs) == {"tracks/screen", "tracks/system"}
    assert len(fake_media[0][1]) == 1


def test_paths_cannot_escape_bundle(recorded_bundle, fake_media, tmp_path):
    external = tmp_path / "external.mp4"
    external.write_bytes(b"external media")
    recorded_bundle.manifest.recording.tracks["screen"].path = str(external)
    with pytest.raises(InvalidArgumentError, match="within"):
        run_playback(recorded_bundle)


@pytest.mark.parametrize("alias", ["symlink", "track_path"])
def test_output_cannot_overwrite_an_original_track(recorded_bundle, fake_media, alias):
    source = recorded_bundle.abspath("tracks/screen.mp4")
    original = source.read_bytes()
    output = recorded_bundle.abspath(OUTPUT_PATH)
    output.parent.mkdir()
    if alias == "symlink":
        output.symlink_to(source)
    else:
        source.replace(output)
        recorded_bundle.manifest.recording.tracks["screen"].path = OUTPUT_PATH
        source = output
    with pytest.raises(InvalidArgumentError, match="original|dedicated"):
        run_playback(recorded_bundle)
    assert source.read_bytes() == original
    assert not fake_media


@pytest.mark.parametrize("alias", ["manifest", "directory"])
def test_output_alias_cannot_overwrite_other_bundle_files(recorded_bundle, fake_media, alias):
    output = recorded_bundle.abspath(OUTPUT_PATH)
    original_manifest = recorded_bundle.manifest_path.read_bytes()
    if alias == "manifest":
        output.parent.mkdir()
        output.symlink_to(recorded_bundle.manifest_path)
    else:
        other_directory = recorded_bundle.abspath("context")
        output.parent.symlink_to(other_directory, target_is_directory=True)
        (other_directory / "recording.mp4").write_bytes(b"existing other artifact")
    with pytest.raises(InvalidArgumentError, match="dedicated"):
        run_playback(recorded_bundle)
    assert recorded_bundle.manifest_path.read_bytes() == original_manifest
    if alias == "directory":
        assert output.read_bytes() == b"existing other artifact"
    assert not fake_media


def test_cancel_before_work_does_not_generate(recorded_bundle, fake_media):
    with pytest.raises(CancelledError):
        run_playback(recorded_bundle, should_cancel=lambda: True)
    assert not fake_media


def test_playback_info_is_lightweight_and_hides_discarded_or_missing_artifact(
    recorded_bundle, fake_media, monkeypatch
):
    assert playback_info(recorded_bundle) is None
    result = run_playback(recorded_bundle)

    def no_hash(*args):
        pytest.fail("display metadata must not hash large media files")

    monkeypatch.setattr("narumi.playback.stage._file_hash", no_hash)
    assert playback_info(recorded_bundle) is not None
    recorded_bundle.manifest.recording.tracks["screen"].discarded = True
    assert playback_info(recorded_bundle) is None
    recorded_bundle.manifest.recording.tracks["screen"].discarded = False
    result.path.unlink()
    assert playback_info(recorded_bundle) is None
