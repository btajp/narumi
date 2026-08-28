"""App-facing Gaia tools, metadata projection and secret-safe failures through dispatch/MCP."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

import pytest
from conftest import PerCallClient, call, make_recorded_bundle
from narumi.errors import (
    ContractMismatchError,
    EngineUnavailableError,
    ErrorCode,
    NarumiError,
    ScopeDeniedError,
)
from narumi.gaia.settings import ENV_GAIA_API_KEY, ENV_GAIA_URL, GaiaConnectionStore
from narumi_server.app import _checked_envelope, dispatch
from narumi_server.context import ServerContext, build_context

URL = "http://127.0.0.1:4111/mcp"
KEY = "fake-server-secret-90731468"


@pytest.fixture(autouse=True)
def no_real_gaia_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ENV_GAIA_URL, raising=False)
    monkeypatch.delenv(ENV_GAIA_API_KEY, raising=False)


@pytest.fixture(autouse=True)
def authenticated_resident_context(ctx: ServerContext):
    # The in-memory harness models an already-authenticated resident request. Transport
    # rejection is covered separately; these tests exercise the secret-safe handlers.
    ctx.transports = ["streamable-http"]


def rid() -> str:
    return str(uuid.uuid4())


async def test_settings_tools_are_public_and_credentials_are_not(
    client: PerCallClient, ctx: ServerContext, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.INFO)
    initial = await call(client, "get_gaia_connection")
    assert initial == {"connection": {"url": None, "has_api_key": False, "source": "unconfigured"}}
    request_id = rid()
    result = await call(
        client,
        "set_gaia_connection",
        {"url": URL, "api_key": KEY, "request_id": request_id},
    )
    assert result == {"connection": {"url": URL, "has_api_key": True, "source": "saved"}}
    assert await call(client, "get_gaia_connection") == result
    cached = ctx.catalog.get_request(request_id)
    assert cached is not None and cached["response"] == result
    audit = ctx.catalog.list_audit(action="set_gaia_connection")
    assert audit[0]["detail"] == {
        "updated": ["url", "api_key"],
        "enabled": True,
        "has_api_key": True,
    }
    make_recorded_bundle(ctx, meeting_id="20260827T010000Z-aaff1122")
    for value in (result, cached, audit, ctx.profiles.list(), repr(ctx), caplog.text):
        assert KEY not in str(value)
    for path in ctx.data_root.rglob("*"):
        if path.is_file() and path != ctx.gaia.path:
            assert KEY.encode() not in path.read_bytes(), path.name
    assert json.loads(ctx.gaia.path.read_text())["api_key"] == KEY


async def test_set_replay_does_not_replace_key_or_repeat_audit(
    client: PerCallClient, ctx: ServerContext, caplog: pytest.LogCaptureFixture
):
    request_id = rid()
    arguments = {"url": URL, "api_key": KEY, "request_id": request_id}
    first = await call(client, "set_gaia_connection", arguments)
    replay = await call(client, "set_gaia_connection", {**arguments, "api_key": "replacement-key"})
    assert replay == first
    assert json.loads(ctx.gaia.path.read_text())["api_key"] == KEY
    assert len(ctx.catalog.list_audit(action="set_gaia_connection")) == 1
    assert KEY not in caplog.text
    assert "replacement-key" not in str(ctx.catalog.get_request(request_id))


def test_context_root_override_isolates_saved_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    default_root = tmp_path / "default"
    monkeypatch.setenv("NARUMI_HOME", str(default_root))
    GaiaConnectionStore(default_root / "gaia.json", environ={}).set(url=URL, api_key=KEY)
    context = build_context(tmp_path / "override")
    try:
        assert context.gaia.path == tmp_path / "override" / "gaia.json"
        assert context.gaia.get() == {
            "url": None,
            "has_api_key": False,
            "source": "unconfigured",
        }
        assert context.gaia.client() is None
        context.gaia.set(url=None)
        assert json.loads(default_root.joinpath("gaia.json").read_text())["api_key"] == KEY
    finally:
        context.close()


@pytest.mark.parametrize("with_default_scope", [False, True])
def test_connection_test_only_reads_and_returns_allowed_metadata(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch, with_default_scope: bool
):
    invoked: list[Any] = []

    class FakeClient:
        def require_capabilities(self, *names):
            invoked.append(names)

        def get_server_info(self):
            invoked.append("get_server_info")
            return {
                "name": "gaia_library",
                "version": "0.1.0",
                "contract_version": "1.0.0",
                "api_key": KEY,
                "capabilities": {"private": KEY},
                "client": {
                    "name": "narumi",
                    "role": "agent",
                    **({"default_scope": "test-scope"} if with_default_scope else {}),
                    "api_key": KEY,
                },
            }

    def factory(*, timeout: float):
        invoked.append(timeout)
        return FakeClient()

    monkeypatch.setattr(ctx.gaia, "client", factory)
    outcome = dispatch(ctx, "test_gaia_connection", {"timeout_seconds": 3})
    assert not outcome.is_error
    assert outcome.payload == {
        "connected": True,
        "name": "gaia_library",
        "version": "0.1.0",
        "contract_version": "1.0.0",
        "client": {
            "name": "narumi",
            "role": "agent",
            "default_scope": "test-scope" if with_default_scope else None,
        },
    }
    assert invoked == [
        3,
        ("search_context", "get_glossary", "resolve_speakers", "propose_update"),
        "get_server_info",
    ]
    assert not ctx.catalog.list_audit()
    assert not ctx.gaia.path.exists()


def test_unconfigured_connection_test_is_engine_unavailable(ctx: ServerContext):
    outcome = dispatch(ctx, "test_gaia_connection", {})
    assert outcome.is_error
    assert outcome.payload["error"]["code"] == "engine_unavailable"


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (EngineUnavailableError(KEY), "engine_unavailable"),
        (ScopeDeniedError(KEY), "engine_unavailable"),
        (ContractMismatchError(KEY), "contract_mismatch"),
    ],
)
def test_connection_test_maps_authentication_errors_and_never_exposes_remote_secret(
    ctx: ServerContext,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exception: NarumiError,
    code: str,
):
    class UnavailableClient:
        def require_capabilities(self, *_names):
            raise exception

    monkeypatch.setattr(ctx.gaia, "client", lambda **_kwargs: UnavailableClient())
    outcome = dispatch(ctx, "test_gaia_connection", {})
    assert outcome.is_error and outcome.payload["error"]["code"] == code
    assert KEY not in json.dumps(outcome.payload)
    assert KEY not in caplog.text


@pytest.mark.parametrize(
    "arguments",
    [
        {"api_key": {KEY: KEY}, "request_id": "fake-request-12345"},
        {"api_key": [KEY], "request_id": "fake-request-12345"},
        {"api_key": KEY * 500, "request_id": "fake-request-12345"},
        {"api_key": KEY, "request_id": {KEY: KEY}},
        {"api_key": KEY, "request_id": "fake-request-12345", KEY: KEY},
        {"url": {KEY: KEY}, "request_id": "fake-request-12345"},
        [KEY],
        KEY,
    ],
)
def test_invalid_secret_inputs_do_not_enter_responses_logs_or_cache(
    ctx: ServerContext, caplog: pytest.LogCaptureFixture, arguments: Any
):
    outcome = dispatch(ctx, "set_gaia_connection", arguments)
    assert outcome.is_error and outcome.payload["error"]["code"] == "invalid_argument"
    assert KEY not in json.dumps(outcome.payload)
    assert KEY not in outcome.to_call_tool_result().content[0].text
    assert KEY not in caplog.text
    assert ctx.catalog.get_request("fake-request-12345") is None
    assert not ctx.catalog.list_audit()
    assert not ctx.gaia.path.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        {"api_key": {KEY: KEY}, "request_id": "fake-request-12345"},
        {"api_key": KEY * 500, "request_id": "fake-request-12345"},
        {"api_key": KEY, "request_id": "fake-request-12345", KEY: KEY},
    ],
)
async def test_mcp_validation_response_and_debug_logs_are_secret_safe(
    client: PerCallClient, caplog: pytest.LogCaptureFixture, arguments: dict[str, Any]
):
    caplog.set_level(logging.DEBUG)
    outcome = await client.call_tool("set_gaia_connection", arguments)
    assert outcome.is_error
    assert outcome.structured_content["error"]["code"] == "invalid_argument"
    assert KEY not in str(outcome)
    assert KEY not in caplog.text


@pytest.mark.parametrize(
    "tool", ["get_gaia_connection", "set_gaia_connection", "test_gaia_connection"]
)
@pytest.mark.parametrize("structured", [False, True])
def test_unexpected_or_structured_exception_is_secret_safe(
    ctx: ServerContext, caplog: pytest.LogCaptureFixture, tool: str, structured: bool
):
    def broken_handler(_ctx, _args):
        if structured:
            raise NarumiError(KEY, code=ErrorCode.INTERNAL, details={KEY: [KEY]})
        raise RuntimeError({KEY: [KEY]})

    ctx.handlers = {**ctx.handlers, tool: broken_handler}
    args = {"url": URL, "api_key": KEY, "request_id": rid()} if tool.startswith("set_") else {}
    outcome = dispatch(ctx, tool, args)
    assert outcome.is_error and outcome.payload["error"]["code"] == "internal"
    assert KEY not in json.dumps(outcome.payload)
    assert KEY not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_bad_secret_output_is_rejected_before_idempotency_cache_even_without_debug_validation(
    ctx: ServerContext, caplog: pytest.LogCaptureFixture
):
    ctx.validate_output = False
    ctx.handlers = {
        **ctx.handlers,
        "set_gaia_connection": lambda _ctx, _args: {
            "connection": {"url": URL, "has_api_key": True, "source": "saved", "api_key": KEY}
        },
    }
    request_id = rid()
    outcome = dispatch(
        ctx, "set_gaia_connection", {"url": URL, "api_key": KEY, "request_id": request_id}
    )
    assert outcome.is_error and outcome.payload["error"]["code"] == "contract_mismatch"
    assert ctx.catalog.get_request(request_id) is None
    assert KEY not in json.dumps(outcome.payload)
    assert KEY not in caplog.text


def test_corrupt_saved_settings_error_does_not_leak_parser_input(
    ctx: ServerContext, caplog: pytest.LogCaptureFixture
):
    ctx.gaia.path.write_text(json.dumps({"version": KEY, "url": URL, "api_key": {KEY: KEY}}))
    outcome = dispatch(ctx, "get_gaia_connection", {})
    assert outcome.is_error and outcome.payload["error"]["code"] == "internal"
    assert KEY not in json.dumps(outcome.payload)
    assert KEY not in caplog.text


def test_invalid_exception_code_is_not_echoed(ctx: ServerContext, caplog: pytest.LogCaptureFixture):
    def broken_handler(_ctx, _args):
        raise NarumiError(KEY, code=KEY)

    ctx.handlers = {**ctx.handlers, "get_gaia_connection": broken_handler}
    outcome = dispatch(ctx, "get_gaia_connection", {})
    assert outcome.payload["error"]["code"] == "internal"
    assert KEY not in json.dumps(outcome.payload)
    assert KEY not in caplog.text


def test_sensitive_error_envelope_fallback_is_also_redacted(
    ctx: ServerContext, caplog: pytest.LogCaptureFixture
):
    envelope = _checked_envelope(
        ctx,
        {"error": {"code": KEY, "message": KEY, "details": {KEY: KEY}}},
        "set_gaia_connection",
        sensitive=True,
    )
    assert envelope["error"]["code"] == "internal"
    assert KEY not in json.dumps(envelope)
    assert KEY not in caplog.text
