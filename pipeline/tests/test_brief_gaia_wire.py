"""Brief/export integration through the real Gaia client and an in-process MCP fake."""

from __future__ import annotations

from pathlib import Path

import pytest
from narumi.brief import BRIEF_ARTIFACT_KEY, BRIEF_PATH, build_brief
from narumi.errors import ContractMismatchError, EngineUnavailableError
from narumi.export.gaia import GaiaExporter
from narumi.gaia import GaiaClient

from .test_brief import StubGaia, make_bundle
from .test_export_gaia import MINUTES, bundle_with_minutes
from .test_gaia_client import FakeGaiaServer, tool_ok


@pytest.fixture()
def gaia_wire():
    blueprint = StubGaia()
    server = FakeGaiaServer()
    server.info["client"]["default_scope"] = "client-a"
    server.tools.update(
        {
            "get_engagement": lambda _: tool_ok(blueprint.engagement),
            "get_glossary": lambda _: tool_ok(blueprint.glossary),
            "search_context": lambda _: tool_ok(blueprint.search),
            "propose_update": lambda _: tool_ok(
                {"proposal_id": 17, "status": "pending", "duplicate": False}
            ),
        }
    )
    server.start()
    try:
        yield server, GaiaClient(server.url, api_key="test-brief-client-key", timeout=1), blueprint
    finally:
        server.stop()


def test_real_client_builds_scoped_brief_and_proposes_contract_valid_minutes(
    tmp_path: Path, gaia_wire
):
    server, client, blueprint = gaia_wire
    bundle = bundle_with_minutes(tmp_path)
    brief = build_brief(bundle, client)
    assert brief.participants[0].person_id == 9
    assert "SCIM" in brief.vocab_hints and "スキム" in brief.vocab_hints
    assert brief.gaia_context["search_context"] == blueprint.search
    outcome = GaiaExporter(client_factory=lambda: client).export(
        bundle, minutes_version=1, options={}
    )
    assert outcome.details["proposal_id"] == 17 and outcome.details["status"] == "pending"
    calls = [
        (frame["params"]["name"], frame["params"]["arguments"]) for frame in server.call_frames()
    ]
    assert [name for name, _ in calls] == [
        "get_server_info",
        "get_engagement",
        "get_glossary",
        "search_context",
        "get_engagement",
        "propose_update",
    ]
    assert all(args["scope"] == "client-a" for name, args in calls if name != "get_server_info")
    assert calls[1][1] == {"name": "acme", "scope": "client-a"}
    assert calls[2][1] == {"engagement_id": 42, "scope": "client-a"}
    assert calls[3][1] == {"query": bundle.manifest.meeting_name, "scope": "client-a"}
    assert calls[-1][1]["patch"]["summary"] == MINUTES


@pytest.mark.parametrize("tool", ["get_engagement", "get_glossary", "search_context"])
def test_malformed_nested_tool_results_cannot_produce_a_thinner_brief(
    tmp_path: Path, gaia_wire, tool: str
):
    server, client, _ = gaia_wire
    server.tools[tool] = lambda _: tool_ok({"results": []})
    bundle = make_bundle(tmp_path)
    with pytest.raises(ContractMismatchError):
        build_brief(bundle, client)
    assert BRIEF_ARTIFACT_KEY not in bundle.manifest.artifacts
    assert not bundle.abspath(BRIEF_PATH).exists()


def test_same_client_cannot_reuse_brief_after_server_becomes_unreachable(tmp_path: Path, gaia_wire):
    server, client, _ = gaia_wire
    bundle = make_bundle(tmp_path)
    build_brief(bundle, client)
    original = bundle.abspath(BRIEF_PATH).read_bytes()
    server.http_status = 503
    with pytest.raises(EngineUnavailableError):
        build_brief(bundle, client)
    assert bundle.abspath(BRIEF_PATH).read_bytes() == original


def test_self_name_is_not_assigned_an_id_from_a_same_name_search_result(tmp_path: Path):
    bundle = make_bundle(tmp_path)
    bundle.manifest.config.self_name = "田中太郎"
    brief = build_brief(bundle, StubGaia())  # type: ignore[arg-type]
    assert brief.participants[0].person_id is None
    assert brief.participants[1].person_id == 9
    assert brief.participants[0].note == "記録者（本人）"
