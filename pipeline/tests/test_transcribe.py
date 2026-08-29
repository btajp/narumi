from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from narumi.bundle import Bundle, sha256_file
from narumi.errors import (
    CancelledError,
    ConfigurationConflictError,
    EngineUnavailableError,
    ErrorCode,
    InvalidArgumentError,
    NarumiError,
    PolicyViolationError,
)
from narumi.models import ExternalSendPolicy, MeetingConfig, Segment, Transcript
from narumi.preprocess import run_preprocess
from narumi.providers.audio_response import AudioSegment, AudioTranscriptionResult, AudioWord
from narumi.transcribe import (
    ENGINE_FACTORIES,
    EngineProfile,
    FakeEngine,
    FasterWhisperEngine,
    MlxWhisperEngine,
    api_transcript,
    available_engines,
    build_initial_prompt,
    check_send_policy,
    checkpoints,
    chunks,
    engine_profile,
    faster_whisper_engine,
    get_engine,
    mlx_whisper_engine,
    registry,
    run_transcribe,
    sidecar_path,
)
from narumi.transcription_selection import TranscriptionRetry

from .media_fixtures import make_bundle_with_tracks, make_sine_wav, write_sidecar
from .provider_fakes import FakeTranscriptionResolver, api_transcription_config

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


# ----------------------------------------------------------------------------- API coordinator
@pytest.fixture
def api_bundle(tmp_path: Path, monkeypatch):
    # The same sample-exact chunker runs with short fixture windows, without huge audio files.
    monkeypatch.setattr(chunks, "CHUNK_SAMPLES", 16_000)
    bundle = make_bundle_with_tracks(tmp_path, seconds=1.5, config=api_transcription_config())
    run_preprocess(bundle)
    return bundle


def _transcript(bundle: Bundle, track: str) -> Transcript:
    return Transcript.model_validate_json(
        bundle.artifact_path(f"transcripts/own-{track}").read_text()
    )


def _retry_proof(error: EngineUnavailableError) -> TranscriptionRetry:
    return TranscriptionRetry(
        **{
            key: error.details[key]
            for key in ("input_fingerprint", "chunk_fingerprint", "blocked_epoch")
        }
    )


@pytest.mark.parametrize("model_id", ["whisper-1", "gpt-4o-transcribe-diarize"])
def test_api_transcribe_native_timing_and_epoch_reuse(
    api_bundle: Bundle, model_id: str, monkeypatch
):
    bundle = api_bundle
    bundle.manifest.config.transcription_model.model_id = model_id
    bundle.manifest.config.vocab_hints = ["非送信の語彙"]
    resolver = FakeTranscriptionResolver()

    def local_engine_must_not_run(*args):
        pytest.fail("API selection must not resolve the legacy local engine")

    monkeypatch.setattr("narumi.transcribe.stage.get_engine", local_engine_must_not_run)
    progress = []
    results = run_transcribe(
        bundle,
        transcription_resolver=resolver,
        vocab_hints=["ブリーフの語彙も非送信"],
        progress=lambda stage, fraction: progress.append((stage, fraction)),
    )
    assert len(resolver.calls) == 4
    assert [call[1] for call in resolver.calls] == [1.0, 0.5, 1.0, 0.5]
    assert [stage.key for stage in results] == ["transcripts/own-mic", "transcripts/own-system"]
    assert [fraction for _, fraction in progress] == sorted(f for _, f in progress)
    assert progress[-1][1] == 1.0
    previous = {}
    for track in ("mic", "system"):
        transcript = _transcript(bundle, track)
        assert transcript.engine.name == "openai-api"
        assert transcript.engine.params["model"] == model_id
        assert transcript.time_offset == 0.0
        assert [segment.id for segment in transcript.segments] == [
            f"own-{track}:0",
            f"own-{track}:1",
        ]
        assert [(s.start, s.end) for s in transcript.segments] == [(0.0, 0.25), (1.0, 1.25)]
        if model_id == "whisper-1":
            assert all(s.speaker is None for s in transcript.segments)
            assert [(w.start, w.end) for s in transcript.segments for w in s.words] == [
                (0.0, 0.25),
                (1.0, 1.25),
            ]
        else:
            assert [s.speaker for s in transcript.segments] == [
                f"asr:{track}:0:A",
                f"asr:{track}:1:A",
            ]
            assert all(s.words is None for s in transcript.segments)
        path = bundle.artifact_path(f"transcripts/own-{track}")
        previous[track] = (path, path.read_bytes())
        assert path.parent.name == f"own-{track}" and len(path.stem) == 64
        assert "cache_epoch" not in path.read_text() and "非送信" not in path.read_text()

    bundle.manifest.config.transcription_model.cache_epoch = 1
    bundle.manifest.config.vocab_hints = ["語彙変更もAPI入力に影響しない"]
    bundle.save()
    bundle = Bundle.open(bundle.path)
    assert all(r.skipped for r in run_transcribe(bundle, transcription_resolver=resolver))
    assert len(resolver.calls) == 4
    for track, (path, payload) in previous.items():
        assert bundle.artifact_path(f"transcripts/own-{track}") == path
        assert path.read_bytes() == payload


