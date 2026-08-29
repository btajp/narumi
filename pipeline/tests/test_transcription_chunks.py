from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import wave
from dataclasses import replace
from pathlib import Path

import pytest
from narumi.bundle import Bundle
from narumi.errors import (
    CancelledError,
    ConfigurationConflictError,
    EngineUnavailableError,
    InvalidArgumentError,
    NarumiError,
)
from narumi.transcribe import _wav, chunks
from narumi.transcribe._storage import transcription_execution_lock
from narumi.transcribe.chunks import TranscriptionPlan, build_transcription_plan


@pytest.fixture
def bundle(tmp_path: Path) -> Bundle:
    return Bundle.create(tmp_path, meeting_name="synthetic audio")


@pytest.fixture
def params() -> dict:
    return {
        "provider": "openai-api",
        "connection_id": "conn-0123456789abcdef",
        "connection_revision": 1,
        "model_id": "whisper-1",
        "language": "ja",
        "effective_parameters": {
            "response_format": "verbose_json",
            "timestamp_granularities": ["segment", "word"],
        },
        "adapter_version": "1",
        "capability_table_version": "1",
        "runtime_version": "0.5.0",
        "runtime_sha256": "a" * 64,
        "runtime_catalog_revision": "catalog-1",
        "model_capabilities_sha256": "b" * 64,
        "endpoint": "https://api.openai.com",
    }


