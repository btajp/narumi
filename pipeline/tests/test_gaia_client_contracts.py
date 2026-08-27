"""Real Gaia payload shapes and semantic adapter regressions, using only fake servers."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from narumi.errors import (
    ContractMismatchError,
    EngineUnavailableError,
    InvalidArgumentError,
    NarumiError,
)
from narumi.gaia import GaiaClient

from .test_gaia_client import (
    FakeGaiaServer,
    empty_search,
    engagement_result,
    tool_error,
    tool_ok,
)
from .test_gaia_client import gaia_server as gaia_server


def search_result() -> dict[str, Any]:
    result = empty_search("案件")
    result["scopes"] = ["cn", "partner"]
    result["cross_scope"] = True
    result["entities"] = [
        {
            "type": "engagement",
            "id": 42,
            "name": "案件",
            "summary": "導入計画",
            "score": 1.0,
            "matched_on": ["name"],
            "facts": [
                {
                    "id": 5,
                    "entity_type": "engagement",
                    "entity_id": 42,
                    "statement": "来月に開始する",
                    "kind": "fact",
                    "scope": "cn",
                    "created_at": "2026-08-27T00:00:00Z",
                }
            ],
            "refs": [
                {
                    "id": 6,
                    "target_type": "engagement",
                    "target_id": 42,
                    "system": "file",
                    "uri": "file:///tmp/meeting.md",
                    "note": "前回の合意",
                    "snapshot": "来月開始",
                    "scope": "cn",
                    "created_at": "2026-08-27T00:00:00Z",
                }
            ],
        }
    ]
    return result


def proposal_args() -> dict[str, Any]:
    return {
        "target_type": "interaction",
        "action": "insert",
        "kind": "fact",
        "patch": {"kind": "meeting", "occurred_at": "2026-08-27", "summary": "合意の議事録"},
        "scope": "cn",
        "request_id": "narumi-request-123",
        "provenance": {"system": "minutes", "uri": "minutes://meeting/1", "note": "議事録 v1"},
    }


def test_search_preserves_entities_facts_and_refs(gaia_server: FakeGaiaServer):
    result = search_result()
    gaia_server.tools["search_context"] = lambda _: tool_ok(result)
    actual = GaiaClient(gaia_server.url).search_context(
        "案件", scope=["cn", "partner"], limit=12, types=["engagement"]
    )
    assert actual == result
    assert actual["entities"][0]["facts"][0]["statement"] == "来月に開始する"
    assert actual["entities"][0]["refs"][0]["snapshot"] == "来月開始"
    assert gaia_server.call_frames()[-1]["params"]["arguments"] == {
        "query": "案件",
        "scope": ["cn", "partner"],
        "limit": 12,
        "types": ["engagement"],
    }


@pytest.mark.parametrize("scope", ["cn", ["cn", "partner"]])
def test_glossary_known_id_propagates_scope_without_name_lookup(gaia_server, scope):
    result = {"terms": [], "vocabulary_hints": ["案件", "田中"]}
    gaia_server.tools["get_glossary"] = lambda _: tool_ok(result)
    assert GaiaClient(gaia_server.url).get_glossary(engagement_id=42, scope=scope) == result
    assert gaia_server.call_frames()[-1]["params"]["arguments"] == {
        "engagement_id": 42,
        "scope": scope,
    }
    assert len(gaia_server.call_frames()) == 2


def test_speakers_keep_ambiguity_and_use_scoped_engagement_id(gaia_server: FakeGaiaServer):
    names = ["tanaka", "yamada", "unknown"]
    result = {
        "results": [
            {
                "input": "tanaka",
                "normalized": "tanaka",
                "status": "matched",
                "confidence": 1.0,
                "person": {"id": 3, "name": "田中太郎", "aliases": [{"alias": "tanaka"}]},
                "candidates": [],
            },
            {
                "input": "yamada",
                "normalized": "yamada",
                "status": "ambiguous",
                "confidence": 0.5,
                "candidates": [
                    {"person_id": 8, "name": "山田", "confidence": 0.5, "reason": "alias"}
                ],
            },
            {
                "input": "unknown",
                "normalized": "unknown",
                "status": "unmatched",
                "confidence": 0.0,
                "candidates": [],
            },
        ]
    }
    gaia_server.tools["get_engagement"] = lambda _: tool_ok(engagement_result())
    gaia_server.tools["resolve_speakers"] = lambda _: tool_ok(result)
    assert (
        GaiaClient(gaia_server.url).resolve_speakers(names, engagement="acme", scope="cn") == result
    )
    assert [frame["params"] for frame in gaia_server.call_frames()][1:] == [
        {"name": "get_engagement", "arguments": {"name": "acme", "scope": "cn"}},
        {
            "name": "resolve_speakers",
            "arguments": {"display_names": names, "engagement_id": 42, "scope": "cn"},
        },
    ]


def test_missing_engagement_is_not_silently_ignored(gaia_server: FakeGaiaServer):
    gaia_server.tools["get_engagement"] = lambda _: tool_error("not_found", "engagement is absent")
    with pytest.raises(NarumiError) as exc:
        GaiaClient(gaia_server.url).get_glossary("absent", scope="cn")
    assert exc.value.code == "not_found"
    assert gaia_server.call_frames()[-1]["params"]["name"] == "get_engagement"


def test_proposal_has_real_contract_fields_and_object_provenance(gaia_server: FakeGaiaServer):
    result = {"proposal_id": 12, "status": "pending", "duplicate": False}
    gaia_server.tools["propose_update"] = lambda _: tool_ok(result)
    client = GaiaClient(gaia_server.url)
    assert client.propose_update(**proposal_args()) == result
    assert gaia_server.call_frames()[-1]["params"]["arguments"] == proposal_args()
    result["duplicate"] = True
    assert client.propose_update(**proposal_args())["duplicate"] is True


def test_update_and_existing_ref_provenance(gaia_server: FakeGaiaServer):
    gaia_server.tools["propose_update"] = lambda _: tool_ok(
        {"proposal_id": 8, "status": "pending", "duplicate": False}
    )
    args = {**proposal_args(), "action": "update", "target_id": 7, "provenance": {"ref_id": 4}}
    GaiaClient(gaia_server.url).propose_update(**args)
    assert gaia_server.call_frames()[-1]["params"]["arguments"] == args


@pytest.mark.parametrize("version", ["0.9.0", "2.0.0", "1", "1.0", "01.0.0", "draft"])
def test_wrong_contract_major_or_version_blocks_typed_calls(gaia_server, version):
    gaia_server.info["contract_version"] = version
    with pytest.raises(ContractMismatchError):
        GaiaClient(gaia_server.url).propose_update(**proposal_args())
    assert [frame["params"]["name"] for frame in gaia_server.call_frames()] == ["get_server_info"]


def test_missing_capability_blocks_writes_before_submission(gaia_server: FakeGaiaServer):
    gaia_server.info["capabilities"]["tools"].remove("propose_update")
    with pytest.raises(EngineUnavailableError) as exc:
        GaiaClient(gaia_server.url).propose_update(**proposal_args())
    assert exc.value.details["missing_tools"] == ["propose_update"]
    assert len(gaia_server.call_frames()) == 1


def test_server_info_cache_isolated_and_refreshable(gaia_server: FakeGaiaServer):
    client = GaiaClient(gaia_server.url)
    info = client.require_capabilities("search_context", "propose_update")
    assert info == gaia_server.info
    info["capabilities"]["tools"].append("forged")
    with pytest.raises(EngineUnavailableError):
        client.require_capabilities("forged")
    gaia_server.info["client"]["default_scope"] = "partner"
    assert client.get_server_info()["client"]["default_scope"] == "cn"
    assert client.get_server_info(refresh=True)["client"]["default_scope"] == "partner"
    assert len(gaia_server.call_frames()) == 2


@pytest.mark.parametrize(
    ("tool", "payload"),
    [
        ("get_server_info", {}),
        ("search_context", {"references": []}),
        ("get_engagement", {"engagement": {"id": 1}}),
        ("get_glossary", {"terms": []}),
        ("get_glossary", {"terms": [], "vocabulary_hints": [42]}),
        ("resolve_speakers", {"speakers": {}}),
        ("resolve_speakers", {"results": []}),
        (
            "resolve_speakers",
            {
                "results": [
                    {
                        "input": "name",
                        "normalized": "name",
                        "status": "matched",
                        "confidence": 1.0,
                        "candidates": [],
                    }
                ]
            },
        ),
        ("propose_update", {"proposal_id": "prop-1", "status": "pending", "duplicate": False}),
        ("propose_update", {"proposal_id": True, "status": "pending", "duplicate": False}),
        ("propose_update", {"proposal_id": 1, "status": "queued", "duplicate": False}),
        ("propose_update", {"proposal_id": 1, "status": "pending", "duplicate": 0}),
    ],
)
def test_malformed_or_draft_responses_never_become_empty_success(gaia_server, tool, payload):
    gaia_server.tools[tool] = lambda _: tool_ok(payload)
    client = GaiaClient(gaia_server.url)
    methods = {
        "get_server_info": lambda: client.get_server_info(),
        "search_context": lambda: client.search_context("q"),
        "get_engagement": lambda: client.get_engagement("acme"),
        "get_glossary": lambda: client.get_glossary(),
        "resolve_speakers": lambda: client.resolve_speakers(["name"]),
        "propose_update": lambda: client.propose_update(**proposal_args()),
    }
    with pytest.raises(ContractMismatchError):
        methods[tool]()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("entities",), ["not an object"]),
        (("entities", 0, "facts", 0, "kind"), "uncertain"),
        (("entities", 0, "refs", 0, "note"), None),
        (("entities", 0, "refs", 0, "id"), "6"),
        (("entities", 0, "refs"), {}),
        (("entities", 0, "score"), float("nan")),
    ],
)
def test_search_validates_nested_fact_and_reference_fields(gaia_server, path, value):
    result = copy.deepcopy(search_result())
    node = result
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    gaia_server.tools["search_context"] = lambda _: tool_ok(result)
    with pytest.raises(ContractMismatchError):
        GaiaClient(gaia_server.url).search_context("q")


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_type": "minutes"},
        {"action": "delete"},
        {"kind": "guess"},
        {"request_id": "short"},
        {"request_id": "あ" * 100},
        {"scope": ["cn"]},
        {"target_id": 4},
        {"action": "update"},
        {"action": "supersede", "target_id": 4},
        {"provenance": "minutes://meeting/1"},
        {"provenance": {"uri": "minutes://meeting/1"}},
        {"provenance": {"ref_id": True}},
        {"provenance": {"ref_id": 3, "note": "n"}},
    ],
)
def test_invalid_proposals_are_rejected_before_any_request(gaia_server, overrides):
    with pytest.raises(InvalidArgumentError):
        GaiaClient(gaia_server.url).propose_update(**{**proposal_args(), **overrides})
    assert gaia_server.frames == []


@pytest.mark.parametrize("scope", ["", " ", [], ["cn", ""], 1, [1]])
def test_invalid_scope_is_not_dropped_or_coerced(gaia_server, scope):
    with pytest.raises(InvalidArgumentError):
        GaiaClient(gaia_server.url).get_glossary(scope=scope)
    assert gaia_server.frames == []


def test_conflicting_name_and_id_are_rejected(gaia_server: FakeGaiaServer):
    with pytest.raises(InvalidArgumentError):
        GaiaClient(gaia_server.url).get_glossary("acme", engagement_id=1, scope="cn")
    assert gaia_server.frames == []


def test_legal_server_metadata_without_default_scope_is_preserved(gaia_server):
    del gaia_server.info["client"]["default_scope"]
    info = GaiaClient(gaia_server.url).get_server_info()
    assert info["client"] == {"name": "narumi", "role": "agent"}


def test_business_name_containing_unknown_tool_is_not_misclassified(gaia_server):
    gaia_server.rpc_errors["get_engagement"] = {
        "code": -32602,
        "message": "engagement `unknown tool` not found",
        "data": {"code": "not_found", "message": "engagement `unknown tool` not found"},
    }
    with pytest.raises(NarumiError) as exc:
        GaiaClient(gaia_server.url).get_engagement("unknown tool", scope="cn")
    assert exc.value.code == "not_found"


@pytest.mark.parametrize("invalid_payload", [None, [], {"terms": []}])
def test_invalid_structured_content_is_not_replaced_by_text_fallback(gaia_server, invalid_payload):
    gaia_server.tools["get_glossary"] = lambda _: {
        "structuredContent": invalid_payload,
        "content": [{"type": "text", "text": '{"terms": [], "vocabulary_hints": []}'}],
    }
    with pytest.raises(ContractMismatchError):
        GaiaClient(gaia_server.url).get_glossary()
