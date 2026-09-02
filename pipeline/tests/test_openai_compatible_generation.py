"""Both compatible surfaces are explicit, strict, text-only and non-retrying."""

from __future__ import annotations

import copy
import socket

import pytest
from narumi.errors import (
    AuthenticationRequiredError,
    EngineUnavailableError,
    InvalidArgumentError,
    ModelUnavailableError,
)
from narumi.providers.metadata.openai_compatible import model_descriptor
from narumi.providers.metadata.openai_compatible_transport import OpenAICompatibleTransport
from narumi.providers.openai_compatible import (
    VERIFY_PROMPT,
    VERIFY_SENTINEL,
    OpenAICompatibleBackend,
)
from narumi.providers.openai_compatible_response import OUTCOME_UNKNOWN

ENDPOINT = "https://compatible.fixture.test/prefix/v1"
KEY = "fixture-compatible-generation-key"
MODEL = "fixture-text-model"
PUBLIC = "8.8.8.8"
LOCAL = "http://127.0.0.1:8080/v1"
FETCHED = "2026-09-02T09:00:00Z"


def resolver(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443))]


def responses_reply(text="# Minutes", *, model=MODEL, **changes):
    result = {
        "id": "resp_fixture",
        "object": "response",
        "status": "completed",
        "incomplete_details": None,
        "parallel_tool_calls": False,
        "model": model,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }
    result.update(changes)
    return result


def chat_reply(text="# Minutes", *, model=MODEL, **changes):
    result = {
        "id": "chatcmpl_fixture",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "logprobs": None,
                "message": {"role": "assistant", "content": text},
            }
        ],
    }
    result.update(changes)
    return result


class FakeHTTP:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        return copy.deepcopy(self.response)


def setup(response):
    http = FakeHTTP(response)
    transport = OpenAICompatibleTransport(http=http, resolver=resolver, monotonic=lambda: 0.0)
    return OpenAICompatibleBackend(transport=transport), http


def verified_model():
    return model_descriptor(MODEL, fetched_at=FETCHED, verified=True)


def complete(response, surface="responses", token_field=None, **changes):
    backend, http = setup(response)
    result = backend.complete(
        ENDPOINT,
        KEY,
        changes.pop("model", verified_model()),
        changes.pop("parameters", {"max_tokens": 512}),
        changes.pop("prompt", "Meeting transcript"),
        auth_method=changes.pop("auth_method", "api_key"),
        api_surface=surface,
        chat_max_tokens_field=token_field,
        system=changes.pop("system", "Write concise minutes"),
        **changes,
    )
    return result, http


def test_responses_builder_is_closed_and_preserves_endpoint_prefix():
    result, http = complete(responses_reply())
    assert result.text == "# Minutes" and result.returned_model == MODEL
    assert result.usage is None
    assert http.calls == [
        {
            "method": "POST",
            "url": ENDPOINT + "/responses",
            "headers": {"Authorization": "Bearer " + KEY},
            "payload": {
                "model": MODEL,
                "input": "Meeting transcript",
                "instructions": "Write concise minutes",
                "tools": [],
                "tool_choice": "none",
                "parallel_tool_calls": False,
                "store": False,
                "background": False,
                "stream": False,
                "truncation": "disabled",
                "max_output_tokens": 512,
            },
            "timeout": 600.0,
            "response_kind": "generation",
            "resolved_addresses": (PUBLIC,),
        }
    ]


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_chat_builder_uses_only_the_explicit_token_field(field):
    result, http = complete(chat_reply(), "chat_completions", field)
    assert result.text == "# Minutes"
    payload = http.calls[0]["payload"]
    assert http.calls[0]["url"] == ENDPOINT + "/chat/completions"
    assert payload[field] == 512
    assert ({"max_tokens", "max_completion_tokens"} - {field}).isdisjoint(payload)
    assert payload == {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Write concise minutes"},
            {"role": "user", "content": "Meeting transcript"},
        ],
        "tools": [],
        "tool_choice": "none",
        "parallel_tool_calls": False,
        "store": False,
        "stream": False,
        "n": 1,
        field: 512,
    }


