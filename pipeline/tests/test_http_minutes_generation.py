"""Saved selections drive bounded HTTP generation using only fixture credentials."""

from __future__ import annotations

import copy
import io
import json
import logging
import threading
import traceback
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from narumi.errors import (
    AuthenticationRequiredError,
    CancelledError,
    ConfigurationConflictError,
    EngineUnavailableError,
    InvalidArgumentError,
    ModelUnavailableError,
)
from narumi.providers.http_generation import HTTPCompletionResult, HTTPMinutesBackend
from narumi.providers.http_generation_response import OUTCOME_UNKNOWN
from narumi.providers.metadata.http import JSONHTTPClient
from narumi.providers.metadata.openai_capabilities import model_capabilities
from narumi.providers.metadata.validation import parameter_schema

KEY = "fixture-private-http-minutes-key-not-real"
ENDPOINTS = {
    "openai-api": "https://api.openai.com",
    "anthropic-api": "https://api.anthropic.com",
    "ollama": "http://127.0.0.1:11434",
}


def model(provider="openai-api", model_id=None, **updates):
    model_id = (
        model_id
        or {
            "openai-api": "gpt-5.4",
            "anthropic-api": "fixture-anthropic-model",
            "ollama": "fixture-llama:latest",
        }[provider]
    )
    capabilities = model_capabilities(model_id) if provider == "openai-api" else None
    return {
        "model_id": model_id,
        "resolved_revision": "a" * 64 if provider == "ollama" else None,
        "availability": "available",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "roles": ["llm"],
        "max_output_tokens": capabilities.max_output_tokens if capabilities else 8192,
        "parameter_schema": capabilities.parameter_schema()
        if capabilities
        else parameter_schema(8192),
        "source": "runtime" if provider == "ollama" else "provider_api",
        "billing": {"kind": "local" if provider == "ollama" else "api"},
        **updates,
    }


def reply(provider="openai-api", **updates):
    if provider == "openai-api":
        return {
            "object": "response",
            "status": "completed",
            "model": "gpt-5.4",
            "error": None,
            "incomplete_details": None,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": " # Fixture minutes\n "}],
                }
            ],
            **updates,
        }
    if provider == "anthropic-api":
        return {
            "type": "message",
            "role": "assistant",
            "model": "fixture-anthropic-model",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "content": [{"type": "text", "text": " # Fixture minutes\n "}],
            **updates,
        }
    return {
        "model": "fixture-llama:latest:local",
        "response": " # Fixture minutes\n ",
        "done": True,
        "done_reason": "stop",
        **updates,
    }


class FakeHTTP:
    def __init__(self, response=None, *, error=None):
        self.response = reply() if response is None else response
        self.error = error
        self.calls = []

    def request(self, method, url, **options):
        self.calls.append({"method": method, "url": url, **options})
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.response)


class FakeMetadata:
    def __init__(self, response=None, *, error=None):
        self.response = model("ollama") if response is None else response
        self.error = error
        self.calls = []

    def require_local_ollama_model(self, endpoint, model_id, **options):
        self.calls.append((endpoint, model_id, options))
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.response)


def generate(provider="openai-api", *, http=None, metadata=None, **options):
    return HTTPMinutesBackend(http=http or FakeHTTP(reply(provider)), metadata=metadata).complete(
        provider,
        options.pop("endpoint", ENDPOINTS[provider]),
        options.pop("api_key", None if provider == "ollama" else KEY),
        options.pop("model", model(provider)),
        options.pop("parameters", {"max_tokens": 2048}),
        options.pop("prompt", "Fixture meeting text"),
        **options,
    )


def assert_private(error, caplog):
    assert KEY not in str(error.to_payload())
    assert KEY not in "".join(traceback.format_exception(error))
    logging.getLogger(__name__).error(
        "Fixture generation failed", exc_info=(type(error), error, error.__traceback__)
    )
    assert KEY not in caplog.text