def _pcm(count: int) -> bytes:
    pattern = b"\0\0\x01\0\xff\x7f\0\x80"
    return (pattern * ((count + 3) // 4))[: count * 2]


def _source(bundle: Bundle, track: str = "mic", count: int = 5) -> Path:
    path = bundle.path / "preprocess" / f"{track}.wav"
    path.write_bytes(_wav.canonical_wave(_pcm(count)))
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _riff(*values: tuple[bytes, bytes]) -> bytes:
    contents = b"WAVE" + b"".join(
        name + struct.pack("<I", len(data)) + data + b"\0" * (len(data) % 2)
        for name, data in values
    )
    return b"RIFF" + struct.pack("<I", len(contents)) + contents


_FMT = struct.pack("<HHIIHH", 1, 1, 16_000, 32_000, 2, 16)


def test_fixed_boundaries_preserve_all_samples_and_separate_tracks(bundle, params):
    mic = _source(bundle, "mic", chunks.CHUNK_SAMPLES + 17)
    system = _source(bundle, "system", chunks.CHUNK_SAMPLES)
    plan = build_transcription_plan(
        bundle,
        sources={"system": system, "mic": mic},
        params=params,
        expected_hashes={"mic": _sha(mic), "system": _sha(system)},
    )
    assert [(c.track, c.index, c.start_sample, c.end_sample) for c in plan.chunks] == [
        ("mic", 0, 0, 9_600_000),
        ("mic", 1, 9_600_000, 9_600_017),
        ("system", 2, 0, 9_600_000),
    ]
    assert plan.total_samples == 19_200_017
    assert [c.duration_sec for c in plan.chunks] == [600, 17 / 16_000, 600]
    for track, source in (("mic", mic), ("system", system)):
        recovered = bytearray()
        for chunk in (c for c in plan.chunks if c.track == track):
            audio = chunk.read_audio()
            assert len(audio) == 44 + (chunk.end_sample - chunk.start_sample) * 2
            assert len(audio) <= 24_000_000
            assert chunk.source_sha256 == _sha(source)
            with wave.open(io.BytesIO(audio), "rb") as stream:
                assert (stream.getnchannels(), stream.getframerate(), stream.getsampwidth()) == (
                    1,
                    16_000,
                    2,
                )
                recovered.extend(stream.readframes(stream.getnframes()))
        assert recovered == source.read_bytes()[44:]
    assert not list((bundle.path / "preprocess" / "transcription" / "chunks").glob(".pending-*"))


def test_plan_is_deterministic_and_params_are_defensive(bundle, params):
    sources = {"mic": _source(bundle), "system": _source(bundle, "system")}
    first = build_transcription_plan(bundle, sources=sources, params=params)
    second = build_transcription_plan(
        bundle,
        sources=dict(reversed(list(sources.items()))),
        params=dict(reversed(list(params.items()))),
    )
    assert first.as_payload() == second.as_payload()
    original = copy.deepcopy(params)
    params["effective_parameters"]["timestamp_granularities"].append("changed")
    first.params["effective_parameters"]["timestamp_granularities"].clear()
    payload = first.as_payload()
    payload["params"]["language"] = "en"
    assert first.params == original
    assert set(payload) == {
        "version",
        "chunker_version",
        "input_fingerprint",
        "params",
        "total_samples",
        "chunks",
    }
    assert all("path" not in item for item in payload["chunks"])


def test_adding_mic_preserves_system_chunk_identity(bundle, params):
    system = _source(bundle, "system")
    original = build_transcription_plan(bundle, sources={"system": system}, params=params)
    expanded = build_transcription_plan(
        bundle,
        sources={"mic": _source(bundle), "system": system},
        params=params,
    )
    before, after = original.chunks[0], expanded.chunks[1]
    assert (before.index, after.index) == (0, 1)
    assert before.fingerprint == after.fingerprint and before.path == after.path
    assert before.read_audio() == after.read_audio()
    assert original.input_fingerprint != expanded.input_fingerprint


def test_source_metadata_is_hashed_but_never_copied_to_upload(bundle, params):
    path = _source(bundle)
    original = build_transcription_plan(bundle, sources={"mic": path}, params=params)
    private = b"synthetic filename, meeting name, metadata"
    path.write_bytes(
        _riff((b"fmt ", _FMT), (b"LIST", private), (b"data", _pcm(5)), (b"JUNK", b"x"))
    )
    modified = build_transcription_plan(bundle, sources={"mic": path}, params=params)
    assert original.chunks[0].read_audio() == modified.chunks[0].read_audio()
    assert private not in modified.chunks[0].read_audio()
    assert original.chunks[0].source_sha256 != modified.chunks[0].source_sha256
    assert original.input_fingerprint != modified.input_fingerprint


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("connection_revision", 2),
        ("model_id", "gpt-4o-transcribe-diarize"),
        ("language", "auto"),
        ("runtime_sha256", "c" * 64),
        ("effective_parameters", {"response_format": "diarized_json", "stream": False}),
    ],
)
def test_effective_params_change_fingerprints(bundle, params, key, value):
    sources = {"mic": _source(bundle)}
    original = build_transcription_plan(bundle, sources=sources, params=params)
    params[key] = value
    changed = build_transcription_plan(bundle, sources=sources, params=params)
    assert changed.chunks[0].audio_sha256 == original.chunks[0].audio_sha256
    assert changed.input_fingerprint != original.input_fingerprint


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_TOTAL_SAMPLES", 4),
        ("MAX_CHUNKS", 1),
        ("MAX_AUDIO_BYTES", 50),
    ],
)
def test_limits_fail_before_writing_any_chunks(bundle, params, monkeypatch, name, value):
    sources = {"mic": _source(bundle), "system": _source(bundle, "system")}
    monkeypatch.setattr(chunks, name, value)
    with pytest.raises(InvalidArgumentError):
        build_transcription_plan(bundle, sources=sources, params=params)
    assert not (bundle.path / "preprocess" / "transcription").exists()


@pytest.mark.parametrize("case", ["over_24_hours", "over_144_chunks"])
def test_real_audio_limits_are_checked_from_sparse_headers(bundle, params, case):
    counts = (
        {"mic": chunks.MAX_TOTAL_SAMPLES + 1}
        if case == "over_24_hours"
        else {"mic": 1, "system": chunks.MAX_TOTAL_SAMPLES - 1}
    )
    sources = {}
    for track, count in counts.items():
        path = bundle.path / "preprocess" / f"{track}.wav"
        with path.open("wb") as stream:
            stream.write(_wav.canonical_wave_header(count * 2))
            stream.truncate(44 + count * 2)
        sources[track] = path
    with pytest.raises(InvalidArgumentError):
        build_transcription_plan(bundle, sources=sources, params=params)
    assert not (bundle.path / "preprocess" / "transcription").exists()