def test_numeric_loopback_generation_can_explicitly_use_no_auth_without_dns():
    http = FakeHTTP(chat_reply())

    def forbidden(*_args, **_kwargs):
        pytest.fail("numeric loopback must not use DNS")

    backend = OpenAICompatibleBackend(
        transport=OpenAICompatibleTransport(http=http, resolver=forbidden, monotonic=lambda: 0.0)
    )
    result = backend.complete(
        LOCAL,
        None,
        verified_model(),
        {"max_tokens": 32},
        "Transcript",
        auth_method="none",
        api_surface="chat_completions",
        chat_max_tokens_field="max_tokens",
    )
    assert result.text == "# Minutes"
    assert http.calls[0]["headers"] == {}
    assert http.calls[0]["resolved_addresses"] == ("127.0.0.1",)


@pytest.mark.parametrize(
    ("surface", "field"),
    [("responses", "max_tokens"), ("chat_completions", None), ("unknown", None)],
)
def test_invalid_surface_configuration_fails_before_http(surface, field):
    backend, http = setup(responses_reply())
    with pytest.raises(InvalidArgumentError):
        backend.complete(
            ENDPOINT,
            KEY,
            verified_model(),
            {"max_tokens": 32},
            "Transcript",
            auth_method="api_key",
            api_surface=surface,
            chat_max_tokens_field=field,
        )
    assert http.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"model": model_descriptor(MODEL, fetched_at=FETCHED, verified=False)},
        {"parameters": {}},
        {"parameters": {"max_tokens": 0}},
        {"parameters": {"max_tokens": 10, "temperature": 0}},
        {"prompt": ""},
        {"system": 1},
    ],
)
def test_unverified_model_and_open_parameters_never_reach_transport(changes):
    backend, http = setup(responses_reply())
    with pytest.raises((InvalidArgumentError, ModelUnavailableError)):
        backend.complete(
            ENDPOINT,
            KEY,
            changes.get("model", verified_model()),
            changes.get("parameters", {"max_tokens": 32}),
            changes.get("prompt", "Transcript"),
            auth_method="api_key",
            api_surface="responses",
            system=changes.get("system"),
        )
    assert http.calls == []


@pytest.mark.parametrize(
    "response",
    [
        responses_reply(model="different-model"),
        responses_reply(status="incomplete"),
        responses_reply(parallel_tool_calls=True),
        responses_reply(parallel_tool_calls=0),
        responses_reply(output=[]),
        responses_reply(
            output=[
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [],
                }
            ]
        ),
        responses_reply(output=[{"type": "function_call", "name": "tool", "arguments": "{}"}]),
        chat_reply(model="different-model"),
        chat_reply(
            choices=[
                {
                    "index": False,
                    "finish_reason": "stop",
                    "logprobs": None,
                    "message": {"role": "assistant", "content": "looks valid"},
                }
            ]
        ),
        chat_reply(
            choices=[
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "partial"},
                }
            ]
        ),
        chat_reply(
            choices=[
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "call"}],
                    },
                }
            ]
        ),
    ],
)
def test_malformed_partial_model_mismatch_and_tool_replies_are_unknown_without_retry(response):
    surface = "chat_completions" if response.get("object") == "chat.completion" else "responses"
    field = "max_tokens" if surface == "chat_completions" else None
    backend, http = setup(response)
    with pytest.raises(EngineUnavailableError) as failure:
        backend.complete(
            ENDPOINT,
            KEY,
            verified_model(),
            {"max_tokens": 32},
            "Transcript",
            auth_method="api_key",
            api_surface=surface,
            chat_max_tokens_field=field,
        )
    assert failure.value.details == {"reason": OUTCOME_UNKNOWN, "outcome_unknown": True}
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    ("surface", "response"),
    [
        (
            "responses",
            responses_reply(
                output=[
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "refusal", "refusal": "cannot comply"}],
                    }
                ]
            ),
        ),
        (
            "chat_completions",
            chat_reply(
                choices=[
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "logprobs": None,
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "refusal": "cannot comply",
                        },
                    }
                ]
            ),
        ),
    ],
)
def test_explicit_refusal_is_known_and_never_retried(surface, response):
    backend, http = setup(response)
    with pytest.raises(EngineUnavailableError) as failure:
        backend.complete(
            ENDPOINT,
            KEY,
            verified_model(),
            {"max_tokens": 32},
            "Transcript",
            auth_method="api_key",
            api_surface=surface,
            chat_max_tokens_field="max_tokens" if surface == "chat_completions" else None,
        )
    assert failure.value.details == {"reason": "provider_generation_refused"}
    assert len(http.calls) == 1


