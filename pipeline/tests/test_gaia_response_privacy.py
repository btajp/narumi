"""Successful Gaia responses must not move bearer credentials into returned or saved data."""

from __future__ import annotations

import json
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import pytest
from narumi.brief import BRIEF_ARTIFACT_KEY, BRIEF_PATH, build_brief, inject_brief
from narumi.errors import ContractMismatchError, NarumiError
from narumi.gaia import GaiaClient

from .test_brief import StubGaia, make_bundle
from .test_gaia_client import FakeGaiaServer, empty_search, engagement_result, tool_error, tool_ok
from .test_gaia_client import gaia_server as gaia_server

KEY = "gaia_privacy_ab12+cd34/ef56="
FAILURE = "gaia-library returned credential material in a tool response"
TOOLS = ["get_glossary", "search_context", "get_engagement", "resolve_speakers", "propose_update"]


def encoded_key(form: str) -> str:
    if form == "full_percent":
        return "".join(f"%{byte:02X}" for byte in KEY.encode("ascii"))
    if form == "lower_percent":
        return quote(KEY, safe="").replace("%2B", "%2b").replace("%2F", "%2f").replace("%3D", "%3d")
    if form == "nested_percent":
        return quote(encoded_key("full_percent"), safe="")
    return KEY


def valid_response(tool: str) -> dict[str, Any]:
    if tool == "search_context":
        return empty_search("定例")
    if tool == "get_engagement":
        return engagement_result()
    if tool == "get_glossary":
        return {"terms": [], "vocabulary_hints": ["SCIM"]}
    if tool == "resolve_speakers":
        return {
            "results": [
                {
                    "input": "Tanaka",
                    "normalized": "Tanaka",
                    "status": "unmatched",
                    "confidence": 0.0,
                    "candidates": [],
                }
            ]
        }
    if tool == "propose_update":
        return {"proposal_id": 17, "status": "pending", "duplicate": False}
    return {"opaque": {"values": [1, True, None, "safe"]}}


def call_tool(client: GaiaClient, tool: str) -> dict[str, Any]:
    if tool == "search_context":
        return client.search_context("定例", scope="cn")
    if tool == "get_engagement":
        return client.get_engagement("acme", scope="cn")
    if tool == "get_glossary":
        return client.get_glossary(scope="cn")
    if tool == "resolve_speakers":
        return client.resolve_speakers(["Tanaka"], scope="cn")
    if tool == "propose_update":
        return client.propose_update(
            target_type="interaction",
            action="insert",
            kind="fact",
            patch={"kind": "meeting", "occurred_at": "2026-08-27T10:00:00Z", "summary": "safe"},
            request_id="privacy-request-17",
            scope="cn",
        )
    return client.call(tool)


def reflected_response(tool: str, position: str) -> dict[str, Any]:
    payload = valid_response(tool)
    values = {
        "value": KEY,
        "array": [1, {"nested": [KEY]}],
        "url": f"https://example.test/ref?token={KEY}",
        "encoded_url": f"https://example.test/ref?token={quote(KEY, safe='')}",
        "full_percent_url": f"https://example.test/ref?token={encoded_key('full_percent')}",
        "lower_percent_url": f"https://example.test/ref?token={encoded_key('lower_percent')}",
        "nested_percent_url": f"https://example.test/ref?token={encoded_key('nested_percent')}",
        "percent_key": [{encoded_key("full_percent"): "safe"}],
        "percent_array": [{"safe": [encoded_key("lower_percent")]}],
        "bearer": f"Bearer {KEY}",
        "nested_key": [{f"prefix-{KEY}-suffix": {"safe": True}}],
    }
    if position == "key":
        payload[f"prefix-{KEY}-suffix"] = "safe"
    else:
        payload["unknown_additive_field"] = values[position]
    return payload


@pytest.mark.parametrize("tool", [*TOOLS, "custom_read"])
@pytest.mark.parametrize(
    "position",
    [
        "value",
        "key",
        "array",
        "url",
        "encoded_url",
        "bearer",
        "nested_key",
        "full_percent_url",
        "lower_percent_url",
        "nested_percent_url",
        "percent_key",
        "percent_array",
    ],
)
def test_reflected_credentials_fail_closed_for_all_successful_tool_apis(
    gaia_server: FakeGaiaServer, caplog, tool: str, position: str
):
    gaia_server.tools[tool] = lambda _: tool_ok(reflected_response(tool, position))
    client = GaiaClient(gaia_server.url, api_key=KEY)
    with pytest.raises(ContractMismatchError) as caught:
        call_tool(client, tool)
    assert str(caught.value) == FAILURE
    public_error = json.dumps(caught.value.to_payload())
    trace = "".join(traceback.format_exception(caught.value))
    for secret in (
        KEY,
        quote(KEY, safe=""),
        encoded_key("full_percent"),
        encoded_key("lower_percent"),
        encoded_key("nested_percent"),
    ):
        assert secret not in public_error and secret not in trace and secret not in caplog.text


