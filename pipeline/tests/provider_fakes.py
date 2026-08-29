"""Effect fakes shared by provider settings, authentication and preparation tests."""

from __future__ import annotations

import copy
import uuid
from concurrent.futures import Future

from narumi.contracts.loader import load_contracts
from narumi.errors import AuthenticationRequiredError, CancelledError
from narumi.providers._common import AUTH_METHODS
from narumi.providers.runtime import RuntimeInspector

INSTANCE_ONE = "00000000-0000-4000-8000-000000000001"
INSTANCE_TWO = "00000000-0000-4000-8000-000000000002"


class MemorySecretStore:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.fail_set = False
        self.fail_delete = False

    def get(self, account):
        self.calls.append(("get", account))
        return self.values.get(account)

    def set(self, account, value):
        self.calls.append(("set", account))
        if self.fail_set and not account.endswith("request-hmac"):
            raise RuntimeError("unexpected upstream secret: fixture-key")
        self.values[account] = value

    def delete(self, account):
        self.calls.append(("delete", account))
        if self.fail_delete:
            raise RuntimeError("unexpected upstream secret: fixture-key")
        self.values.pop(account, None)


class FakeMetadata:
    def __init__(self, models=None):
        self.calls = []
        self.error = None
        self.models = (
            models
            if models is not None
            else load_contracts()["list_provider_models"].output_examples[0]["models"]
        )

    def fetch(self, provider_id, endpoint, api_key):
        self.calls.append((provider_id, endpoint, api_key))
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.models)


class ManualExecutor:
    def __init__(self):
        self.pending = []

    def submit(self, function, *args):
        future = Future()
        self.pending.append((future, function, args))
        return future

    def run_next(self):
        future, function, args = self.pending.pop(0)
        result = function(*args)
        future.set_result(result)
        return result


class FakeProgress:
    def __init__(self, job_id, *, cancelled=False):
        self.job_id = job_id
        self.cancelled = cancelled
        self.stages = []

    def __call__(self, stage, fraction):
        if self.cancelled:
            raise CancelledError("fixture cancellation")
        self.stages.append((stage, fraction))


class JobQueue:
    def __init__(self):
        self.calls = []

    def __call__(self, function):
        job_id = "job-" + uuid.uuid4().hex
        self.calls.append((job_id, function))
        return job_id

    def run(self, index=0, *, cancelled=False):
        job_id, function = self.calls[index]
        return function(FakeProgress(job_id, cancelled=cancelled))


class FakeRuntimeInspector(RuntimeInspector):
    def __init__(self):
        self.calls = []
        self.error = None
        self.version = "1.0.0"

    def resource(self, provider_id):
        return {
            "resource_id": {
                "anthropic-api": "anthropic-client",
                "ollama": "local-ollama",
                "claude-agent-sdk": "claude-sdk",
                "openai-api": "openai-client",
            }[provider_id],
            "display_name": "Installed runtime fixture",
            "kind": "runtime",
            "version": self.version,
            "source": "installed",
            "download_host": None,
            "sha256": "a" * 64,
            "license": "Fixture license",
        }

    def prepare(self, root, provider_id, resource, progress):
        self.calls.append((provider_id, resource))
        progress("fixture_runtime_inspection", 0.5)
        if self.error is not None:
            raise self.error


