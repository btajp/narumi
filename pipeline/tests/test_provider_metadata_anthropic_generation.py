"""Anthropic generation uses only fake HTTP and fixture credentials."""

from __future__ import annotations

import base64
import builtins
import io
import json
import logging
import traceback
import urllib.error
import urllib.request

import pytest
from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.llm.anthropic_api import DEFAULT_MODEL, MESSAGES_URL, AnthropicAPIProvider
from narumi.providers.metadata.http import JSONHTTPClient, RejectRedirects

KEY = "fixture-private-anthropic-key-not-real"
AMBIENT_KEY = "fixture-ambient-key-not-real"


def message(**overrides):
    return {
        "type": "message",
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": " Fixture completion "}],
        **overrides,
    }


class FakeHTTP:
    def __init__(self, body=None, *, failure=None):
        self.body = message() if body is None else body
        self.failure = failure
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.failure is not None:
            raise self.failure
        return self.body


class Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, body):
        super().__init__(json.dumps(body).encode())

    def geturl(self):
        return MESSAGES_URL


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        assert len(self.calls) == 1, "generation must not retry or fall back"
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def assert_private_error(error, caplog):
    assert KEY not in json.dumps(error.to_payload())
    assert KEY not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None and error.__suppress_context__ is True
    logging.getLogger(__name__).error(
        "Generation failed", exc_info=(type(error), error, error.__traceback__)
    )
    assert KEY not in caplog.text


def test_generation_preserves_explicit_model_images_and_text_only_output(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", AMBIENT_KEY)
    monkeypatch.setenv("NARUMI_ANTHROPIC_MODEL", "fixture-ambient-model")
    image = tmp_path / "slide.png"
    image.write_bytes(b"fixture image")
    http = FakeHTTP(
        message(
            content=[
                {"type": "thinking", "thinking": "private intermediate text"},
                {"type": "text", "text": " First\n"},
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "text", "text": "second "},
            ]
        )
    )
    provider = AnthropicAPIProvider(model="fixture-model", api_key=KEY, http=http)
    assert provider.complete("private prompt", system="system", images=[image], max_tokens=20) == (
        "First\nsecond"
    )
    assert http.calls == [
        {
            "method": "POST",
            "url": MESSAGES_URL,
            "headers": {"x-api-key": KEY, "anthropic-version": "2023-06-01"},
            "payload": {
                "model": "fixture-model",
                "max_tokens": 20,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": base64.standard_b64encode(image.read_bytes()).decode(),
                                },
                            },
                            {"type": "text", "text": "private prompt"},
                        ],
                    }
                ],
                "system": "system",
            },
            "timeout": 600.0,
            "response_kind": "generation",
        }
    ]
    assert provider.profile.tool_use is False


