from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import pytest
from narumi.bundle import Bundle
from narumi.diarize import (
    ENGINE_FACTORIES,
    DiarizationProfile,
    FakeDiarizationEngine,
    NoneEngine,
    PyannoteEngine,
    assign_speakers,
    available_engines,
    build_layer1,
    fake_sidecar_path,
    get_engine,
    pyannote_engine,
    registry,
    run_diarize,
)
from narumi.errors import (
    EngineUnavailableError,
    ErrorCode,
    InvalidArgumentError,
    NarumiError,
    PolicyViolationError,
)
from narumi.models import (
    Diarization,
    EngineInfo,
    ExternalSendPolicy,
    MeetingConfig,
    Segment,
    Transcript,
    Turn,
)
from narumi.preprocess import run_preprocess
from narumi.transcribe import run_transcribe

from .media_fixtures import make_bundle_with_tracks, make_sine_wav, write_sidecar


def _config(engine: str = "none") -> MeetingConfig:
    return MeetingConfig(transcription_engine="fake", diarization_engine=engine)


def _pyannote_installed() -> bool:
    try:
        return importlib.util.find_spec("pyannote.audio") is not None
    except ImportError:
        return False


# ----------------------------------------------------------------------------- engines
def test_none_engine_single_speaker(tmp_path: Path):
    wav = make_sine_wav(tmp_path / "a.wav", seconds=7.0)
    turns = NoneEngine().diarize(wav)
    assert turns == [Turn(start=0.0, end=7.0, speaker="SPEAKER_00", confidence=1.0, layer=2)]


def test_fake_engine_alternates(tmp_path: Path):
    wav = make_sine_wav(tmp_path / "a.wav", seconds=25.0)
    turns = FakeDiarizationEngine().diarize(wav)
    assert [(t.start, t.end, t.speaker) for t in turns] == [
        (0.0, 10.0, "SPEAKER_00"),
        (10.0, 20.0, "SPEAKER_01"),
        (20.0, 25.0, "SPEAKER_00"),
    ]
    assert all(t.layer == 2 and t.confidence == 1.0 for t in turns)
    three = FakeDiarizationEngine().diarize(wav, num_speakers=3)
    assert [t.speaker for t in three] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]


def test_fake_engine_sidecar(tmp_path: Path):
    wav = make_sine_wav(tmp_path / "a.wav", seconds=3.0)
    write_sidecar(
        fake_sidecar_path(wav),
        [{"start": 0, "end": 1, "speaker": "SPEAKER_01", "confidence": 0.5}],
    )
    turns = FakeDiarizationEngine().diarize(wav)
    assert turns == [Turn(start=0, end=1, speaker="SPEAKER_01", confidence=0.5, layer=2)]
    write_sidecar(fake_sidecar_path(wav), [{"start": 0}])
    with pytest.raises(InvalidArgumentError):
        FakeDiarizationEngine().diarize(wav)


def test_registry(monkeypatch):
    assert available_engines()[:2] == ["none", "fake"]
    assert isinstance(get_engine("none"), NoneEngine)
    assert isinstance(get_engine("fake"), FakeDiarizationEngine)
    with pytest.raises(InvalidArgumentError):
        get_engine("bogus")
    with pytest.raises(InvalidArgumentError):
        get_engine("")
    monkeypatch.setattr(registry, "_module_available", lambda module: False)
    assert available_engines() == ["none", "fake"]
    with pytest.raises(EngineUnavailableError) as excinfo:
        get_engine("pyannote")
    assert "uv sync --extra pyannote" in str(excinfo.value)