@pytest.mark.parametrize("failed_call", [1, 2])
def test_api_unknown_requires_exact_proof_and_reuses_success(api_bundle: Bundle, failed_call: int):
    bundle = api_bundle
    resolver = FakeTranscriptionResolver()
    resolver.failures[failed_call] = EngineUnavailableError(
        "Synthetic ambiguous response", details={"outcome_unknown": True}
    )
    with pytest.raises(EngineUnavailableError) as failure:
        run_transcribe(bundle, transcription_resolver=resolver)
    details = failure.value.details
    assert details["reason"] == "transcription_outcome_unknown"
    assert details["chunk_index"] == failed_call and details["completed_chunks"] == failed_call
    assert details["chunk_count"] == 4 and details["blocked_epoch"] == 0
    assert (bundle.artifact("transcripts/own-mic") is not None) is (failed_call == 2)
    assert bundle.artifact("transcripts/own-system") is None
    proof = _retry_proof(failure.value)

    for epoch in (0, 1):
        bundle.manifest.config.transcription_model.cache_epoch = epoch
        bundle.save()
        bundle = Bundle.open(bundle.path)
        with pytest.raises(EngineUnavailableError) as again:
            run_transcribe(bundle, transcription_resolver=resolver)
        assert _retry_proof(again.value) == proof
        assert len(resolver.calls) == failed_call + 1
    for bad_proof in (
        proof.model_copy(update={"input_fingerprint": "c" * 64}),
        proof.model_copy(update={"chunk_fingerprint": "d" * 64}),
        proof.model_copy(update={"blocked_epoch": 1}),
    ):
        with pytest.raises(ConfigurationConflictError):
            run_transcribe(bundle, transcription_resolver=resolver, transcription_retry=bad_proof)
        assert len(resolver.calls) == failed_call + 1

    completed = run_transcribe(bundle, transcription_resolver=resolver, transcription_retry=proof)
    assert len(resolver.calls) == 5  # four successful chunks plus exactly one failed attempt
    assert resolver.calls[failed_call][0] == resolver.calls[failed_call + 1][0]
    assert [r.key for r in completed] == ["transcripts/own-mic", "transcripts/own-system"]
    assert _transcript(bundle, "mic").segments[0].text == "合成発話0"
    with pytest.raises(ConfigurationConflictError):
        run_transcribe(bundle, transcription_resolver=resolver, transcription_retry=proof)
    assert len(resolver.calls) == 5


def test_api_cancellation_after_reply_preserves_success(api_bundle: Bundle):
    resolver = FakeTranscriptionResolver()
    cancelled = False

    def cancel_after_reply(index):
        nonlocal cancelled
        cancelled = True

    resolver.after_reply = cancel_after_reply
    with pytest.raises(CancelledError):
        run_transcribe(api_bundle, transcription_resolver=resolver, should_cancel=lambda: cancelled)
    assert len(resolver.calls) == 1 and api_bundle.artifact("transcripts/own-mic") is None
    resolver.after_reply = None
    run_transcribe(Bundle.open(api_bundle.path), transcription_resolver=resolver)
    assert len(resolver.calls) == 4


