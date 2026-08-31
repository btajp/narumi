"""Synthetic audio and public provider setup for server transcription integration tests."""

from __future__ import annotations

import io
import json
import wave
from dataclasses import dataclass, replace
from uuid import uuid4

import pytest
from narumi.models import MeetingConfig
from narumi_server.app import dispatch
from narumi_server.context import build_context
from test_surface_tools import write_silence_wav

from pipeline.tests.audio_provider_fakes import (
    FakeAudioBackend,
    audio_model_descriptor,
    audio_result,
)
from pipeline.tests.provider_fakes import (
    FakeCodexBackend,
    FakeHTTPBackend,
    FakeMetadata,
    FakeRuntimeInspector,
    MemorySecretStore,
)

SECRET = "api-transcription-server-fixture-secret"
MODELS = ("whisper-1", "gpt-4o-transcribe-diarize")


def result(ctx, tool, args):
    outcome = dispatch(ctx, tool, args)
    assert not outcome.is_error, outcome.payload
    ctx.contracts.validate_output(tool, outcome.payload)
    assert SECRET not in json.dumps(outcome.payload)
    return outcome.payload


def rejected(ctx, tool, args, code):
    outcome = dispatch(ctx, tool, args)
    assert outcome.is_error, outcome.payload
    ctx.contracts.validate_error_envelope(outcome.payload)
    assert outcome.payload["error"]["code"] == code, outcome.payload
    assert SECRET not in json.dumps(outcome.payload)
    return outcome.payload["error"]


def audio_models():
    return [audio_model_descriptor(model_id) for model_id in MODELS]


def checked_audio_backend():
    """Check real chunk bytes and retain native sub-second times in the shared fake."""
    backend = FakeAudioBackend()

    def observe(call):
        duration = call["chunk_duration"]
        with wave.open(io.BytesIO(call["audio"]), "rb") as handle:
            assert (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) == (
                1,
                2,
                16000,
            )
            assert handle.getnframes() / 16000 == duration
        response = audio_result(call["model_id"], duration=duration, text="合成音声の発話")
        backend.response = replace(
            response,
            segments=(replace(response.segments[0], start=0.125, end=0.75),),
            words=(replace(response.words[0], start=0.125, end=0.75),) if response.words else None,
            usage={"type": "duration", "seconds": duration},
        )

    backend.on_call = observe
    return backend


def context(home, secrets, metadata, audio, *, transport="streamable-http"):
    ctx = build_context(
        home,
        transports=[transport],
        validate_output=True,
        provider_secret_store=secrets,
        provider_metadata_client=metadata,
        provider_codex_backend=FakeCodexBackend(),
        provider_http_backend=FakeHTTPBackend(),
        provider_audio_backend=audio,
    )
    ctx.providers.runtime.inspector = FakeRuntimeInspector()
    return ctx


@dataclass
class ASRContext:
    ctx: object
    secrets: MemorySecretStore
    metadata: FakeMetadata
    audio: FakeAudioBackend
    config: dict

    def restart(self):
        self.ctx.close()
        self.ctx = context(self.ctx.data_root, self.secrets, self.metadata, self.audio)


@pytest.fixture
def asr_context(home, request, monkeypatch):
    model_id = getattr(request, "param", "whisper-1")
    secrets, metadata, audio = (
        MemorySecretStore(),
        FakeMetadata(audio_models()),
        checked_audio_backend(),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-api-transcription-fixture-key")
    state = ASRContext(context(home, secrets, metadata, audio), secrets, metadata, audio, {})
    try:
        connection = result(
            state.ctx,
            "set_provider_connection",
            {
                "provider_id": "openai-api",
                "display_name": "合成音声のAPI接続",
                "auth_method": "api_key",
                "api_key": SECRET,
                "request_id": str(uuid4()),
            },
        )["connection"]
        cid = connection["connection_id"]
        assert (
            result(
                state.ctx, "list_provider_models", {"connection_id": cid, "role": "transcription"}
            )["models"]
            == []
        )
        assert metadata.calls == audio.calls == []
        providers = result(state.ctx, "list_providers", {})["providers"]
        runtime = next(item["runtime"] for item in providers if item["provider_id"] == "openai-api")
        prepared = result(
            state.ctx,
            "prepare_provider_runtime",
            {
                "provider_id": "openai-api",
                "resource_id": runtime["resources"][0]["resource_id"],
                "expected_catalog_revision": runtime["catalog_revision"],
                "action": "prepare",
                "request_id": str(uuid4()),
            },
        )
        assert state.ctx.jobs.wait(prepared["job_id"], timeout=5)["status"] == "succeeded"
        models = result(
            state.ctx,
            "list_provider_models",
            {
                "connection_id": cid,
                "role": "transcription",
                "refresh": True,
            },
        )
        assert models["catalog_state"] == "ready"
        assert {item["model_id"] for item in models["models"]} == set(MODELS)
        assert metadata.calls == [("openai-api", "https://api.openai.com", SECRET)]
        assert (
            result(state.ctx, "list_provider_models", {"connection_id": cid, "role": "llm"})[
                "models"
            ]
            == []
        )
        assert audio.calls == []
        state.config = MeetingConfig.model_validate(
            {
                "transcription_engine": "fake",
                "diarization_engine": "none",
                "llm_provider": "none",
                "external_send_policy": "api_ok",
                "language": "ja",
                "vocab_hints": ["社内略語"],
                "transcription_model": {
                    "provider": "openai-api",
                    "connection_id": cid,
                    "connection_revision": connection["revision"],
                    "model_id": model_id,
                },
            }
        ).model_dump(mode="json")
        yield state
    finally:
        state.ctx.close()


def import_audio(state, *, auto_process=False, profile=None, config=None):
    root = state.ctx.data_root.parent
    args = {
        "meeting_name": "合成音声ASR検証",
        "mic_path": str(write_silence_wav(root / "mic.wav", seconds=1.0)),
        "system_path": str(write_silence_wav(root / "system.wav", seconds=1.25)),
        "auto_process": auto_process,
        "request_id": str(uuid4()),
    }
    if profile is not None:
        args["profile"] = profile
    else:
        args["config"] = state.config if config is None else config
    return result(state.ctx, "import_recording", args)


def current_config(state, meeting_id):
    return result(state.ctx, "get_meeting", {"meeting_id": meeting_id})["config"]


def wait_job(state, job_id):
    state.ctx.jobs.wait(job_id, timeout=15)
    return result(state.ctx, "get_job_status", {"job_id": job_id})["job"]


def regenerate(state, meeting_id, *, retry=None):
    args = {
        "meeting_id": meeting_id,
        "expected_config": current_config(state, meeting_id),
        "request_id": str(uuid4()),
    }
    if retry is not None:
        args["transcription_retry"] = retry
    receipt = result(state.ctx, "regenerate", args)
    return wait_job(state, receipt["job_id"])


def assert_no_secret(state, caplog):
    assert SECRET not in caplog.text
    for path in state.ctx.data_root.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path.name
