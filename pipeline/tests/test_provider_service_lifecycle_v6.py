"""ProviderService owns lazy adapters without exposing shutdown implementation failures."""

from __future__ import annotations

from concurrent.futures import Future

import narumi.providers.audio_transcription as audio_module
import narumi.providers.claude as claude_module
import narumi.providers.codex as codex_module
import narumi.providers.http_generation as http_module
import narumi.providers.openai_compatible as compatible_module
import narumi.providers.service as service_module
import pytest
from narumi.errors import NarumiError
from narumi.providers.service import ProviderService

from .provider_fakes import MemorySecretStore


class FakeBackend:
    def __init__(self, name: str, events: list[tuple[str, str]], *, fail_close: bool = False):
        self.name = name
        self.events = events
        self.fail_close = fail_close

    def close(self) -> None:
        self.events.append(("close", self.name))
        if self.fail_close:
            raise RuntimeError("private upstream token at /private/provider/session")


class FakeExecutor:
    def __init__(self, *args, **kwargs):
        self.constructor_args = args
        self.constructor_kwargs = kwargs
        self.shutdown_calls: list[tuple[bool, bool]] = []

    def submit(self, function, *args) -> Future:
        raise AssertionError("lifecycle tests must not schedule provider work")

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        self.shutdown_calls.append((wait, cancel_futures))


def install_backend_factories(monkeypatch, events):
    created: dict[str, FakeBackend] = {}

    def construct(name: str) -> FakeBackend:
        events.append(("construct", name))
        backend = FakeBackend(name, events)
        created[name] = backend
        return backend

    monkeypatch.setattr(codex_module, "CodexBackend", lambda _root: construct("codex"))
    monkeypatch.setattr(claude_module, "ClaudeSDKBackend", lambda _root: construct("claude"))
    monkeypatch.setattr(
        compatible_module, "OpenAICompatibleBackend", lambda: construct("openai-compatible")
    )
    monkeypatch.setattr(http_module, "HTTPMinutesBackend", lambda **_kwargs: construct("http"))
    monkeypatch.setattr(audio_module, "AudioTranscriptionBackend", lambda: construct("audio"))
    return created


def test_default_backends_are_constructed_lazily_once(tmp_path, monkeypatch):
    events: list[tuple[str, str]] = []
    created = install_backend_factories(monkeypatch, events)
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        auth_executor=FakeExecutor(),
        recover=False,
    )

    assert events == []
    assert service.list_connections() == {"connections": []}
    assert events == []

    properties = {
        "codex": "codex_backend",
        "claude": "claude_backend",
        "openai-compatible": "openai_compatible_backend",
        "http": "http_backend",
        "audio": "audio_backend",
    }
    for name, property_name in properties.items():
        backend = getattr(service, property_name)
        assert backend is created[name]
        assert getattr(service, property_name) is backend

    assert [event for event in events if event[0] == "construct"] == [
        ("construct", name) for name in properties
    ]
    service.close()


def test_injected_backends_are_used_and_closed(tmp_path):
    events: list[tuple[str, str]] = []
    backends = {
        "codex_backend": FakeBackend("codex", events),
        "claude_backend": FakeBackend("claude", events),
        "openai_compatible_backend": FakeBackend("openai-compatible", events),
        "http_backend": FakeBackend("http", events),
        "audio_backend": FakeBackend("audio", events),
    }
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        auth_executor=FakeExecutor(),
        recover=False,
        **backends,
    )

    for property_name, backend in backends.items():
        assert getattr(service, property_name) is backend

    service.close()
    assert events == [
        ("close", "codex"),
        ("close", "claude"),
        ("close", "openai-compatible"),
        ("close", "http"),
        ("close", "audio"),
    ]


def test_close_attempts_every_backend_and_owned_executor_without_exposing_raw_error(
    tmp_path, monkeypatch
):
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(service_module, "ThreadPoolExecutor", FakeExecutor)
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        recover=False,
        codex_backend=FakeBackend("codex", events),
        claude_backend=FakeBackend("claude", events, fail_close=True),
        openai_compatible_backend=FakeBackend("openai-compatible", events),
        http_backend=FakeBackend("http", events),
        audio_backend=FakeBackend("audio", events),
    )

    with pytest.raises(NarumiError) as failure:
        service.close()

    assert events == [
        ("close", "codex"),
        ("close", "claude"),
        ("close", "openai-compatible"),
        ("close", "http"),
        ("close", "audio"),
    ]
    assert service.auth_executor.shutdown_calls == [(False, True)]
    assert failure.value.to_payload() == {
        "error": {
            "code": "internal",
            "message": "Provider runtime shutdown could not be confirmed",
        }
    }
    assert "private upstream token" not in str(failure.value)
    assert "/private/provider/session" not in str(failure.value)


def test_closed_service_does_not_construct_a_new_backend(tmp_path, monkeypatch):
    events: list[tuple[str, str]] = []
    install_backend_factories(monkeypatch, events)
    service = ProviderService(
        tmp_path,
        secret_store=MemorySecretStore(),
        auth_executor=FakeExecutor(),
        recover=False,
    )
    service.close()

    for property_name in (
        "codex_backend",
        "claude_backend",
        "openai_compatible_backend",
        "http_backend",
        "audio_backend",
    ):
        with pytest.raises(NarumiError, match="Provider service is closed"):
            getattr(service, property_name)

    assert events == []