@pytest.mark.parametrize(
    "case",
    [
        "empty_file",
        "empty_audio",
        "truncated_header",
        "truncated_data",
        "extra_bytes",
        "riff_size",
        "not_riff",
        "not_wave",
        "format",
        "channels",
        "rate",
        "byte_rate",
        "align",
        "bits",
        "duplicate_fmt",
        "duplicate_data",
        "data_before_fmt",
        "missing_data",
        "odd_pcm",
        "invalid_extension",
        "truncated_chunk",
        "partial_chunk_header",
    ],
)
def test_invalid_wave_is_rejected_before_any_track_is_saved(bundle, params, case):
    valid = _source(bundle)
    raw = bytearray(_wav.canonical_wave(_pcm(5)))
    patch = {
        "format": (20, "H", 3),
        "channels": (22, "H", 2),
        "rate": (24, "I", 8_000),
        "byte_rate": (28, "I", 16_000),
        "align": (32, "H", 4),
        "bits": (34, "H", 8),
    }
    if case in patch:
        offset, kind, value = patch[case]
        struct.pack_into("<" + kind, raw, offset, value)
    else:
        raw = {
            "empty_file": b"",
            "empty_audio": _wav.canonical_wave(b""),
            "truncated_header": b"RIFF",
            "truncated_data": raw[:-1],
            "extra_bytes": raw + b"x",
            "riff_size": raw[:4] + b"\0" * 4 + raw[8:],
            "not_riff": b"RF64" + raw[4:],
            "not_wave": raw[:8] + b"NONE" + raw[12:],
            "duplicate_fmt": _riff((b"fmt ", _FMT), (b"fmt ", _FMT), (b"data", _pcm(5))),
            "duplicate_data": _riff((b"fmt ", _FMT), (b"data", _pcm(5)), (b"data", _pcm(5))),
            "data_before_fmt": _riff((b"data", _pcm(5)), (b"fmt ", _FMT)),
            "missing_data": _riff((b"fmt ", _FMT)),
            "odd_pcm": _riff((b"fmt ", _FMT), (b"data", b"x")),
            "invalid_extension": _riff((b"fmt ", _FMT + b"\1\0"), (b"data", _pcm(5))),
            "truncated_chunk": _riff((b"fmt ", _FMT)) + b"data" + struct.pack("<I", 999),
            "partial_chunk_header": _riff((b"fmt ", _FMT)) + b"data",
        }[case]
        if case in {"truncated_chunk", "partial_chunk_header"}:
            raw = raw[:4] + struct.pack("<I", len(raw) - 8) + raw[8:]
    bad = bundle.path / "preprocess" / "system.wav"
    bad.write_bytes(raw)
    with pytest.raises(InvalidArgumentError):
        build_transcription_plan(bundle, sources={"mic": valid, "system": bad}, params=params)
    assert not (bundle.path / "preprocess" / "transcription").exists()


def test_fmt_extension_zero_is_valid_and_metadata_has_a_bound(bundle, params, monkeypatch):
    path = _source(bundle)
    path.write_bytes(_riff((b"fmt ", _FMT + b"\0\0"), (b"data", _pcm(5))))
    assert build_transcription_plan(bundle, sources={"mic": path}, params=params).total_samples == 5
    monkeypatch.setattr(_wav, "MAX_METADATA_BYTES", 64)
    path.write_bytes(_riff((b"fmt ", _FMT), (b"LIST", b"x" * 65), (b"data", _pcm(5))))
    with pytest.raises(InvalidArgumentError):
        build_transcription_plan(bundle, sources={"mic": path}, params=params)