def test_pyannote_requires_package_and_token(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(registry, "_module_available", lambda module: module == "pyannote.audio")
    monkeypatch.setattr(pyannote_engine, "package_version", lambda *a, **k: "0.0-test")
    for env in ("HF_TOKEN", "HUGGINGFACE_TOKEN"):
        monkeypatch.delenv(env, raising=False)
    assert available_engines() == ["none", "fake", "pyannote"]
    with pytest.raises(EngineUnavailableError) as excinfo:
        get_engine("pyannote")
    assert "HF_TOKEN" in str(excinfo.value)

    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_dummy")
    engine = get_engine("pyannote")
    assert isinstance(engine, PyannoteEngine)
    assert engine.model == "pyannote/speaker-diarization-3.1"
    assert not engine.profile.sends_audio_externally
    monkeypatch.setenv("NARUMI_PYANNOTE_MODEL", "org/other-model")
    assert get_engine("pyannote").model == "org/other-model"
    if not _pyannote_installed():
        # the lazy import must surface as a structured error, never a bare ImportError
        with pytest.raises(EngineUnavailableError):
            engine.diarize(make_sine_wav(tmp_path / "a.wav", seconds=1.0))


# ----------------------------------------------------------------------------- layer 1
def _transcript(
    source_id: str,
    track: str,
    spans: Sequence[tuple[float, float]],
    *,
    offset: float = 0.0,
) -> Transcript:
    return Transcript(
        source_id=source_id,
        kind="own",
        track=track,  # type: ignore[arg-type]
        engine=EngineInfo(name="fake", version="1"),
        time_offset=offset,
        segments=[
            Segment(id=f"{source_id}:{i}", start=start, end=end, text=f"t{i}")
            for i, (start, end) in enumerate(spans)
        ],
    )


def test_build_layer1():
    mic = _transcript("own-mic", "mic", [(0, 2), (6, 8)])
    system = _transcript("own-system", "system", [(2, 5)], offset=0.5)
    diarization = build_layer1([system, mic])
    assert diarization.layer == 1
    assert diarization.engine == EngineInfo(name="tracks", version="1")
    assert [(t.start, t.end, t.speaker, t.source_id) for t in diarization.turns] == [
        (0.0, 2.0, "me", "own-mic"),
        (2.5, 5.5, "other", "own-system"),
        (6.0, 8.0, "me", "own-mic"),
    ]
    assert all(t.layer == 1 and t.confidence == 1.0 for t in diarization.turns)
    assert build_layer1([mic, system]) == diarization
    external = Transcript(
        source_id="ext-1", kind="external", engine=EngineInfo(name="x", version="1")
    )
    with pytest.raises(InvalidArgumentError):
        build_layer1([external])


# ----------------------------------------------------------------------------- assignment
def test_assign_speakers_majority_overlap_and_ties():
    turns = [
        Turn(start=0, end=10, speaker="SPEAKER_00", layer=2),
        Turn(start=10, end=20, speaker="SPEAKER_01", layer=2),
        Turn(start=20, end=25, speaker="SPEAKER_00", layer=2),
    ]
    segments = [
        Segment(id="s:0", start=0, end=4, text="a"),
        Segment(id="s:1", start=8, end=14, text="b"),  # 2 s vs 4 s → SPEAKER_01
        Segment(id="s:2", start=5, end=15, text="c"),  # 5 s vs 5 s → earliest turn wins
        Segment(id="s:3", start=30, end=32, text="d", speaker="keep"),  # no overlap
        Segment(id="s:4", start=10, end=10, text="e"),  # point → earliest containing turn
        Segment(id="s:5", start=18, end=22, text="f"),  # 2 s vs 2 s → earliest turn wins
    ]
    assigned = assign_speakers(segments, turns)
    assert [s.speaker for s in assigned] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_00",
        "keep",
        "SPEAKER_00",
        "SPEAKER_01",
    ]
    assert [s.id for s in assigned] == [s.id for s in segments]
    assert all(s.speaker is None for s in segments[:3])  # inputs are not mutated
    limited = assign_speakers(segments, turns, min_overlap=3.0)
    assert limited[0].speaker == "SPEAKER_00"
    assert limited[1].speaker == "SPEAKER_01"
    assert limited[5].speaker is None
    assert assign_speakers([], turns) == []
    assert [s.speaker for s in assign_speakers(segments[:1], [])] == [None]
    with pytest.raises(InvalidArgumentError):
        assign_speakers(segments, turns, min_overlap=-1)


# ----------------------------------------------------------------------------- stage
def _prepared_bundle(
    tmp_path: Path,
    *,
    engine: str = "none",
    tracks: Sequence[str] = ("mic", "system"),
    seconds: float = 25.0,
) -> Bundle:
    bundle = make_bundle_with_tracks(
        tmp_path, tracks=tracks, seconds=seconds, config=_config(engine)
    )
    run_preprocess(bundle)
    run_transcribe(bundle)
    return bundle


def test_run_diarize_none(tmp_path: Path):
    bundle = _prepared_bundle(tmp_path, seconds=12.0)
    results = run_diarize(bundle)
    assert [r.key for r in results] == ["diarization/layer1"]
    assert not results[0].skipped
    assert results[0].path == bundle.abspath("diarization/layer1-tracks.json")
    diarization = Diarization.model_validate_json(results[0].path.read_text(encoding="utf-8"))
    assert diarization.layer == 1 and diarization.engine.name == "tracks"
    assert {t.speaker for t in diarization.turns} == {"me", "other"}
    assert {t.source_id for t in diarization.turns} == {"own-mic", "own-system"}
    assert len(diarization.turns) == 6
    record = bundle.artifact("diarization/layer1")
    assert record is not None
    assert record.inputs == {
        "transcripts/own-mic": bundle.artifact_hash("transcripts/own-mic"),
        "transcripts/own-system": bundle.artifact_hash("transcripts/own-system"),
    }
    assert record.producer.name == "tracks" and record.producer.version == "1"
    assert bundle.artifact("diarization/layer2") is None
    reopened = Bundle.open(bundle.path)
    assert [r.skipped for r in run_diarize(reopened)] == [True]
    assert [r.skipped for r in run_diarize(reopened, force=True)] == [False]