def test_api_configuration_change_after_reply_does_not_publish_old_selection(api_bundle: Bundle):
    resolver = FakeTranscriptionResolver()

    def change_after_reply(index):
        if index == 1:
            api_bundle.manifest.config.language = "en"

    resolver.after_reply = change_after_reply
    with pytest.raises(ConfigurationConflictError):
        run_transcribe(api_bundle, transcription_resolver=resolver)
    assert len(resolver.calls) == 2 and api_bundle.artifact("transcripts/own-mic") is None
    resolver.after_reply = None
    api_bundle.manifest.config.language = "ja"
    run_transcribe(api_bundle, transcription_resolver=resolver)
    assert len(resolver.calls) == 4


def test_api_success_committed_before_fsync_error_is_not_failed_again(
    api_bundle: Bundle, monkeypatch
):
    resolver = FakeTranscriptionResolver()
    original = checkpoints.write_bytes
    injected = False

    def fail_after_success(directory, name, data, **kwargs):
        nonlocal injected
        original(directory, name, data, **kwargs)
        if name == "ledger.json" and not injected:
            document = json.loads(data)
            if any(entry["state"] == "succeeded" for entry in document["entries"].values()):
                injected = True
                raise OSError("synthetic directory fsync failure after replace")

    with monkeypatch.context() as patch:
        patch.setattr(checkpoints, "write_bytes", fail_after_success)
        with pytest.raises(EngineUnavailableError) as failure:
            run_transcribe(api_bundle, transcription_resolver=resolver)
        assert failure.value.details["reason"] == "transcription_outcome_unknown"
    assert injected and len(resolver.calls) == 1
    run_transcribe(Bundle.open(api_bundle.path), transcription_resolver=resolver)
    assert len(resolver.calls) == 4


def test_api_known_failure_resumes_same_epoch(api_bundle: Bundle):
    resolver = FakeTranscriptionResolver()
    resolver.failures[1] = EngineUnavailableError("Synthetic rejection before transcription")
    with pytest.raises(EngineUnavailableError, match="Synthetic rejection"):
        run_transcribe(api_bundle, transcription_resolver=resolver)
    run_transcribe(Bundle.open(api_bundle.path), transcription_resolver=resolver)
    assert len(resolver.calls) == 5
    assert resolver.calls[0][0] != resolver.calls[2][0]  # successful first window was not resent


def test_api_malformed_fresh_reply_is_unknown(api_bundle: Bundle):
    resolver = FakeTranscriptionResolver()
    resolver.reply_factory = lambda index, duration: AudioTranscriptionResult(
        text="invalid", duration=duration, segments=(AudioSegment(0, 0.0, duration + 1, "invalid"),)
    )
    for _ in range(2):
        with pytest.raises(EngineUnavailableError) as failure:
            run_transcribe(api_bundle, transcription_resolver=resolver)
        assert failure.value.details["reason"] == "transcription_outcome_unknown"
    assert len(resolver.calls) == 1
    assert api_bundle.artifact("transcripts/own-mic") is None


def test_api_word_attachment_uses_native_segment_and_word_bounds(api_bundle: Bundle):
    resolver = FakeTranscriptionResolver()
    resolver.reply_factory = lambda index, duration: AudioTranscriptionResult(
        text="one two",
        duration=duration,
        segments=(AudioSegment(0, 0.0, 0.2, "one"), AudioSegment(1, 0.2, 0.5, "two")),
        words=(AudioWord(0.0, 0.15, "one"), AudioWord(0.15, 0.4, "two")),
        language="ja",
    )
    run_transcribe(api_bundle, transcription_resolver=resolver)
    segments = _transcript(api_bundle, "mic").segments
    assert [(w.start, w.end) for s in segments for w in s.words] == [
        (0.0, 0.15),
        (0.15, 0.4),
        (1.0, 1.15),
        (1.15, 1.4),
    ]
    assert [w.text for w in segments[1].words] == ["two"]


