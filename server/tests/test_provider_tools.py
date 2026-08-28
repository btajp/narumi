"""Contract-to-service integration with fake secrets and metadata, never real credentials."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from narumi.contracts import load_contracts
from narumi.errors import EngineUnavailableError
from narumi.providers.runtime import RuntimeInspector
from narumi_server.app import dispatch
from narumi_server.context import build_context
from narumi_server.provider_tools import PROVIDER_TOOLS

SECRET = "fake-provider-integration-secret-739125"


class MemorySecrets:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get(self, account: str) -> str | None:
        return self.values.get(account)

    def set(self, account: str, value: str) -> None:
        self.values[account] = value

    def delete(self, account: str) -> None:
        self.values.pop(account, None)


class Metadata:
    def __init__(self):
        self.calls = 0

    def fetch(self, provider_id: str, endpoint: str, api_key: str | None) -> list[dict]:
        assert provider_id == "anthropic-api"
        assert endpoint == "https://api.anthropic.com"
        assert api_key == SECRET
        self.calls += 1
        return copy.deepcopy(load_contracts()["list_provider_models"].output_examples[0]["models"])


def new_connection() -> dict[str, Any]:
    return {
        "provider_id": "anthropic-api",
        "display_name": "議事録用 API",
        "auth_method": "api_key",
        "api_key": SECRET,
        "request_id": str(uuid4()),
    }


def result(ctx, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    outcome = dispatch(ctx, tool, args)
    assert not outcome.is_error, outcome.payload
    ctx.contracts.validate_output(tool, outcome.payload)
    assert SECRET not in json.dumps(outcome.payload)
    return outcome.payload


def test_connection_round_trip_metadata_cas_and_key_removal(tmp_path: Path, caplog):
    secrets, metadata = MemorySecrets(), Metadata()
    ctx = build_context(
        tmp_path,
        transports=["streamable-http"],
        validate_output=True,
        provider_secret_store=secrets,
        provider_metadata_client=metadata,
    )
    try:
        created = result(ctx, "set_provider_connection", new_connection())["connection"]
        assert created["credential_present"] is True
        assert created["auth_state"] == "unverified"
        listed = result(ctx, "list_provider_connections", {})["connections"]
        assert listed == [created]
        cid = created["connection_id"]
        checked = result(
            ctx, "test_provider_connection", {"connection_id": cid, "expected_revision": 1}
        )
        assert checked["connected"] is True
        assert checked["connection"]["revision"] == 1
        assert checked["connection"]["last_generation_state"] == "never"
        models = result(ctx, "list_provider_models", {"connection_id": cid})
        assert models["models"][0]["model_id"] == "fixture-text-model"
        assert metadata.calls == 1
        changed = result(
            ctx,
            "set_provider_connection",
            {
                "connection_id": cid,
                "expected_revision": 1,
                "enabled": False,
                "request_id": str(uuid4()),
            },
        )["connection"]
        assert changed["revision"] == 2 and changed["credential_present"]
        conflict = dispatch(
            ctx,
            "set_provider_connection",
            {
                "connection_id": cid,
                "expected_revision": 1,
                "enabled": True,
                "request_id": str(uuid4()),
            },
        )
        assert conflict.is_error
        assert conflict.payload["error"]["code"] == "configuration_conflict"
        result(
            ctx,
            "delete_provider_connection",
            {
                "connection_id": cid,
                "expected_revision": 2,
                "confirm": True,
                "request_id": str(uuid4()),
            },
        )
        assert result(ctx, "list_provider_connections", {})["connections"] == []
        assert SECRET not in secrets.values.values()
        assert SECRET not in caplog.text
        for file in tmp_path.rglob("*"):
            if file.is_file():
                assert SECRET.encode() not in file.read_bytes(), file.name
    finally:
        ctx.close()


def test_secret_replay_survives_restart_and_does_not_bypass_argument_checks(tmp_path: Path):
    secrets = MemorySecrets()
    args = new_connection()
    ctx = build_context(tmp_path, transports=["streamable-http"], provider_secret_store=secrets)
    original = result(ctx, "set_provider_connection", args)
    ctx.close()
    restarted = build_context(
        tmp_path, transports=["streamable-http"], provider_secret_store=secrets
    )
    try:
        assert result(restarted, "set_provider_connection", args) == original
        changed = dispatch(
            restarted, "set_provider_connection", {**args, "api_key": "different-fake-secret"}
        )
        assert changed.is_error
        assert SECRET not in json.dumps(changed.payload)
        assert "different-fake-secret" not in json.dumps(changed.payload)
        assert len(result(restarted, "list_provider_connections", {})["connections"]) == 1
    finally:
        restarted.close()


@pytest.mark.parametrize("queued", [True, False])
def test_runtime_cancel_keeps_exclusion_until_worker_stops(tmp_path: Path, queued: bool):
    started, release = threading.Event(), threading.Event()

    class Inspector(RuntimeInspector):
        def resource(self, provider_id):
            return {
                "resource_id": "anthropic-client",
                "display_name": "Fixture installed client",
                "kind": "runtime",
                "version": "1.0.0",
                "source": "installed",
                "download_host": None,
                "sha256": "a" * 64,
                "license": "Fixture",
            }

        def prepare(self, root, provider_id, resource, progress):
            started.set()
            assert release.wait(5)
            progress("fixture_inspection", 0.5)

    instance_id = str(uuid4())
    ctx = build_context(
        tmp_path,
        transports=["streamable-http"],
        provider_secret_store=MemorySecrets(),
        server_instance_id=instance_id,
    )
    ctx.providers.runtime.inspector = Inspector()

    def runtime():
        providers = result(ctx, "list_providers", {})["providers"]
        return next(p["runtime"] for p in providers if p["provider_id"] == "anthropic-api")

    def prepare_args():
        current = runtime()
        return {
            "provider_id": "anthropic-api",
            "resource_id": current["resources"][0]["resource_id"],
            "expected_catalog_revision": current["catalog_revision"],
            "action": "prepare",
            "request_id": str(uuid4()),
        }

    def occupy_worker(_progress):
        started.set()
        assert release.wait(5)
        return {}

    try:
        assert ctx.server_instance_id == ctx.providers.server_instance_id == instance_id
        if queued:
            ctx.jobs.submit("process", None, occupy_worker)
            assert started.wait(5)
        job_id = result(ctx, "prepare_provider_runtime", prepare_args())["job_id"]
        assert started.wait(5)
        cancelled = result(ctx, "cancel_job", {"job_id": job_id, "request_id": str(uuid4())})["job"]
        assert cancelled["status"] == ("cancelled" if queued else "running")
        if not queued:
            assert runtime()["active_setup"]["job_id"] == job_id
            refused = dispatch(ctx, "prepare_provider_runtime", prepare_args())
            assert refused.is_error and refused.payload["error"]["code"] == "busy"
        release.set()
        assert ctx.jobs.wait(job_id, timeout=5)["status"] == "cancelled"
        assert result(ctx, "get_job_status", {"job_id": job_id})["job"]["status"] == "cancelled"
        assert runtime()["active_setup"] is None
        next_job_id = result(ctx, "prepare_provider_runtime", prepare_args())["job_id"]
        assert ctx.jobs.wait(next_job_id, timeout=5)["status"] == "succeeded"
        assert runtime()["last_setup"]["job_id"] == next_job_id
    finally:
        release.set()
        ctx.close()


class ExplodingService:
    def __getattr__(self, name: str):
        def fail(*args, **kwargs):
            raise EngineUnavailableError(SECRET, details={"upstream": SECRET})

        return fail

    def close(self):
        pass


@pytest.mark.parametrize("tool", sorted(PROVIDER_TOOLS))
def test_all_provider_failures_are_redacted_before_response_audit_or_replay(
    tmp_path: Path, caplog, tool: str
):
    ctx = build_context(
        tmp_path, transports=["streamable-http"], provider_service=ExplodingService()
    )
    try:
        outcome = dispatch(ctx, tool, ctx.contracts[tool].input_examples[0])
        assert outcome.is_error
        assert SECRET not in json.dumps(outcome.payload)
        assert SECRET not in caplog.text
        for file in tmp_path.rglob("*"):
            if file.is_file():
                assert SECRET.encode() not in file.read_bytes(), file.name
    finally:
        ctx.close()


@pytest.mark.parametrize("tool", [*sorted(PROVIDER_TOOLS), "set_gaia_connection"])
def test_private_operations_cannot_bypass_resident_authentication_via_stdio(tmp_path: Path, tool):
    ctx = build_context(tmp_path, transports=["stdio"], provider_service=ExplodingService())
    try:
        outcome = dispatch(ctx, tool, ctx.contracts[tool].input_examples[0])
        assert outcome.is_error
        assert outcome.payload["error"]["code"] == "authentication_required"
    finally:
        ctx.close()