def test_default_transport_ignores_ambient_sdk_endpoint_headers_and_proxy(monkeypatch):
    ambient = {
        "ANTHROPIC_API_KEY": AMBIENT_KEY,
        "ANTHROPIC_AUTH_TOKEN": AMBIENT_KEY,
        "CLAUDE_CODE_OAUTH_TOKEN": AMBIENT_KEY,
        "ANTHROPIC_BASE_URL": "https://unapproved.invalid/",
        "ANTHROPIC_CUSTOM_HEADERS": f"Authorization: Bearer {AMBIENT_KEY}",
        "ANTHROPIC_LOG": "debug",
        **{
            name: "http://unapproved-proxy.invalid:8888"
            for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy")
        },
    }
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)
    opener = Opener(Response(message()))
    handlers = []
    real_import = builtins.__import__

    def no_sdk(name, *args, **kwargs):
        assert name != "anthropic" and not name.startswith("anthropic."), "SDK was imported"
        return real_import(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("ambient transport was used")

    def build(*configured_handlers):
        handlers.extend(configured_handlers)
        return opener

    monkeypatch.setattr(builtins, "__import__", no_sdk)
    monkeypatch.setattr(urllib.request, "build_opener", build)
    monkeypatch.setattr(urllib.request, "getproxies", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    provider = AnthropicAPIProvider(model="fixture-model", api_key=KEY)
    assert provider.complete("private prompt") == "Fixture completion"
    assert next(h for h in handlers if isinstance(h, urllib.request.ProxyHandler)).proxies == {}
    assert any(isinstance(h, RejectRedirects) for h in handlers)
    request, timeout = opener.calls[0]
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_method() == "POST" and timeout == 600.0
    assert {name.lower(): value for name, value in request.header_items()} == {
        "accept": "application/json",
        "accept-encoding": "identity",
        "content-type": "application/json",
        "x-api-key": KEY,
        "anthropic-version": "2023-06-01",
    }
    assert KEY not in request.data.decode() and AMBIENT_KEY not in str(request.header_items())


@pytest.mark.parametrize("environment_model", [None, "fixture-env-model"])
def test_legacy_model_and_key_environment_selection_is_preserved(monkeypatch, environment_model):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    if environment_model is None:
        monkeypatch.delenv("NARUMI_ANTHROPIC_MODEL", raising=False)
    else:
        monkeypatch.setenv("NARUMI_ANTHROPIC_MODEL", environment_model)
    http = FakeHTTP()
    provider = AnthropicAPIProvider(http=http)
    provider.complete("prompt")
    assert http.calls[0]["payload"]["model"] == (environment_model or DEFAULT_MODEL)
    assert http.calls[0]["payload"]["max_tokens"] == provider.profile.max_output_tokens
    assert http.calls[0]["headers"]["x-api-key"] == KEY


def test_explicit_empty_configuration_does_not_fall_back_to_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", AMBIENT_KEY)
    monkeypatch.setenv("NARUMI_ANTHROPIC_MODEL", "fixture-env-model")
    http = FakeHTTP()
    with pytest.raises(EngineUnavailableError):
        AnthropicAPIProvider(api_key="", http=http)
    with pytest.raises(InvalidArgumentError):
        AnthropicAPIProvider(api_key=KEY, model="", http=http)
    assert not http.calls


@pytest.mark.parametrize("key", ["bad\r\nheader", "has space", "非ascii", "a" * 4097])
def test_invalid_credential_is_rejected_before_transport(key):
    http = FakeHTTP()
    with pytest.raises(InvalidArgumentError) as failure:
        AnthropicAPIProvider(api_key=key, http=http)
    assert key not in str(failure.value.to_payload()) and not http.calls


@pytest.mark.parametrize("max_tokens", [0, -1, True, "10"])
def test_invalid_generation_limit_does_not_make_a_request(max_tokens):
    http = FakeHTTP()
    provider = AnthropicAPIProvider(api_key=KEY, http=http)
    with pytest.raises(InvalidArgumentError):
        provider.complete("prompt", max_tokens=max_tokens)
    assert not http.calls


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_upstream_error_body_and_traceback_never_enter_public_errors_or_logs(status, caplog):
    upstream = urllib.error.HTTPError(
        MESSAGES_URL + KEY, status, KEY, {"x-api-key": KEY}, io.BytesIO(KEY.encode())
    )
    opener = Opener(upstream)
    provider = AnthropicAPIProvider(api_key=KEY, http=JSONHTTPClient(opener=opener))
    with pytest.raises(EngineUnavailableError) as failure:
        provider.complete("prompt")
    assert_private_error(failure.value, caplog)
    assert upstream.fp.closed and len(opener.calls) == 1


@pytest.mark.parametrize("error", [RuntimeError(KEY), EngineUnavailableError(KEY)])
def test_unexpected_transport_exception_is_also_sanitized(error, caplog):
    http = FakeHTTP(failure=error)
    provider = AnthropicAPIProvider(api_key=KEY, http=http)
    with pytest.raises(EngineUnavailableError) as failure:
        provider.complete("prompt")
    assert_private_error(failure.value, caplog)
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        message(content=[{"type": "text", "text": KEY}]),
        message(unused={"reflected_key": KEY}),
    ],
)
def test_response_credential_reflections_fail_closed(body, caplog):
    opener = Opener(Response(body))
    provider = AnthropicAPIProvider(api_key=KEY, http=JSONHTTPClient(opener=opener))
    with pytest.raises(EngineUnavailableError) as failure:
        provider.complete("prompt")
    assert_private_error(failure.value, caplog)


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"type": "error", "error": KEY},
        message(stop_reason="refusal", content=[{"type": "text", "text": KEY}]),
        message(stop_reason=KEY),
        message(content=[]),
        message(content=[{"type": "text", "text": " \n "}]),
        message(content=[KEY]),
        message(content=KEY),
        message(content=[{"type": "text", "text": {"unsafe": KEY}}]),
        message(content=[{"type": "tool_use", "input": KEY}]),
        message(content=[{"type": KEY}]),
    ],
)
def test_refusal_empty_or_malformed_response_is_fixed_error_without_upstream_text(body, caplog):
    provider = AnthropicAPIProvider(api_key=KEY, http=FakeHTTP(body))
    with pytest.raises(EngineUnavailableError) as failure:
        provider.complete("prompt")
    assert failure.value.details == {"provider": "anthropic-api"}
    assert_private_error(failure.value, caplog)