def test_api_publication_failure_keeps_prior_source_and_cached_success(
    api_bundle: Bundle, monkeypatch
):
    selection = api_bundle.manifest.config.transcription_model
    api_bundle.manifest.config.transcription_model = None
    run_transcribe(api_bundle)
    prior = api_bundle.artifact_path("transcripts/own-mic")
    old_bytes = prior.read_bytes()
    api_bundle.manifest.config.transcription_model = selection
    api_bundle.save()
    resolver = FakeTranscriptionResolver()

    def fail_write(*args, **kwargs):
        raise OSError("synthetic artifact persistence failure")

    with monkeypatch.context() as patch:
        patch.setattr(api_transcript, "write_bytes", fail_write)
        with pytest.raises(EngineUnavailableError) as failure:
            run_transcribe(api_bundle, transcription_resolver=resolver)
        assert failure.value.details["reason"] == "transcription_artifact_unavailable"
    assert len(resolver.calls) == 2
    assert api_bundle.artifact_path("transcripts/own-mic") == prior
    assert prior.read_bytes() == old_bytes
    api_bundle = Bundle.open(api_bundle.path)
    run_transcribe(api_bundle, transcription_resolver=resolver)
    assert len(resolver.calls) == 4
    assert api_bundle.artifact_path("transcripts/own-mic") != prior
    assert prior.read_bytes() == old_bytes


def test_api_mic_extension_keeps_system_speaker_namespace_and_source(api_bundle: Bundle):
    api_bundle.manifest.config.transcription_model.model_id = "gpt-4o-transcribe-diarize"
    resolver = FakeTranscriptionResolver()
    run_transcribe(api_bundle, transcription_resolver=resolver)
    original = api_bundle.artifact("transcripts/own-system").model_dump()
    original_bytes = api_bundle.artifact_path("transcripts/own-system").read_bytes()
    record = api_bundle.manifest.recording.tracks["mic"]
    path = make_sine_wav(api_bundle.abspath(record.path), seconds=2.5)
    record.sha256, record.bytes, record.duration_sec = sha256_file(path), path.stat().st_size, 2.5
    api_bundle.manifest.recording.duration_sec = 2.5
    api_bundle.save()
    run_preprocess(api_bundle)
    results = run_transcribe(api_bundle, transcription_resolver=resolver)
    assert len(resolver.calls) == 7  # three changed mic windows, both system windows reused
    assert not results[0].skipped and results[1].skipped
    assert api_bundle.artifact("transcripts/own-system").model_dump() == original
    assert api_bundle.artifact_path("transcripts/own-system").read_bytes() == original_bytes


@pytest.mark.parametrize("change", ["model", "connection"])
def test_api_changed_selection_is_separate_input(api_bundle: Bundle, change: str):
    resolver = FakeTranscriptionResolver()
    run_transcribe(api_bundle, transcription_resolver=resolver)
    previous = api_bundle.artifact_path("transcripts/own-mic")
    if change == "model":
        api_bundle.manifest.config.transcription_model.model_id = "gpt-4o-transcribe-diarize"
    else:
        api_bundle.manifest.config.transcription_model.connection_revision = 2
    run_transcribe(api_bundle, transcription_resolver=resolver)
    assert len(resolver.calls) == 8
    assert api_bundle.artifact_path("transcripts/own-mic") != previous and previous.is_file()
    api_bundle.manifest.config.transcription_model = None
    run_transcribe(api_bundle)
    assert _transcript(api_bundle, "mic").engine.name == "fake"


def test_api_guards_precede_audio_and_success_reuse(api_bundle: Bundle):
    resolver = FakeTranscriptionResolver()
    with pytest.raises(InvalidArgumentError):
        run_transcribe(api_bundle, force=True, transcription_resolver=resolver)
    with pytest.raises(CancelledError):
        run_transcribe(api_bundle, transcription_resolver=resolver, should_cancel=lambda: True)
    with pytest.raises(EngineUnavailableError):
        run_transcribe(api_bundle)
    api_bundle.manifest.config.external_send_policy = ExternalSendPolicy.LOCAL_ONLY
    with pytest.raises(PolicyViolationError):
        run_transcribe(api_bundle, transcription_resolver=resolver)
    assert resolver.resolve_calls == resolver.calls == []


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