@pytest.mark.parametrize("tool", TOOLS)
def test_low_level_call_cannot_bypass_the_success_response_privacy_guard(
    gaia_server: FakeGaiaServer, tool: str
):
    gaia_server.tools[tool] = lambda _: tool_ok(reflected_response(tool, "nested_key"))
    with pytest.raises(ContractMismatchError, match="credential material"):
        GaiaClient(gaia_server.url, api_key=KEY).call(tool)


@pytest.mark.parametrize("tool", [*TOOLS, "custom_read"])
@pytest.mark.parametrize("api_key", [None, KEY])
def test_safe_successful_responses_retain_contract_and_additive_fields(
    gaia_server: FakeGaiaServer, tool: str, api_key: str | None
):
    payload = valid_response(tool)
    payload["new_field"] = {
        "nested": ["safe value", {"bool": True, "number": 7}],
        "Bearer token": ["Bearer authentication", "Bearer token の仕組み"],
        "reference": "https://example.test/Bearer%20authentication",
        "percent_text": "a%20b%2b%252F",
        "case_sensitive": KEY.upper(),
    }
    gaia_server.tools[tool] = lambda _: tool_ok(payload)
    assert call_tool(GaiaClient(gaia_server.url, api_key=api_key), tool) == payload


@pytest.mark.parametrize("entry", ["metadata", "raw", "implicit"])
@pytest.mark.parametrize("encoding", ["raw", "full_percent", "lower_percent", "nested_percent"])
def test_server_info_redaction_remains_compatible_on_every_metadata_path(
    gaia_server: FakeGaiaServer, entry: str, encoding: str
):
    secret = encoded_key(encoding)
    gaia_server.info["client"]["name"] = secret
    gaia_server.info["extra"] = {secret: [{"url": f"https://example.test/{secret}"}]}
    gaia_server.info["ordinary_text"] = {"Bearer token": ["Bearer authentication"]}
    gaia_server.tools["get_glossary"] = lambda _: tool_ok(valid_response("get_glossary"))
    client = GaiaClient(gaia_server.url, api_key=KEY)
    if entry == "implicit":
        client.get_glossary()
        result = client.require_capabilities("get_glossary")
    elif entry == "raw":
        result = client.call("get_server_info")
    else:
        result = client.get_server_info()
    assert result["client"]["name"] == "[REDACTED]"
    assert result["ordinary_text"] == gaia_server.info["ordinary_text"]
    serialized = json.dumps({"returned": result, "cached": client._server_info})
    assert secret not in serialized and KEY not in unquote(unquote(serialized))


@pytest.mark.parametrize("entry", ["metadata", "raw", "implicit"])
def test_mixed_raw_and_encoded_keys_are_all_redacted_before_metadata_is_cached(
    gaia_server: FakeGaiaServer, entry: str
):
    secrets = " | ".join(
        encoded_key(form) for form in ["raw", "full_percent", "lower_percent", "nested_percent"]
    )
    redacted = " | ".join(["[REDACTED]"] * 4)
    gaia_server.info["client"]["name"] = secrets
    gaia_server.info["extra"] = {secrets: [{"url": f"https://example.test/{secrets}"}]}
    client = GaiaClient(gaia_server.url, api_key=KEY)
    if entry == "implicit":
        client.require_capabilities("get_glossary")
        result = client.get_server_info()
    elif entry == "raw":
        result = client.call("get_server_info")
    else:
        result = client.get_server_info()
    assert result["client"]["name"] == redacted
    assert result["extra"] == {redacted: [{"url": f"https://example.test/{redacted}"}]}
    serialized = json.dumps({"returned": result, "cached": client._server_info})
    assert KEY not in unquote(unquote(serialized))


@pytest.mark.parametrize("tool", ["get_glossary", "get_engagement", "search_context"])
@pytest.mark.parametrize("existing_brief", [False, True])
@pytest.mark.parametrize("encoding", ["raw", "full_percent", "lower_percent"])
def test_secret_response_never_reaches_brief_storage_or_prompt_injection(
    tmp_path: Path, gaia_server: FakeGaiaServer, tool: str, existing_brief: bool, encoding: str
):
    blueprint = StubGaia()
    payloads = {
        "get_glossary": deepcopy(blueprint.glossary),
        "get_engagement": deepcopy(blueprint.engagement),
        "search_context": deepcopy(blueprint.search),
    }
    secret = encoded_key(encoding)
    if tool == "get_glossary":
        payloads[tool]["vocabulary_hints"].append(secret)
    elif tool == "get_engagement":
        payloads[tool]["people"][0]["person"]["aliases"].append({"alias": secret})
    else:
        payloads[tool]["entities"][0]["refs"][0]["uri"] = f"https://example.test/{secret}"
    for name, payload in payloads.items():
        gaia_server.tools[name] = lambda _, result=payload: tool_ok(result)
    bundle = make_bundle(tmp_path)
    old_brief = build_brief(bundle) if existing_brief else None
    original = bundle.abspath(BRIEF_PATH).read_bytes() if existing_brief else None
    with pytest.raises(ContractMismatchError, match="credential material"):
        build_brief(bundle, GaiaClient(gaia_server.url, api_key=KEY))
    if old_brief is not None:
        assert bundle.abspath(BRIEF_PATH).read_bytes() == original
        assert KEY not in inject_brief(old_brief, budget_chars=10_000)
    else:
        assert not bundle.abspath(BRIEF_PATH).exists()
        assert BRIEF_ARTIFACT_KEY not in bundle.manifest.artifacts
    assert KEY not in bundle.abspath("manifest.json").read_text(encoding="utf-8")


