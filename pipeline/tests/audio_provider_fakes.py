"""Synthetic audio and explicit connection fixtures; no live credentials or HTTP."""

from __future__ import annotations

import copy
import io
import wave

from narumi.contracts.loader import load_contracts
from narumi.errors import CancelledError
from narumi.providers.metadata.audio_capabilities import audio_model_capabilities

from .provider_fakes import create_connection


def synthetic_wav(*, sample_count=16000):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * sample_count)
    return stream.getvalue()


def audio_model_descriptor(model_id="whisper-1"):
    capabilities = audio_model_capabilities(model_id)
    assert capabilities is not None
    model = copy.deepcopy(load_contracts()["list_provider_models"].output_examples[0]["models"][0])
    model.update(
        model_id=model_id,
        display_name=capabilities.display_name,
        resolved_revision=capabilities.resolved_revision,
        input_modalities=["audio"],
        output_modalities=["text"],
        roles=["transcription"],
        timestamp_support=capabilities.timestamp_support,
        parameter_schema=capabilities.parameter_schema(),
        availability=capabilities.availability,
        availability_expires_on=None,
        reason=capabilities.reason,
        source="provider_api",
    )
    return model


def audio_result(model_id="whisper-1", *, duration=1.0, text="合成音声の確認"):
    from narumi.providers.audio_response import (
        AudioSegment,
        AudioTranscriptionResult,
        AudioWord,
    )

    diarized = model_id == "gpt-4o-transcribe-diarize"
    return AudioTranscriptionResult(
        text=text,
        duration=duration,
        segments=(
            AudioSegment(
                native_id="0" if diarized else 0,
                start=0.0,
                end=duration,
                text=text,
                speaker="A" if diarized else None,
            ),
        ),
        words=None if diarized else (AudioWord(start=0.0, end=duration, text=text),),
        language=None if diarized else "japanese",
        usage=None,
    )


class FakeAudioBackend:
    """One-based failure injection and recorded arguments for deterministic chunk tests."""

    def __init__(self):
        self.calls = []
        self.failures = {}
        self.response = None
        self.on_call = None

    def transcribe(
        self,
        endpoint,
        api_key,
        model_id,
        audio,
        *,
        language="auto",
        parameters=None,
        chunk_duration,
        should_cancel=None,
    ):
        if should_cancel is not None and should_cancel():
            raise CancelledError("fixture audio cancelled")
        call = {
            "endpoint": endpoint,
            "api_key": api_key,
            "model_id": model_id,
            "audio": audio,
            "language": language,
            "parameters": copy.deepcopy(parameters),
            "chunk_duration": chunk_duration,
        }
        self.calls.append(call)
        if self.on_call is not None:
            self.on_call(call)
        if len(self.calls) in self.failures:
            raise self.failures[len(self.calls)]
        if self.response is not None:
            return copy.deepcopy(self.response)
        return audio_result(model_id, duration=chunk_duration)


def prepared_audio_connection(service, *, models=None, request_id="prepared-audio"):
    record = create_connection(service, provider_id="openai-api", request_id=request_id)
    service.metadata.models = copy.deepcopy(
        models if models is not None else [audio_model_descriptor()]
    )
    with service.store.transaction() as document:
        runtime = service.runtime._current("openai-api", document)
        runtime["state"] = "ready"
        document["runtimes"]["openai-api"] = runtime
    return service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": record["revision"]}
    )["connection"]
