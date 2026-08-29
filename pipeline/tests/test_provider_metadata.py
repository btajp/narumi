"""Discovery uses fake HTTP exclusively, with real public-contract validation."""

from __future__ import annotations

import builtins
import copy
from datetime import UTC, datetime

import pytest
from jsonschema import Draft202012Validator
from narumi.contracts import load_contracts
from narumi.errors import (
    AuthenticationRequiredError,
    CancelledError,
    EngineUnavailableError,
    InvalidArgumentError,
    ModelUnavailableError,
)
from narumi.providers.metadata import MetadataClient, validate_endpoint

API = "https://api.anthropic.com"
LOCAL = "http://127.0.0.1:11434"
KEY = "fixture-metadata-key-not-a-real-credential"
NOW = datetime(2026, 8, 28, 9, tzinfo=UTC)
DIGEST = "a" * 64


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        assert self.responses, "unexpected metadata request"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


def client(*responses, **kwargs):
    http = FakeHTTP(responses)
    return MetadataClient(http=http, now=lambda: NOW, **kwargs), http


def api_model(model_id="fixture-model", **changes):
    result = {
        "id": model_id,
        "display_name": "Fixture model",
        "type": "model",
        "max_input_tokens": 200_000,
        "max_tokens": 8192,
        "capabilities": {
            "image_input": {"supported": True},
            "thinking": {"supported": True},
            "effort": {"supported": True, "max": {"supported": True}},
        },
    }
    result.update(changes)
    return result


def page(*models, has_more=False, last_id=None):
    return {"data": list(models), "has_more": has_more, "last_id": last_id}


def local_model(model_id="fixture:1", **changes):
    result = {
        "name": model_id,
        "model": model_id,
        "digest": DIGEST,
        "size": 1200,
        "details": {"format": "gguf"},
    }
    result.update(changes)
    return result


def local_show(**changes):
    result = {
        "details": {"format": "gguf"},
        "capabilities": ["completion", "vision", "tools", "thinking"],
        "model_info": {"general.architecture": "fixture", "fixture.context_length": 8192},
        "modelfile": "FROM /a/private/local/model/path",
        "system": "Private system prompt",
        "messages": [{"role": "user", "content": "Private model history"}],
    }
    result.update(changes)
    return result


def validate_models(models):
    schema = {"$ref": "#/$defs/provider_model_descriptor", "$defs": load_contracts().defs}
    validator = Draft202012Validator(schema)
    for model in models:
        validator.validate(model)


def test_anthropic_discovery_intersects_adapter_capabilities_and_contract():
    metadata, http = client(page(api_model()))
    models = metadata.fetch("anthropic-api", API, KEY)
    validate_models(models)
    model = models[0]
    assert model["availability"] == "available"
    assert model["input_modalities"] == ["text", "image"]
    assert model["parameter_schema"]["properties"] == {
        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 8192, "default": 4096}
    }
    assert model["resolved_revision"] is None
    assert model["billing"]["kind"] == "api"
    assert all(value is None for key, value in model["billing"].items() if key != "kind")
    assert http.calls == [
        {
            "method": "GET",
            "url": API + "/v1/models?limit=100",
            "headers": {"x-api-key": KEY, "anthropic-version": "2023-06-01"},
            "payload": None,
            "timeout": 10.0,
        }
    ]


def test_missing_capabilities_and_limits_are_not_invented():
    metadata, _ = client(page(api_model(capabilities=None, max_tokens=None, max_input_tokens=None)))
    model = metadata.fetch("anthropic-api", API, KEY)[0]
    assert model["availability"] == "unverified"
    assert model["reason"] == "model_capabilities_unavailable"
    assert model["input_modalities"] == ["text"]
    assert model["context_window"] is model["max_output_tokens"] is None
    validate_models([model])


@pytest.mark.parametrize(
    ("known_limit", "maximum", "default"),
    [
        (None, 32768, 4096),
        (1, 1, 1),
        (2048, 2048, 2048),
        (32768, 32768, 4096),
        (128000, 32768, 4096),
    ],
)
def test_anthropic_output_option_separates_application_limit_from_model_capability(
    known_limit, maximum, default
):
    metadata, _ = client(page(api_model(max_tokens=known_limit)))
    model = metadata.fetch("anthropic-api", API, KEY)[0]
    assert model["max_output_tokens"] == known_limit
    option = model["parameter_schema"]["properties"]["max_tokens"]
    assert option == {"type": "integer", "minimum": 1, "maximum": maximum, "default": default}
    validator = Draft202012Validator(model["parameter_schema"])
    validator.validate({"max_tokens": maximum})
    for parameters in (
        {"max_tokens": 0},
        {"max_tokens": maximum + 1},
        {"max_tokens": True},
        {"reasoning_effort": "high"},
    ):
        assert not validator.is_valid(parameters)
    validate_models([model])