@pytest.mark.parametrize(
    "kind", ["file_symlink", "ancestor_symlink", "hardlink", "outside", "fifo", "writable"]
)
def test_sources_reject_unsafe_paths_and_files(bundle, params, tmp_path, kind):
    source = _source(bundle)
    if kind == "file_symlink":
        alias = source.with_name("alias.wav")
        alias.symlink_to(source)
        source = alias
    elif kind == "ancestor_symlink":
        alias = bundle.path / "alias"
        alias.symlink_to(source.parent, target_is_directory=True)
        source = alias / source.name
    elif kind == "hardlink":
        os.link(source, source.with_name("linked.wav"))
    elif kind == "outside":
        outside = tmp_path / "outside.wav"
        outside.write_bytes(source.read_bytes())
        source = outside
    elif kind == "fifo":
        source.unlink()
        os.mkfifo(source)
    else:
        source.chmod(0o666)
    with pytest.raises(InvalidArgumentError):
        build_transcription_plan(bundle, sources={"mic": source}, params=params)


def test_expected_source_hash_mismatch_discards_staged_audio(bundle, params):
    with pytest.raises(ConfigurationConflictError):
        build_transcription_plan(
            bundle,
            sources={"mic": _source(bundle)},
            params=params,
            expected_hashes={"mic": "0" * 64},
        )
    assert not list((bundle.path / "preprocess" / "transcription" / "chunks").iterdir())


def test_source_change_during_read_is_rejected_without_stale_hash(bundle, params, monkeypatch):
    source = _source(bundle, count=600_000)
    original = _wav.canonical_wave

    def mutate(samples):
        with source.open("r+b") as stream:
            stream.seek(44)
            stream.write(b"\x10\x20")
        return original(samples)

    monkeypatch.setattr(_wav, "canonical_wave", mutate)
    with pytest.raises(ConfigurationConflictError):
        build_transcription_plan(bundle, sources={"mic": source}, params=params)
    assert not list((bundle.path / "preprocess" / "transcription" / "chunks").iterdir())


@pytest.mark.parametrize("when", ["before", "hash_read", "after_chunk"])
def test_cancel_during_chunk_build_never_leaves_unverified_audio(bundle, params, monkeypatch, when):
    source = _source(bundle, count=1_100_000)
    cancelled = when == "before"
    if when == "hash_read":
        real_check = _wav.check_cancelled
        calls = 0

        def stop_read(callback):
            nonlocal calls
            calls += 1
            if calls == 6:
                raise CancelledError("synthetic cancellation")
            real_check(callback)

        monkeypatch.setattr(_wav, "check_cancelled", stop_read)
    elif when == "after_chunk":
        real_write = chunks.write_bytes

        def stop_after_write(*args, **kwargs):
            nonlocal cancelled
            real_write(*args, **kwargs)
            cancelled = True

        monkeypatch.setattr(chunks, "write_bytes", stop_after_write)
    with pytest.raises(CancelledError):
        build_transcription_plan(
            bundle, sources={"mic": source}, params=params, should_cancel=lambda: cancelled
        )
    directory = bundle.path / "preprocess" / "transcription" / "chunks"
    assert not directory.exists() or not list(directory.iterdir())


@pytest.mark.parametrize("kind", ["modified", "missing", "symlink", "parent_symlink", "hardlink"])
def test_read_audio_revalidates_saved_file(bundle, params, kind):
    chunk = build_transcription_plan(
        bundle, sources={"mic": _source(bundle)}, params=params
    ).chunks[0]
    if kind == "modified":
        chunk.path.write_bytes(chunk.path.read_bytes()[:-1] + b"x")
    elif kind == "missing":
        chunk.path.unlink()
    elif kind == "symlink":
        target = bundle.path / "copied.wav"
        target.write_bytes(chunk.path.read_bytes())
        chunk.path.unlink()
        chunk.path.symlink_to(target)
    elif kind == "parent_symlink":
        target = bundle.path / "moved"
        chunk.path.parent.rename(target)
        chunk.path.parent.symlink_to(target, target_is_directory=True)
    else:
        os.link(chunk.path, chunk.path.with_name("duplicate.wav"))
    with pytest.raises(NarumiError):
        chunk.read_audio()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("cache_epoch", 1),
        ("api_key", "synthetic"),
        ("connection_revision", True),
        ("runtime_sha256", "invalid"),
        ("endpoint", "https://example.invalid"),
        ("effective_parameters", {"cache_epoch": 2}),
        ("effective_parameters", {"authorization": "synthetic"}),
    ],
)
def test_plan_rejects_secret_epoch_or_unpinned_params(bundle, params, key, value):
    params[key] = value
    with pytest.raises(NarumiError):
        build_transcription_plan(bundle, sources={"mic": _source(bundle)}, params=params)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("index", True),
        ("sample_rate", 8_000),
        ("start_sample", 1),
        ("end_sample", 0),
        ("source_sha256", "bad"),
        ("audio_sha256", "c" * 64),
        ("fingerprint", "d" * 64),
        ("track", "other"),
        ("path", Path("/outside.wav")),
    ],
)
def test_plan_validates_forged_chunk_metadata_before_persistence(bundle, params, key, value):
    plan = build_transcription_plan(bundle, sources={"mic": _source(bundle)}, params=params)
    with pytest.raises(InvalidArgumentError):
        TranscriptionPlan(
            plan.input_fingerprint,
            (replace(plan.chunks[0], **{key: value}),),
            plan.params,
            plan.total_samples,
        )