def test_openai_request_has_exact_model_effort_output_limit_and_no_inherited_state(monkeypatch):
    for name, value in {
        "OPENAI_API_KEY": "fixture-ambient-key",
        "OPENAI_BASE_URL": "https://unapproved.invalid",
        "OPENAI_MODEL": "fixture-ambient-model",
        "OPENAI_LOG": "debug",
    }.items():
        monkeypatch.setenv(name, value)
    selected = model(model_id="gpt-5.6-sol")
    http, metadata = FakeHTTP(reply(model="gpt-5.6-sol")), FakeMetadata()

    def cancel():
        return False

    result = generate(
        http=http,
        metadata=metadata,
        model=selected,
        parameters={"max_tokens": 200, "reasoning_effort": "high"},
        system="Fixed Markdown instructions",
        should_cancel=cancel,
    )
    assert result == HTTPCompletionResult("# Fixture minutes", "gpt-5.6-sol")
    assert http.calls == [
        {
            "method": "POST",
            "url": "https://api.openai.com/v1/responses",
            "headers": {"Authorization": "Bearer " + KEY},
            "timeout": 600.0,
            "response_kind": "generation",
            "should_cancel": cancel,
            "payload": {
                "model": "gpt-5.6-sol",
                "input": "Fixture meeting text",
                "instructions": "Fixed Markdown instructions",
                "tools": [],
                "tool_choice": "none",
                "store": False,
                "background": False,
                "stream": False,
                "truncation": "disabled",
                "max_output_tokens": 200,
                "reasoning": {"effort": "high", "mode": "standard", "context": "current_turn"},
            },
        }
    ]
    assert not metadata.calls


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("gpt-5.4", {"effort": "none"}),
        ("gpt-5.4-2026-03-05", {"effort": "none"}),
        ("gpt-4.1", None),
        ("gpt-4.1-mini-2025-04-14", None),
    ],
)
def test_openai_only_sends_confirmed_reasoning_parameters(model_id, expected):
    http = FakeHTTP(reply(model=model_id))
    generate(http=http, model=model(model_id=model_id))
    assert http.calls[0]["payload"].get("reasoning") == expected
    assert "text" not in http.calls[0]["payload"]  # Existing prompts request Markdown, not JSON.


def test_anthropic_uses_explicit_messages_model_key_and_limit():
    http = FakeHTTP(reply("anthropic-api"))
    generate("anthropic-api", http=http, system="Fixed instructions")
    assert http.calls[0] == {
        "method": "POST",
        "url": "https://api.anthropic.com/v1/messages",
        "headers": {"x-api-key": KEY, "anthropic-version": "2023-06-01"},
        "payload": {
            "model": "fixture-anthropic-model",
            "max_tokens": 2048,
            "stream": False,
            "system": "Fixed instructions",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Fixture meeting text"}]}
            ],
        },
        "timeout": 600.0,
        "response_kind": "generation",
        "should_cancel": None,
    }


def test_ollama_revalidates_same_local_digest_before_every_call():
    http, metadata = FakeHTTP(reply("ollama")), FakeMetadata()
    backend = HTTPMinutesBackend(http=http, metadata=metadata)
    for _ in range(2):
        result = backend.complete(
            "ollama",
            ENDPOINTS["ollama"],
            None,
            model("ollama"),
            {"max_tokens": 99},
            "Fixture meeting text",
            system="Fixed instructions",
        )
        assert result.returned_model == "fixture-llama:latest:local"
    assert len(metadata.calls) == len(http.calls) == 2
    assert all(call[:2] == (ENDPOINTS["ollama"], "fixture-llama:latest") for call in metadata.calls)
    assert http.calls[0]["payload"] == {
        "model": "fixture-llama:latest:local",
        "prompt": "Fixture meeting text",
        "stream": False,
        "system": "Fixed instructions",
        "options": {"num_predict": 99},
    }
    assert http.calls[0]["headers"] == {}


@pytest.mark.parametrize("revision", [None, "", "invalid", "B" * 64])
def test_unverified_ollama_digest_never_reaches_even_metadata(revision):
    http, metadata = FakeHTTP(reply("ollama")), FakeMetadata()
    with pytest.raises(ModelUnavailableError):
        generate(
            "ollama",
            http=http,
            metadata=metadata,
            model=model("ollama", resolved_revision=revision),
        )
    assert not http.calls and not metadata.calls