def test_sdk_candidates_do_not_import_sdk_or_claim_generation(monkeypatch):
    original_import = builtins.__import__

    def no_sdk(name, *args, **kwargs):
        assert not name.startswith(("anthropic", "claude_agent_sdk")), "SDK was imported"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sdk)
    metadata, _ = client(page(api_model()))
    model = metadata.fetch("claude-agent-sdk", API, KEY)[0]
    assert model["availability"] == "unverified"
    assert model["reason"] == "sdk_authentication_and_history_isolation_unverified"
    assert model["input_modalities"] == ["text"]
    assert model["parameter_schema"]["properties"] == {}
    assert model["billing"]["kind"] == "api"
    validate_models([model])


def test_pagination_is_explicit_bounded_and_deduplicated():
    metadata, http = client(
        page(api_model("fixture-one"), has_more=True, last_id="fixture-one"),
        page(api_model("fixture-two")),
    )
    assert len(metadata.fetch("anthropic-api", API, KEY)) == 2
    assert http.calls[1]["url"] == API + "/v1/models?limit=100&after_id=fixture-one"
    metadata, http = client(
        *[page(api_model(f"fixture-{i}"), has_more=True, last_id=f"fixture-{i}") for i in range(5)]
    )
    with pytest.raises(EngineUnavailableError) as failure:
        metadata.fetch("anthropic-api", API, KEY)
    assert failure.value.details["reason"] == "metadata_page_limit"
    assert len(http.calls) == 5


@pytest.mark.parametrize(
    "response",
    [
        {"data": [], "has_more": True, "last_id": "not-present"},
        page(api_model(), api_model()),
        page(api_model(max_tokens=True)),
        page(api_model(capabilities={"image_input": {"supported": "yes"}})),
        page(api_model(id="bad\nidentifier")),
        page(api_model(display_name="sk-ant-fixture-secret")),
        page(api_model(), has_more=True, last_id="https://malicious.example"),
        {"data": "unexpected", "has_more": False},
        [],
    ],
)
def test_malformed_catalogs_fail_without_returning_raw_payload(response):
    metadata, _ = client(response)
    with pytest.raises(EngineUnavailableError) as failure:
        metadata.fetch("anthropic-api", API, KEY)
    assert "sk-ant-fixture-secret" not in str(failure.value.to_payload())
    assert "malicious.example" not in str(failure.value.to_payload())


def test_credentials_never_come_from_environment_or_echo_in_results(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "Authorization: fixture-inherited")
    metadata, http = client(page(api_model(display_name=KEY)))
    with pytest.raises(AuthenticationRequiredError):
        metadata.fetch("anthropic-api", API, None)
    assert not http.calls
    with pytest.raises(EngineUnavailableError) as failure:
        metadata.fetch("anthropic-api", API, KEY)
    assert KEY not in str(failure.value.to_payload())
    assert "fixture-inherited" not in str(http.calls)


def test_ollama_verifies_local_selector_and_discards_private_model_content():
    metadata, http = client({"models": [local_model()]}, local_show())
    models = metadata.fetch("ollama", LOCAL, None)
    validate_models(models)
    model = models[0]
    assert model["availability"] == "available"
    assert model["resolved_revision"] == DIGEST
    assert model["context_window"] == 8192
    assert model["max_output_tokens"] is None
    assert model["input_modalities"] == ["text", "image"]
    assert model["parameter_schema"]["properties"] == {
        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 32768, "default": 4096}
    }
    assert model["billing"]["kind"] == "local"
    assert "private" not in str(models).lower()
    assert http.calls[1]["payload"] == {"model": "fixture:1:local"}
    assert [call["url"] for call in http.calls] == [LOCAL + "/api/tags", LOCAL + "/api/show"]


@pytest.mark.parametrize(
    "raw",
    [
        local_model("fixture:cloud"),
        local_model("fixture:1-cloud"),
        local_model(remote_host="https://remote.example", remote_model="other"),
        local_model(remote_host={"unexpected": "value"}),
        local_model(remote_model="remote-only"),
    ],
)
def test_cloud_candidates_are_visible_but_never_trigger_show(raw):
    metadata, http = client({"models": [raw]})
    models = metadata.fetch("ollama", LOCAL, None)
    assert models[0]["availability"] == "unsupported"
    assert models[0]["reason"] == "remote_models_not_supported"
    assert len(http.calls) == 1
    validate_models(models)


@pytest.mark.parametrize(
    "show",
    [
        local_show(remote_host="https://remote.example"),
        local_show(capabilities=["embedding"]),
        local_show(capabilities=None),
        local_show(details={"format": "unknown"}),
        EngineUnavailableError("fixture failure"),
    ],
)
def test_unverified_models_do_not_silently_fallback(show):
    metadata, http = client({"models": [local_model()]}, show)
    with pytest.raises(ModelUnavailableError):
        metadata.require_local_ollama_model(LOCAL, "fixture:1")
    assert len(http.calls) == 2
    assert http.calls[1]["payload"]["model"] == "fixture:1:local"


