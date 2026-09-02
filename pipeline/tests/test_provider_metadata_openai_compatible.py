"""OpenAI-compatible discovery remains display-only until an explicit paid probe."""

from __future__ import annotations

import copy
import socket
from datetime import UTC, datetime

import pytest
from jsonschema import Draft202012Validator
from narumi.contracts import load_contracts
from narumi.errors import (
    AuthenticationRequiredError,
    EngineUnavailableError,
    InvalidArgumentError,
)
from narumi.providers.metadata import MetadataClient

ENDPOINT = "https://models.fixture.test/prefix/v1"
LOCAL = "http://127.0.0.1:8080/v1"
KEY = "fixture-compatible-key-not-real"
NOW = datetime(2026, 9, 2, 9, tzinfo=UTC)
PUBLIC = "8.8.8.8"


def resolver(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443))]


class FakeHTTP:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        return copy.deepcopy(self.response)


def metadata(response, *, endpoint=ENDPOINT, key=KEY, auth_method="api_key", surface="responses"):
    http = FakeHTTP(response)
    client = MetadataClient(http=http, now=lambda: NOW, resolver=resolver, monotonic=lambda: 0.0)
    result = client.fetch_openai_compatible(
        endpoint, key, auth_method=auth_method, api_surface=surface
    )
    return result, http


def catalog(*ids):
    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "created": 1, "owned_by": "fixture"}
            for model_id in ids
        ],
    }


def test_discovery_preserves_base_path_pins_dns_and_does_not_claim_capabilities():
    models, http = metadata(catalog("fixture-model"))
    assert len(models) == 1
    model = models[0]
    assert model["model_id"] == "fixture-model"
    assert model["availability"] == "unverified"
    assert model["reason"] == "adapter_capability_verification_required"
    assert model["roles"] == model["input_modalities"] == model["output_modalities"] == []
    assert model["parameter_schema"]["properties"] == {}
    assert model["billing"]["kind"] == "api"
    Draft202012Validator(
        {"$ref": "#/$defs/provider_model_descriptor", "$defs": load_contracts().defs}
    ).validate(model)
    assert http.calls == [
        {
            "method": "GET",
            "url": ENDPOINT + "/models",
            "headers": {"Authorization": "Bearer " + KEY},
            "payload": None,
            "timeout": 10.0,
            "response_kind": "metadata",
            "resolved_addresses": (PUBLIC,),
        }
    ]


@pytest.mark.parametrize("surface", ["responses", "chat_completions"])
def test_discovery_surface_is_explicit_but_never_changes_models_route(surface):
    models, http = metadata(catalog("fixture-model"), surface=surface)
    assert models[0]["availability"] == "unverified"
    assert http.calls[0]["url"] == ENDPOINT + "/models"


def test_numeric_loopback_may_use_no_auth_and_never_calls_dns():
    def forbidden(*_args, **_kwargs):
        pytest.fail("numeric loopback must not use DNS")

    http = FakeHTTP(catalog("local-model"))
    client = MetadataClient(http=http, now=lambda: NOW, resolver=forbidden)
    models = client.fetch_openai_compatible(
        LOCAL, None, auth_method="none", api_surface="chat_completions"
    )
    assert models[0]["model_id"] == "local-model"
    assert http.calls[0]["headers"] == {}
    assert http.calls[0]["resolved_addresses"] == ("127.0.0.1",)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://models.fixture.test/v1",
        "https://user@models.fixture.test/v1",
        "https://models.fixture.test/v1/",
        "https://models.fixture.test/a//b",
        "https://models.fixture.test/a/./b",
        "https://models.fixture.test/a/../b",
        "https://models.fixture.test/a%2fb",
        "https://models.fixture.test/a b",
        "https://models.fixture.test/a@b",
        'https://models.fixture.test/a"b',
        "https://models.fixture.test/a:b",
        "https://models.fixture.test/v1?key=secret",
        "https://models.fixture.test/v1#fragment",
        "https://models.fixture.test/v1\\models",
        "https://192.168.1.1/v1",
        "file:///tmp/api",
    ],
)
def test_unsafe_endpoints_are_rejected_before_http(endpoint):
    http = FakeHTTP(catalog("fixture-model"))
    client = MetadataClient(http=http, now=lambda: NOW, resolver=resolver)
    with pytest.raises((InvalidArgumentError, EngineUnavailableError)):
        client.fetch_openai_compatible(
            endpoint, KEY, auth_method="api_key", api_surface="responses"
        )
    assert http.calls == []