@pytest.mark.parametrize("changed", [{"resolved_revision": "b" * 64}, {"model_id": "other:latest"}])
def test_changed_local_model_requires_reselection_without_sending_text(changed):
    http, metadata = FakeHTTP(), FakeMetadata(model("ollama", **changed))
    with pytest.raises(ConfigurationConflictError):
        generate("ollama", http=http, metadata=metadata)
    assert not http.calls and len(metadata.calls) == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"availability": "unsupported"},
        {"source": "provider_api"},
        {"billing": {"kind": "api"}},
        {"output_modalities": ["audio"]},
    ],
)
def test_ollama_does_not_silently_use_an_unverified_or_remote_model(changed):
    http = FakeHTTP()
    with pytest.raises(ModelUnavailableError):
        generate("ollama", http=http, metadata=FakeMetadata(model("ollama", **changed)))
    assert not http.calls


@pytest.mark.parametrize("provider", ["openai-api", "anthropic-api"])
@pytest.mark.parametrize("key", [None, "", "has space", "bad\r\nheader", "非ascii", "x" * 4097])
def test_credentials_never_fall_back_to_environment(provider, key, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    http = FakeHTTP()
    with pytest.raises((AuthenticationRequiredError, InvalidArgumentError)):
        generate(provider, http=http, api_key=key)
    assert not http.calls


@pytest.mark.parametrize(
    "provider,endpoint",
    [
        ("openai-api", "https://unapproved.invalid"),
        ("openai-api", "https://api.openai.com/v1"),
        ("anthropic-api", "https://api.openai.com"),
        ("ollama", "http://localhost:11434"),
        ("ollama", "http://192.0.2.1:11434"),
    ],
)
def test_invalid_endpoints_are_rejected_before_http(provider, endpoint):
    http = FakeHTTP()
    with pytest.raises(InvalidArgumentError):
        generate(provider, http=http, endpoint=endpoint)
    assert not http.calls


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"max_tokens": 0},
        {"max_tokens": True},
        {"max_tokens": 1.5},
        {"max_tokens": "2048"},
        {"max_tokens": 32769},
        {"max_tokens": 2048, "temperature": 0},
        {"max_tokens": 2048, "reasoning_effort": "unverified"},
    ],
)
def test_invalid_or_unresolved_parameters_never_send(parameters):
    http = FakeHTTP()
    with pytest.raises(InvalidArgumentError):
        generate(http=http, parameters=parameters)
    assert not http.calls


def test_model_capability_output_limit_is_not_overridden_by_application_limit():
    http = FakeHTTP()
    with pytest.raises(InvalidArgumentError):
        generate(http=http, model=model(max_output_tokens=100), parameters={"max_tokens": 101})
    assert not http.calls


@pytest.mark.parametrize("provider", ["anthropic-api", "ollama"])
def test_reasoning_is_not_forwarded_to_other_providers(provider):
    http = FakeHTTP()
    with pytest.raises(InvalidArgumentError):
        generate(provider, http=http, parameters={"max_tokens": 100, "reasoning_effort": "high"})
    assert not http.calls


def test_unknown_openai_model_cannot_borrow_a_known_model_capability():
    http = FakeHTTP()
    with pytest.raises(ModelUnavailableError):
        generate(http=http, model=model(model_id="gpt-5.6-unverified"))
    assert not http.calls


@pytest.mark.parametrize("provider", ["openai-api", "anthropic-api", "ollama"])
def test_cancellation_before_start_does_not_contact_any_provider(provider):
    http, metadata = FakeHTTP(), FakeMetadata()
    with pytest.raises(CancelledError) as failure:
        generate(provider, http=http, metadata=metadata, should_cancel=lambda: True)
    assert not failure.value.details.get("outcome_unknown")
    assert not http.calls and not metadata.calls


def test_cancellation_during_ollama_metadata_is_known_and_does_not_generate():
    http = FakeHTTP()
    with pytest.raises(CancelledError) as failure:
        generate(
            "ollama",
            http=http,
            metadata=FakeMetadata(error=CancelledError(KEY)),
            should_cancel=lambda: False,
        )
    assert not failure.value.details.get("outcome_unknown") and KEY not in str(failure.value)
    assert not http.calls