def test_plan_revalidates_mutated_private_params_and_total(bundle, params):
    plan = build_transcription_plan(bundle, sources={"mic": _source(bundle)}, params=params)
    with pytest.raises(InvalidArgumentError):
        TranscriptionPlan(plan.input_fingerprint, plan.chunks, plan.params, True)
    plan._params["language"] = "en"
    with pytest.raises(InvalidArgumentError):
        plan.validate()


def _rekey(chunk, params, **changes):
    updated = replace(chunk, **changes)
    fingerprint = chunks._chunk_fingerprint(
        updated.track,
        updated.start_sample,
        updated.end_sample,
        updated.source_sha256,
        updated.audio_sha256,
        params,
    )
    return replace(
        updated, fingerprint=fingerprint, path=updated.path.with_name(f"{fingerprint}.wav")
    )


@pytest.mark.parametrize(
    "case", ["gap", "overlap", "partial_middle", "source_change", "wrong_order", "mixed_roots"]
)
def test_plan_rejects_inconsistent_sequence_even_with_matching_fingerprints(
    bundle, params, monkeypatch, case
):
    monkeypatch.setattr(chunks, "CHUNK_SAMPLES", 4)
    plan = build_transcription_plan(
        bundle,
        sources={"mic": _source(bundle, count=9), "system": _source(bundle, "system", 4)},
        params=params,
    )
    items = list(plan.chunks)
    if case == "gap":
        items[1] = _rekey(items[1], params, start_sample=5)
    elif case == "overlap":
        items[1] = _rekey(items[1], params, start_sample=3, end_sample=7)
    elif case == "partial_middle":
        items[0] = _rekey(items[0], params, end_sample=3)
        items[1] = _rekey(items[1], params, start_sample=3, end_sample=7)
        items[2] = _rekey(items[2], params, start_sample=7)
    elif case == "source_change":
        items[1] = _rekey(items[1], params, source_sha256="f" * 64)
    elif case == "wrong_order":
        items = [items[-1], *items[:-1]]
        items = [replace(item, index=index) for index, item in enumerate(items)]
    else:
        root = bundle.path.parent / "another-bundle"
        items[-1] = replace(
            items[-1],
            _bundle_root=root,
            path=root / "preprocess" / "transcription" / "chunks" / items[-1].path.name,
        )
    with pytest.raises(InvalidArgumentError):
        TranscriptionPlan(
            chunks._plan_fingerprint(items),
            tuple(items),
            params,
            sum(item.end_sample - item.start_sample for item in items),
        )


@pytest.mark.parametrize(
    "sources,expected",
    [
        ({}, None),
        ({"other": "file.wav"}, None),
        ({"mic": "file.wav"}, None),
        ({"mic": Path("missing.wav")}, {}),
        ({"mic": Path("missing.wav")}, {"mic": "bad"}),
    ],
)
def test_plan_rejects_incomplete_or_untyped_input_maps(bundle, params, sources, expected):
    with pytest.raises(InvalidArgumentError):
        build_transcription_plan(bundle, sources=sources, params=params, expected_hashes=expected)


