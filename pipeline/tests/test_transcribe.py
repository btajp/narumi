from __future__ import annotations

import os
from pathlib import Path

import pytest
from narumi.bundle import Bundle
from narumi.errors import (
    EngineUnavailableError,
    ErrorCode,
    InvalidArgumentError,
    NarumiError,
    PolicyViolationError,
)
from narumi.models import ExternalSendPolicy, MeetingConfig, Segment, Transcript
from narumi.preprocess import run_preprocess
from narumi.transcribe import (
    ENGINE_FACTORIES,
    EngineProfile,
    FakeEngine,
    FasterWhisperEngine,
    MlxWhisperEngine,
    available_engines,
    build_initial_prompt,
    check_send_policy,
    engine_profile,
    faster_whisper_engine,
    get_engine,
    mlx_whisper_engine,
    registry,
    run_transcribe,
    sidecar_path,
)

from .media_fixtures import make_bundle_with_tracks, make_sine_wav, write_sidecar

REAL_TESTS = os.environ.get("NARUMI_REAL_TESTS") == "1"
needs_real = pytest.mark.skipif(
    not REAL_TESTS, reason="set NARUMI_REAL_TESTS=1 to run real engine smoke tests"
)


def _fake_config(**overrides: object) -> MeetingConfig:
    return MeetingConfig(transcription_engine="fake", **overrides)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- fake engine
def test_fake_engine_chunks_by_duration(tmp_path: Path):
    wav = make_sine_wav(tmp_path / "a.wav", seconds=12.0)
    segments = FakeEngine().transcribe(wav, source_id="own-mic", language="ja", vocab_hints=[])
    assert [s.id for s in segments] == ["own-mic:0", "own-mic:1", "own-mic:2"]
    assert [(s.start, s.end) for s in segments] == [(0.0, 5.0), (5.0, 10.0), (10.0, 12.0)]
    assert segments[0].text == "ダミー発話 0"
    assert segments == FakeEngine().transcribe(
        wav, source_id="own-mic", language="ja", vocab_hints=["x"]
    )


def test_fake_engine_sidecar(tmp_path: Path):
    wav = make_sine_wav(tmp_path / "a.wav", seconds=3.0)
    write_sidecar(
        sidecar_path(wav),
        [
            {"start": 0, "end": 1.5, "text": "こんにちは", "speaker": "me"},
            {"start": 1.5, "end": 3, "text": "はい", "confidence": 0.9},
        ],
    )
    segments = FakeEngine().transcribe(wav, source_id="own-system", language="ja", vocab_hints=[])
    assert [s.text for s in segments] == ["こんにちは", "はい"]
    assert segments[0].speaker == "me" and segments[1].speaker is None
    assert segments[1].id == "own-system:1" and segments[1].confidence == 0.9
    write_sidecar(sidecar_path(wav), [{"start": 0, "end": 1, "bogus": 1}])
    with pytest.raises(InvalidArgumentError):
        FakeEngine().transcribe(wav, source_id="own-system", language="ja", vocab_hints=[])


# ----------------------------------------------------------------------------- registry
def test_registry_basics():
    assert "fake" in available_engines()
    assert isinstance(get_engine("fake"), FakeEngine)
    assert engine_profile("fake") == FakeEngine.profile
    assert not engine_profile("fake").sends_audio_externally
    with pytest.raises(InvalidArgumentError):
        get_engine("nope")
    with pytest.raises(InvalidArgumentError):
        get_engine("")


def _simulate_installed(monkeypatch, *modules: str) -> None:
    monkeypatch.setattr(registry, "_module_available", lambda module: module in modules)
    monkeypatch.setattr(mlx_whisper_engine, "package_version", lambda *a, **k: "0.0-test")
    monkeypatch.setattr(faster_whisper_engine, "package_version", lambda *a, **k: "0.0-test")


def test_auto_prefers_mlx(monkeypatch):
    _simulate_installed(monkeypatch, "mlx_whisper", "faster_whisper")
    engine = get_engine("auto")
    assert isinstance(engine, MlxWhisperEngine)
    assert engine.name == "mlx-whisper" and engine.version == "0.0-test"
    assert engine.model == mlx_whisper_engine.DEFAULT_MODEL
    assert available_engines() == ["fake", "mlx-whisper", "faster-whisper"]


def test_auto_falls_back_to_faster(monkeypatch):
    _simulate_installed(monkeypatch, "faster_whisper")
    engine = get_engine("auto")
    assert isinstance(engine, FasterWhisperEngine)
    assert engine.model == faster_whisper_engine.DEFAULT_MODEL
    assert available_engines() == ["fake", "faster-whisper"]