def test_usage_is_normalized_without_inventing_missing_counters():
    response = chat_reply(
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2},
        }
    )
    result, _ = complete(response, "chat_completions", "max_completion_tokens")
    assert result.usage == {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
        "cached_input_tokens": 3,
        "reasoning_output_tokens": 2,
    }


def test_post_dispatch_failure_stays_unknown_and_is_not_retried():
    backend, http = setup(
        EngineUnavailableError(
            "fixture",
            details={"reason": OUTCOME_UNKNOWN, "outcome_unknown": True, "unsafe": KEY},
        )
    )
    with pytest.raises(EngineUnavailableError) as failure:
        backend.complete(
            ENDPOINT,
            KEY,
            verified_model(),
            {"max_tokens": 32},
            "Transcript",
            auth_method="api_key",
            api_surface="responses",
        )
    assert failure.value.details == {"reason": OUTCOME_UNKNOWN, "outcome_unknown": True}
    assert len(http.calls) == 1 and KEY not in str(failure.value.to_payload())


@pytest.mark.parametrize("status", [408, 409, 425, 429, 460, 500, 503])
def test_ambiguous_http_status_stays_unknown_without_retry(status):
    backend, http = setup(
        EngineUnavailableError(
            "fixture", details={"reason": "metadata_http_error", "status": status}
        )
    )
    with pytest.raises(EngineUnavailableError) as failure:
        backend.complete(
            ENDPOINT,
            KEY,
            verified_model(),
            {"max_tokens": 32},
            "Transcript",
            auth_method="api_key",
            api_surface="responses",
        )
    assert failure.value.details == {"reason": OUTCOME_UNKNOWN, "outcome_unknown": True}
    assert len(http.calls) == 1


def test_clear_compatible_http_rejection_remains_known():
    backend, http = setup(
        EngineUnavailableError("fixture", details={"reason": "metadata_http_error", "status": 400})
    )
    with pytest.raises(EngineUnavailableError) as failure:
        backend.complete(
            ENDPOINT,
            KEY,
            verified_model(),
            {"max_tokens": 32},
            "Transcript",
            auth_method="api_key",
            api_surface="responses",
        )
    assert failure.value.details == {"reason": "provider_generation_rejected", "status": 400}
    assert len(http.calls) == 1


def test_verify_model_uses_one_fixed_small_probe_and_promotes_only_exact_sentinel():
    backend, http = setup(responses_reply(VERIFY_SENTINEL))
    descriptor = backend.verify_model(
        ENDPOINT,
        KEY,
        MODEL,
        auth_method="api_key",
        api_surface="responses",
        fetched_at=FETCHED,
    )
    assert descriptor["availability"] == "available" and descriptor["reason"] is None
    assert descriptor["roles"] == ["llm"]
    assert descriptor["parameter_schema"]["properties"]["max_tokens"]["maximum"] == 32768
    assert len(http.calls) == 1
    assert http.calls[0]["payload"]["input"] == VERIFY_PROMPT
    assert http.calls[0]["payload"]["max_output_tokens"] == 32
    assert http.calls[0]["payload"]["parallel_tool_calls"] is False


def test_chat_model_verification_uses_selected_surface_without_fallback():
    backend, http = setup(chat_reply(VERIFY_SENTINEL))
    descriptor = backend.verify_model(
        ENDPOINT,
        KEY,
        MODEL,
        auth_method="api_key",
        api_surface="chat_completions",
        chat_max_tokens_field="max_completion_tokens",
        fetched_at=FETCHED,
    )
    assert descriptor["availability"] == "available"
    assert len(http.calls) == 1
    assert http.calls[0]["url"] == ENDPOINT + "/chat/completions"
    assert http.calls[0]["payload"]["max_completion_tokens"] == 32
    assert http.calls[0]["payload"]["messages"] == [{"role": "user", "content": VERIFY_PROMPT}]