@pytest.mark.parametrize("error_type", [EngineUnavailableError, CancelledError])
def test_unknown_transport_outcome_survives_boundary_without_raw_diagnostics(error_type, caplog):
    http = FakeHTTP(
        error=error_type(
            KEY, details={"reason": OUTCOME_UNKNOWN, "outcome_unknown": True, "unsafe": KEY}
        )
    )
    with pytest.raises(error_type) as failure:
        generate(http=http)
    assert failure.value.details == {"reason": OUTCOME_UNKNOWN, "outcome_unknown": True}
    assert len(http.calls) == 1
    assert_private(failure.value, caplog)


@pytest.mark.parametrize("error", [RuntimeError(KEY), EngineUnavailableError(KEY)])
def test_unclassified_transport_failure_is_unknown_and_never_retried(error, caplog):
    http = FakeHTTP(error=error)
    with pytest.raises(EngineUnavailableError) as failure:
        generate(http=http)
    assert failure.value.details["outcome_unknown"] is True
    assert len(http.calls) == 1
    assert_private(failure.value, caplog)


class Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, body):
        super().__init__(json.dumps(body).encode())

    def geturl(self):
        return ENDPOINTS["openai-api"] + "/v1/responses"


class Opener:
    def __init__(self, response):
        self.response, self.calls = response, []

    def open(self, request, *, timeout):
        self.calls.append(request)
        request.narumi_deadline.mark_request_started()
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.mark.parametrize("provider", ["openai-api", "anthropic-api"])
@pytest.mark.parametrize(
    "status", [400, 401, 403, 404, 405, 413, 415, 422, 408, 409, 425, 429, 460, 500, 503]
)
def test_real_http_error_classification_never_exposes_body_or_retries(provider, status, caplog):
    error = urllib.error.HTTPError(
        ENDPOINTS["openai-api"] + KEY, status, KEY, {}, io.BytesIO(KEY.encode())
    )
    opener = Opener(error)
    expected = AuthenticationRequiredError if status in {401, 403} else EngineUnavailableError
    with pytest.raises(expected) as failure:
        generate(provider, http=JSONHTTPClient(opener=opener))
    assert bool(failure.value.details.get("outcome_unknown")) == (
        status not in {400, 401, 403, 404, 405, 413, 415, 422}
    )
    assert error.fp.closed and len(opener.calls) == 1
    assert_private(failure.value, caplog)


@pytest.mark.parametrize("provider", ["openai-api", "anthropic-api"])
@pytest.mark.parametrize("status", [408, 409, 425, 429, 460, 500, 503])
def test_backend_never_downgrades_ambiguous_http_status_to_known_failure(provider, status):
    http = FakeHTTP(
        error=EngineUnavailableError(
            KEY, details={"reason": "metadata_http_error", "status": status}
        )
    )
    with pytest.raises(EngineUnavailableError) as failure:
        generate(provider, http=http)
    assert failure.value.details == {"reason": OUTCOME_UNKNOWN, "outcome_unknown": True}
    assert len(http.calls) == 1


@pytest.mark.parametrize("provider", ["openai-api", "anthropic-api", "ollama"])
def test_successful_text_with_absent_usage_is_unknown_usage_not_zero(provider):
    result = generate(provider, metadata=FakeMetadata())
    assert result.text == "# Fixture minutes" and result.usage is None


def test_openai_usage_only_preserves_observed_counters():
    http = FakeHTTP(
        reply(
            usage={
                "input_tokens": 20,
                "output_tokens": 8,
                "total_tokens": 28,
                "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 1},
                "output_tokens_details": {"reasoning_tokens": 2},
            }
        )
    )
    assert generate(http=http).usage == {
        "input_tokens": 20,
        "output_tokens": 8,
        "total_tokens": 28,
        "cached_input_tokens": 3,
        "cache_write_input_tokens": 1,
        "reasoning_output_tokens": 2,
    }
    http.response["usage"] = {"input_tokens": 20}
    assert generate(http=http).usage == {"input_tokens": 20}


def test_anthropic_and_ollama_usage_does_not_invent_total_tokens():
    http = FakeHTTP(
        reply(
            "anthropic-api",
            usage={
                "input_tokens": 9,
                "output_tokens": 4,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 1,
            },
        )
    )
    assert generate("anthropic-api", http=http).usage == {
        "input_tokens": 9,
        "output_tokens": 4,
        "cached_input_tokens": 2,
        "cache_write_input_tokens": 1,
    }
    http.response = reply("ollama", prompt_eval_count=9, eval_count=4)
    assert generate("ollama", http=http, metadata=FakeMetadata()).usage == {
        "input_tokens": 9,
        "output_tokens": 4,
    }