def test_auto_without_whisper_raises_helpful_error(monkeypatch):
    _simulate_installed(monkeypatch)
    with pytest.raises(EngineUnavailableError) as excinfo:
        get_engine("auto")
    message = str(excinfo.value)
    assert "whisper-mlx" in message and "whisper-faster" in message
    assert excinfo.value.details["tried"] == ["mlx-whisper", "faster-whisper"]
    assert available_engines() == ["fake"]
    with pytest.raises(EngineUnavailableError):
        get_engine("mlx-whisper")


def test_whisper_model_env_override(monkeypatch):
    _simulate_installed(monkeypatch, "mlx_whisper", "faster_whisper")
    monkeypatch.setenv("NARUMI_WHISPER_MODEL", "mlx-community/whisper-tiny")
    assert get_engine("mlx-whisper").model == "mlx-community/whisper-tiny"
    assert get_engine("faster-whisper").model == "mlx-community/whisper-tiny"
    assert MlxWhisperEngine(model="explicit").model == "explicit"


def test_build_initial_prompt():
    assert build_initial_prompt([], language="ja") is None
    assert build_initial_prompt([" ", ""], language="ja") is None
    assert build_initial_prompt(["鳴海", " gaia-library "], language="ja") == "鳴海、gaia-library"
    assert build_initial_prompt(["a", "b"], language="en") == "a, b"


# ----------------------------------------------------------------------------- policy
LOCAL = EngineProfile(False, True, True)
METERED = EngineProfile(True, True, True, data_destination="example-cloud", cost_class="api")
SUBSCRIPTION = EngineProfile(
    True, True, True, data_destination="example-cloud", cost_class="subscription"
)


@pytest.mark.parametrize("policy", list(ExternalSendPolicy))
def test_policy_allows_local_engines(policy: ExternalSendPolicy):
    check_send_policy(policy, LOCAL, subject="x")


def test_policy_blocks_external_engines():
    with pytest.raises(PolicyViolationError) as excinfo:
        check_send_policy(ExternalSendPolicy.LOCAL_ONLY, METERED, subject="engine 'x'")
    assert excinfo.value.code == ErrorCode.POLICY_VIOLATION
    assert excinfo.value.details["required_policy"] == "api_ok"
    assert "engine 'x'" in str(excinfo.value)
    with pytest.raises(PolicyViolationError):
        check_send_policy("subscription_ok", METERED, subject="x")
    check_send_policy("api_ok", METERED, subject="x")
    with pytest.raises(PolicyViolationError) as excinfo:
        check_send_policy("local_only", SUBSCRIPTION, subject="x")
    assert excinfo.value.details["required_policy"] == "subscription_ok"
    check_send_policy("subscription_ok", SUBSCRIPTION, subject="x")
    check_send_policy("api_ok", SUBSCRIPTION, subject="x")


# ----------------------------------------------------------------------------- stage
def test_run_transcribe_writes_valid_transcripts(tmp_path: Path):
    bundle = make_bundle_with_tracks(
        tmp_path, seconds=7.0, config=_fake_config(vocab_hints=["鳴海"])
    )
    run_preprocess(bundle)
    results = run_transcribe(bundle)
    assert [r.key for r in results] == ["transcripts/own-mic", "transcripts/own-system"]
    assert all(not r.skipped for r in results)
    assert results[0].path == bundle.abspath("transcripts/own-mic.json")
    transcript = Transcript.model_validate_json(results[0].path.read_text(encoding="utf-8"))
    assert transcript.source_id == "own-mic"
    assert transcript.kind == "own" and transcript.track == "mic"
    assert transcript.engine.name == "fake" and transcript.engine.version == "1"
    assert transcript.engine.params["model"] == "fake"
    assert transcript.language == "ja" and transcript.time_offset == 0.0
    assert [s.id for s in transcript.segments] == ["own-mic:0", "own-mic:1"]
    system = Transcript.model_validate_json(results[1].path.read_text(encoding="utf-8"))
    assert system.source_id == "own-system" and system.track == "system"

    record = bundle.artifact("transcripts/own-mic")
    assert record is not None
    assert record.inputs == {"preprocess/audio/mic": bundle.artifact_hash("preprocess/audio/mic")}
    assert record.params["engine"] == "fake" and record.params["version"] == "1"
    assert record.params["model"] == "fake" and record.params["language"] == "ja"
    assert record.params["vocab_hints"] == ["鳴海"]
    assert record.producer.name == "fake"

    reopened = Bundle.open(bundle.path)
    assert all(r.skipped for r in run_transcribe(reopened))
    reopened.manifest.config.vocab_hints = ["鳴海", "探偵"]
    assert all(not r.skipped for r in run_transcribe(reopened))
    assert all(r.skipped for r in run_transcribe(reopened))
    assert all(not r.skipped for r in run_transcribe(reopened, force=True))


