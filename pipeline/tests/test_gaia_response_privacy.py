"""Successful Gaia responses must not move bearer credentials into returned or saved data."""

from __future__ import annotations

import json
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from narumi.brief import BRIEF_ARTIFACT_KEY, BRIEF_PATH, build_brief, inject_brief
from narumi.errors import ContractMismatchError
from narumi.gaia import GaiaClient

from .test_brief import StubGaia, make_bundle
from .test_gaia_client import FakeGaiaServer, empty_search, engagement_result, tool_ok
from .test_gaia_client import gaia_server as gaia_server

KEY = "gaia_privacy_ab12+cd34/ef56="
FAILURE = "gaia-library returned credential material in a tool response"
TOOLS = ["get_glossary", "search_context", "get_engagement", "resolve_speakers", "propose_update"]


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
    "position", ["value", "key", "array", "url", "encoded_url", "bearer", "nested_key"]
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
    for secret in (KEY, quote(KEY, safe="")):
        assert secret not in public_error and secret not in trace and secret not in caplog.text


@pytest.mark.parametrize("tool", TOOLS)
def test_low_level_call_cannot_bypass_the_success_response_privacy_guard(
    gaia_server: FakeGaiaServer, tool: str
):
    gaia_server.tools[tool] = lambda _: tool_ok(reflected_response(tool, "nested_key"))
    with pytest.raises(ContractMismatchError, match="credential material"):
        GaiaClient(gaia_server.url, api_key=KEY).call(tool)


@pytest.mark.parametrize("tool", [*TOOLS, "custom_read"])
def test_safe_successful_responses_retain_contract_and_additive_fields(
    gaia_server: FakeGaiaServer, tool: str
):
    payload = valid_response(tool)
    payload["new_field"] = {"nested": ["safe value", {"bool": True, "number": 7}]}
    gaia_server.tools[tool] = lambda _: tool_ok(payload)
    assert call_tool(GaiaClient(gaia_server.url, api_key=KEY), tool) == payload


@pytest.mark.parametrize("entry", ["metadata", "raw", "implicit"])
def test_server_info_redaction_remains_compatible_on_every_metadata_path(
    gaia_server: FakeGaiaServer, entry: str
):
    gaia_server.info["client"]["name"] = KEY
    gaia_server.info["extra"] = {KEY: [{"url": f"https://example.test/{quote(KEY, safe='')}"}]}
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
    serialized = json.dumps({"returned": result, "cached": client._server_info})
    assert KEY not in serialized and quote(KEY, safe="") not in serialized


@pytest.mark.parametrize("tool", ["get_glossary", "get_engagement", "search_context"])
@pytest.mark.parametrize("existing_brief", [False, True])
def test_secret_response_never_reaches_brief_storage_or_prompt_injection(
    tmp_path: Path, gaia_server: FakeGaiaServer, tool: str, existing_brief: bool
):
    blueprint = StubGaia()
    payloads = {
        "get_glossary": deepcopy(blueprint.glossary),
        "get_engagement": deepcopy(blueprint.engagement),
        "search_context": deepcopy(blueprint.search),
    }
    if tool == "get_glossary":
        payloads[tool]["vocabulary_hints"].append(KEY)
    elif tool == "get_engagement":
        payloads[tool]["people"][0]["person"]["aliases"].append({"alias": KEY})
    else:
        payloads[tool]["entities"][0]["refs"][0]["uri"] = f"https://example.test/{KEY}"
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