@pytest.mark.parametrize("counter", [-1, True, 1.5, "12", 2**53])
def test_invalid_usage_does_not_become_successful_output(counter):
    with pytest.raises(EngineUnavailableError) as failure:
        generate(http=FakeHTTP(reply(usage={"input_tokens": counter})))
    assert failure.value.details["outcome_unknown"] is True


@pytest.mark.parametrize(
    "selected,returned,accepted",
    [
        ("gpt-5.4", "gpt-5.4-2026-03-05", True),
        ("gpt-4.1", "gpt-4.1-2025-04-14", True),
        ("gpt-5.4", "gpt-5.4-2026-08-29", False),
        ("gpt-5.4-2026-03-05", "gpt-5.4", False),
        ("gpt-5.6-sol", "gpt-5.6-terra", False),
        ("gpt-5.6-sol", "gpt-5.6-sol-2026-08-29", False),
    ],
)
def test_response_model_must_be_exact_or_officially_confirmed_snapshot(
    selected, returned, accepted
):
    http = FakeHTTP(reply(model=returned))
    if accepted:
        assert generate(http=http, model=model(model_id=selected)).returned_model == returned
    else:
        with pytest.raises(EngineUnavailableError) as failure:
            generate(http=http, model=model(model_id=selected))
        assert failure.value.details["outcome_unknown"] is True
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    "provider,changes",
    [
        (
            "openai-api",
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        ),
        ("openai-api", {"status": "failed", "error": {"code": "server_error", "message": KEY}}),
        ("openai-api", {"output": [{"type": "function_call", "arguments": KEY}]}),
        ("openai-api", {"output": [{"type": "web_search_call"}]}),
        ("openai-api", {"output": []}),
        ("anthropic-api", {"stop_reason": "max_tokens"}),
        ("anthropic-api", {"content": [{"type": "tool_use", "input": KEY}]}),
        ("anthropic-api", {"role": "user"}),
        ("anthropic-api", {"model": "another-model"}),
        ("ollama", {"done": False}),
        ("ollama", {"done_reason": "length"}),
        ("ollama", {"model": "fixture-llama:latest"}),
        ("ollama", {"remote_model": "another-model"}),
        ("ollama", {"tool_calls": [{"function": {"name": "unsafe"}}]}),
        ("ollama", {"image": "fixture-image"}),
    ],
)
def test_partial_tool_remote_or_malformed_success_is_unknown(provider, changes, caplog):
    http = FakeHTTP(reply(provider, **changes))
    with pytest.raises(EngineUnavailableError) as failure:
        generate(provider, http=http, metadata=FakeMetadata())
    assert failure.value.details == {"reason": OUTCOME_UNKNOWN, "outcome_unknown": True}
    assert len(http.calls) == 1
    assert_private(failure.value, caplog)


@pytest.mark.parametrize(
    "provider,response",
    [
        (
            "openai-api",
            reply(
                output=[
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "refusal", "refusal": "Fixture refusal"}],
                    }
                ]
            ),
        ),
        ("anthropic-api", reply("anthropic-api", stop_reason="refusal")),
    ],
)
def test_explicit_refusal_is_not_a_success_or_an_unknown_response(provider, response):
    with pytest.raises(EngineUnavailableError) as failure:
        generate(provider, http=FakeHTTP(response))
    assert failure.value.details == {"reason": "provider_generation_refused"}


@pytest.mark.parametrize("provider", ["openai-api", "anthropic-api"])
@pytest.mark.parametrize("reflected", [KEY, "Bearer " + KEY])
def test_raw_key_and_full_header_reflection_are_rejected_in_unused_fields(
    provider, reflected, caplog
):
    with pytest.raises(EngineUnavailableError) as failure:
        generate(provider, http=FakeHTTP(reply(provider, unused={"reflected": reflected})))
    assert failure.value.details["outcome_unknown"] is True
    assert_private(failure.value, caplog)