def test_restart_after_process_exit_reclaims_staging_without_changing_chunks(bundle, params):
    source = _source(bundle)
    with transcription_execution_lock(bundle):
        original = build_transcription_plan(bundle, sources={"mic": source}, params=params)
    audio = original.chunks[0].read_audio()
    child = """
import json
import os
import sys
from pathlib import Path
from narumi.bundle import Bundle
from narumi.transcribe import chunks
from narumi.transcribe._storage import transcription_execution_lock

write_bytes = chunks.write_bytes
def write_then_exit(directory, name, data, **kwargs):
    write_bytes(directory, name, data, **kwargs)
    if name.startswith(".pending-"):
        os._exit(86)

chunks.write_bytes = write_then_exit
bundle = Bundle.open(Path(sys.argv[1]))
with transcription_execution_lock(bundle):
    chunks.build_transcription_plan(
        bundle, sources={"mic": bundle.path / "preprocess" / "mic.wav"},
        params=json.loads(sys.argv[2]),
    )
"""
    exited = subprocess.run(
        [sys.executable, "-I", "-c", child, str(bundle.path), json.dumps(params)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert (exited.returncode, exited.stdout, exited.stderr) == (86, "", "")
    directory = original.chunks[0].path.parent
    pending = list(directory.glob(".pending-*.wav"))
    assert len(pending) == 1 and pending[0].read_bytes() == audio
    temporary = [
        directory / f".{original.chunks[0].fingerprint}.wav.{'b' * 32}.tmp",
        directory / f"..pending-{'c' * 32}.wav.{'d' * 32}.tmp",
    ]
    for path in temporary:
        path.write_bytes(b"incomplete synthetic data")
    unrelated = [
        directory / ".pending-short.wav",
        directory / f".pending-{'A' * 32}.wav",
        directory / f".pending-{'a' * 32}.wav.keep",
        directory / f".unrelated.txt.{'a' * 32}.tmp",
        directory / "notes.tmp",
    ]
    for path in unrelated:
        path.write_bytes(b"unchanged synthetic data")
    alias = directory / "unrelated-link.tmp"
    alias.symlink_to(unrelated[0])
    with transcription_execution_lock(Bundle.open(bundle.path)):
        assert all(not path.exists() for path in [*pending, *temporary])
        rebuilt = build_transcription_plan(bundle, sources={"mic": source}, params=params)
    assert rebuilt.as_payload() == original.as_payload()
    assert rebuilt.chunks[0].read_audio() == audio
    assert set(directory.glob(".pending-*.wav")) == {unrelated[0], unrelated[1]}
    assert all(path.read_bytes() == b"unchanged synthetic data" for path in unrelated)
    assert alias.is_symlink() and alias.readlink() == unrelated[0]


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
@pytest.mark.parametrize(
    "filename",
    [
        f".pending-{'a' * 32}.wav",
        f"..pending-{'a' * 32}.wav.{'b' * 32}.tmp",
        f".{'c' * 64}.wav.{'d' * 32}.tmp",
    ],
)
def test_restart_preserves_unsafe_staging_links_and_their_targets(
    bundle, params, tmp_path, link_kind, filename
):
    with transcription_execution_lock(bundle):
        plan = build_transcription_plan(bundle, sources={"mic": _source(bundle)}, params=params)
    chunk = plan.chunks[0]
    audio = chunk.read_audio()
    target = tmp_path / "unrelated-private-file"
    target.write_bytes(b"unchanged synthetic contents")
    staging = chunk.path.parent / filename
    if link_kind == "symlink":
        staging.symlink_to(target)
    else:
        os.link(target, staging)
    with pytest.raises(EngineUnavailableError) as caught, transcription_execution_lock(bundle):
        pytest.fail("An unsafe staging link must stop restart before processing")
    assert caught.value.details["reason"] == "transcription_checkpoint_unavailable"
    assert staging.exists()
    assert target.read_bytes() == b"unchanged synthetic contents"
    assert chunk.read_audio() == audio
    if link_kind == "symlink":
        assert staging.is_symlink() and staging.readlink() == target
    else:
        assert staging.stat().st_ino == target.stat().st_ino