def test_missing_local_proof_does_not_call_show():
    metadata, http = client({"models": [local_model(digest=None)]})
    models = metadata.fetch("ollama", LOCAL, None)
    assert models[0]["availability"] == "unverified"
    assert models[0]["reason"] == "local_model_metadata_unverified"
    assert len(http.calls) == 1


def test_ollama_unknown_context_and_output_limits_remain_unknown():
    metadata, _ = client({"models": [local_model()]}, local_show(model_info={}))
    model = metadata.require_local_ollama_model(LOCAL, "fixture:1")
    assert model["context_window"] is model["max_output_tokens"] is None
    assert model["parameter_schema"]["properties"]["max_tokens"]["maximum"] == 32768
    assert model["parameter_schema"]["properties"]["max_tokens"]["default"] == 4096
    validate_models([model])


def test_local_model_verification_checks_cancellation_before_request():
    metadata, http = client()
    with pytest.raises(CancelledError):
        metadata.require_local_ollama_model(LOCAL, "fixture:1", should_cancel=lambda: True)
    assert http.calls == []


def test_local_model_verification_forwards_cancellation_and_stops_between_requests():
    calls = iter([False, True])

    def should_cancel():
        return next(calls)

    metadata, http = client({"models": [local_model()]})
    with pytest.raises(CancelledError):
        metadata.require_local_ollama_model(LOCAL, "fixture:1", should_cancel=should_cancel)
    assert len(http.calls) == 1
    assert http.calls[0]["should_cancel"] is should_cancel


def test_local_model_verification_does_not_hide_http_cancellation_as_unverified():
    metadata, http = client({"models": [local_model()]}, CancelledError("fixture cancelled"))
    with pytest.raises(CancelledError):
        metadata.require_local_ollama_model(LOCAL, "fixture:1", should_cancel=lambda: False)
    assert len(http.calls) == 2


def test_discovery_has_an_overall_deadline():
    ticks = iter([0.0, 31.0])
    metadata, http = client(monotonic=lambda: next(ticks))
    with pytest.raises(EngineUnavailableError) as failure:
        metadata.fetch("ollama", LOCAL, None)
    assert failure.value.details["reason"] == "metadata_timeout"
    assert not http.calls


def test_oversized_catalog_stops_before_model_detail_requests():
    metadata, http = client({"models": [local_model(f"fixture:{i}") for i in range(201)]})
    with pytest.raises(EngineUnavailableError) as failure:
        metadata.fetch("ollama", LOCAL, None)
    assert failure.value.details["reason"] == "metadata_catalog_limit"
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:11434",
        "http://remote.example:11434",
        "http://192.168.0.1:11434",
        "http://127.0.0.1@remote.example",
        "http://user:secret@127.0.0.1",
        "http://127.0.0.1/path",
        "http://127.0.0.1?query=secret",
        "http://127.0.0.1#fragment",
        "file:///tmp/socket",
        "http://[::ffff:127.0.0.1]",
        "http://[::1%25en0]",
        "http://127.0.0.1:0",
        "http://127.0.0.1:99999",
        "http://127.0.0.1\n",
    ],
)
def test_endpoints_reject_dns_remote_origins_and_embedded_secrets(endpoint):
    with pytest.raises(InvalidArgumentError) as failure:
        validate_endpoint("ollama", endpoint)
    assert endpoint not in str(failure.value.to_payload())


def test_endpoint_canonicalization_keeps_only_allowed_origins():
    assert validate_endpoint("anthropic-api", API + "/") == API
    assert validate_endpoint("claude-agent-sdk", API + ":443") == API
    assert validate_endpoint("ollama", LOCAL + "/") == LOCAL
    assert validate_endpoint("ollama", "https://[0:0:0:0:0:0:0:1]:11434") == "https://[::1]:11434"
    assert validate_endpoint("ollama", "http://127.1.2.3:42") == "http://127.1.2.3:42"
    with pytest.raises(InvalidArgumentError):
        validate_endpoint("anthropic-api", "https://api.anthropic.com.attacker.example")
    with pytest.raises(InvalidArgumentError):
        validate_endpoint("anthropic-api", "http://api.anthropic.com")
    with pytest.raises(InvalidArgumentError):
        validate_endpoint("unsupported", API)


@pytest.mark.parametrize("credential", [None, KEY])
def test_codex_never_reuses_the_anthropic_http_metadata_adapter(credential):
    metadata, http = client()
    with pytest.raises(InvalidArgumentError):
        metadata.fetch("codex-app-server", "https://chatgpt.com", credential)
    assert http.calls == []