class FakeCodexBackend:
    """No subprocess, credential access or external request, including during construction."""

    def __init__(self):
        self.calls = []
        self.authenticated = set()
        self.version = "1.0.0"
        self.error = None
        self.complete_error = None
        self.response = "fixture completion"
        self.authorization_url = "https://auth.openai.com/codex/device"
        self.user_code = "FIXTURE-USER-CODE"
        self.on_auth = None
        model = copy.deepcopy(
            load_contracts()["list_provider_models"].output_examples[0]["models"][0]
        )
        model.update(
            model_id="codex-fixture-model",
            display_name="Codex fixture model",
            availability="available",
            reason=None,
            source="runtime",
            parameter_schema={
                "type": "object",
                "properties": {
                    "reasoning_effort": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": [],
                "additionalProperties": False,
            },
        )
        model["billing"]["kind"] = "subscription"
        self.models = [model]

    def resource(self):
        return {
            "resource_id": "codex-runtime",
            "display_name": "Codex runtime fixture",
            "kind": "runtime",
            "version": self.version,
            "source": "approved_download",
            "download_host": "github.com",
            "sha256": "a" * 64,
            "license": "Apache-2.0",
        }

    def prepare(self, resource, progress):
        self.calls.append(("prepare", resource))
        progress("fixture_codex_preparation", 0.5)
        if self.error is not None:
            raise self.error

    def authenticate(self, connection_id, *, on_authorization_code, cancelled, operation_id=None):
        self.calls.append(("authenticate", connection_id))
        on_authorization_code(self.authorization_url, self.user_code)
        if self.on_auth is not None:
            self.on_auth(connection_id, cancelled)
        if cancelled():
            raise CancelledError("fixture authentication cancelled")
        if self.error is not None:
            raise self.error
        self.authenticated.add(connection_id)

    def list_models(self, connection_id):
        self.calls.append(("list_models", connection_id))
        if self.error is not None:
            raise self.error
        if connection_id not in self.authenticated:
            raise AuthenticationRequiredError("fixture account is not authenticated")
        return copy.deepcopy(self.models)

    def logout(self, connection_id):
        self.calls.append(("logout", connection_id))
        if self.error is not None:
            raise self.error
        self.authenticated.discard(connection_id)

    def cancel_auth(self, connection_id, *, operation_id=None):
        self.calls.append(("cancel_auth", connection_id))

    def complete(
        self,
        connection_id,
        model_id,
        parameters,
        prompt,
        *,
        system=None,
        should_cancel=None,
        max_tokens=None,
    ):
        self.calls.append(("complete", connection_id, model_id, parameters, prompt, system))
        if should_cancel is not None and should_cancel():
            raise CancelledError("fixture generation cancelled")
        if self.complete_error is not None:
            raise self.complete_error
        return self.response

    def close(self):
        self.calls.append(("close",))


class FakeHTTPBackend:
    """Record explicit generation arguments without HTTP, ambient keys or worker threads."""

    def __init__(self):
        self.calls = []
        self.response = "fixture completion"
        self.returned_model = None
        self.usage = None
        self.complete_error = None

    def complete(
        self,
        provider_id,
        endpoint,
        api_key,
        model,
        parameters,
        prompt,
        *,
        system=None,
        should_cancel=None,
    ):
        from narumi.providers.http_generation import HTTPCompletionResult

        self.calls.append(
            (
                "complete",
                provider_id,
                endpoint,
                api_key,
                copy.deepcopy(model),
                copy.deepcopy(parameters),
                prompt,
                system,
            )
        )
        if should_cancel is not None and should_cancel():
            raise CancelledError("fixture generation cancelled")
        if self.complete_error is not None:
            raise self.complete_error
        return HTTPCompletionResult(
            self.response,
            self.returned_model if self.returned_model is not None else model["model_id"],
            copy.deepcopy(self.usage),
        )


def create_connection(
    service, *, provider_id="anthropic-api", key="fixture-key", request_id="create"
):
    args = {
        "provider_id": provider_id,
        "display_name": "Fixture connection",
        "auth_method": AUTH_METHODS[provider_id],
        "request_id": "provider-" + request_id,
    }
    if AUTH_METHODS[provider_id] == "api_key" and key is not None:
        args["api_key"] = key
    return service.set_connection(args)["connection"]


def prepared_codex_connection(service, *, models=None, request_id="prepared-codex"):
    """Seed verified local observations without performing authentication or generation."""
    record = create_connection(service, provider_id="codex-app-server", request_id=request_id)
    backend = service.codex_backend
    selected_models = copy.deepcopy(backend.models if models is None else models)
    fetched_at = "2026-08-29T00:00:00Z"
    with service.store.transaction() as document:
        runtime = service.runtime._current("codex-app-server", document)
        runtime["state"] = "ready"
        document["runtimes"]["codex-app-server"] = runtime
        saved = document["connections"][record["connection_id"]]
        saved.update(
            auth_state="authenticated",
            credential_present=True,
            catalog_state="ready",
            checked_at=fetched_at,
        )
        document["catalogs"][record["connection_id"]] = {
            "models": selected_models,
            "connection_revision": record["revision"],
            "runtime_catalog_revision": runtime["catalog_revision"],
            "catalog_id": uuid.uuid4().hex,
            "fetched_at": fetched_at,
        }
        record = copy.deepcopy(saved)
    backend.authenticated.add(record["connection_id"])
    return record


def prepared_http_connection(
    service, provider_id="openai-api", *, models=None, request_id="prepared-http", key="fixture-key"
):
    """Verify fake HTTP metadata after seeding a prepared, connection-independent adapter."""
    record = create_connection(service, provider_id=provider_id, request_id=request_id, key=key)
    if models is None:
        model = copy.deepcopy(
            load_contracts()["list_provider_models"].output_examples[0]["models"][0]
        )
        model.update(
            model_id={
                "openai-api": "gpt-4.1",
                "ollama": "fixture-local-model:latest",
            }.get(provider_id, "fixture-model"),
            availability="available",
            reason=None,
            source="runtime" if provider_id == "ollama" else "provider_api",
            resolved_revision="sha256:" + "b" * 64 if provider_id == "ollama" else None,
            parameter_schema={
                "type": "object",
                "properties": {
                    "max_tokens": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 32768,
                        "default": 4096,
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
        )
        model["billing"]["kind"] = "local" if provider_id == "ollama" else "api"
        models = [model]
    service.metadata.models = copy.deepcopy(models)
    with service.store.transaction() as document:
        runtime = service.runtime._current(provider_id, document)
        runtime["state"] = "ready"
        document["runtimes"][provider_id] = runtime
    result = service.test_connection(
        {"connection_id": record["connection_id"], "expected_revision": record["revision"]}
    )
    assert result["connected"] is True
    return result["connection"]


def api_transcription_config(model_id="whisper-1", **updates):
    """Explicit fake selection; neither configuration nor resolution opens a key store."""
    from narumi.models import MeetingConfig
    from narumi.transcription_selection import TranscriptionModelSelection

    values = {
        "transcription_engine": "fake",
        "transcription_model": TranscriptionModelSelection(
            provider="openai-api",
            connection_id="conn-0123456789abcdef",
            connection_revision=1,
            model_id=model_id,
        ),
        "external_send_policy": "api_ok",
    }
    return MeetingConfig(**(values | updates))


class FakeTranscriptionResolver:
    """A deterministic audio response with no service, metadata, key or network access."""

    def __init__(self):
        self.calls = []
        self.resolve_calls = []
        self.failures = {}
        self.after_reply = None
        self.reply_factory = None

    def resolve(self, config, *, should_cancel=None):
        from narumi.providers.audio_transcription import fixed_transcription_parameters

        selected = config.transcription_model
        assert selected is not None
        self.resolve_calls.append(config.model_copy(deep=True))
        self.transcription_params = {
            "provider": selected.provider,
            "connection_id": selected.connection_id,
            "connection_revision": selected.connection_revision,
            "model_id": selected.model_id,
            "language": config.language,
            "effective_parameters": fixed_transcription_parameters(
                selected.model_id, config.language
            ),
            "adapter_version": "fixture-1",
            "capability_table_version": "fixture-1",
            "runtime_version": "fixture-1",
            "runtime_sha256": "a" * 64,
            "runtime_catalog_revision": "fixture-1",
            "model_capabilities_sha256": "b" * 64,
            "endpoint": "https://api.openai.com",
        }
        return self

    def transcribe_chunk(self, audio, duration_sec):
        from narumi.providers.audio_response import (
            AudioSegment,
            AudioTranscriptionResult,
            AudioWord,
        )

        index = len(self.calls)
        self.calls.append((audio, duration_sec, copy.deepcopy(self.transcription_params)))
        if index in self.failures:
            raise self.failures[index]
        if self.reply_factory is not None:
            result = self.reply_factory(index, duration_sec)
        else:
            diarized = self.transcription_params["model_id"] == "gpt-4o-transcribe-diarize"
            end = min(duration_sec, 0.25)
            text = f"合成発話{index}"
            result = AudioTranscriptionResult(
                text=text,
                duration=duration_sec,
                segments=(
                    AudioSegment(
                        native_id=f"segment-{index}" if diarized else index,
                        start=0.0,
                        end=end,
                        text=text,
                        speaker="A" if diarized else None,
                    ),
                ),
                words=None if diarized else (AudioWord(start=0.0, end=end, text=text),),
                language=None if diarized else "ja",
            )
        if self.after_reply is not None:
            self.after_reply(index)
        return result