def test_remote_no_auth_is_rejected_before_dns_or_http():
    http = FakeHTTP(catalog("fixture-model"))
    client = MetadataClient(http=http, now=lambda: NOW, resolver=resolver)
    with pytest.raises(InvalidArgumentError):
        client.fetch_openai_compatible(ENDPOINT, None, auth_method="none", api_surface="responses")
    assert http.calls == []


@pytest.mark.parametrize("key", [None, "", "bad key", "bad\r\nkey", 1])
def test_api_key_auth_rejects_missing_or_invalid_key_before_http(key):
    http = FakeHTTP(catalog("fixture-model"))
    client = MetadataClient(http=http, now=lambda: NOW, resolver=resolver)
    expected = AuthenticationRequiredError if key in {None, ""} else InvalidArgumentError
    with pytest.raises(expected):
        client.fetch_openai_compatible(
            ENDPOINT, key, auth_method="api_key", api_surface="responses"
        )
    assert http.calls == []


def test_any_non_public_dns_answer_rejects_the_whole_request_before_http():
    def mixed(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    http = FakeHTTP(catalog("fixture-model"))
    client = MetadataClient(http=http, now=lambda: NOW, resolver=mixed)
    with pytest.raises(InvalidArgumentError):
        client.fetch_openai_compatible(
            ENDPOINT, KEY, auth_method="api_key", api_surface="responses"
        )
    assert http.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"object": "list", "data": "models"},
        catalog("duplicate", "duplicate"),
        {"object": "list", "data": [{"id": "bad\nmodel", "object": "model"}]},
        {"object": "list", "data": [{"id": "ok", "object": "not-model"}]},
        {"object": "list", "data": [{"id": "ok", "created": True}]},
        {"object": "list", "data": [{"id": "ok", "owned_by": " leading"}]},
    ],
)
def test_malformed_catalogs_fail_closed_after_one_request(body):
    http = FakeHTTP(body)
    client = MetadataClient(http=http, now=lambda: NOW, resolver=resolver)
    with pytest.raises(EngineUnavailableError):
        client.fetch_openai_compatible(
            ENDPOINT, KEY, auth_method="api_key", api_surface="responses"
        )
    assert len(http.calls) == 1


def test_key_reflection_is_never_returned_from_fake_transport():
    http = FakeHTTP({"object": "list", "data": [], "extra": "Bearer " + KEY})
    client = MetadataClient(http=http, now=lambda: NOW, resolver=resolver)
    with pytest.raises(EngineUnavailableError) as failure:
        client.fetch_openai_compatible(
            ENDPOINT, KEY, auth_method="api_key", api_surface="responses"
        )
    assert KEY not in str(failure.value.to_payload())
    assert len(http.calls) == 1


def test_api_key_cannot_be_embedded_in_saved_endpoint_path():
    http = FakeHTTP(catalog("fixture-model"))
    client = MetadataClient(http=http, now=lambda: NOW, resolver=resolver)
    with pytest.raises(InvalidArgumentError):
        client.fetch_openai_compatible(
            "https://models.fixture.test/" + KEY,
            KEY,
            auth_method="api_key",
            api_surface="responses",
        )
    assert http.calls == []


def test_short_api_key_matching_hostname_text_is_not_mistaken_for_url_embedding():
    http = FakeHTTP(catalog("fixture-model"))
    client = MetadataClient(http=http, now=lambda: NOW, resolver=resolver)
    models = client.fetch_openai_compatible(
        "https://api.fixture.test/v1",
        "api",
        auth_method="api_key",
        api_surface="responses",
    )
    assert models[0]["model_id"] == "fixture-model"
    assert http.calls[0]["headers"] == {"Authorization": "Bearer api"}


def test_short_api_key_matching_normal_v1_path_is_not_mistaken_for_embedding():
    http = FakeHTTP(catalog("fixture-model"))
    client = MetadataClient(http=http, now=lambda: NOW, resolver=resolver)
    models = client.fetch_openai_compatible(
        "https://api.fixture.test/v1",
        "v1",
        auth_method="api_key",
        api_surface="responses",
    )
    assert models[0]["model_id"] == "fixture-model"
    assert http.calls[0]["headers"] == {"Authorization": "Bearer v1"}
