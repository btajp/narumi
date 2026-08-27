"""Freeze the effective read scope and prevent cache attribution across Gaia session changes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from narumi.brief import BRIEF_ARTIFACT_KEY, BRIEF_PATH, Brief, build_brief
from narumi.errors import ContractMismatchError, ScopeDeniedError
from narumi.gaia import GaiaClient

from .test_brief import make_bundle
from .test_gaia_client import FakeGaiaServer, empty_search, engagement_result, tool_ok


@pytest.fixture()
def scoped_gaia():
    server = FakeGaiaServer()
    server.info["client"]["default_scope"] = "client-a"

    def scope_for(args: dict[str, Any]) -> str:
        return args.get("scope", server.info["client"].get("default_scope"))

    def glossary(args: dict[str, Any]) -> dict[str, Any]:
        scope = scope_for(args)
        return tool_ok({"terms": [], "vocabulary_hints": [f"vocabulary from {scope}"]})

    def search(args: dict[str, Any]) -> dict[str, Any]:
        scope = scope_for(args)
        result = empty_search(args["query"])
        result["scopes"] = [scope]
        result["interactions"] = [
            {
                "id": 17,
                "kind": "meeting",
                "occurred_at": "2026-08-20T00:00:00Z",
                "summary": f"context from {scope}",
                "scope": scope,
                "person_ids": [],
            }
        ]
        return tool_ok(result)

    server.tools.update(
        {
            "get_engagement": lambda args: tool_ok(
                engagement_result(args["name"], scope_for(args))
            ),
            "get_glossary": glossary,
            "search_context": search,
        }
    )
    server.start()
    try:
        yield server, GaiaClient(server.url)
    finally:
        server.stop()


def scoped_calls(server: FakeGaiaServer) -> list[tuple[str, dict[str, Any]]]:
    return [
        (frame["params"]["name"], frame["params"]["arguments"])
        for frame in server.call_frames()
        if frame["params"]["name"] != "get_server_info"
    ]


@pytest.mark.parametrize("manifest_scope", [None, "client-b"])
def test_resolved_scope_is_explicit_on_every_read_and_in_cache_params(
    tmp_path: Path, scoped_gaia, manifest_scope: str | None
):
    server, client = scoped_gaia
    bundle = make_bundle(tmp_path)
    bundle.manifest.scope = manifest_scope
    brief = build_brief(bundle, client)
    expected_scope = manifest_scope or "client-a"
    assert [name for name, _ in scoped_calls(server)] == [
        "get_engagement",
        "get_glossary",
        "search_context",
    ]
    assert all(args["scope"] == expected_scope for _, args in scoped_calls(server))
    assert f"vocabulary from {expected_scope}" in brief.vocab_hints
    assert brief.previous_points == [f"context from {expected_scope}"]
    assert bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY].params["gaia_scope"] == expected_scope


@pytest.mark.parametrize("default_scope", [None, "", " "])
def test_no_effective_scope_fails_closed_before_reading_context(
    tmp_path: Path, scoped_gaia, default_scope: str | None
):
    server, client = scoped_gaia
    if default_scope is None:
        server.info["client"].pop("default_scope")
    else:
        server.info["client"]["default_scope"] = default_scope
    bundle = make_bundle(tmp_path)
    bundle.manifest.scope = None
    with pytest.raises(ScopeDeniedError, match="meeting scope or a client default scope"):
        build_brief(bundle, client)
    assert scoped_calls(server) == []
    assert not bundle.abspath(BRIEF_PATH).exists()
    assert BRIEF_ARTIFACT_KEY not in bundle.manifest.artifacts


def test_explicit_scope_works_without_a_client_default(tmp_path: Path, scoped_gaia):
    server, client = scoped_gaia
    server.info["client"].pop("default_scope")
    bundle = make_bundle(tmp_path)
    brief = build_brief(bundle, client)
    assert brief.previous_points == ["context from client-a"]
    assert all(args["scope"] == "client-a" for _, args in scoped_calls(server))


@pytest.mark.parametrize("retry_tool", ["get_glossary", "search_context"])
@pytest.mark.parametrize("identity_field", ["default_scope", "name"])
def test_reinitialized_connection_cannot_read_implicit_new_scope_or_save_misattributed_brief(
    tmp_path: Path, scoped_gaia, retry_tool: str, identity_field: str
):
    server, client = scoped_gaia
    previous_tool = "get_engagement" if retry_tool == "get_glossary" else "get_glossary"
    previous_handler = server.tools[previous_tool]

    def expire_session(args):
        result = previous_handler(args)
        server.info["client"][identity_field] = (
            "client-b" if identity_field == "default_scope" else "another-client"
        )
        server.fail_next_call_with_404 = True
        return result

    server.tools[previous_tool] = expire_session
    bundle = make_bundle(tmp_path)
    bundle.manifest.scope = None
    original = build_brief(bundle)
    original_bytes = bundle.abspath(BRIEF_PATH).read_bytes()
    with pytest.raises(ContractMismatchError, match="connection identity changed"):
        build_brief(bundle, client)
    retried = [args for name, args in scoped_calls(server) if name == retry_tool]
    assert len(retried) == 2
    assert all(args["scope"] == "client-a" for args in retried)
    assert bundle.abspath(BRIEF_PATH).read_bytes() == original_bytes
    assert "context from client-b" not in str(original)

    # Returning to the original identity must not revive a wrongly attributed B artifact.
    server.tools[previous_tool] = previous_handler
    server.info["client"][identity_field] = (
        "client-a" if identity_field == "default_scope" else "narumi"
    )
    refreshed = build_brief(bundle, client)
    assert refreshed.previous_points == ["context from client-a"]
    assert "vocabulary from client-b" not in refreshed.vocab_hints


def test_legacy_version_two_cache_is_regenerated_with_pinned_scope(tmp_path: Path, scoped_gaia):
    server, client = scoped_gaia
    bundle = make_bundle(tmp_path)
    bundle.manifest.scope = None
    build_brief(bundle, client)
    current = bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY]
    old_params = {key: value for key, value in current.params.items() if key != "gaia_scope"}
    old_params["version"] = 2
    bundle.run_stage(
        BRIEF_ARTIFACT_KEY,
        inputs=current.inputs,
        params=old_params,
        producer=("brief", "2"),
        output=BRIEF_PATH,
        fn=lambda _: bundle.write_json(BRIEF_PATH, Brief(vocab_hints=["wrong client-b context"])),
        force=True,
    )
    server.frames.clear()
    repaired = build_brief(bundle, client)
    assert "wrong client-b context" not in repaired.vocab_hints
    assert scoped_calls(server)
    assert bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY].producer.version == "3"