def test_run_transcribe_uses_sidecar(tmp_path: Path):
    bundle = make_bundle_with_tracks(tmp_path, tracks=("mic",), config=_fake_config())
    run_preprocess(bundle)
    wav = bundle.artifact_path("preprocess/audio/mic")
    write_sidecar(sidecar_path(wav), [{"start": 0.0, "end": 2.0, "text": "議事録を作ります"}])
    [result] = run_transcribe(bundle)
    transcript = Transcript.model_validate_json(result.path.read_text(encoding="utf-8"))
    assert [s.text for s in transcript.segments] == ["議事録を作ります"]


def test_run_transcribe_requires_preprocess(tmp_path: Path):
    bundle = make_bundle_with_tracks(tmp_path, config=_fake_config())
    with pytest.raises(InvalidArgumentError):
        run_transcribe(bundle)


class ExternalFakeEngine(FakeEngine):
    name = "ext-fake"
    profile = EngineProfile(True, True, False, data_destination="example-cloud", cost_class="api")


def test_run_transcribe_enforces_policy(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(ENGINE_FACTORIES, "ext-fake", ExternalFakeEngine)
    bundle = make_bundle_with_tracks(
        tmp_path, tracks=("mic",), config=MeetingConfig(transcription_engine="ext-fake")
    )
    run_preprocess(bundle)
    with pytest.raises(PolicyViolationError) as excinfo:
        run_transcribe(bundle)
    assert excinfo.value.details["policy"] == "local_only"
    assert bundle.artifact("transcripts/own-mic") is None
    bundle.manifest.config.external_send_policy = ExternalSendPolicy.SUBSCRIPTION_OK
    with pytest.raises(PolicyViolationError):
        run_transcribe(bundle)
    bundle.manifest.config.external_send_policy = ExternalSendPolicy.API_OK
    [result] = run_transcribe(bundle)
    assert result.record.producer.name == "ext-fake"


class BadIdEngine(FakeEngine):
    name = "bad-ids"

    def transcribe(
        self, wav: Path, *, source_id: str, language: str, vocab_hints: list[str]
    ) -> list[Segment]:
        segments = super().transcribe(
            wav, source_id=source_id, language=language, vocab_hints=vocab_hints
        )
        return [s.model_copy(update={"id": "other:9"}) for s in segments]


def test_run_transcribe_rejects_bad_segment_ids(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(ENGINE_FACTORIES, "bad-ids", BadIdEngine)
    bundle = make_bundle_with_tracks(
        tmp_path, tracks=("mic",), config=MeetingConfig(transcription_engine="bad-ids")
    )
    run_preprocess(bundle)
    with pytest.raises(NarumiError) as excinfo:
        run_transcribe(bundle)
    assert excinfo.value.code == ErrorCode.INTERNAL
    assert bundle.artifact("transcripts/own-mic") is None


# ----------------------------------------------------------------------------- real engines
@pytest.mark.real
@needs_real
def test_real_mlx_whisper_tiny(tmp_path: Path, monkeypatch):
    pytest.importorskip("mlx_whisper")
    monkeypatch.setenv("NARUMI_WHISPER_MODEL", "mlx-community/whisper-tiny")
    wav = make_sine_wav(tmp_path / "a.wav", seconds=3.0)
    engine = get_engine("mlx-whisper")
    segments = engine.transcribe(wav, source_id="own-mic", language="ja", vocab_hints=["鳴海"])
    assert [s.id for s in segments] == [f"own-mic:{i}" for i in range(len(segments))]
    assert segments == engine.transcribe(
        wav, source_id="own-mic", language="ja", vocab_hints=["鳴海"]
    )


@pytest.mark.real
@needs_real
def test_real_faster_whisper_tiny(tmp_path: Path, monkeypatch):
    pytest.importorskip("faster_whisper")
    monkeypatch.setenv("NARUMI_WHISPER_MODEL", "tiny")
    wav = make_sine_wav(tmp_path / "a.wav", seconds=3.0)
    engine = get_engine("faster-whisper")
    segments = engine.transcribe(wav, source_id="own-mic", language="ja", vocab_hints=["鳴海"])
    assert [s.id for s in segments] == [f"own-mic:{i}" for i in range(len(segments))]
    assert segments == engine.transcribe(
        wav, source_id="own-mic", language="ja", vocab_hints=["鳴海"]
    )
