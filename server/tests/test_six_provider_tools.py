"""Resident-server integration for the complete six-provider surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from narumi_server.app import dispatch
from narumi_server.context import build_context
from narumi_server.provider_tools import PROVIDER_TOOLS

from pipeline.tests.provider_fakes import FakeCodexBackend, MemorySecretStore
from pipeline.tests.test_provider_model_verification import (
    MODEL_IDS,
    SECRET,
    ProbeBackend,
    VerificationMetadata,
)
from pipeline.tests.test_six_provider_connections import (
    AUTH_METHODS,
    PROVIDER_IDS,
    StaticRuntimeInspector,
)


def successful(ctx, tool: str, args: dict) -> dict:
    outcome = dispatch(ctx, tool, args)
    assert not outcome.is_error, outcome.payload
    ctx.contracts.validate_output(tool, outcome.payload)
    assert SECRET not in json.dumps(outcome.payload)
    return outcome.payload


@pytest.mark.parametrize("tool", sorted(PROVIDER_TOOLS))
def test_every_provider_handler_requires_the_resident_transport(home: Path, tool: str):
    ctx = build_context(
        home,
        transports=["stdio"],
        provider_secret_store=MemorySecretStore(),
        provider_codex_backend=FakeCodexBackend(),
    )
    try:
        outcome = dispatch(ctx, tool, ctx.contracts[tool].input_examples[0])
        assert outcome.is_error
        assert outcome.payload["error"]["code"] == "authentication_required"
    finally:
        ctx.close()


def test_resident_server_advertises_exact_six_provider_capabilities(home: Path):
    ctx = build_context(
        home,
        transports=["streamable-http"],
        validate_output=True,
        provider_secret_store=MemorySecretStore(),
        provider_codex_backend=FakeCodexBackend(),
    )
    ctx.providers.runtime.inspector = StaticRuntimeInspector()
    try:
        info = successful(ctx, "get_server_info", {})
        assert info["capabilities"]["workflow"] == {
            "provider_connections": True,
            "provider_models": True,
            "provider_model_verification": True,
            "stage_model_selection": True,
            "ensemble_generation": False,
        }
        assert info["capabilities"]["minutes_model_providers"] == PROVIDER_IDS
        assert info["capabilities"]["transcription_model_providers"] == ["openai-api"]

        providers = successful(ctx, "list_providers", {})["providers"]
        assert [provider["provider_id"] for provider in providers] == PROVIDER_IDS
        assert {
            provider["provider_id"]: provider["auth_methods"] for provider in providers
        } == AUTH_METHODS
    finally:
        ctx.close()


def test_verify_tool_audit_and_registry_never_persist_the_api_key(
    home: Path, caplog: pytest.LogCaptureFixture
):
    secrets = MemorySecretStore()
    metadata = VerificationMetadata()
    backend = ProbeBackend("openai-compatible-api")
    ctx = build_context(
        home,
        transports=["streamable-http"],
        validate_output=True,
        provider_secret_store=secrets,
        provider_metadata_client=metadata,
        provider_codex_backend=FakeCodexBackend(),
        provider_openai_compatible_backend=backend,
    )
    ctx.providers.runtime.inspector = StaticRuntimeInspector()
    try:
        connection = successful(
            ctx,
            "set_provider_connection",
            {
                "provider_id": "openai-compatible-api",
                "display_name": "Audited compatible fixture",
                "endpoint": "https://127.0.0.1:9443/v1",
                "auth_method": "api_key",
                "api_surface": "responses",
                "api_key": SECRET,
                "request_id": "server-compatible-create-001",
            },
        )["connection"]
        with ctx.providers.store.transaction() as document:
            runtime = ctx.providers.runtime._current("openai-compatible-api", document)
            runtime["state"] = "ready"
            document["runtimes"]["openai-compatible-api"] = runtime

        tested = successful(
            ctx,
            "test_provider_connection",
            {
                "connection_id": connection["connection_id"],
                "expected_revision": connection["revision"],
            },
        )
        assert tested["connected"] is True
        verified = successful(
            ctx,
            "verify_provider_model",
            {
                "connection_id": connection["connection_id"],
                "expected_revision": connection["revision"],
                "model_id": MODEL_IDS["openai-compatible-api"],
                "confirmation": "send_test_prompt_and_may_charge",
                "request_id": "server-compatible-verify-001",
            },
        )
        assert verified["model"]["availability"] == "available"
        assert len(backend.verify_calls) == 1

        audits = [
            *ctx.catalog.list_audit(action="set_provider_connection"),
            *ctx.catalog.list_audit(action="verify_provider_model"),
        ]
        assert len(audits) == 2
        assert audits[-1]["detail"] == {"connection_id": connection["connection_id"]}
        assert SECRET not in json.dumps(audits)
        assert SECRET not in caplog.text
        for path in home.rglob("*"):
            if path.is_file():
                assert SECRET.encode() not in path.read_bytes(), path
    finally:
        ctx.close()