def test_ordinary_bearer_vocabulary_reaches_brief_and_prompt_unchanged(
    tmp_path: Path, gaia_server: FakeGaiaServer
):
    blueprint = StubGaia()
    terms = ["Bearer token", "Bearer authentication"]
    blueprint.glossary["vocabulary_hints"].extend(terms)
    for tool, payload in {
        "get_engagement": blueprint.engagement,
        "get_glossary": blueprint.glossary,
        "search_context": blueprint.search,
    }.items():
        gaia_server.tools[tool] = lambda _, result=payload: tool_ok(result)
    bundle = make_bundle(tmp_path)
    brief = build_brief(bundle, GaiaClient(gaia_server.url, api_key=KEY))
    prompt = inject_brief(brief, budget_chars=10_000)
    for term in terms:
        assert term in brief.vocab_hints and term in prompt
    assert "[REDACTED]" not in bundle.abspath(BRIEF_PATH).read_text(encoding="utf-8")


def test_error_scrubbing_keeps_generic_bearer_mask_separate_from_success_detection(gaia_server):
    client = GaiaClient(gaia_server.url, api_key=KEY)
    assert client._transport.scrub("Bearer a-different-token") == "Bearer [REDACTED]"
    assert not client._transport.contains_api_key("Bearer a-different-token")


def test_error_scrubbing_redacts_mixed_encoded_keys_in_values_and_dictionary_keys(gaia_server):
    client = GaiaClient(gaia_server.url, api_key=KEY)
    secrets = " | ".join(
        encoded_key(form) for form in ["raw", "full_percent", "lower_percent", "nested_percent"]
    )
    message = "Bearer a-different-token " + secrets
    error = ContractMismatchError(message, details={secrets: [{"value": message}]})
    scrubbed = client._transport.scrub_error(error)
    redacted = " | ".join(["[REDACTED]"] * 4)
    assert str(scrubbed) == "Bearer [REDACTED] " + redacted
    assert scrubbed.details == {redacted: [{"value": "Bearer [REDACTED] " + redacted}]}
    assert KEY not in unquote(unquote(json.dumps(scrubbed.to_payload())))


@pytest.mark.parametrize("tool", ["custom_read", "get_server_info"])
def test_excessively_nested_percent_encoding_fails_closed_with_a_fixed_error(
    gaia_server: FakeGaiaServer, tool: str
):
    secret = encoded_key("full_percent")
    for _ in range(40):
        secret = quote(secret, safe="")
    if tool == "get_server_info":
        gaia_server.info["extra"] = [{secret: "safe"}]
    else:
        gaia_server.tools[tool] = lambda _: tool_ok({"extra": [secret]})
    with pytest.raises(ContractMismatchError) as caught:
        GaiaClient(gaia_server.url, api_key=KEY).call(tool)
    assert str(caught.value) == "gaia-library returned excessively nested percent encoding"
    assert KEY not in json.dumps(caught.value.to_payload())
    assert secret not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("channel", ["http", "rpc", "tool"])
def test_uninspectable_error_text_is_fully_redacted_without_exposing_exception_context(
    gaia_server: FakeGaiaServer, channel: str
):
    secret = encoded_key("full_percent")
    for _ in range(40):
        secret = quote(secret, safe="")
    message = f"reflected {KEY} {secret}"
    if channel == "http":
        gaia_server.http_status = 401
        gaia_server.http_body = message.encode("utf-8")
    elif channel == "rpc":
        gaia_server.rpc_errors["get_server_info"] = {
            "code": -32001,
            "message": message,
            "data": {"code": "unauthorized", "message": message},
        }
    else:
        gaia_server.tools["get_server_info"] = lambda _: tool_error(
            "unauthorized", message, {message: [secret]}
        )
    with pytest.raises(NarumiError) as caught:
        GaiaClient(gaia_server.url, api_key=KEY).get_server_info()
    assert caught.value.code == "scope_denied"
    trace = "".join(traceback.format_exception(caught.value))
    assert KEY not in trace and secret not in trace
    assert KEY not in json.dumps(caught.value.to_payload())


def test_malformed_response_with_secret_field_is_rejected_without_leaking_validation_paths(
    gaia_server: FakeGaiaServer,
):
    gaia_server.tools["get_glossary"] = lambda _: tool_ok(
        {"terms": [], "vocabulary_hints": {KEY: "not a list"}}
    )
    with pytest.raises(ContractMismatchError) as caught:
        GaiaClient(gaia_server.url, api_key=KEY).get_glossary()
    assert str(caught.value) == FAILURE
    assert KEY not in json.dumps(caught.value.to_payload())