@pytest.mark.parametrize(
    "response",
    [
        chat_reply("wrong sentinel"),
        chat_reply(
            VERIFY_SENTINEL,
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "logprobs": None,
                    "tool_calls": [{"name": "fixture"}],
                    "message": {"role": "assistant", "content": VERIFY_SENTINEL},
                }
            ],
        ),
    ],
)
def test_chat_model_verification_rejects_wrong_sentinel_and_hidden_choice_tool(response):
    backend, http = setup(response)
    with pytest.raises(EngineUnavailableError) as failure:
        backend.verify_model(
            ENDPOINT,
            KEY,
            MODEL,
            auth_method="api_key",
            api_surface="chat_completions",
            chat_max_tokens_field="max_tokens",
            fetched_at=FETCHED,
        )
    assert failure.value.details["outcome_unknown"] is True
    assert len(http.calls) == 1


def test_verify_model_wrong_sentinel_is_unknown_and_does_not_promote_or_retry():
    backend, http = setup(responses_reply("almost"))
    with pytest.raises(EngineUnavailableError) as failure:
        backend.verify_model(
            ENDPOINT,
            KEY,
            MODEL,
            auth_method="api_key",
            api_surface="responses",
            fetched_at=FETCHED,
        )
    assert failure.value.details["outcome_unknown"] is True
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    "output",
    [
        [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "tool_calls": [{"name": "fixture"}],
                "content": [{"type": "output_text", "text": VERIFY_SENTINEL}],
            }
        ],
        [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": VERIFY_SENTINEL,
                        "function_call": {"name": "fixture"},
                    }
                ],
            }
        ],
    ],
)
def test_verify_model_rejects_responses_tool_signal_even_with_exact_sentinel(output):
    backend, http = setup(responses_reply(VERIFY_SENTINEL, output=output))
    with pytest.raises(EngineUnavailableError) as failure:
        backend.verify_model(
            ENDPOINT,
            KEY,
            MODEL,
            auth_method="api_key",
            api_surface="responses",
            fetched_at=FETCHED,
        )
    assert failure.value.details["outcome_unknown"] is True
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    "parts",
    [
        ["fixture-compatible-", "generation-key"],
        ["fixture-", "compatible-", "generation-key"],
        ["Bearer fixture-compatible-", "generation-key"],
    ],
)
def test_joined_responses_blocks_cannot_reconstruct_saved_credentials(parts):
    output = [
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": part} for part in parts],
        }
    ]
    backend, http = setup(responses_reply(output=output))
    with pytest.raises(EngineUnavailableError) as failure:
        backend.complete(
            ENDPOINT,
            KEY,
            verified_model(),
            {"max_tokens": 32},
            "Transcript",
            auth_method="api_key",
            api_surface="responses",
        )
    assert failure.value.details["outcome_unknown"] is True
    assert KEY not in str(failure.value.to_payload())
    assert len(http.calls) == 1


@pytest.mark.parametrize("reply", [" " + VERIFY_SENTINEL, VERIFY_SENTINEL + "\n"])
def test_verify_model_requires_byte_exact_sentinel_text(reply):
    backend, http = setup(responses_reply(reply))
    with pytest.raises(EngineUnavailableError) as failure:
        backend.verify_model(
            ENDPOINT,
            KEY,
            MODEL,
            auth_method="api_key",
            api_surface="responses",
            fetched_at=FETCHED,
        )
    assert failure.value.details["outcome_unknown"] is True
    assert len(http.calls) == 1


def test_missing_api_key_is_rejected_before_dns_or_http():
    backend, http = setup(responses_reply())
    with pytest.raises(AuthenticationRequiredError):
        backend.complete(
            ENDPOINT,
            None,
            verified_model(),
            {"max_tokens": 32},
            "Transcript",
            auth_method="api_key",
            api_surface="responses",
        )
    assert http.calls == []