def test_http_and_backend_both_reject_bare_bearer_key_reflection(caplog):
    opener = Opener(Response(reply(unused=KEY)))
    with pytest.raises(EngineUnavailableError) as failure:
        generate(http=JSONHTTPClient(opener=opener))
    assert failure.value.details["outcome_unknown"] is True
    assert_private(failure.value, caplog)


def test_reasoning_outputs_are_not_copied_into_minutes():
    response = reply()
    response["output"].insert(
        0,
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "Fixture hidden reasoning"}],
        },
    )
    assert generate(http=FakeHTTP(response)).text == "# Fixture minutes"
    response = reply("anthropic-api")
    response["content"].insert(0, {"type": "thinking", "thinking": "Fixture hidden reasoning"})
    assert generate("anthropic-api", http=FakeHTTP(response)).text == "# Fixture minutes"


def message_output(phase, text="Fixture commentary"):
    return {
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "phase": phase,
        "content": [{"type": "output_text", "text": text}],
    }


def test_openai_final_answer_excludes_commentary_and_prior_unphased_text():
    response = reply(
        output=[
            message_output("commentary"),
            message_output(None, "Fixture prior text"),
            message_output("final_answer", "# Fixture final minutes"),
        ]
    )
    assert generate(http=FakeHTTP(response)).text == "# Fixture final minutes"


@pytest.mark.parametrize("phase", ["commentary", "unverified", 3, {}])
def test_openai_commentary_only_or_unknown_phase_cannot_be_a_minutes_artifact(phase):
    with pytest.raises(EngineUnavailableError) as failure:
        generate(http=FakeHTTP(reply(output=[message_output(phase)])))
    assert failure.value.details["outcome_unknown"] is True


def test_openai_null_phase_preserves_older_text_model_support():
    assert (
        generate(http=FakeHTTP(reply(output=[message_output(None, "# Legacy minutes")]))).text
        == "# Legacy minutes"
    )


def test_openai_refusal_is_checked_even_in_an_excluded_commentary_message():
    refused = message_output("commentary")
    refused["content"] = [{"type": "refusal", "refusal": "Fixture refusal"}]
    with pytest.raises(EngineUnavailableError) as failure:
        generate(
            http=FakeHTTP(reply(output=[refused, message_output("final_answer", "# Minutes")]))
        )
    assert failure.value.details == {"reason": "provider_generation_refused"}


@pytest.mark.parametrize("changed_digest", [False, True])
def test_real_loopback_metadata_and_generation_share_verified_local_identity(changed_digest):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def log_message(self, *args):
            pass

        def respond(self):
            size = int(self.headers.get("Content-Length", "0"))
            request_body = json.loads(self.rfile.read(size)) if size else None
            requests.append((self.path, request_body))
            if self.path == "/api/tags":
                body = {
                    "models": [
                        {
                            "model": "fixture-llama:latest",
                            "size": 100,
                            "digest": ("b" if changed_digest else "a") * 64,
                            "details": {"format": "gguf"},
                        }
                    ]
                }
            elif self.path == "/api/show":
                body = {
                    "details": {"format": "gguf"},
                    "capabilities": ["completion"],
                    "model_info": {
                        "general.architecture": "fixture",
                        "fixture.context_length": 8192,
                    },
                }
            else:
                body = reply("ollama", prompt_eval_count=13, eval_count=5)
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        backend = HTTPMinutesBackend()
        args = (
            "ollama",
            f"http://127.0.0.1:{server.server_port}",
            None,
            model("ollama"),
            {"max_tokens": 2048},
            "Fixture meeting text",
        )
        if changed_digest:
            with pytest.raises(ConfigurationConflictError):
                backend.complete(*args)
        else:
            assert backend.complete(*args) == HTTPCompletionResult(
                "# Fixture minutes",
                "fixture-llama:latest:local",
                {"input_tokens": 13, "output_tokens": 5},
            )
        assert requests[:2] == [
            ("/api/tags", None),
            ("/api/show", {"model": "fixture-llama:latest:local"}),
        ]
        assert len(requests) == (2 if changed_digest else 3)
        if not changed_digest:
            assert requests[2][0] == "/api/generate"
            assert requests[2][1]["model"] == "fixture-llama:latest:local"
            assert requests[2][1]["options"] == {"num_predict": 2048}
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert not worker.is_alive()