def test_run_diarize_fake_layer2(tmp_path: Path):
    bundle = _prepared_bundle(tmp_path, engine="fake", seconds=25.0)
    results = run_diarize(bundle)
    assert [r.key for r in results] == ["diarization/layer1", "diarization/layer2"]
    layer2 = results[1]
    assert layer2.path == bundle.abspath("diarization/layer2-fake.json")
    diarization = Diarization.model_validate_json(layer2.path.read_text(encoding="utf-8"))
    assert diarization.layer == 2 and diarization.engine.name == "fake"
    assert [t.speaker for t in diarization.turns] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert all(t.layer == 2 for t in diarization.turns)
    record = bundle.artifact("diarization/layer2")
    assert record is not None
    assert record.inputs == {
        "preprocess/audio/system": bundle.artifact_hash("preprocess/audio/system")
    }
    assert record.params["engine"] == "fake" and record.params["version"] == "1"
    assert record.producer.name == "fake"
    assert [r.skipped for r in run_diarize(bundle)] == [True, True]
    assert [r.skipped for r in run_diarize(bundle, force=True)] == [False, False]
    # switching the engine off must not leave the old engine's turns in the manifest
    bundle.manifest.config.diarization_engine = "none"
    assert [r.key for r in run_diarize(bundle)] == ["diarization/layer1"]
    assert bundle.artifact("diarization/layer2") is None
    assert not layer2.path.exists()
    assert Bundle.open(bundle.path).artifact("diarization/layer2") is None
    assert [r.key for r in run_diarize(bundle)] == ["diarization/layer1"]  # idempotent
    # switching engines re-points the key and removes the replaced engine's file
    bundle.manifest.config.diarization_engine = "fake"
    assert [r.skipped for r in run_diarize(bundle)] == [True, False]
    bundle.manifest.config.diarization_engine = "none"
    assert run_diarize(bundle)[0].key == "diarization/layer1"


def test_run_diarize_requires_transcripts(tmp_path: Path):
    bundle = make_bundle_with_tracks(tmp_path, config=_config())
    with pytest.raises(InvalidArgumentError):
        run_diarize(bundle)


def test_run_diarize_layer2_needs_system_track(tmp_path: Path):
    bundle = _prepared_bundle(tmp_path, engine="fake", tracks=("mic",), seconds=3.0)
    with pytest.raises(InvalidArgumentError) as excinfo:
        run_diarize(bundle)
    assert "system" in str(excinfo.value)
    assert bundle.artifact("diarization/layer1") is not None


class ExternalDiarizer(FakeDiarizationEngine):
    name = "ext-diar"
    profile = DiarizationProfile(True, data_destination="example-cloud", cost_class="api")


def test_run_diarize_enforces_policy(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(ENGINE_FACTORIES, "ext-diar", ExternalDiarizer)
    bundle = _prepared_bundle(tmp_path, engine="ext-diar", seconds=3.0)
    with pytest.raises(PolicyViolationError) as excinfo:
        run_diarize(bundle)
    assert excinfo.value.code == ErrorCode.POLICY_VIOLATION
    assert bundle.artifact("diarization/layer2") is None
    bundle.manifest.config.external_send_policy = ExternalSendPolicy.API_OK
    results = run_diarize(bundle)
    assert results[1].path == bundle.abspath("diarization/layer2-ext-diar.json")


class WrongLayerEngine(FakeDiarizationEngine):
    name = "wrong-layer"

    def diarize(self, wav: Path, *, num_speakers: int | None = None) -> list[Turn]:
        turns = super().diarize(wav, num_speakers=num_speakers)
        return [t.model_copy(update={"layer": 1}) for t in turns]


def test_run_diarize_rejects_wrong_layer(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(ENGINE_FACTORIES, "wrong-layer", WrongLayerEngine)
    bundle = _prepared_bundle(tmp_path, engine="wrong-layer", seconds=3.0)
    with pytest.raises(NarumiError) as excinfo:
        run_diarize(bundle)
    assert excinfo.value.code == ErrorCode.INTERNAL
    assert bundle.artifact("diarization/layer2") is None
